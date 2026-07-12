# `scripts/prereqs-macos.sh`

Platforms: macOS.

## Purpose

`prereqs-macos.sh` installs the command-line prerequisites [`setup.sh`](setup-sh.md)
needs, using Homebrew: the Xcode Command Line Tools, Python 3.12+, Git, the
Azure CLI, and — unless skipped — the .NET SDK, the Microsoft Agent 365
DevTools CLI (`a365`), and PowerShell 7+. It is safe to re-run; each step
detects an existing install and skips straight to the next one.

## Requirements

- **macOS only.** The script checks `uname -s` for `Darwin` and exits `1` on
  any other platform, pointing Linux users at their package manager and
  Windows users at [`prereqs-windows.ps1`](prereqs-windows-ps1.md).
- **Homebrew must already be installed.** The script does not install
  Homebrew itself — Homebrew's own installer needs `sudo` and an interactive
  EULA acceptance, which this script cannot safely automate. If `brew` is not
  on `PATH` (checking both the Apple Silicon and Intel prefixes), it prints
  the official install command and exits `1`.
- A working internet connection for Homebrew downloads.

## Usage

```bash
# Install everything (core tools + .NET SDK/a365 + PowerShell 7)
./scripts/prereqs-macos.sh

# Skip the .NET SDK and Agent 365 DevTools CLI
./scripts/prereqs-macos.sh --skip-a365

# Skip PowerShell 7
./scripts/prereqs-macos.sh --skip-pwsh

# Skip both optional groups — only the core tools setup.sh strictly requires
./scripts/prereqs-macos.sh --core-only
```

## Options

- `--skip-a365` — skip installing the .NET SDK and the Microsoft Agent 365
  DevTools CLI (`a365`). Use `setup.sh --with-a365-work-iq` later to add them
  when Work IQ is needed.
- `--skip-pwsh` — skip installing PowerShell 7+. Only needed for
  `setup.sh --configure-a365-work-iq`.
- `--core-only` — shorthand for `--skip-a365 --skip-pwsh`.
- `-h`, `--help` — print the script's header comment as usage text and exit
  `0`.
- Any unrecognized argument prints `ERROR: Unknown argument: <arg>` to
  stderr, then falls through to the same help text as `--help` and **exits
  `0`** — it does not abort with a non-zero status.

## Effects

1. Verifies macOS and resolves the Homebrew prefix (`/opt/homebrew` or
   `/usr/local`), exporting it onto `PATH` for the rest of the run if not
   already present.
2. **Xcode Command Line Tools** — if not already installed
   (`xcode-select -p`), triggers `xcode-select --install`, which opens a
   system dialog. This step is interactive: the script prints a prompt and
   blocks on `read` until the operator accepts the dialog and presses Enter.
3. **Python 3.12+** — probes `python3.13`, `python3.12`, then `python3` for a
   version ≥ 3.12; installs `python@3.12` via Homebrew if none qualify.
4. **Git** — installs via Homebrew only if no `git` is already on `PATH`
   (Xcode CLT usually provides one).
5. **Azure CLI** — installs `azure-cli` via Homebrew if `az` is missing.
6. **.NET SDK + `a365`** (skipped by `--skip-a365`/`--core-only`) — installs
   the `dotnet` Homebrew formula, then installs or updates the Microsoft
   Agent 365 DevTools CLI via `dotnet tool install/update --global
   Microsoft.Agents.A365.DevTools.Cli`, adding `~/.dotnet/tools` to `PATH`
   for the rest of the run.
7. **PowerShell 7+** (skipped by `--skip-pwsh`/`--core-only`) — installs the
   `powershell` Homebrew formula if `pwsh` is missing.
8. Runs a final validation pass re-checking every tool it was asked to
   install (skipping checks for groups that were skipped) and prints a
   summary of what was already present, newly installed, and failed.

## Exit behavior

- `0` — all requested tools are installed and confirmed on `PATH`, `--help`
  was requested, or an unrecognized argument was passed (help text is shown
  first).
- `0` — installs all succeeded but one or more newly installed tools are not
  yet visible on `PATH` in the current shell (most commonly `python3.12` or
  `a365` right after their first install). The script warns and tells the
  operator to open a new terminal before running `setup.sh`.
- `1` — not running on macOS, Homebrew is missing, Xcode CLT installation
  did not complete, or any Homebrew install step failed.

## Common failures

- **`Homebrew not found`** — install it with the official one-liner printed
  by the script, then re-run.
- **Stuck at the Xcode CLT dialog** — the system dialog can appear behind
  other windows; check the Dock, accept it, then return to the terminal and
  press Enter.
- **`python3.12 not on PATH` after install** — open a new terminal (Homebrew
  changes to `PATH` only take effect in new shells) and re-run.
- **`a365` missing after install** — same PATH-refresh issue for
  `~/.dotnet/tools`; the script prints the exact `export PATH` line to add to
  `~/.zshrc` if needed.

## Related commands

- [Script reference — Setup](../index.md#setup)
- [`setup.sh`](setup-sh.md) — the script this prepares the machine for.
- [`prereqs-windows.ps1`](prereqs-windows-ps1.md) — the Windows counterpart.
- [macOS / Linux installation](../../../getting-started/macos-linux.md).
