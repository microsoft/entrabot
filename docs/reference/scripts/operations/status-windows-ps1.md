# `status-windows.ps1`

Platforms: Windows.

## Purpose

`status-windows.ps1` shows the agent's identity, sponsors, permissions,
licences, certificates and health checks on Windows. It changes nothing and
installs nothing. It is a thin wrapper over
[`show_agent_status.py`](show-agent-status-py.md), run through
`python -m entrabot.winsetup status` so that it uses the project's own Python
environment and the same Azure CLI that setup used.

## Requirements

- Native Windows, and Windows PowerShell 5.1 (ships with Windows) or
  PowerShell 7.
- A completed [`setup-windows.ps1`](../setup/setup-windows-ps1.md) run in this
  folder.
- Network access to Microsoft Graph.

## Usage

```powershell
powershell -ExecutionPolicy Bypass -File status-windows.ps1
powershell -ExecutionPolicy Bypass -File status-windows.ps1 -HealthOnly -Strict
powershell -ExecutionPolicy Bypass -File status-windows.ps1 -Json
```

## Options

- `-Json` — machine-readable output. Nothing else is printed, so it can be
  piped.
- `-HealthOnly` — only the health checks.
- `-Strict` — return non-zero when a health check fails.
- `-Help` — show help and exit `0`.

## Effects

Read-only. On Windows the health checks include a **Local certificate** check:
this PC's Blueprint certificate is in the certificate store, has its private
key, and is in date, with the key store (`tpm` or `software`) and the expiry
date.

It never creates the Python environment or installs packages. If `.venv` is
missing it says to run `scripts\setup-windows.cmd` and stops.

In a folder that has neither `.env` nor `.entrabot-state.json` (a fresh copy, or
one that teardown has disconnected) it prints one line, "This folder is not
connected to an agent. Run scripts\setup-windows.cmd to set one up.", and exits
`1`. With `-Json` that answer is a small JSON object
(`{"configured": false, "message": ...}`).

`PASS`, `WARN` and `FAIL` labels are coloured only when the output is a
terminal that can draw colour (Windows Terminal, or a classic console where
colour can be switched on). When the output is piped, captured, or sent over a
session that cannot draw it, they are plain text. Setting the `NO_COLOR`
environment variable turns colour off everywhere.

The JSON output includes full certificate thumbprints.

For a check that also signs the agent in and tests Teams, use
`scripts\setup-windows.cmd -Diagnose`.

## Exit behavior

- The exit code of `show_agent_status.py`: `0` on success; `1` when a token
  could not be acquired, or when a health check fails under `-Strict` or
  `-HealthOnly`.
- `1` — `.venv` is missing, or this folder is not connected to an agent.

## Related commands

- [Script reference — Operations](../index.md#operations)
- [`show_agent_status.py`](show-agent-status-py.md) — the script this wraps.
- [`setup-windows.ps1`](../setup/setup-windows-ps1.md) — `-Diagnose` and
  `-Status`.
- [`status.sh`](status-sh.md) — the macOS and Linux equivalent.
