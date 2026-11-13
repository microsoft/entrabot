# `scripts/prereqs-windows.ps1`

Platforms: Windows.

## Purpose

`prereqs-windows.ps1` installs optional extras on Windows, and only the ones
you ask for. **It is not a required step.**
[`setup-windows.cmd`](setup-windows-cmd.md) installs everything Entrabot itself
needs, and the setup wizard installs the Azure CLI. This helper covers what
sits around Entrabot:

- Git for Windows and Claude Code, which you need to run the agent in Claude
  Code. The setup wizard calls this script for you when you say yes to
  installing them.
- Microsoft's Agent 365 CLI, for using it yourself. **Setup does not need it**:
  it switches Word, Excel and OneDrive files (Work IQ) on by itself, with no
  CLI, no .NET and no PowerShell 7.
- The C++ build tools, which few people need.

## Requirements

- Native Windows 10 or 11, and Windows PowerShell 5.1 (ships with Windows) or
  PowerShell 7.
- `-WithGit` and `-WithClaudeCode`: no administrator rights and no `winget`.
  Network access to `github.com` and `claude.ai`.
- `-WithWorkIq`: no administrator rights. Network access to Microsoft's .NET
  downloads and to `nuget.org`, and about 1 GB of disk space when the .NET SDK
  has to be installed. `winget` only when PowerShell 7 is missing.
- `-WithBuildTools`: `winget`, and its installer asks for administrator
  approval.

## Usage

```powershell
# What can be installed (installs nothing)
powershell -ExecutionPolicy Bypass -File scripts\prereqs-windows.ps1

# What Claude Code needs
powershell -ExecutionPolicy Bypass -File scripts\prereqs-windows.ps1 -WithGit -WithClaudeCode

# Microsoft's Agent 365 CLI, to use yourself (setup does not need it)
powershell -ExecutionPolicy Bypass -File scripts\prereqs-windows.ps1 -WithWorkIq
```

## Options

- `-WithGit` — Git for Windows, for the current user. Claude Code runs this
  project's hooks through Git Bash.
- `-WithClaudeCode` — Claude Code, using its official installer.
- `-WithWorkIq` — optional, and not used by setup: Microsoft's Agent 365 CLI
  (`a365`), for the current user only, with the .NET SDK it runs on when the PC
  has no .NET SDK 8 or later, and PowerShell 7, which its
  `a365 setup permissions` step needs.
- `-WithBuildTools` — Visual Studio Build Tools with the C++ workload, about
  6 GB. Only needed if setup reports that a package had to be compiled.
- `-SkipBuildTools` — accepted and ignored; build tools are no longer installed
  unless asked for.
- `-Help` — show help and exit `0`.

## Effects

Anything already installed and working is left alone, so the script is safe to
re-run.

- **`-WithGit`.** Looks for a working Git Bash where Claude Code looks
  (`CLAUDE_CODE_GIT_BASH_PATH`, `%ProgramFiles%\Git`,
  `%LOCALAPPDATA%\Programs\Git`, next to `git` on `PATH`). If there is none, it
  downloads the Git for Windows installer from the project's GitHub releases,
  checks that its signature is valid and from the Git for Windows maintainer
  before running it, and installs for the current user. If the installer
  cannot run in this kind of session, it falls back to the portable archive,
  unpacked to `%LOCALAPPDATA%\Programs\Git`. If Git Bash ends up somewhere
  Claude Code does not look, it sets the user environment variable
  `CLAUDE_CODE_GIT_BASH_PATH`.
- **`-WithClaudeCode`.** Runs Claude Code's documented installer
  (`https://claude.ai/install.ps1`), which installs to
  `%USERPROFILE%\.local\bin`. If that folder is not on the user `PATH`, it is
  appended (user `PATH` only; the machine `PATH` is never changed).
- **`-WithWorkIq`.** No administrator rights and no `winget` for .NET or
  `a365`:
  - An `a365` that already runs is left alone: one on `PATH`, one in the .NET
    global tools folder (`%USERPROFILE%\.dotnet\tools`, which is updated with
    `dotnet tool update --global`), or Entrabot's own copy.
  - Otherwise it uses a .NET SDK 8 or later that the PC already has, judged by
    `dotnet --list-sdks`. With none, it downloads Microsoft's install script
    (`https://dot.net/v1/dotnet-install.ps1`), runs it only when Windows says
    its signature is valid and from Microsoft Corporation, and installs the
    .NET 10 SDK (the long-term support release) into
    `%LOCALAPPDATA%\entrabot\tools\dotnet`, with nothing added to `PATH`.
    About 1 GB on disk; a minute or two.
  - It then installs `a365` into `%LOCALAPPDATA%\entrabot\tools\a365` with
    `dotnet tool install --tool-path`, allowing it to run on a newer .NET than
    it was built for (`--allow-roll-forward`). For Entrabot's own .NET it sets
    `DOTNET_ROOT` for the install and the check, because `a365.exe` cannot find
    a .NET that is not installed for the whole PC without it. .NET telemetry is
    turned off for these steps (`DOTNET_CLI_TELEMETRY_OPTOUT`).
  - PowerShell 7 is installed with `winget` if `pwsh` is missing. On the test
    PC, a standard user over SSH, `winget` installed it for that account
    (`%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe`).

  To run that `a365`, start `%LOCALAPPDATA%\entrabot\tools\a365\a365.exe`,
  after setting `DOTNET_ROOT=%LOCALAPPDATA%\entrabot\tools\dotnet` when it runs
  on Entrabot's own .NET; neither folder is on `PATH`. On Windows each `a365` command signs in through its own Windows
  sign-in window, so it needs the PC's desktop.
  `teardown-windows.ps1 -PurgeData` deletes both folders. .NET's own per-user
  caches (`%USERPROFILE%\.dotnet`, `%USERPROFILE%\.nuget`) stay.
- **`-WithBuildTools`.** Installs Visual Studio 2022 Build Tools with the C++
  workload through `winget`.

A tool is reported as installed only if it actually runs afterwards
(`bash --version`, `claude --version`, `a365 --version`). The script ends with
a summary of what
was newly installed, what was already there, and what failed.

## Exit behavior

- `0` — everything asked for is installed and runs; or no switch was given; or
  `-Help`.
- `1` — at least one item could not be installed. The summary names it.

## Common failures

- **Signature check failed** — the download is not run. Try again later, or
  install Git for Windows yourself from <https://git-scm.com/download/win>, or
  the .NET SDK from <https://learn.microsoft.com/dotnet/core/install/windows>.
- **`winget` is not available** (PowerShell 7 for `-WithWorkIq`,
  `-WithBuildTools`) — install the tool from its vendor's site; the message
  gives the address.
- **The Agent 365 CLI does not run after its install** — the summary lists it
  as failed. Check that `%LOCALAPPDATA%\entrabot\tools\dotnet\dotnet.exe`
  exists, then run the script again; it reinstalls a copy that does not run.

## Related commands

- [Script reference — Setup](../index.md#setup)
- [`setup-windows.cmd`](setup-windows-cmd.md) and
  [`setup-windows.ps1`](setup-windows-ps1.md) — the setup this helper supports.
- [`start-entrabot.cmd`](start-entrabot-cmd.md) — starts the agent in Claude
  Code.
- [`prereqs-macos.sh`](prereqs-macos-sh.md) — the macOS equivalent.
- [Windows installation](../../../getting-started/windows.md).
