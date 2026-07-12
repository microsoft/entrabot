# `scripts/prereqs-windows.ps1`

Platforms: Windows.

## Purpose

`prereqs-windows.ps1` installs the prerequisites [`setup-windows.ps1`](setup-windows-ps1.md)
needs via `winget`: PowerShell 7+, Python 3.12+, Git, the Azure CLI, the .NET
SDK, the Microsoft Agent 365 DevTools CLI (`a365`), and — unless skipped —
Visual Studio Build Tools with the C++ workload (needed to compile native
Python packages such as `cryptography`/`cffi`). It is safe to re-run; each
step detects an existing install and skips ahead.

## Requirements

- **Can run from Windows PowerShell 5.1** — unlike `setup-windows.ps1`, this
  script does not require `pwsh` first; it only needs `#Requires -Version
  5.1`, so it can bootstrap `pwsh` itself from the pre-installed shell.
- **`winget` (Windows Package Manager) must already be available.** It ships
  with Windows 11 and Windows 10 (1809+); if missing, the script prints the
  `https://aka.ms/getwinget` install link and exits.
- A working internet connection for `winget` downloads.

## Usage

```powershell
# Install everything
.\scripts\prereqs-windows.ps1

# Skip Visual Studio Build Tools (large download, ~6 GB)
.\scripts\prereqs-windows.ps1 -SkipBuildTools

# Show detailed help
.\scripts\prereqs-windows.ps1 -Help
```

## Options

- `-SkipBuildTools` — skip installing Visual Studio Build Tools with the C++
  workload. Native Python package builds will fail without a C/C++ toolchain
  unless one is already present.
- `-Help` — print the script's `Get-Help`-based documentation and exit `0`.

## Effects

1. Verifies `winget` is on `PATH`; exits `1` immediately if not.
2. **PowerShell 7+** — installs or upgrades via
   `winget install --id Microsoft.PowerShell` if the detected `pwsh` version
   is below 7.0 or missing entirely.
3. **Python 3.12+** — checks `python` on `PATH`, treating the Microsoft Store
   stub `python.exe` under `WindowsApps` as "not installed" so it always
   falls through to a real `winget install --id Python.Python.3.12`.
4. **Git** — `winget install --id Git.Git` if missing.
5. **Azure CLI** — `winget install --id Microsoft.AzureCLI` if `az` is
   missing.
6. **.NET SDK** — `winget install --id Microsoft.DotNet.SDK.9` if `dotnet` is
   missing.
7. **Microsoft Agent 365 DevTools CLI (`a365`)** — installs or updates via
   `dotnet tool install/update --global Microsoft.Agents.A365.DevTools.Cli`,
   adding `%USERPROFILE%\.dotnet\tools` to `PATH` for the rest of the run.
8. **Visual Studio Build Tools + C++ workload** (skipped by
   `-SkipBuildTools`) — probes `vswhere.exe` for the
   `Microsoft.VisualStudio.Component.VC.Tools.x86.x64` component; if absent,
   installs via `winget install --id Microsoft.VisualStudio.2022.BuildTools`
   with the VC++ workload and Windows SDK. This step alone can take 5–10
   minutes and ~6 GB of disk.
9. Refreshes `PATH` from the registry (`Refresh-PathEnv`) after each install
   step, then runs a final validation pass across `pwsh`, `python`, `git`,
   `az`, `dotnet`, and `a365`, printing a summary of already-present,
   newly-installed, and failed tools.

## Exit behavior

- `0` — all checked tools validate on `PATH` at the end, or `-Help` was
  requested.
- `0` — every install step succeeded but one or more tools are still not
  visible on `PATH` in the current session (common right after a fresh
  install); the script warns and tells the operator to open a new terminal
  before running `setup-windows.ps1`.
- `1` — `winget` is not available, or any individual `winget`/`dotnet tool`
  install step reports a non-zero exit code (VS Build Tools is re-checked via
  `vswhere` once before being counted as failed, since `winget` can report a
  non-zero code on an otherwise-successful Visual Studio install).

## Common failures

- **`winget not found`** — install Windows Package Manager from
  `https://aka.ms/getwinget`, then re-run.
- **Tools "not found" right after install** — close and reopen the terminal
  so the refreshed machine/user `PATH` is picked up, then re-run to confirm.
- **VS Build Tools install looks stuck** — it is a large, slow `winget`
  install; give it the full 5–10 minutes before assuming failure.
- **Python still resolves to the Microsoft Store stub** — the script already
  ignores the `WindowsApps` stub and installs a real Python via `winget`; if
  `python` still points at the stub afterward, check `PATH` ordering in a new
  terminal.

## Related commands

- [Script reference — Setup](../index.md#setup)
- [`setup-windows.ps1`](setup-windows-ps1.md) — the script this prepares the
  machine for.
- [`prereqs-macos.sh`](prereqs-macos-sh.md) — the macOS counterpart.
- [Windows installation](../../../getting-started/windows.md).
