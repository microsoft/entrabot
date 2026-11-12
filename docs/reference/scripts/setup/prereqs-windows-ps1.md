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
- The Work IQ tools and the C++ build tools, which few people need.

## Requirements

- Native Windows 10 or 11, and Windows PowerShell 5.1 (ships with Windows) or
  PowerShell 7.
- `-WithGit` and `-WithClaudeCode`: no administrator rights and no `winget`.
  Network access to `github.com` and `claude.ai`.
- `-WithWorkIq` and `-WithBuildTools`: `winget`, and their installers may ask
  for administrator approval.

## Usage

```powershell
# What can be installed (installs nothing)
powershell -ExecutionPolicy Bypass -File scripts\prereqs-windows.ps1

# What Claude Code needs
powershell -ExecutionPolicy Bypass -File scripts\prereqs-windows.ps1 -WithGit -WithClaudeCode

# For scripts\setup-windows.cmd -ConfigureA365WorkIq
powershell -ExecutionPolicy Bypass -File scripts\prereqs-windows.ps1 -WithWorkIq
```

## Options

- `-WithGit` — Git for Windows, for the current user. Claude Code runs this
  project's hooks through Git Bash.
- `-WithClaudeCode` — Claude Code, using its official installer.
- `-WithWorkIq` — the .NET SDK 9 and the Microsoft Agent 365 CLI (`a365`). It
  does not install PowerShell 7, which the `a365 setup permissions` step also
  requires.
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
- **`-WithWorkIq`.** Installs the .NET SDK 9 with `winget` if `dotnet` is
  missing, then installs or updates the `a365` CLI with `dotnet tool`.
- **`-WithBuildTools`.** Installs Visual Studio 2022 Build Tools with the C++
  workload through `winget`.

A tool is reported as installed only if it actually runs afterwards
(`bash --version`, `claude --version`). The script ends with a summary of what
was newly installed, what was already there, and what failed.

## Exit behavior

- `0` — everything asked for is installed and runs; or no switch was given; or
  `-Help`.
- `1` — at least one item could not be installed. The summary names it.

## Common failures

- **Signature check failed** — the download is not run. Try again later, or
  install Git for Windows yourself from <https://git-scm.com/download/win>.
- **`winget` is not available** (`-WithWorkIq`, `-WithBuildTools`) — install the
  tool from its vendor's site; the message gives the address.

## Related commands

- [Script reference — Setup](../index.md#setup)
- [`setup-windows.cmd`](setup-windows-cmd.md) and
  [`setup-windows.ps1`](setup-windows-ps1.md) — the setup this helper supports.
- [`start-entrabot.cmd`](start-entrabot-cmd.md) — starts the agent in Claude
  Code.
- [`prereqs-macos.sh`](prereqs-macos-sh.md) — the macOS equivalent.
- [Windows installation](../../../getting-started/windows.md).
