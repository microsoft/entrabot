# Setup scripts

One-shot scripts that bootstrap the agent on a fresh machine. All are idempotent: re-running them detects existing state and only fills the gaps.

State lives in `.entrabot-state.json`. The OS credential store (Keychain on macOS, Keyring on Linux, Cert Store on Windows) holds private keys.

## `setup.sh`

End-to-end macOS / Linux setup. Provisions the Blueprint, Agent Identity, and Agent User chain, mints a cert, writes `.env`, and registers the MCP server with Claude Code and Copilot CLI.

### Usage

```bash
# First-time provisioning (creates a new chain)
./scripts/setup.sh --new --with-upn-suffix=workstation

# Attach this machine to an existing Blueprint (multi-device)
./scripts/setup.sh --use-blueprint=<APP_ID> --agent-user-upn=<UPN>

# Opt into cloud-hosted memory (Azure Blob)
./scripts/setup.sh --new --with-upn-suffix=workstation --use-cloud-memory

# Skip setup and run the consolidated status check
./scripts/setup.sh --status --json
```

Run `./scripts/setup.sh --help` for the full flag matrix.

### What it does

- Verifies `az` login, Python 3.12+, and required CLI tools.
- Calls `entra_provisioning.py` to mint or reuse the dedicated Provisioner app (cert-auth).
- Calls `create_entra_agent_ids.py` to create Blueprint + Agent Identity + Agent User.
- Generates a Blueprint cert, stores the private key in the OS keystore, uploads the public cert to the Blueprint app.
- Writes `.env` with the resulting IDs and thumbprints.
- Optionally provisions Azure Blob Storage when `--use-cloud-memory` is passed (see `provision_blob_storage.py`).
- Registers `entrabot` in `.mcp.json` and `~/.copilot/mcp-config.json` via `mcp_config.py`.
- With `--status`, skips provisioning and delegates to `./status.sh`, forwarding status arguments such as `--json`, `--health-only`, and `--strict`.

### Idempotency

Re-runs reuse the existing chain unless `--new` is passed. Each step short-circuits when its target already exists; cert verification (`verify_blueprint_cert.py`) decides whether to keep or rotate the cert.

ADR-003 covers the certificate-auth choice. ADR-005 covers cloud memory.

## `prereqs-macos.sh`

Installs or verifies the macOS command-line prerequisites used by `setup.sh`.

```bash
./scripts/prereqs-macos.sh
```

The helper uses Homebrew for Python 3.12+, Azure CLI, GitHub CLI, Node.js, Claude Code, and `jq`, and verifies the Xcode Command Line Tools.

## `setup_delegated.sh`

Browser-sign-in setup for `delegated` mode. Caches an MSAL token in the OS keystore so the MCP server can pick it up silently. Localhost browser authentication is primary; device code is the headless fallback.

### Usage

```bash
./scripts/setup_delegated.sh
```

### What it does

- Reads `ENTRABOT_CLIENT_ID` from `.env`.
- Opens the browser for Entra sign-in (MSAL localhost redirect, port 8400).
- Caches the token in Keychain.
- Next Claude Code session picks it up via `try_silent()` — no blocking prompt.

## `setup_ado_credentials.sh`

Stores an Azure DevOps Personal Access Token in macOS Keychain so `git push`/`pull` against `dev.azure.com` authenticates automatically.

### Usage

```bash
./scripts/setup_ado_credentials.sh
```

Prompts for the PAT. Required scope: Code (Read & Write). Generate at `https://dev.azure.com/<your-org>/_usersSettings/tokens`.

## `setup-windows.ps1` / `setup-windows.cmd`

Windows mirror of `setup.sh`. The `.cmd` is a thin wrapper that elevates PowerShell with `-ExecutionPolicy Bypass`.

### Usage

```cmd
scripts\setup-windows.cmd -NewChain -UpnSuffix workstation
```

```powershell
.\scripts\setup-windows.ps1 -NewChain -UpnSuffix workstation
.\scripts\setup-windows.ps1 -UseBlueprint <APP_ID>
```

### What it does

- Refuses to run under WSL (use `setup.sh` there).
- Probes for PowerShell 7, Python 3.12+, `az` CLI, and Git.
- Bootstraps the venv and installs the package.
- Runs the legacy `~/.entrabot` migration helper.
- Provisions identity via `entra_provisioning.py` + `create_entra_agent_ids.py`.
- Generates the Blueprint cert (TPM-first via `generate_windows_cert.py`, falls back to the software KSP).
- Uploads the cert public key to the Blueprint and writes both thumbprints to `.env`.
- Registers the MCP server via `mcp_config.py`.

See `docs/architecture/PLAN-windows-port.md` for the design and failure-modes table.

## `prereqs-windows.ps1`

Installs the prerequisites needed by `setup-windows.ps1`. Safe to re-run.

### Usage

```powershell
.\scripts\prereqs-windows.ps1
```

### What it installs

- PowerShell 7 (`winget install Microsoft.PowerShell`)
- Python 3.12+
- Git
- Azure CLI
- .NET SDK
- Microsoft Agent 365 DevTools CLI (`a365`)
- Visual Studio Build Tools with C++ workload
- Windows SDK

Runs from Windows PowerShell 5.1 so users do not need `pwsh` first.

## `deploy-windows.ps1`

Windows cert rotation. Wraps `rotate_cert_windows.py` with the smoke-test rollback contract.

### Usage

```powershell
.\scripts\deploy-windows.ps1                 # rotate
.\scripts\deploy-windows.ps1 -Status         # status only
.\scripts\deploy-windows.ps1 -Status -Json   # machine-readable
.\scripts\deploy-windows.ps1 -Status -HealthOnly -Strict
```

### What it does

- Captures the current cert's public DER before generating the new one (TPM keys are non-exportable, so this is the only chance).
- Calls `generate_windows_cert.py` for the new cert.
- Hands both DERs to `rotate_cert_windows.rotate()` for the transactional rotation.
- Deletes the old cert from `Cert:\CurrentUser\My` only after the smoke test passes.

## `mcp_config.py`

Dual-host MCP config writer. `setup.sh` and `setup-windows.ps1` call this to register the `entrabot` server with both Claude Code and Copilot CLI.

### Usage

```bash
python scripts/mcp_config.py register --command <path-to-entrabot-mcp>
python scripts/mcp_config.py unregister
```

### What it does

- Writes `entrabot` into `<project-root>/.mcp.json` (Claude Code).
- Writes the same entry into `$COPILOT_HOME/mcp-config.json`, defaulting to `~/.copilot/mcp-config.json` (Copilot CLI).
- Both entries are byte-identical; the host distinction happens at runtime via `clientInfo.name` in the MCP server.
