# Installing EntraBot

Platform-specific setup instructions. On every platform setup is one command,
and it starts the same setup wizard. On macOS and Linux it needs Python 3.12 or
newer first; on Windows it needs nothing.

---

## Table of Contents

- [Windows](#windows)
- [macOS](#macos)
- [Linux](#linux)
- [Verify Installation](#verify-installation)
- [Troubleshooting](#troubleshooting)

---

## Windows

There is no prerequisites step. Nothing has to be installed first: no
PowerShell 7, Python, Azure CLI, `winget`, or administrator rights on the PC.

### Setup

Double-click `scripts\setup-windows.cmd`, or run it from a terminal in the repo
root:

```bat
scripts\setup-windows.cmd
```

The setup wizard asks a few questions. Each has a suggested answer; Enter
accepts it. It then:

1. Finds Python 3.12 or newer, or downloads a private, signature-checked copy,
   and builds the project's `.venv`
2. Signs you in to Microsoft (installing the Azure CLI if there is none: with
   `winget` when your account is a local administrator and has it, otherwise
   as a private copy)
3. Creates or joins the Entra Agent Identity chain (Blueprint → Agent Identity
   → Agent User), assigns a Teams-capable licence when one is free, and grants
   the Graph permissions
4. Creates this PC's certificate, with a non-exportable key, and registers it
5. Writes `.env`, restricted to your account
6. Registers the MCP server for Claude Code (`.mcp.json`) and Copilot CLI
   (`~/.copilot/mcp-config.json`), and offers to install Git for Windows and
   Claude Code if they are missing
7. Tests that the agent can sign in and reach Teams

Nothing is changed in your tenant until you confirm the review screen.
Running setup again repairs or changes an existing setup.

What you need: a Microsoft 365 tenant, an account that administers it (granting
admin consent needs a Global Administrator or Privileged Role Administrator),
and a free licence that includes Teams for the agent.

#### Start the agent

Double-click `start-entrabot.cmd` in the repo root.

#### Certificate storage

| The PC's TPM | Where the key is created | Protection |
|---|---|---|
| Accepts a key | Microsoft Platform Crypto Provider | Non-exportable; the key never leaves the hardware |
| Absent, or refuses the key | Microsoft Software Key Storage Provider | Non-exportable; protected by your Windows account |

Setup finds out by creating a key, which needs no administrator rights. The
finish screen says which one you got.

#### Common flags

```bat
rem Unattended: a new agent
scripts\setup-windows.cmd -NewChain -UpnSuffix workstation -Yes

rem Reuse an existing Blueprint from another machine
scripts\setup-windows.cmd -UseBlueprint <app-id> -Yes

rem Azure Blob Storage for operational data
scripts\setup-windows.cmd -NewChain -UpnSuffix workstation -UseCloudMemory

rem Check an existing setup. Changes nothing.
scripts\setup-windows.cmd -Diagnose
```

Full guide, every question the wizard asks, and what has and has not been
tested: [docs/getting-started/windows.md](docs/getting-started/windows.md).

### Optional extras

`scripts\prereqs-windows.ps1` installs extras on request. It is not required
before setup.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\prereqs-windows.ps1 -WithGit -WithClaudeCode
```

| Switch | Installs |
|--------|----------|
| `-WithGit` | Git for Windows, for your account. Claude Code needs it for this repo's hooks |
| `-WithClaudeCode` | Claude Code |
| `-WithWorkIq` | Microsoft's Agent 365 CLI (with the .NET SDK it runs on, and PowerShell 7), for using it yourself. Setup does not need it: it switches on Word, Excel and OneDrive files by itself |
| `-WithBuildTools` | VS Build Tools (C++ workload, ~6 GB). Only if setup reports a package had to be compiled |

### Certificate rotation

```powershell
powershell -ExecutionPolicy Bypass -File scripts\deploy-windows.ps1
```

The current certificate stays in use until the new one is proven to work.

### Teardown

```powershell
powershell -ExecutionPolicy Bypass -File scripts\teardown-windows.ps1
```

Disconnects this PC: removes its certificate from the agent's Blueprint and
from the Windows certificate store, removes the `entrabot` MCP entries, backs
up then removes `.env` and `.entrabot-state.json`, and retires this PC's setup
certificate (`-KeepSetupCredential` keeps it). It keeps the
tenant-side Agent User, Agent Identity, and Blueprint unless you add
`-DeleteAgent`, and keeps `%LOCALAPPDATA%\entrabot` unless you add `-PurgeData`.

---

## macOS

The wizard on macOS has not yet been run end to end against a real tenant;
see [what has been tested](docs/getting-started/macos-linux.md#what-has-been-tested).

### Prerequisites

Python 3.12 or newer. Nothing else: setup offers to install a private Azure
CLI, and signs you in itself, so no `az login` is needed first. With Homebrew:

```bash
brew install python@3.12
```

Or run the optional helper, which also installs the Xcode Command Line Tools,
Git and the Azure CLI through Homebrew:

```bash
./scripts/prereqs-macos.sh
```

### Setup

Run it from a terminal in the repo root. The wizard asks a few questions; each
has a suggested answer, and Enter accepts it. Nothing is changed in your tenant
until you confirm the review screen. Running it again repairs or changes an
existing setup.

```bash
./scripts/setup.sh
```

To answer up front instead:

```bash
# A new agent, unattended
./scripts/setup.sh --new --with-upn-suffix=workstation --yes

# Join an existing agent from another machine
./scripts/setup.sh --use-blueprint=<blueprint-app-id> --yes
```

#### Start the agent

```bash
./start-entrabot.sh
```

At a terminal, setup offers to do this for you as its last question.

#### Certificate storage

The agent's certificate and its private key are one item in your **login
keychain** (service `entrabot-blueprint-cert`, one item per certificate),
accessed through the `keyring` Python package. No PEM files on disk. Over SSH
the login keychain is usually locked: unlock it with `security unlock-keychain`
before running setup or starting the agent.

#### With Azure Blob Storage

```bash
./scripts/setup.sh --use-cloud-memory
```

#### Certificate rotation

```bash
./scripts/setup.sh --rotate-cert
```

The current certificate stays in use until the new one is proven to work.

### Teardown

```bash
./scripts/teardown.sh
```

Disconnects this computer only: removes its certificate from the agent's
Blueprint and from the keychain, removes the `entrabot` MCP entries, backs up
then removes `.env` and `.entrabot-state.json`, and retires this computer's
setup certificate (`--keep-setup-credential` keeps it). It keeps the agent
unless you add `--delete-agent`, and keeps `~/.entrabot` unless you add
`--purge-data`. `--yes` no longer deletes the agent: it used to. The older
tenant-wide teardown runs with `--tenant-wide`.

---

## Linux

Setup on Linux has not been run yet; see
[what has been tested](docs/getting-started/macos-linux.md#what-has-been-tested).

### Prerequisites

Python 3.12 or newer, and a keyring for the agent's key.

**Ubuntu/Debian:**
```bash
sudo apt update
sudo apt install python3.12 python3.12-venv
```

**Fedora/RHEL:**
```bash
sudo dnf install python3.12
```

Git and the Azure CLI are optional: setup offers to install a private Azure
CLI with `pip`, with no `sudo`.

### Setup

```bash
./scripts/setup.sh
```

The same wizard, questions and flags as on macOS (above). Start the agent with
`./start-entrabot.sh`, and rotate the certificate with
`./scripts/setup.sh --rotate-cert`.

#### Certificate storage

The agent's certificate and its private key are one item in a **Secret
Service** keyring (GNOME Keyring or KDE Wallet), accessed through the `keyring`
Python package. The keyring must be running and unlocked in a D-Bus session that
setup and the agent can reach: a desktop session has one. Setup checks this
first and stops, having changed nothing, if there is none.

Entrabot never falls back to a file: a `keyring` backend that is not the
operating system's own (for example one selected with `PYTHON_KEYRING_BACKEND`)
is refused. For a server with no desktop, see
[Preparing a Linux host](docs/platform-docs/platform-linux.md#preparing-a-linux-host).

### Teardown

```bash
./scripts/teardown.sh
```

As on macOS: it disconnects this computer only, unless you add
`--delete-agent`.

---

## Verify Installation

Setup ends with this check on every platform. To run it again:

```bat
rem Windows
scripts\setup-windows.cmd -Diagnose
```

```bash
# macOS or Linux
./scripts/setup.sh --diagnose
```

On macOS or Linux, to verify the three-hop flow by hand:

```bash
# Activate the venv
source .venv/bin/activate

# Test three-hop token acquisition
python - <<'PY'
from entrabot.config import get_config
from entrabot.tools.teams import acquire_agent_user_token

print(acquire_agent_user_token(get_config())[:40])
PY
```

You should see a 40-character token prefix. If you get an AADSTS error, check:
- The Agent User has a Teams-capable license
- The certificate `.env` names (`ENTRABOT_BLUEPRINT_CERT_SHA1`) is in
  `Cert:\CurrentUser\My` (Windows) or your keychain or keyring (macOS, Linux),
  in date, and unlocked in this session; the health check above checks it

### Boot the MCP server

**Claude Code:** Open the project — it auto-loads from `.mcp.json`. For Teams
messages as they arrive, start it with `./start-entrabot.sh` (macOS, Linux) or
double-click `start-entrabot.cmd` (Windows).

**Copilot CLI:** Run `copilot` from the repo directory — it loads from
`~/.copilot/mcp-config.json`.

---

## Troubleshooting

### Windows

Start with `scripts\setup-windows.cmd -Diagnose`. Running
`scripts\setup-windows.cmd` again repairs most problems.

| Problem | Fix |
|---------|-----|
| "Entrabot setup cannot start on this PC" | Group Policy blocks PowerShell scripts. Ask IT to allow locally created scripts for your account |
| Setup ends with "no Teams licence", or `403` on `/me/chats` | Make a Teams-capable licence available in the tenant, then run setup again |
| The sign-in page never appears | Look for a new tab or window in your browser, or run `scripts\setup-windows.cmd -DeviceCode` |
| Setup says your account lacks admin rights | Sign in as a Global Administrator or Privileged Role Administrator |
| Claude Code blocks every edit or Teams message | Git for Windows is missing: `scripts\prereqs-windows.ps1 -WithGit` |
| `claude` is not recognized | Use `start-entrabot.cmd`, or run `scripts\prereqs-windows.ps1 -WithClaudeCode` |
| "another instance is already running" | One Entrabot server per PC. Close the other assistant first |
| The agent ignores someone in Teams | Add them as a sponsor: `scripts\setup-windows.cmd -Sponsor person@example.com` |
| Certificate expired or missing | Run setup again. To replace one before it expires: `scripts\deploy-windows.ps1` |

More, with causes: [docs/troubleshooting/windows.md](docs/troubleshooting/windows.md).

### macOS and Linux

Start with `./scripts/setup.sh --diagnose`. Running `./scripts/setup.sh` again
repairs most problems.

| Problem | Fix |
|---------|-----|
| "Entrabot needs Python 3.12 or later" | Install it (`brew install python@3.12`, or `sudo apt install python3.12 python3.12-venv`), then run setup again |
| "Setup can't use this account's key store" (macOS, over SSH) | Unlock the login keychain: `security unlock-keychain`. Or run setup at the Mac's own desktop |
| "Setup can't use this account's key store" (Linux) | Start and unlock a Secret Service keyring in a D-Bus session: see [Preparing a Linux host](docs/platform-docs/platform-linux.md#preparing-a-linux-host). Unset `PYTHON_KEYRING_BACKEND` if it is set |
| "ERROR: Unknown option" (exit 2) | Write values with `=`, for example `--use-blueprint=APP_ID` |
| Setup ends with "no Teams licence", or `403` on `/me/chats` | Make a Teams-capable licence available in the tenant, then run setup again |
| Claude Code is not installed | Setup does not install it on macOS or Linux: <https://code.claude.com/docs/en/setup> |
| A script in `scripts/` run by hand cannot find `az` | Setup's private Azure CLI is not on your `PATH` (setup, teardown and `./status.sh` find it themselves): `PATH="$HOME/.entrabot/tools/azure-cli/bin:$PATH" .venv/bin/python scripts/list_sponsors.py` |
| `./scripts/teardown.sh --yes` did not delete the agent | It now disconnects this computer only. Add `--delete-agent` |
| Certificate expired or missing | Run setup again. To replace one before it expires: `./scripts/setup.sh --rotate-cert` |

More, with causes: [docs/troubleshooting/macos-linux.md](docs/troubleshooting/macos-linux.md).

### All platforms

| Problem | Fix |
|---------|-----|
| AADSTS700024 (client assertion invalid) | Cert thumbprint mismatch — re-run setup to regenerate |
| 403 on `wait_for_sponsor_dm` | Agent Identity missing `Application.Read.All` — re-run provisioning or grant manually |
| MCP server exits silently | Check `~/.entrabot/logs/entrabot.log` for errors (`%LOCALAPPDATA%\entrabot\logs` on Windows) |
