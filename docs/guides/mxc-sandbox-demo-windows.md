# Demo Walkthrough — EntraBot × MXC Sandbox on Windows

> The Windows run-of-show for demonstrating OS-enforced, least-privilege local
> execution — the counterpart to the macOS Seatbelt demo. Everything below was
> verified against the **real** `wxc-exec.exe` (`@microsoft/mxc-sdk` v0.7.0) on
> Windows 11 24H2+ with the `processcontainer` backend.

**The one-line story** (say this at the top and the bottom):

> *"The agent has its own Entra identity and can read what you allow — but the
> **OS**, not the agent's good behavior, stops it from writing where it
> shouldn't. Least privilege, enforced by the kernel, attributed to the agent,
> audited before every action."*

---

## 0. What the audience will see (and why it lands)

Three layers of proof, from "always works" to "BUILD-stage flashy":

| Layer | What it shows | Needs admin? |
|---|---|---|
| **A. The harness** (`demo_sandbox.ps1`) | The clamp dropping out-of-ceiling paths to `[]`, then `BLOCKED by the Windows kernel — Access is denied` inline. The money-shot. | No |
| **B. `wxc-exec --debug`** | The *resolved policy* the kernel enforces (`readwrite_paths`, `denied_paths`, `containment: processcontainer`, `selected isolation tier`). | No |
| **C. `mxc-diagnostic-console` (elevated)** | The **live ETW event stream** from the MXC OS provider as each sandbox runs — the Build-2026-stage "watch the kernel" view. | **Yes** |

Run **A** for everyone; drop to **B** when a developer asks "what does the
policy actually look like?"; run **C** in a second elevated window for the full
effect.

---

## 1. Prerequisites (one-time)

```powershell
# From the repo root, in PowerShell:

# 1. Provision the MXC binary + pin its SHA256 + write .env defaults.
.\scripts\setup_sandbox.ps1

# 2. (Recommended) Stabilize the processcontainer tier. On boxes where MXC falls
#    back to the AppContainer+DACL tier, the sandbox can't read C:\ root
#    metadata, so cmd.exe/pwsh.exe startup can intermittently fail. This grants
#    the minimal metadata ACEs and makes the demo rock-solid. Run ELEVATED:
#    (Right-click PowerShell -> Run as administrator)
& "$env:MXC_BIN_DIR\arm64\wxc-host-prep.exe" prepare-system-drive   # or \x64\ on Intel

# 3. Confirm the operator ceiling in .env. On Windows, paths are ';'-separated:
#    ENTRABOT_ENABLE_RUN_CODE=1
#    MXC_BIN_DIR=...\.mxc-build\npm\node_modules\@microsoft\mxc-sdk\bin
#    ENTRABOT_SANDBOX_READONLY_PATHS=C:\Users\you\Documents;%TEMP%
#    ENTRABOT_SANDBOX_READWRITE_PATHS=%TEMP%;C:\Users\you\Downloads
#    ENTRABOT_SANDBOX_NETWORK=block
```

> **Check the tier:** `& "$env:MXC_BIN_DIR\arm64\wxc-exec.exe" --probe` prints the
> selected isolation tier and `uiCapabilities` as JSON. `processcontainer` is the
> default, non-experimental backend on Windows 11 24H2+ (build 26100+); no
> `--experimental` flag is needed.

---

## 2. Part 1 — Local proof harness (screen-share)

This drives the real binary through the **exact** `run_code` enforcement chain
the MCP server uses (operator ceiling → clamp → canonicalize → MXC) and narrates
each beat.

```powershell
.\scripts\demo_sandbox.ps1              # press Enter between beats (live)
.\scripts\demo_sandbox.ps1 -NoPause     # straight through (recording / CI)
.\scripts\demo_sandbox.ps1 -ConfigOnly  # just show the operator ceiling + backend
```

**What to say as it runs:**

1. *"The operator sets a ceiling in `.env`. The agent can only narrow it, never
   widen it."*
2. **READ Documents** → *"The agent can read your files for analysis."* ✅
3. **WRITE Documents** → *"It tries to tamper — watch the clamp drop the path to
   `[]`, and the kernel says no."* ⛔ (`Access is denied.`)
4. **WRITE %TEMP% + Downloads** → *"Scoped output dirs the operator allowed."* ✅
5. **WRITE C:\Windows** → *"It can't reach the OS itself — dropped and blocked."* ⛔

The harness prints, per scenario, the **clamp decision** (`dropped WRITE
C:\Users\you\Documents (outside operator ceiling)`), the **exact policy sent to
MXC**, and the **kernel verdict** (`[x] BLOCKED by the Windows kernel  exit=1
reason: Access is denied.`).

---

## 3. Part 2 — Show the enforcement internals (developer beat)

When someone asks "but what is actually enforced?", run the real binary with
`--debug` on a blocked write and point at the resolved policy:

```powershell
$cfg = '{"version":"0.6.0-alpha","containment":"process","process":{"commandLine":"cmd /c echo HACK > \"C:\\Users\\you\\Documents\\hack.txt\"","timeout":15000},"filesystem":{"readonlyPaths":[],"readwritePaths":["%TEMP%"]},"network":{"defaultPolicy":"block"}}'
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($cfg))
& "$env:MXC_BIN_DIR\arm64\wxc-exec.exe" --debug --config-base64 $b64
```

It prints the full resolved `ExecutionRequest`, including:

```
  "containment": "processcontainer",
    "readwrite_paths": [ ... only what the operator allowed ... ],
    "readonly_paths": [],
    "denied_paths": [],
    "default_network_policy": "block",
selected isolation tier: appcontainer-dacl
```

> ⚠️ `--debug` wraps the process and returns **exit 0** for the diagnostic run —
> do **not** use `--debug` to judge allow/block. Without it, a blocked write
> returns **exit 1 + `Access is denied.`** (this is what the harness relies on).

---

## 4. Part 3 — The live "watch the kernel" view (elevated, Build-stage)

The Windows analog to macOS's `log stream` is **`mxc-diagnostic-console.exe`**,
which streams the **MXC OS-provider ETW events** plus pipe log messages from
`wxc-exec`. It **requires Administrator** for two reasons we verified:

- ETW capture (`StartTraceW`) needs admin.
- `wxc-exec` refuses to send diagnostics to a console running below **High
  integrity** (i.e. a non-elevated console) as a security measure.

**Window A — the live console (Run as administrator):**

```powershell
$env:MXC_DIAG_CONSOLE = "1"
& "$env:MXC_BIN_DIR\arm64\mxc-diagnostic-console.exe" --verbose
#   add --collect to also zip a timestamped capture into %TEMP% on Ctrl+C
```

**Window B — also elevated, same session, so `wxc-exec` talks to the console:**

```powershell
$env:MXC_DIAG_CONSOLE = "1"
.\scripts\demo_sandbox.ps1 -NoPause
```

As each scenario runs, the console shows `wxc-exec` connect/disconnect and the
OS-provider events for the allowed vs. denied file operations in real time. Pause
on the **WRITE Documents** beat so the audience watches the denial appear live.

> **No-admin fallback:** if you can't elevate, the harness (Part 1) and `--debug`
> (Part 2) already prove containment without ETW. You can also run **Process
> Monitor** (Sysinternals `procmon`) filtered to `Result is ACCESS DENIED` — the
> classic Windows "kernel said no" red rows — as a familiar alternative.

---

## 5. Part 4 — Do it live in Teams (the identity beat)

This is where EntraBot's half of the thesis shows: the contained execution is
attributed to the **agent's own Entra identity**, not yours. Chat with the agent
(`entrabot-agent@werner.ac`) in Teams, in plain language. It calls
`run_code` / `read_local_file` / `write_local_file` under the hood.

**Agent first-person script** (what the agent says back, mirroring the Mac demo):

> 👋 Hi — I'm **entrabot-agent**, an agent with my own Entra identity. I'm going
> to try to touch files on this Windows PC. Everything I run is contained by the
> MXC sandbox (Windows `processcontainer`), and the limits were set by you, the
> operator — I can only narrow them, never widen them.
>
> **Operator ceiling**
> 📖 read-only: `C:\Users\you\Documents`, `%TEMP%`
> ✏️ read-write: `%TEMP%`, `C:\Users\you\Downloads`
>
> **1) Read your Documents.** Documents is in my read-only ceiling, so this is
> allowed.
> ✅ I read it: *"SECRET: quarterly numbers the agent may read but must not alter."*
>
> **2) Write to your Documents.** Documents is not in my read-write ceiling — the
> policy clamps it to `[]`, and the OS kernel blocks the write.
> ⛔ Blocked — *"Access is denied."*
>
> **3) Write a scratch report to %TEMP%.** TEMP is in my read-write ceiling.
> ✅ Wrote it: *scratch report.*
>
> **4) Write an export to your Downloads.** Downloads is in my read-write ceiling.
> ✅ Wrote it: *export data.*
>
> 🔒 **Recap:** I could read your Documents but could not write to them — even
> though I asked. I could write only where you allowed (`%TEMP%`, Downloads). The
> boundary is enforced by the OS, attributed to my identity, and logged before
> each action. That's least-privilege for agentic work on your device.

**Suggested prompts to type in Teams:**

1. *"Read my file at `~\Documents\entrabot-secret.txt` and tell me what it says."*
   → Agent reads it. Point out: Documents is read-only in the ceiling.
2. *"Now save the text `hello` to `~\Documents\note.txt`."*
   → Blocked. The agent reports it can't write there. Show the audit log.
3. *"Write a short summary to `~\Downloads\summary.txt` instead."*
   → Works. Downloads is in the read-write ceiling.

> **Make `run_code` the agent's only path to the disk.** MXC contains code run
> *through the entrabot tools* — not your host's built-in `Bash`/`Write`/`Edit`.
> For an honest demo, disable the host's built-in file/shell tools (Copilot CLI:
> `--deny-tool`/`--available-tools`; Claude Code: `--disallowedTools "..."`). See
> [the sandbox guide](mxc-sandbox.md#critical-the-sandbox-contains-run_code-not-the-agent).

---

## 6. The honest caveat (say it — it builds trust)

MXC is an **early preview** and Microsoft is explicit that *"no MXC profiles
should be treated as security boundaries currently."* In this demo MXC is
**defense-in-depth** layered *under* EntraBot's existing identity, attribution,
and audit gates — it never relaxes one. The filesystem enforcement you're
watching is real and kernel-backed; the maturity bar for "trusted boundary" is
still ahead (micro-VM / session isolation tiers on the roadmap).

---

## 7. Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `Sandbox unavailable` / binary not found | Run `.\scripts\setup_sandbox.ps1`; confirm `MXC_BIN_DIR`. |
| `Untrusted binary` (SHA mismatch) | The binary changed but `PINNED_HASHES` wasn't updated. Re-run `setup_sandbox.ps1` (it re-pins). |
| An **allowed** write intermittently fails (exit 1) | AppContainer+DACL tier can't stat `C:\` root, so `cmd.exe` startup flakes. Run `wxc-host-prep prepare-system-drive` **elevated** (Prereqs step 2). |
| Ceiling paths look shredded (`C` and `\Users\...`) | Old colon-split bug; ensure you're on this branch (ceiling is parsed with `os.pathsep` = `;` on Windows). |
| Diagnostic console shows no events | Not elevated. ETW + the High-integrity pipe both require **Run as administrator**, and set `MXC_DIAG_CONSOLE=1` in **both** windows. |
| `&&` errors running a command | `wxc-exec` runs `commandLine` via `CreateProcessW` (no shell). Wrap shell syntax in `cmd /c "..."`. |
| Read shows stray `ï»¿` bytes | A UTF-8 BOM in the fixture file; write fixtures as ASCII / UTF-8-no-BOM. |

---

## 8. Reference

- Harness: [`scripts/demo_sandbox.ps1`](../../scripts/demo_sandbox.ps1) ·
  engine: [`scripts/demo_sandbox_run.py`](../../scripts/demo_sandbox_run.py)
- Setup: [`scripts/setup_sandbox.ps1`](../../scripts/setup_sandbox.ps1)
- Sandbox guide: [`mxc-sandbox.md`](mxc-sandbox.md)
- What the Windows preview actually exposes:
  [`mxc-windows-sandbox-preview.md`](../platform-learnings/mxc-windows-sandbox-preview.md)
- Decision record: [ADR-007](../decisions/007-mxc-sandbox-integration.md)
