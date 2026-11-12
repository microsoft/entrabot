# Installing EntraBot

Platform-specific setup instructions. On macOS, run the prerequisites step
first, then the one-command setup. On Windows there is only the setup command.

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
| `-WithWorkIq` | .NET SDK and the Microsoft Agent 365 CLI, for `-ConfigureA365WorkIq` |
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

### Prerequisites

Run the repository prerequisite helper:

```bash
./scripts/prereqs-macos.sh
```

It installs or verifies Python 3.12+, Azure CLI, GitHub CLI, Node.js, Claude Code,
`jq`, and the Xcode Command Line Tools through Homebrew.

### Setup

Choose exactly one identity mode:

```bash
# Create a fresh identity chain
./scripts/setup.sh --new --with-upn-suffix=workstation

# Or attach to an existing Blueprint
./scripts/setup.sh --use-blueprint=<blueprint-app-id>
```

#### Certificate storage

Private key is stored in **macOS Keychain** (login keychain), accessed via
the `keyring` Python package. No PEM files on disk.

#### With Azure Blob Storage

```bash
./scripts/setup.sh --new --with-upn-suffix=workstation --use-cloud-memory
# Or add --use-cloud-memory to an existing --use-blueprint invocation.
```

### Teardown

```bash
./scripts/teardown.sh
```

---

## Linux

### Prerequisites

**Ubuntu/Debian:**
```bash
sudo apt update
sudo apt install python3.12 python3.12-venv python3-pip git curl
# Azure CLI
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
```

**Fedora/RHEL:**
```bash
sudo dnf install python3.12 git curl
# Azure CLI
sudo rpm --import https://packages.microsoft.com/keys/microsoft.asc
sudo dnf install azure-cli
```

### Setup

```bash
./scripts/setup.sh --new --with-upn-suffix=workstation
```

#### Certificate storage

Private key is stored via **Secret Service** (GNOME Keyring or KDE Wallet),
accessed via the `keyring` Python package. Requires an active D-Bus session
(interactive login, not a headless server).

> **Headless Linux (no GUI / no D-Bus):** Set `PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring`
> as a fallback — stores in `~/.local/share/python_keyring/`. Less secure but
> functional for development.

### Teardown

```bash
./scripts/teardown.sh
```

---

## Verify Installation

Setup ends with this check on every platform. To run it again on Windows:

```bat
scripts\setup-windows.cmd -Diagnose
```

On macOS or Linux, verify the three-hop flow works:

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
- `az login` is current
- The Agent User has a Teams-capable license
- The certificate thumbprint in `.env` matches `Cert:\CurrentUser\My` (Windows)
  or Keychain (Mac)

### Boot the MCP server

**Claude Code:** Open the project — it auto-loads from `.mcp.json`. On Windows,
double-click `start-entrabot.cmd`.

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

### macOS

| Problem | Fix |
|---------|-----|
| `keyring` can't access Keychain | Ensure you're in an interactive session (not SSH without forwarding) |
| `setup.sh` fails on cert generation | Check `security list-keychains` includes login keychain |

### Linux

| Problem | Fix |
|---------|-----|
| `keyring` fails with "No recommended backend" | Install `gnome-keyring` or set `PYTHON_KEYRING_BACKEND` env var |
| D-Bus errors | Start a session: `eval $(dbus-launch --sh-syntax)` |

### All platforms

| Problem | Fix |
|---------|-----|
| AADSTS700024 (client assertion invalid) | Cert thumbprint mismatch — re-run setup to regenerate |
| 403 on `wait_for_sponsor_dm` | Agent Identity missing `Application.Read.All` — re-run provisioning or grant manually |
| MCP server exits silently | Check `~/.entrabot/logs/entrabot.log` for errors (`%LOCALAPPDATA%\entrabot\logs` on Windows) |
