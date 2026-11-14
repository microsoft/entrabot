# `start-entrabot.sh`

Platforms: macOS, Linux.

## Purpose

`start-entrabot.sh` starts your agent. It is in the Entrabot folder. It starts
Claude Code in that folder with the Entrabot channel switched on, so the agent
hears Teams messages as they arrive. It is the macOS and Linux counterpart of
[`start-entrabot.cmd`](start-entrabot-cmd.md).

Setup's last screen names it. At a terminal, when everything needed to start
is in place, setup also asks "Start your agent now, in this terminal?" and, on a
yes, finishes and runs this script in its place.

## Requirements

- [`setup.sh`](setup-sh.md) has been run in this folder.
- Claude Code is installed. Setup does not install it on macOS or Linux; see
  <https://code.claude.com/docs/en/setup>. The project's safety checks need
  Claude Code 2.1.295 or later, which setup checks.
- `bash`. The hooks Claude Code runs for this project use the `bash` on your
  `PATH`.
- The account's key store (the macOS Keychain or the Linux keyring) unlocked in
  the session you start it from: the agent reads its sign-in key there. Over SSH
  on a Mac, run `security unlock-keychain` first.

## Usage

From a terminal in the Entrabot folder:

```bash
./start-entrabot.sh
./start-entrabot.sh --resume
```

Anything after the script name is passed on to Claude Code. The script changes
to its own folder first, so it can also be started from anywhere by its full
path, for example `~/entrabot/start-entrabot.sh`.

## Effects

1. Changes to its own folder.
2. Checks that `.venv/bin/entrabot-mcp` is there and executable, and that
   `.mcp.json` exists. If not, it says to run `./scripts/setup.sh` first and
   stops.
3. Finds Claude Code: `claude` on `PATH`, otherwise `~/.local/bin/claude`, then
   `~/.claude/local/claude`, where Claude Code's own installer puts it. If none
   exists, it says where to install it from and stops.
4. Says what Claude Code asks the first time, in the terminal where the
   questions appear: whether to trust this folder, whether to use the
   `entrabot` MCP server, and whether to load a development channel. Answer yes
   to each, then say *Who are you?*
5. Starts, in its own place:

   ```bash
   claude --dangerously-load-development-channels server:entrabot
   ```

It changes nothing on disk.

## Exit behavior

- The exit code of Claude Code: the script hands its process over to it.
- `1` — setup has not been run in this folder, or Claude Code is not installed.

## Common failures

- **"Entrabot is not set up in this folder yet"** — run `./scripts/setup.sh`.
- **"Claude Code is not installed"** — install it from
  <https://code.claude.com/docs/en/setup>, then run this again. A terminal that
  was open before the install may not have it on `PATH` yet; the script also
  looks in `~/.local/bin` and `~/.claude/local`.
- **`Permission denied`** — the copy of the folder lost the files' executable
  bits (some ZIP tools do this). Run `chmod +x start-entrabot.sh scripts/*.sh`
  or start it with `bash start-entrabot.sh`.
- **The agent cannot sign in when started over SSH** — the key store is locked
  in that session. See
  [macOS and Linux troubleshooting](../../../troubleshooting/macos-linux.md).
- **"another entrabot-mcp instance is already running"** — another assistant
  on this computer already has the Entrabot server open. Close it first.
- **Claude Code asks about the folder, the `entrabot` server or a development
  channel** — that is its first start in this folder. Answer yes to each: the
  folder is the one setup set up, the server is your agent, and the channel is
  how Teams messages reach it.

## Related commands

- [Script reference — Setup](../index.md#setup)
- [`setup.sh`](setup-sh.md) — run this once first.
- [`start-entrabot.cmd`](start-entrabot-cmd.md) — the Windows counterpart.
- [macOS and Linux installation](../../../getting-started/macos-linux.md).
