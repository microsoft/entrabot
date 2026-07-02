# Upstream feedback for `microsoft/mxc` — Windows `processcontainer` child-process & diagnostics papercuts

**Audience:** the MXC maintainers (`github.com/microsoft/mxc`).
**From:** the entrabot project (a third-party integrator embedding MXC for OS-enforced
local code execution behind an Entra Agent identity).
**Binary under test:** `wxc-exec.exe` from `@microsoft/mxc-sdk` **v0.7.0** (npm),
Windows **11 build 28120 (26H1), ARM64**, `processcontainer` backend, policy schema
`0.6.0-alpha`, config delivered via `--config-base64`.
**Selected isolation tier:** `appcontainer-dacl` (see Issue 3).
**Date:** 2026-06-29.

This note is intentionally self-contained so it can be forwarded as-is. **None of these
are correctness/security bugs** — default-deny behaves correctly throughout. They are three
developer-experience papercuts we hit while wiring a byte-exact file writer through the
Windows backend, plus one question.

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
   could not test the prepped tier (it needs elevation).

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
