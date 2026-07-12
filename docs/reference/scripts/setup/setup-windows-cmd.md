# `scripts/setup-windows.cmd`

Platforms: Windows.

## Purpose

`setup-windows.cmd` is a one-line `cmd.exe` wrapper that launches
[`setup-windows.ps1`](setup-windows-ps1.md) with the correct execution policy,
so operators do not have to remember to pass `-ExecutionPolicy` themselves. It
forwards all arguments through to the PowerShell script unchanged.

## Requirements

- Native Windows with `cmd.exe`.
- **PowerShell 7 (`pwsh`) on `PATH`** — the wrapper invokes `pwsh`, not Windows
  PowerShell 5.1. If `pwsh` is missing, run
  [`prereqs-windows.ps1`](prereqs-windows-ps1.md) first.
- All prerequisites required by [`setup-windows.ps1`](setup-windows-ps1.md)
  itself.

## Usage

```bat
scripts\setup-windows.cmd -NewChain -UpnSuffix winagent
scripts\setup-windows.cmd -UseBlueprint <BLUEPRINT_APP_ID> -AgentUserUpn <UPN>
scripts\setup-windows.cmd -Status -Json
```

## Effects

- Runs:

  ```bat
  pwsh -ExecutionPolicy Bypass -NoProfile -File "%~dp0setup-windows.ps1" %*
  ```

  where `%~dp0` resolves to the wrapper's own directory (so it always finds the
  sibling `setup-windows.ps1`) and `%*` forwards every argument.
- `-ExecutionPolicy Bypass` avoids an execution-policy prompt for this
  invocation only; it does not change the machine or user policy.
- `-NoProfile` skips PowerShell profile scripts for a clean, reproducible run.
- All real work — prerequisite probing, provisioning, certificate generation,
  `.env` writing, and MCP registration — happens inside
  [`setup-windows.ps1`](setup-windows-ps1.md); this wrapper adds no behavior of
  its own.

## Exit behavior

- The wrapper's exit code is that of the `pwsh`/`setup-windows.ps1` process it
  launches. See the [`setup-windows.ps1` exit behavior](setup-windows-ps1.md#exit-behavior)
  (`0` success, `1` fatal/refusal, `2` mutually exclusive storage flags, and the
  underlying status exit code under `-Status`).

## Common failures

- **`'pwsh' is not recognized`** — PowerShell 7 is not installed or not on
  `PATH`. Install it with [`prereqs-windows.ps1`](prereqs-windows-ps1.md), then
  reopen the terminal.

## Related commands

- [Script reference — Setup](../index.md#setup)
- [`setup-windows.ps1`](setup-windows-ps1.md) — the script this wrapper invokes.
- [`prereqs-windows.ps1`](prereqs-windows-ps1.md) — installs `pwsh` and the rest
  of the Windows prerequisites.
- [Windows installation](../../../getting-started/windows.md).
