# Upstream feedback for `microsoft/mxc` — Windows `processcontainer` child-process & diagnostics papercuts

**Audience:** the MXC maintainers (`github.com/microsoft/mxc`).
**From:** the entrabot project (a third-party integrator embedding MXC for OS-enforced
local code execution behind an Entra Agent identity).
**Binary under test:** `wxc-exec.exe` from `@microsoft/mxc-sdk` **v0.7.0** (npm),
Windows **11 build 28120 (26H1), ARM64**, `processcontainer` backend, policy schema
`0.6.0-alpha`, config delivered via `--config-base64`.
**Selected isolation tier:** `appcontainer-dacl` (see Issue 3).
**Date:** 2026-06-29. **Updated:** 2026-07-02 — added Issue 4 (file- vs directory-level read
grant) and `prepare-system-drive` results (Issue 3 update; answers the prior open question).
**Updated:** 2026-07-06 — added Issue 5 (intermittent pre-containment wedge with zero ETW
events; orphaned descendant outlives a kill of `wxc-exec.exe`; DACL recovery after force-kill).
**Updated:** 2026-07-06 (later) — Issue 5 hardened from intermittent to reproducible: 4/4
wedges when spawned from our long-running host process (allowed AND denied targets), 3/3
clean standalone; read-vs-write asymmetry narrows suspicion to the interpreter-dir DACL
grants.

This note is intentionally self-contained so it can be forwarded as-is. **None of these
are correctness/security bugs** — default-deny behaves correctly throughout. They are four
developer-experience papercuts we hit while wiring byte-exact file read/write through the
Windows backend, plus a couple of questions.

---

## TL;DR

1. **Spawning a child process via `cmd /c <exe>` fails with the opaque message
   `The current directory is invalid.`** when the policy leaves `process.working_directory`
   unset (`""`). `cmd` *builtins* (`type`, `echo > file`) work fine; only spawning a
   separate executable trips it. Either default the container cwd to a granted directory, or
   surface a clearer error pointing at `working_directory`.

2. **A fully successful run (`exit 0`, correct output written) still emits a scary
   `Failed to find real location of <...>\python.exe` line on stderr.** Integrators that
   inspect stderr will misclassify a success as a failure. Either drop this on the success
   path or downgrade it to a debug-level diagnostic.

3. **On the `appcontainer-dacl` fallback tier, the auto-granted baseline can launch
   `cmd.exe` (and its builtins) but not other `System32` executables.** `certutil.exe` →
   `Access is denied.`; `whoami.exe` → `STATUS_DLL_INIT_FAILED`. The build also silently
   falls back to this tier (`bfsCompiledIn: false`). **Question:** does
   `wxc-host-prep prepare-system-drive` (which the `--probe` output recommends) restore the
   ability to launch arbitrary `System32` exes, or only fix `C:\` root metadata stats? We
   could not test the prepped tier (it needs elevation). **Update 2026-07-02:** we have since
   run it elevated — it completes `exit 0`, but `--probe` still reports
   `needsDaclAugmentation: true` afterward (see the Issue 3 update).

4. **A file-level `readonlyPaths` grant is not enough to READ that file on the
   `appcontainer-dacl` tier — you must grant the parent directory.** Granting the exact file and
   `type`-ing it is denied; granting its parent dir (identical command) succeeds. The same
   file-scoped policy works on macOS Seatbelt, so this is a portability surprise (see Issue 4).

5. **Write-policy runs spawned from a long-running host process wedge pre-containment —
   reproducibly (4/4), on allowed AND denied targets — with ZERO `ProcessModel` ETW events
   and no self-enforcement of `process.timeout`.** Identical policies standalone: 3/3 clean
   in seconds. The working server-spawned *read* vs the wedging server-spawned *writes*
   differ only by the interpreter-directory grants, and the grant target is the very venv
   the spawning process runs from. Force-killing `wxc-exec.exe` orphans a descendant that
   can hold inherited stdio pipes (26 minutes observed); the next run prints
   `DACL recovery: … ACE(s) restored`. See Issue 5.

For contrast, granting an executable's *own* dependency tree read-only makes it launch
cleanly — e.g. granting a venv `python.exe`'s venv root + base CPython install as
`readonlyPaths` lets it boot and write byte-exact. That part works exactly as a default-deny
sandbox should; we are not reporting it as a bug.

---

## Issue 1 — `cmd`-spawned child process fails with `The current directory is invalid.`

### Symptom

A policy that grants a writable temp dir and runs `certutil` (a `System32` tool) via `cmd`:

```jsonc
{
  "version": "0.6.0-alpha",
  "containment": "process",
  "process": {
    "commandLine": "cmd /c certutil -f -decode \"C:\\Users\\me\\AppData\\Local\\Temp\\in.b64\" \"C:\\Users\\me\\AppData\\Local\\Temp\\out.txt\"",
    "timeout": 20000
  },
  "filesystem": { "readonlyPaths": [], "readwritePaths": ["C:\\Users\\me\\AppData\\Local\\Temp"] },
  "network": { "defaultPolicy": "block" }
}
```

```
stderr: The current directory is invalid.
exit:   1
```

### Observations

- `cmd` **builtins** under the same policy succeed: `cmd /c type "<granted>\f.txt"` and
  `cmd /c echo data > "<granted>\f.txt"` both work (exit 0). Only spawning a *separate*
  executable from `cmd` fails.
- Setting the cwd inside the command (`cmd /c cd /d "<granted-temp>" && certutil …`) changes
  the error from `The current directory is invalid.` to `Access is denied.` — i.e. the cwd
  problem is distinct from, and precedes, Issue 3's launch problem.
- The resolved `ExecutionRequest` (`--debug`) shows `"working_directory": ""`. We suspect the
  container inherits/derives an inaccessible cwd, and `cmd`'s `CreateProcess` of the child
  re-validates it and fails.

### Suggested fix

Default the container's working directory to a directory the policy already grants (e.g. the
first `readwritePaths` entry, or a per-container scratch dir), **or** validate
`working_directory` up front and emit a message that names the field rather than the generic
Win32 "current directory is invalid."

---

## Issue 2 — Misleading stderr on a successful (`exit 0`) run

### Symptom

Running a venv `python.exe` directly as `commandLine` (with its venv root + base install
granted `readonly`, target parent granted `readwrite`) writes the file **byte-exact** and
returns **exit 0**, but stderr contains:

```
Failed to find real location of C:\Users\me\AppData\Local\Programs\Python\Python313-arm64\python.exe
```

The write fully succeeds (verified byte-for-byte, including embedded quotes, `&`, `%PATH%`,
a mid-content CRLF, and no trailing newline). The line reads like a hard failure but is not.

### Suggested fix

Suppress this on the success path, or route it through `--debug`/`--log-file` only. As-is, any
integrator that treats non-empty stderr as failure (a common, reasonable heuristic) will report
a false negative.

---

## Issue 3 — `appcontainer-dacl` baseline launches `cmd.exe` builtins but not other `System32` exes

### Symptom

On this host the backend selects the `appcontainer-dacl` tier. `--probe`:

```jsonc
{
  "tier": "appcontainer-dacl",
  "needsDaclAugmentation": true,
  "warnings": [
    "BaseContainer API not present or not preferred, and AppContainer + BFS is not compiled into this binary; falling back to AppContainer + DACL",
    "AppContainer + DACL tier selected: AppContainer processes may be unable to read metadata of the system-drive root (e.g. `cmd.exe`, `pwsh.exe`, `node.exe` startup stats of `C:\\`). Run `wxc-host-prep prepare-system-drive` (elevated) to grant the minimal metadata ACEs."
  ],
  "probes": { "baseContainerApiPresent": true, "bfscfgPresent": false, "bfsCompiledIn": false }
}
```

Under this tier, with no extra grants:

| Command (as `process.commandLine`) | Result |
| --- | --- |
| `cmd /c echo hi` / `cmd /c type "<granted>\f.txt"` | ✅ exit 0 (cmd builtins) |
| `cmd /c echo data > "<granted>\f.txt"` (redirect into granted dir) | ✅ exit 0 |
| `whoami` (bare `System32` exe) | ❌ `STATUS_DLL_INIT_FAILED` |
| `cmd /c certutil … ` (cwd set) | ❌ `Access is denied.` |
| a venv `python.exe` **without** its runtime dirs granted | ❌ exit 106, `failed to locate pyvenv.cfg: Access is denied` |
| a venv `python.exe` **with** venv root + base install granted `readonly` | ✅ exit 0, byte-exact |

So the auto-granted baseline is enough to launch `cmd.exe` and run its builtins, but not
enough to launch other executables (even `System32` ones) — those need their full dependency
tree explicitly granted.

### Observations / questions

- This is *consistent* with default-deny and we handle it on our side (grant the writer
  interpreter's dirs). We are **not** asking you to widen the baseline.
- **The two questions for the team:**
  1. Does `wxc-host-prep prepare-system-drive` (elevated) restore launching arbitrary
     `System32` exes (e.g. `certutil.exe`), or does it *only* grant `C:\`-root metadata stats
     so `cmd.exe`/`pwsh.exe`/`node.exe` start? The warning text implies the latter; clarifying
     this in docs would save integrators a lot of guesswork.
  2. The silent fallback to `appcontainer-dacl` (because `bfsCompiledIn: false` in the npm
     build) means integrators get a materially weaker/different tier than `processcontainer`
     implies, with no error — only a `--probe` reveals it. Surfacing the effective tier (and
     why) at spawn time, or shipping a BFS-enabled build on npm, would reduce surprise.

### Update (2026-07-02): we ran `wxc-host-prep prepare-system-drive` (elevated)

This answers the "we could not test the prepped tier" caveat above.

- It completes with **exit 0**.
- **`--probe` afterward still reports `tier: appcontainer-dacl` and `needsDaclAugmentation:
  true`** — the flag does **not** flip to `false` after a successful prep, so integrators cannot
  use it as an "already applied?" signal; it appears to describe the tier *category*, not the
  applied state. (The `warnings` array did shrink 2 → 1.)
- We did not re-measure arbitrary `System32`-exe launch (`certutil.exe`, `whoami.exe`) after the
  prep, so question 1 above remains open — but the persistent `needsDaclAugmentation: true` means
  a tool that keys off that flag to decide whether to run the prep will run it **every time**
  (idempotently). A distinct "applied"/"satisfied" probe field would let integrators make the
  step truly one-time.

---

## Issue 4 — a file-level `readonlyPaths` grant is not sufficient to READ that file; grant the parent directory

### Symptom

Granting read access to a single file and reading it with `cmd /c type` is denied; granting its
**parent directory** (identical command) succeeds. Reproduced with v0.7.0 on the
`appcontainer-dacl` tier:

| `readonlyPaths` | `commandLine` | Result |
| --- | --- | --- |
| `["C:\\Users\\me\\Documents\\info.txt"]` (the file) | `cmd /c type "…\\Documents\\info.txt"` | ❌ exit 1, `Access is denied.` |
| `["C:\\Users\\me\\Documents"]` (the parent dir) | `cmd /c type "…\\Documents\\info.txt"` | ✅ exit 0, contents returned |

### Observations

- On macOS Seatbelt a file-level read grant is sufficient to read that file. On the Windows
  `appcontainer-dacl` tier it is not — opening the file appears to require directory-traversal
  access to the containing directory, so a file-only grant is deterministically denied.
- This is a portability surprise: the same "grant exactly the file you read" policy that works on
  macOS silently fails on Windows. We work around it by granting the file's parent directory
  read-only (still clamped to our operator ceiling) on Windows.

### Suggested fix / question

- Either auto-include the minimal parent-directory traversal grant when a file is passed in
  `readonlyPaths`, or document that on the `appcontainer-dacl` tier reading a file requires its
  parent directory to be reachable. A one-line note in the policy docs would save integrators the
  guesswork.

---

## Issue 5 — intermittent pre-containment wedge: run exceeds `process.timeout` with ZERO `ProcessModel` ETW events; killed run orphans a pipe-holding descendant

### Symptom

**Four out of four** write-policy runs wedged past their configured `process.timeout`
(30000 ms) when `wxc-exec.exe` was spawned from our long-running MCP server process
(2026-07-02 ×1, 2026-07-06 ×3), while the **identical** policies run standalone from a fresh
terminal process completed cleanly **three out of three** (same day, minutes apart):

| Spawned from | Target | Policy verdict | Result |
| --- | --- | --- | --- |
| long-running server | Documents (denied) | outside write ceiling | ❌ wedge >30s, killed (×2: 07-02, 07-06) |
| long-running server | Downloads (allowed) | inside write ceiling | ❌ wedge >30s, killed (×2: 07-06) |
| fresh terminal process | Documents (denied) | outside write ceiling | ✅ clean exit 1 in 4.5 s |
| fresh terminal process | Downloads (allowed) | inside write ceiling | ✅ exit 0 in 2.2–3.5 s, byte-exact |
| long-running server | Documents READ (`cmd /c type`) | inside read ceiling | ✅ exit 0 in ~90 ms (07-02) |

The wedge is therefore **not policy-dependent** (allowed and denied targets both wedge) and
**not machine-state-dependent in general** (standalone runs interleaved with the wedges all
succeed). Two variables correlate: (a) the spawning process is long-running with many open
handles and active worker threads, and (b) the wedging runs are all *writes*, whose policy —
unlike the working server-spawned *read* — includes the interpreter-directory grants
(venv root + base CPython install). Note the venv being granted is the very tree the
spawning server's own `python.exe` is running from, so if grant setup edits DACLs on those
directories, it is editing ACLs on files the parent process holds open. Additional facts:

- No run exited at the configured `process.timeout`; our own watchdog had to terminate every
  wedged run.
- On 2026-07-02, terminating only `wxc-exec.exe` (CPython `subprocess.run`'s kill-direct-child
  behavior) left an orphaned descendant holding the inherited stdout/stderr pipe handles for
  **~26 minutes**, blocking the host's pipe drain the whole time.
- During the 2026-07-06 wedges, the MXC Diagnostic Console (verbose mode, `ProcessModel` +
  `Kernel-General` providers registered) showed **no Sandboxing events at all** for the
  wedged runs — while the successful standalone runs traced normally — so the wedge occurs
  *before* containment setup / tracing begins.
- The first standalone run after each force-killed wedge printed a DACL recovery line
  (`DACL recovery: 1 file(s), 3 ACE(s) restored, 0 error(s)`, later `4 ACE(s)`) — recovery
  worked both times (good), but it confirms a killed run leaves real DACL edits behind, and
  strengthens the suspicion that the wedge itself is serialization/contention on the
  DACL-grant path (debris from a prior killed run, another process granting the same
  interpreter directories, or the grant editing ACLs under the spawning process's own open
  handles — we had two MCP server instances alive during the first incident).
- Environmental note, in case it matters: the host is a Parallels VM (ARM64) whose wall clock
  jumps 2–5 minutes at a time on resume (`prl_tools.exe` `SystemTimeChange` ETW events).

### Suggested fixes / questions

- **Emit one ETW event immediately at `wxc-exec.exe` startup**, before grant setup, so
  integrators can distinguish "wedged before containment" from "container ran and hung".
  Today a pre-containment hang is invisible to the diagnostic console.
- **Place the container host / child tree in a Job Object with `KILL_ON_JOB_CLOSE`** so that
  killing `wxc-exec.exe` reaps every descendant. As-is, integrators must know to
  `taskkill /T` or the orphan outlives the kill holding inherited handles.
- **Clarify whether `process.timeout` is enforced by `wxc-exec.exe` itself.** In both
  incidents the process outlived its configured timeout and the integrator's watchdog had to
  intervene.
- Is there a known lock or serialization point in the `appcontainer-dacl` grant path that
  could block startup when a previous run was killed mid-grant, or when another process is
  granting the same directories?
- **Likely repro shape for your side:** spawn `wxc-exec.exe` from a long-lived parent with
  many open handles/threads, with a policy whose `readonlyPaths` include the directory tree
  the parent's own executable/runtime lives in (our case: the venv the spawning `python.exe`
  runs from). Server-spawned *reads* (no such grants) work; server-spawned *writes* (with
  them) wedge 4/4; both work standalone. If grant setup edits DACLs on directories the
  parent holds open handles into, that is the strongest candidate for the pre-containment
  block.

---

## Environment

| Field | Value |
| --- | --- |
| OS | Windows 11, build **28120** (26H1) |
| Arch | ARM64 |
| Binary | `wxc-exec.exe`, `@microsoft/mxc-sdk` **v0.7.0** (npm) |
| Backend | `processcontainer` → effective tier `appcontainer-dacl` |
| Schema | `0.6.0-alpha` (also accepted by the v0.7.0 parser) |
| Config delivery | `--config-base64` |

All three are reproducible with the policies above. Happy to provide full `--debug` /
`--log-file` captures.
