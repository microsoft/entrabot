# MXC Sandbox — Contained Local Code Execution

Give your agent the ability to run code on the local machine **without** giving it
the run of your filesystem. entrabot integrates
[Microsoft Execution Containers (MXC)](https://github.com/microsoft/mxc) so a
`run_code` tool executes inside an OS-enforced sandbox (Apple **Seatbelt** on macOS).
You — the operator — set a capability ceiling in plain config; the agent can only
ever *narrow* it, and the OS kernel enforces the result.

- **Opt-in.** Disabled by default; you enable it explicitly.
- **Positive allow-list.** The agent gets nothing it isn't granted (no network, no
  filesystem, no Keychain by default).
- **The model can't widen its box.** Requests are clamped to the operator ceiling.
- **Fail-closed + audited.** Every call is audit-logged before it runs; if audit
  can't record, the action doesn't proceed.

> Decision record: [ADR-007](../decisions/007-mxc-sandbox-integration.md) ·
> Platform research: [`mxc-windows-sandbox.md`](../platform-learnings/mxc-windows-sandbox.md)

Phase 1 ships **macOS (Seatbelt)** and **Windows (`processcontainer`)**. Linux
seccomp-bpf is on the roadmap. The Windows path is documented inline below where it
differs; see also
[`mxc-windows-sandbox-preview.md`](../platform-learnings/mxc-windows-sandbox-preview.md)
for what the Windows preview build actually exposes, and run
[`scripts/setup_sandbox.ps1`](../../scripts/setup_sandbox.ps1) (the PowerShell
counterpart to `setup_sandbox.sh`) to provision `wxc-exec.exe` and pin its hash.

> **Windows notes.** Ceiling lists are **`;`-separated** (`os.pathsep`), not
> colon-separated. `wxc-exec.exe` runs commands via `CreateProcessW` with **no
> implicit shell**, so invoke builtins/redirection as `cmd /c ...`. The
> `processcontainer` backend is default (no `--experimental`) on Win11 24H2+.

---

## How it works

```
 Operator config (.env)                 ┌──────────────────────────────┐
 ENTRABOT_SANDBOX_READONLY_PATHS  ─────► │ run_code tool (mcp_server.py)│
 ENTRABOT_SANDBOX_READWRITE_PATHS ─────► │   reads the ceiling from env │
                                         └───────────────┬──────────────┘
 Agent's request (paths it wants) ──────────────────────►│
                                         ┌───────────────▼──────────────┐
                                         │ clamp_to_ceiling (policy.py)  │
                                         │  request ∩ ceiling → narrower │ ← agent can only narrow
                                         └───────────────┬──────────────┘
                                         ┌───────────────▼──────────────┐
                                         │ mxc-exec-mac (SHA256-pinned)  │
                                         │  → Seatbelt profile           │
                                         └───────────────┬──────────────┘
                                         ┌───────────────▼──────────────┐
                                         │ macOS kernel enforces; denies │
                                         │ logged: deny(1) file-write-…  │
                                         └──────────────────────────────┘
```

The rules are read **on every call** from the environment — never from the model.

---

## HOWTO: enable the sandbox

### Prerequisites

- **macOS** (Phase 1). Apple Silicon or Intel.
- A working entrabot agent (`./scripts/setup.sh` already run). See the
  [Quickstart](../getting-started/quickstart.md).
- To **build** the MXC binary from source: **Rust 1.93+** (`https://rustup.rs/`).
  (If you already have a prebuilt `mxc-exec-mac` on `MXC_BIN_DIR` or via npm, the
  build step is skipped.)

### Step 1 — Build and configure the sandbox

```bash
./scripts/setup_sandbox.sh
```

This script is idempotent and does five things:

1. **Finds or builds** the MXC binary. If not already present, it clones
   [`microsoft/mxc`](https://github.com/microsoft/mxc) at the pinned tag
   (`v0.6.1`, commit `161598f…`), applies the bundled
   [stdin-compat patch](../../scripts/mxc-mac-stdin-compat.patch), and `cargo`-builds
   `mxc-exec-mac` into `.mxc-build/target/release/`.
2. **Code-signs** the binary (ad-hoc) so macOS will run it.
3. **Pins its SHA256** into
   [`src/entrabot/sandbox/binary.py`](../../src/entrabot/sandbox/binary.py)
   (`PINNED_HASHES`). At runtime the binary is verified against this hash and refused
   if it doesn't match — a tampered enforcer can't be swapped in.
4. **Writes the sandbox config** into `.env` (see Step 2).
5. Prints a summary (binary path, hash, env).

> Flags: `--force-build` rebuilds even if a binary exists; `--skip-sign` skips
> code-signing. Run `./scripts/setup_sandbox.sh --help` for details.

### Step 2 — Set your operator ceiling

`setup_sandbox.sh` writes safe defaults to `.env` (everything scoped to `/tmp`). Edit
these to grant exactly what your agent needs — **directories**, colon-separated:

```dotenv
# Turn the sandboxed run_code tool on
ENTRABOT_ENABLE_RUN_CODE=1

# Where the verified binary lives (written for you)
MXC_BIN_DIR=/absolute/path/to/.mxc-build/target/release

# The ceiling — the MOST the agent may ever touch. The agent can only narrow this.
ENTRABOT_SANDBOX_READONLY_PATHS=/Users/you/Documents:/tmp     # may READ
ENTRABOT_SANDBOX_READWRITE_PATHS=/tmp:/Users/you/Downloads    # may WRITE

# Guardrails
ENTRABOT_SANDBOX_TIMEOUT_MS=30000     # max wall-clock per execution
ENTRABOT_SANDBOX_NETWORK=block        # block | allow (default block)
```

Guidance:

- **Grant the least you can.** Prefer a scratch output dir in `READWRITE_PATHS` and a
  read-only project tree in `READONLY_PATHS`.
- Use **absolute paths**. `~` and symlinks are resolved (canonicalized) before the
  containment check, so a request can't escape a granted directory via a symlink.
- Leaving a list **empty** means *no* access of that kind. There is no implicit
  default — default-deny is total.
- **Keychain access is hard-disabled** and not overridable by the agent or config.

### Step 3 — Restart the MCP server

Config is read at server boot. Restart your host (e.g. Claude Code / Copilot CLI) so
the `entrabot` MCP server picks up the new `.env`. Confirm the tool is registered:

```bash
# The run_code tool only appears when ENTRABOT_ENABLE_RUN_CODE=1
claude mcp list        # entrabot server should show ✓ Connected
```

> ### ⚠️ Critical: the sandbox contains `run_code`, not "the agent"
>
> MXC sandboxes code executed **through the `run_code` tool**. It does **not**
> contain your *host* (Claude Code, Copilot CLI, Codex, …), which ships its own
> built-in `Bash`/`Edit`/`Write`/`Read` tools with full, unsandboxed disk access.
> If those remain enabled, the agent will simply use them and bypass the sandbox
> entirely — `run_code` is then just *one* door in an open house.
>
> **For the containment to be real, make `run_code` the agent's only path to the
> filesystem** by disabling the host's built-in file/shell tools.
>
> **Claude Code** (verified): deny the built-ins — do **not** use `--tools ""`,
> which removes the *MCP* tools (including `run_code`) and leaves the built-ins:
>
> ```bash
> claude --dangerously-load-development-channels server:entrabot \
>   --disallowedTools "Bash Write Edit NotebookEdit Read Glob Grep WebFetch WebSearch Task"
> ```
>
> With this, `run_code` still works but a direct `Write` returns
> *"No such tool available"* and the file is never created.
>
> **Copilot CLI**: use `--available-tools` (allow-list) or `--deny-tool` to the
> same effect.
>
> **This is a real trade-off, not a tweak.** Stripping the built-ins makes the
> agent MCP-only — it keeps every entrabot tool (Teams, email, Files-via-Graph,
> `run_code`) but loses general local coding (arbitrary file edits, shell). Run
> the *contained* configuration in a **dedicated session**; keep your everyday
> agent fully tooled. Whole-agent containment that *keeps* the powerful tools is a
> separate model (a dedicated OS user / VM the agent runs as) — see
> [ADR-007](../decisions/007-mxc-sandbox-integration.md) Phase 2.
>
> As a defense-in-depth backstop, entrabot's own deliberately-unsafe
> `write_local_file` tool is **off by default** and only registered when
> `ENTRABOT_ENABLE_UNSAFE_WRITE=1`. Leave it unset.

### Step 4 — Verify it works

Show the active configuration (operator's view):

```bash
./scripts/demo_sandbox.py --config-only
```

Run the full enforcement check against the **real** binary (narrated, no agent
required):

```bash
./scripts/demo_sandbox.py             # interactive, pauses between beats
./scripts/demo_sandbox.py --no-pause  # straight through
```

It exercises: read an allowed dir ✅, write a *disallowed* dir ⛔ (blocked by the
kernel), write allowed dirs ✅, and a symlink-escape attempt ⛔.

To watch the kernel enforce in real time, stream Seatbelt denials in another window:

```bash
log stream --predicate 'eventMessage CONTAINS "deny(" AND eventMessage CONTAINS "file-write"' --style compact
```

A blocked write prints instantly:

```
kernel (Sandbox)  Sandbox: bash(NNNNN) deny(1) file-write-create  /Users/you/Documents/note.txt
```

---

## Using it

Enabling the sandbox registers three tools, all gated behind
`ENTRABOT_ENABLE_RUN_CODE` and all enforced by the same operator ceiling:

- **`read_local_file(path)`** — read a file on the user's local disk.
- **`write_local_file(path, content)`** — write/save a file on the local disk.
- **`run_code(argv, …)`** — run an arbitrary command/script in the sandbox.

The two purpose-named file tools exist because models select tools by intent:
they reliably reach for `read_local_file` / `write_local_file` when asked to
"read" or "save" a local file, whereas a single generic `run_code` got skipped
for writes (the model routed "save a file" to the cloud OneDrive tools). All
three share the identical clamp → realpath → Seatbelt machinery.

In practice you just ask the agent, e.g. in Teams:

- *"Read `~/Documents/report.md` and summarize it."* → `read_local_file`; allowed
  if `~/Documents` is in `READONLY_PATHS`.
- *"Save the summary to `~/Documents/summary.md`."* → `write_local_file`;
  **blocked** unless `~/Documents` is in `READWRITE_PATHS` (the kernel returns
  `Operation not permitted` and nothing is written).
- *"Write it to `~/Downloads/summary.md` instead."* → `write_local_file`; allowed
  if `~/Downloads` is in `READWRITE_PATHS`.

`run_code` takes a structured `argv` (no shell string) plus optional
`readonly_paths` / `readwrite_paths` (to *narrow* the ceiling) and `timeout_ms`.
The file tools just take a `path` (and `content` for writes). See the
[MCP tool reference](../reference/mcp-tools.md).

> A deliberately-**unsafe** contrast tool, `unsafe_write_local_file`, bypasses the
> sandbox and writes anywhere. It is off by default and only registered when
> `ENTRABOT_ENABLE_UNSAFE_WRITE=1`; leave it unset outside teaching demos.

---

## Configuration reference

| Variable | Default | Meaning |
|----------|---------|---------|
| `ENTRABOT_ENABLE_RUN_CODE` | *(unset = off)* | `1` registers the `run_code` tool. Off by default. |
| `MXC_BIN_DIR` | *(written by setup)* | Directory containing the verified `mxc-exec-mac`. |
| `ENTRABOT_SANDBOX_READONLY_PATHS` | `/tmp` | Colon-separated dirs the agent may read. |
| `ENTRABOT_SANDBOX_READWRITE_PATHS` | `/tmp` | Colon-separated dirs the agent may read **and** write. |
| `ENTRABOT_SANDBOX_TIMEOUT_MS` | `30000` | Max wall-clock per execution (ms). |
| `ENTRABOT_SANDBOX_NETWORK` | `block` | `block` (no egress) or `allow`. |
| *Keychain* | *off* | Hard-disabled in code; not configurable. |

---

## The security model (why you can trust it)

- **Operator ceiling, model narrows.**
  [`clamp_to_ceiling`](../../src/entrabot/sandbox/policy.py) intersects the agent's
  requested paths with your ceiling. The worst the agent can do is ask for *less*.
- **Canonicalize-then-contain.** Paths are `realpath`-resolved before the containment
  check, so a symlink inside a granted directory can't point outside it. (Details and
  an upstream note for the MXC team:
  [macOS symlink canonicalization](../platform-learnings/mxc-upstream-feedback-macos-symlinks.md).)
- **Tamper-evident binary.** The runner verifies the binary's SHA256 against
  `PINNED_HASHES` in [`binary.py`](../../src/entrabot/sandbox/binary.py) and refuses a
  mismatch.
- **Fail-closed.** If the policy needs a primitive the backend can't enforce, or audit
  can't record, the execution is refused — not silently allowed.
- **Kernel-enforced.** The deny is a real syscall denial in the macOS unified log, not
  a Python check.

---

## Advanced: a throwaway test agent

To exercise the sandbox without touching your production agent's Teams presence, run a
second, isolated agent that shares the Blueprint but has its own Agent User and data
dir:

1. Provision a fresh Agent Identity + Agent User under the existing Blueprint:
   ```bash
   ./scripts/setup.sh --new --use-blueprint=<APP_ID> \
     --agent-user-upn=entrabot-test@yourtenant.com \
     --state-file=.entrabot-state-test.json \
     --env-file=.env.test
   ```
   (See the [setup-script reference](../reference/setup-script.md).)
2. In `.env.test`, add the sandbox vars from Step 2 **plus** an isolated data dir so
   it won't collide with production's singleton lock or local memory:
   ```dotenv
   ENTRABOT_KEEP_MEMORY_LOCAL=true
   ENTRABOT_DATA_DIR=/Users/you/.entrabot-test
   ```
3. Point the MCP server at it via `ENTRABOT_ENV_FILE`. The runtime honors this
   override (falling back to `./.env`):
   ```jsonc
   // .mcp.json
   { "mcpServers": { "entrabot-test": {
       "type": "stdio",
       "command": "/abs/path/.venv/bin/entrabot-mcp",
       "env": { "ENTRABOT_ENV_FILE": "/abs/path/.env.test" }
   }}}
   ```
   Verify with `claude mcp list` (expect `✓ Connected`).

---

## Troubleshooting

| Symptom | Cause / Fix |
|---------|-------------|
| `run_code` tool missing | `ENTRABOT_ENABLE_RUN_CODE` isn't `1`, or the server wasn't restarted after editing `.env`. |
| Agent ignored the sandbox / wrote anyway | The host's built-in `Edit`/`Bash`/`Write` tools were enabled and the agent used those instead of `run_code`. Disable them (see *Critical: the sandbox contains run_code, not "the agent"* above). |
| `run_code` disappeared after adding `--tools ""` | `--tools ""` disables **MCP** tools (incl. `run_code`) and is the wrong flag. Use `--disallowedTools "Bash Write Edit NotebookEdit Read Glob Grep WebFetch WebSearch Task"` instead. |
| `Sandbox unavailable` / binary not found | `MXC_BIN_DIR` is unset/wrong, or the binary wasn't built. Re-run `./scripts/setup_sandbox.sh`. |
| `Untrusted binary` (SHA mismatch) | The binary changed but `PINNED_HASHES` wasn't updated. Re-run `setup_sandbox.sh` (it re-pins), or rebuild from the pinned commit. |
| A write to `/tmp` is denied in raw policy JSON | macOS `/tmp`→`/private/tmp` symlink. The `run_code` chain canonicalizes paths, so this only bites hand-written policy JSON. See the [upstream note](../platform-learnings/mxc-upstream-feedback-macos-symlinks.md). |
| `cargo not found` during build | Install Rust 1.93+ from `https://rustup.rs/`. |
| `entrabot` shows `✗ Failed to connect` in `claude mcp list` | Another entrabot instance (same `ENTRABOT_DATA_DIR`) already holds the singleton lock. Stop it, or give the second agent its own data dir (see *Advanced*). |
| `TypeError: unsupported operand type … '\|'` running a script | A script ran under the system `python3` (3.9). entrabot needs 3.12+; run from the repo so the script re-execs into `.venv/bin/python3`. |

---

## Reference

- [ADR-007 — MXC sandbox integration](../decisions/007-mxc-sandbox-integration.md)
- [MXC platform research](../platform-learnings/mxc-windows-sandbox.md)
- [Upstream note: macOS symlink canonicalization](../platform-learnings/mxc-upstream-feedback-macos-symlinks.md)
- Code: [`src/entrabot/sandbox/`](../../src/entrabot/sandbox/) — `policy.py` (clamp +
  canonicalization), `mac.py` (Seatbelt runner), `binary.py` (SHA256 pin),
  `mcp_server.py` (`run_code` tool)
- Helper: [`scripts/setup_sandbox.sh`](../../scripts/setup_sandbox.sh) ·
  [`scripts/demo_sandbox.py`](../../scripts/demo_sandbox.py)
