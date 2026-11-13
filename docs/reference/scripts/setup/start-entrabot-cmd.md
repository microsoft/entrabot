# `start-entrabot.cmd`

Platforms: Windows.

## Purpose

`start-entrabot.cmd` starts your agent. It is in the Entrabot folder, and it is
the file to double-click after setup. It starts Claude Code in that folder with
the Entrabot channel switched on, so the agent hears Teams messages as they
arrive.

## Requirements

- [`setup-windows.cmd`](setup-windows-cmd.md) has been run in this folder.
- Claude Code is installed, and Git for Windows is installed (Claude Code runs
  this project's hooks through Git Bash). The setup wizard offers to install
  both; so does
  [`prereqs-windows.ps1 -WithGit -WithClaudeCode`](prereqs-windows-ps1.md).

## Usage

Double-click the file, or from a terminal in the Entrabot folder:

```bat
start-entrabot.cmd
start-entrabot.cmd --resume
```

Anything after the file name is passed on to Claude Code.

## Effects

1. Changes to its own folder.
2. Checks that `.venv\Scripts\entrabot-mcp.exe` and `.mcp.json` exist. If not,
   it says to run `scripts\setup-windows.cmd` first and stops.
3. Finds Claude Code: `claude` on `PATH`, otherwise
   `%USERPROFILE%\.local\bin\claude.exe`, where Claude Code's own installer puts
   it. If neither exists, it prints the command that installs it and stops.
4. If `CLAUDE_CODE_GIT_BASH_PATH` is not set, and Git Bash is installed for
   your account (`%LOCALAPPDATA%\Programs\Git`) but not under
   `%ProgramFiles%\Git`, it sets that variable for this window so Claude Code
   finds it.
5. Says what Claude Code asks the first time, in the window where the
   questions appear: whether to trust this folder, whether to use the
   `entrabot` MCP server, and whether to load a development channel. Answer yes
   to each, then say *Who are you?*
6. Runs:

   ```bat
   claude --dangerously-load-development-channels server:entrabot
   ```

It changes nothing on disk. When the window was opened just to run this file
and something went wrong, it waits for a key press so the message stays on
screen.

## Exit behavior

- The exit code of Claude Code.
- `1` — setup has not been run in this folder, or Claude Code is not installed.

## Common failures

- **"Entrabot is not set up in this folder yet"** — run
  `scripts\setup-windows.cmd`.
- **"Claude Code is not installed"** — run the command it prints.
- **Claude Code blocks every edit or Teams message** — Git for Windows is
  missing. See [Windows troubleshooting](../../../troubleshooting/windows.md).
- **"another instance is already running"** — another assistant on this PC
  already has the Entrabot server open. Close it first.
- **Claude Code asks about the folder, the `entrabot` server or a development
  channel** — that is its first start in this folder. Answer yes to each: the
  folder is the one setup set up, the server is your agent, and the channel is
  how Teams messages reach it.

## Related commands

- [Script reference — Setup](../index.md#setup)
- [`setup-windows.cmd`](setup-windows-cmd.md) — run this once first.
- [`prereqs-windows.ps1`](prereqs-windows-ps1.md) — installs Claude Code and
  Git for Windows.
- [Windows installation](../../../getting-started/windows.md).
