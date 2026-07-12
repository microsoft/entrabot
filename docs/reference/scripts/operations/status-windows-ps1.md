# `status-windows.ps1`

## Purpose

Windows PowerShell counterpart to [`status.sh`](status-sh.md). It ensures
a local Python virtual environment exists, installs the status check's
dependencies if needed, loads `.env` into the process environment, and
delegates to [`scripts/show_agent_status.py`](show-agent-status-py.md) —
the same consolidated Agent Identity status/health command used on
macOS/Linux.

## Requirements

- Windows, running under Windows PowerShell 5.1+ or PowerShell 7+ (`pwsh`).
- Python 3.12 or 3.13 available as `python3.12`, `python3.13`, or `python`
  on `PATH` — only needed the first time, to create `.venv`.
- Everything `show_agent_status.py` itself needs at runtime (a bootstrapped
  Provisioner app and its certificate in the Windows Certificate Store) —
  this wrapper only prepares the Python environment, it does not check
  Agent Identity state.

## Usage

```powershell
.\status-windows.ps1
.\status-windows.ps1 -Json
.\status-windows.ps1 -HealthOnly -Strict
.\status-windows.ps1 -Help
```

## Options

| Switch        | Forwarded as     | Meaning                                                      |
| ------------- | ---------------- | ------------------------------------------------------------- |
| `-Json`       | `--json`         | Output machine-readable JSON.                                 |
| `-HealthOnly` | `--health-only`  | Only print health checks.                                     |
| `-Strict`     | `--strict`       | Return non-zero when health checks fail.                      |
| `-Help`       | `--help`         | Show the underlying `show_agent_status.py` help.               |

All four are switches (booleans) and default to `$false`; there are no
positional parameters. See
[`show_agent_status.py`](show-agent-status-py.md#options) for what each
forwarded flag actually changes.

## Effects

1. Resolves the venv Python path, preferring `.venv\Scripts\python.exe`,
   falling back to `.venv/bin/python` if only that exists, and defaulting
   to the Windows path otherwise.
2. If that path doesn't exist, searches `python3.12`, `python3.13`, then
   `python` via `Get-Command`, verifying `sys.version_info >= (3, 12)`;
   prints a red `ERROR: Python 3.12+ is required...` and exits if none
   qualify, otherwise creates the venv with `& $Python -m venv .venv`.
3. Checks whether `import azure.identity, entrabot` succeeds in the venv;
   if `$LASTEXITCODE` is non-zero, installs `.[provisioning]` extras.
4. If `.env` exists, parses it line by line (skipping comments and lines
   without `=`) and sets each variable via
   `[Environment]::SetEnvironmentVariable(..., 'Process')`.
5. Builds a forwarded-argument array from the switches and invokes the
   venv's `python.exe` against `scripts/show_agent_status.py`, then exits
   the wrapper with that call's `$LASTEXITCODE`.

No Graph calls or local-state writes happen in this wrapper itself.

## Exit behavior

- Exits `1` if no Python 3.12+ interpreter can be found to create `.venv`.
- `$ErrorActionPreference = 'Stop'` and `Set-StrictMode -Version Latest`
  mean any other unhandled terminating error in the script (e.g. a broken
  `.env` line, an inaccessible venv path) aborts execution with a generic
  non-zero PowerShell exit — the script does not assign it a specific code.
- Otherwise the script ends with `exit $LASTEXITCODE`, mirroring whatever
  `scripts/show_agent_status.py` returned (`0` success, `1` on failed
  strict/health-only checks or a token error).

## Common failures

- **No Python 3.12+ found** — install Python 3.12+ and ensure it's on
  `PATH`, then re-run.
- **First run is slow** — expected; creating `.venv` and installing
  `.[provisioning]` extras only happens once.
- **Status check itself fails** — see
  [`show_agent_status.py` → Common failures](show-agent-status-py.md#common-failures).

## Related commands

- [`status.sh`](status-sh.md) — macOS/Linux equivalent.
- [`show_agent_status.py`](show-agent-status-py.md) — the command this wrapper delegates to.
- [`health_check.py`](health-check-py.md) — health-only compatibility wrapper around the same command.
- `scripts/setup-windows.ps1 -Status` and `scripts/deploy-windows.ps1 -Status` both forward `-Json`/`-HealthOnly`/`-Strict` to this script; see [`setup-windows.ps1`](../setup/setup-windows-ps1.md) and [`deploy-windows.ps1`](../setup/deploy-windows-ps1.md).
- [Operations scripts index](../index.md#operations)
