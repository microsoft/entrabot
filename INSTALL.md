# Installing EntraBot

Platform-specific setup instructions. Run the prerequisites step first, then
the one-command setup for your platform.

---

## Table of Contents

- [Windows](#windows)
- [macOS](#macos)
- [Linux](#linux)
- [Verify Installation](#verify-installation)
- [Troubleshooting](#troubleshooting)

---

## Windows

### Prerequisites

Run the automated prerequisite installer from any PowerShell terminal (5.1 or 7):

```powershell
.\scripts\prereqs-windows.ps1
```

This checks for and installs (via winget):

| Tool | Why |
|------|-----|
| PowerShell 7+ | Required by setup-windows.ps1 |
| Python 3.12+ | Runtime (rejects Microsoft Store stub) |
| Git | Source control |
| Azure CLI (`az`) | Entra provisioning |
| VS Build Tools (C++ workload) | Compiles native Python packages (cffi, cryptography) |

> **Note:** VS Build Tools is ~6 GB and takes 5–10 minutes. Use `-SkipBuildTools`
> if you already have Visual Studio installed with the C++ workload.

After prereqs install, **close and reopen your terminal** so PATH updates take
effect.

### Setup

From PowerShell 7 (`pwsh`), in the repo root:

```powershell
.\scripts\setup-windows.ps1 -NewChain -UpnSuffix workstation
```

This provisions:
1. Python venv + all dependencies
2. Entra Agent Identity chain (Blueprint → Agent Identity → Agent User)
3. Self-signed certificate (TPM-backed if available, otherwise Software KSP)
4. M365 license assignment
5. Graph permission grants (Chat, Mail, Files, Storage)
6. `.env` file with cert thumbprint and agent config
7. MCP registration for Claude Code (`.mcp.json`) and Copilot CLI (`~/.copilot/mcp-config.json`)

#### Certificate storage

| Machine type | Key Storage Provider | Security |
|---|---|---|
| TPM 2.0 present (`Get-Tpm` → Ready) | Microsoft Platform Crypto Provider | Key never leaves hardware |
| No TPM / VM | Microsoft Software Key Storage Provider | DPAPI-encrypted, bound to user profile |

Both are at least as strong as the macOS Keychain baseline.

#### Common flags

```powershell
# Reuse an existing Blueprint from another machine
.\scripts\setup-windows.ps1 -UseBlueprint <app-id>

# Enable Azure Blob Storage for operational data
.\scripts\setup-windows.ps1 -NewChain -UpnSuffix workstation -UseCloudMemory
```

### Teardown

```powershell
.\scripts\teardown-windows.ps1
```

Removes Blueprint certificates from the Windows Certificate Store, deletes
`%LOCALAPPDATA%\entrabot`, removes certificate settings from `.env`, and
unregisters the MCP servers. It does **not** delete the tenant-side Agent User,
Agent Identity, or Blueprint.

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

After setup completes on any platform, verify the three-hop flow works:

```bash
# Activate the venv
# Windows: .\.venv\Scripts\Activate.ps1
# Mac/Linux: source .venv/bin/activate

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

**Claude Code:** Open the project — it auto-loads from `.mcp.json`.

**Copilot CLI:** Run `copilot` from the repo directory — it loads from
`~/.copilot/mcp-config.json`.

---

## Troubleshooting

### Windows

| Problem | Fix |
|---------|-----|
| `pip install` fails with "Microsoft Visual C++ required" | Run `prereqs-windows.ps1` — it installs VS Build Tools |
| `entrabot-mcp.exe` locked during reinstall | Kill stale processes: `Get-Process entrabot-mcp \| Stop-Process` |
| Python is the Microsoft Store stub | Install real Python via `winget install Python.Python.3.12` |
| `Get-Tpm` requires admin | Use non-elevated PowerShell — the script falls back to Software KSP automatically |
| 36 "missing YAML frontmatter" skill errors | Run from the correct repo directory; skill symlinks don't work on Windows |

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
| MCP server exits silently | Check `~/.entrabot/logs/entrabot.log` for errors |
