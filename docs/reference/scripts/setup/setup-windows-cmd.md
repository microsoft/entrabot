# `scripts/setup-windows.cmd`

Platforms: Windows.

## Purpose

`setup-windows.cmd` is the entry point for setting up Entrabot on Windows. It
is the file to double-click. It starts
[`setup-windows.ps1`](setup-windows-ps1.md) with the Windows PowerShell that
ships with Windows, lifts the execution policy for that one run, and forwards
every argument unchanged.

## Requirements

- Native Windows 10 or 11.
- Nothing else. PowerShell 7, Python, the Azure CLI, `winget`, and
  administrator rights are not needed.

## Usage

```bat
scripts\setup-windows.cmd
scripts\setup-windows.cmd -NewChain -UpnSuffix workstation -Yes
scripts\setup-windows.cmd -UseBlueprint <BLUEPRINT_APP_ID> -Yes
scripts\setup-windows.cmd -Diagnose
```

With no arguments the setup wizard asks its questions. See
[`setup-windows.ps1`](setup-windows-ps1.md) for every parameter.

## Effects

- Checks whether PowerShell scripts are turned off by Group Policy (the
  `MachinePolicy` or `UserPolicy` execution policy). If they are, it explains
  what to ask IT for and stops; `-ExecutionPolicy Bypass` cannot override a
  policy.
- Removes the "downloaded from the internet" mark from the setup scripts, so a
  ZIP download runs without a prompt.
- Runs:

  ```bat
  %SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-windows.ps1" %*
  ```

  `-ExecutionPolicy Bypass` applies to this run only; it does not change the
  machine or user policy.
- When the window was opened just to run this file (a double-click), it waits
  for a key press at the end so the result stays on screen. It does not wait
  when run from an existing console, in Windows Terminal, over SSH, or with
  `-Yes`.

All other work happens in [`setup-windows.ps1`](setup-windows-ps1.md) and the
setup wizard it starts.

## Exit behavior

- The exit code of `setup-windows.ps1`: `0` set up, `1` failed, `2` conflicting
  parameters or a missing answer under `-Yes`, `3` set up with something to
  check, `130` cancelled.
- `1` when Group Policy blocks scripts.

## Common failures

- **"Entrabot setup cannot start on this PC"** — scripts are blocked by Group
  Policy. See [Windows troubleshooting](../../../troubleshooting/windows.md).

## Related commands

- [Script reference — Setup](../index.md#setup)
- [`setup-windows.ps1`](setup-windows-ps1.md) — the script this launcher starts.
- [`start-entrabot.cmd`](start-entrabot-cmd.md) — starts the agent after setup.
- [`prereqs-windows.ps1`](prereqs-windows-ps1.md) — optional extras.
- [Windows installation](../../../getting-started/windows.md).
