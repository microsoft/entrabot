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
powershell -ExecutionPolicy Bypass -File status-windows.ps1 -FullThumbprints
```

## Options

- `-Json` — machine-readable output. Nothing else is printed, so it can be
  piped.
- `-HealthOnly` — only the health checks.
- `-Strict` — return non-zero when a health check fails.
- `-FullThumbprints` — show certificate thumbprints in full, to compare them
  with the Microsoft Entra admin center (passed on as
  `python -m entrabot.winsetup status --full-thumbprints`). Without it only
  their last four characters are shown.
- `-Help` — show help and exit `0`.

## Effects

Read-only. On Windows the health checks include a **Local certificate** check:
this PC's Blueprint certificate is in the certificate store, has its private
key, and is in date, with the key store (`tpm` or `software`) and the expiry
date.

They also include **Older setup keys**: a `WARN` while an older setup key
that setup could not switch off in Microsoft Entra is still waiting to be
removed from the Provisioner app (see
[Windows troubleshooting](../../../troubleshooting/windows.md#an-older-setup-key-for-this-computer-could-not-be-switched-off-in-microsoft-entra)).
It names the key by the last four characters of its thumbprint and its expiry
date, and says where in the Microsoft Entra admin center to remove it.
Otherwise the check is `PASS`, or `SKIP` when no Provisioner app is recorded.
A warning never fails a check, so it does not change the exit code, with or
without `-Strict`.

It never creates the Python environment or installs packages. If `.venv` is
missing it says to run `scripts\setup-windows.cmd` and stops.

In a folder that has neither `.env` nor `.entrabot-state.json` (a fresh copy, or
one that teardown has disconnected) it prints one line, "This folder is not
connected to an agent. Run scripts\setup-windows.cmd to set one up.", and exits
`1`. With `-Json` that answer is a small JSON object
(`{"configured": false, "message": ...}`).

Teardown with `-KeepSetupCredential` can leave `.entrabot-state.json` holding
only setup's note about an older setup key it could not switch off (see
[`teardown-windows.ps1`](../teardown/teardown-windows-ps1.md)). In a folder with
no `.env` and only that note it prints two lines and exits `1`, with
`-HealthOnly` and `-Strict` too: the same "not connected" line, then which key
is still waiting, for example "An older setup key is still waiting to be
switched off in Microsoft Entra: the certificate whose thumbprint ends in C0DE
(expires 2027-02-01). Setup will try again the next time it runs here." With
`-Json` the answer is one JSON object of this shape:

```json
{
  "configured": false,
  "message": "This folder is not connected to an agent. ...",
  "old_setup_keys": {
    "waiting": [{"thumbprint_ends_in": "C0DE", "expires": "2027-02-01"}],
    "record_unreadable": false,
    "message": "An older setup key is still waiting to be switched off ..."
  }
}
```

`expires` is `null` when the note does not say. `record_unreadable` is `true`
when part of the note is not a thumbprint, and the message then asks you to
check the Provisioner app's certificates by hand. A state file that records
anything more (a Blueprint, an agent, or this PC's setup certificate), that
notes no waiting key, or that cannot be read, gets the full report.

`PASS`, `WARN` and `FAIL` labels are coloured only when the output is a
terminal that can draw colour (Windows Terminal, or a classic console where
colour can be switched on). When the output is piped, captured, or sent over a
session that cannot draw it, they are plain text. Setting the `NO_COLOR`
environment variable turns colour off everywhere.

The text and JSON output show each certificate thumbprint by its last four
characters (`…B3C4`): this PC's current Provisioner and Blueprint certificates,
and the Blueprint's key credentials. `-FullThumbprints` shows them in full. The
JSON field names are unchanged; see
[`show_agent_status.py`](show-agent-status-py.md#effects) for the fields whose
values are now in the shorter form. An older setup key is only ever shown by
the last four characters of its thumbprint.

For a check that also signs the agent in and tests Teams, use
`scripts\setup-windows.cmd -Diagnose`.

## Exit behavior

- The exit code of `show_agent_status.py`: `0` on success; `1` when a token
  could not be acquired, or when a health check fails under `-Strict` or
  `-HealthOnly`. A warning, such as an older setup key still waiting, is not a
  failure.
- `1` — `.venv` is missing, or this folder is not connected to an agent,
  including a folder that holds only setup's note about an older setup key.

## Related commands

- [Script reference — Operations](../index.md#operations)
- [`show_agent_status.py`](show-agent-status-py.md) — the script this wraps.
- [`setup-windows.ps1`](../setup/setup-windows-ps1.md) — `-Diagnose` and
  `-Status`.
- [`status.sh`](status-sh.md) — the macOS and Linux equivalent.
