# `status.sh`

## Purpose

Root-level status entry point for macOS and Linux. It exists so operators
never have to think about the local Python environment: it bootstraps a
`.venv` if one is missing, installs the dependencies the status check
needs, loads `.env`, and then delegates entirely to
[`scripts/show_agent_status.py`](show-agent-status-py.md), the canonical
Agent Identity status/health command.

## Requirements

- macOS or Linux (Windows users run [`status-windows.ps1`](status-windows-ps1.md) instead).
- A `bash`-compatible shell (`set -euo pipefail`).
- Python 3.12 or 3.13 available as `python3.12`, `python3.13`, or `python3`
  on `PATH` — only needed the first time, to create `.venv`.
- Everything `show_agent_status.py` itself needs at runtime (a bootstrapped
  Provisioner app and its certificate in the OS keystore) — `status.sh`
  does not check for these itself; it only prepares the Python environment.

## Usage

```bash
./status.sh
./status.sh --json
./status.sh --health-only --strict
./status.sh --help
```

## Options

`status.sh` defines no options of its own. Every argument after the script
name is forwarded verbatim (`"$@"`) to `scripts/show_agent_status.py` — see
[`show_agent_status.py`](show-agent-status-py.md#options) for what `--json`,
`--health-only`, `--strict`, and `--help` do.

## Effects

1. If `.venv/bin/python3` does not exist, searches `python3.12`, `python3.13`,
   then `python3` for one whose `sys.version_info >= (3, 12)`, prints
   `Creating local Python environment at .venv...` to stderr, and runs
   `<python> -m venv .venv`.
2. Checks whether `import azure.identity, entrabot` succeeds in the venv;
   if not, prints `Installing EntraBot status dependencies into .venv...`
   to stderr and runs `pip install -e ".[provisioning]"`.
3. If a `.env` file exists at the project root, sources it (`set -a` / `.
   .env` / `set +a`) so every variable it defines is exported into the
   process environment.
4. `exec`s the venv's `python3` running `scripts/show_agent_status.py "$@"`
   — this replaces the shell process, so `status.sh`'s own exit code
   becomes whatever `show_agent_status.py` returns.

No Graph calls or local-state writes happen in `status.sh` itself; all of
that lives in `show_agent_status.py`.

## Exit behavior

- Exits `1` with `ERROR: Python 3.12+ is required to run EntraBot status.`
  on stderr if no suitable interpreter is found to create `.venv`.
- Otherwise, because of the final `exec`, the process exits with whatever
  code `scripts/show_agent_status.py` returns (`0` on success, `1` when
  `--strict`/`--health-only` checks fail or the status script can't
  acquire a token). `status.sh` has no other exit paths of its own beyond
  `set -euo pipefail` aborting on the first failing command.

## Common failures

- **No Python 3.12+ found** — install Python 3.12 or newer (see the
  platform prerequisites scripts) and re-run.
- **First run is slow** — expected; it's creating `.venv` and installing
  `.[provisioning]` extras. Subsequent runs skip both steps.
- **Status check itself fails** (Provisioner not bootstrapped, missing
  Agent Identity state, expired cert) — see
  [`show_agent_status.py` → Common failures](show-agent-status-py.md#common-failures).

## Related commands

- [`status-windows.ps1`](status-windows-ps1.md) — Windows equivalent.
- [`show_agent_status.py`](show-agent-status-py.md) — the command this wrapper delegates to.
- [`health_check.py`](health-check-py.md) — health-only compatibility wrapper around the same command.
- `./scripts/setup.sh --status` forwards its remaining arguments to this script; see [`setup.sh`](../setup/setup-sh.md).
- [Operations scripts index](../index.md#operations)
