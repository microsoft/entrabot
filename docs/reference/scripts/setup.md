# Setup scripts

One-shot scripts that bootstrap the agent on a fresh machine. All are idempotent: re-running them detects existing state and only fills the gaps.

State lives in `.entrabot-state.json`. The OS credential store (Keychain on macOS, Keyring on Linux, Cert Store on Windows) holds private keys.

## `setup.sh`

End-to-end macOS / Linux setup. Provisions the Blueprint, Agent Identity, and Agent User chain, mints a cert, writes `.env`, and registers the MCP server with Claude Code and Copilot CLI.

### Usage

```bash
# First-time provisioning (creates a new chain)
./scripts/setup.sh --new --with-upn-suffix=sati-agent

# Attach this machine to an existing Blueprint (multi-device)
./scripts/setup.sh --use-blueprint=<APP_ID> --agent-user-upn=<UPN>

# Opt into cloud-hosted memory (Azure Blob)
./scripts/setup.sh --new --with-upn-suffix=sati-agent --use-cloud-memory

# Skip setup and run the consolidated status check
./scripts/setup.sh --status --json
```

Run `./scripts/setup.sh --help` for the full flag matrix.

### What it does

- Verifies `az` login, Python 3.12+, and required CLI tools.
- Calls `entra_provisioning.py` to mint or reuse the dedicated Provisioner app (cert-auth).
- Calls `create_entra_agent_ids.py` to create Blueprint + Agent Identity + Agent User.
- Generates a Blueprint cert, stores the private key in the OS keystore, uploads the public cert to the Blueprint app.
- Writes `.env` with the resulting IDs and thumbprints, then automatically persists the
  shared tenant, Blueprint, and current certificate metadata to the platform config directory
  (`%LOCALAPPDATA%\entrabot\global.env` on Windows, `~/.entrabot/global.env` elsewhere).
- Optionally provisions Azure Blob Storage when `--use-cloud-memory` is passed (see `provision_blob_storage.py`).
- Registers `entrabot` in `.mcp.json` and `~/.copilot/mcp-config.json` via `mcp_config.py`.
- With `--status`, skips provisioning and delegates to `./status.sh`, forwarding status arguments such as `--json`, `--health-only`, and `--strict`.

### Idempotency

Re-runs reuse the existing chain unless `--new` is passed. Each step short-circuits when its target already exists; cert verification (`verify_blueprint_cert.py`) decides whether to keep or rotate the cert.

Shared-config persistence never copies the setup agent's identity or its A365 export flags.
Only tenant/Blueprint IDs and certificate metadata are saved automatically, not agent-specific
storage locations or arbitrary runtime settings. Existing shared preferences are preserved.
Setup rejects a different tenant or Blueprint before provisioning rather than overwriting
another installation. Set `ENTRABOT_HOME` to a separate config location before setting up a
different tenant or a new Blueprint; this is a single shared chain per config home.
A certificate refresh for the same Blueprint
replaces the previous certificate metadata as one unit, so platform-specific SHA-1/KSP fields
cannot remain stale.

For existing installations, Windows certificate registration preserves other registered
public certificates and stops if they cannot be read. It does not silently revoke another
machine's certificate. Certificate rotation/revocation remains a separate administrative action.
An unreadable or malformed `.entrabot-state.json` also stops provisioning: it is not treated as
a new installation. Restore the saved state rather than repeatedly creating replacement agents.

See `docs/reference/setup-script.md` for the long form. ADR-003 covers the cert-auth choice. ADR-005 covers cloud memory.

## A365 observability and ALM evidence

Entrabot identity setup remains the prerequisite. Observability onboarding never creates a
Blueprint, Agent Identity, or Agent User, and it does not run Work IQ tooling setup.

This integration targets Microsoft Entra tenants in the Microsoft public cloud, not just
Microsoft's corporate tenant. Tenant, Blueprint and agent IDs come from the selected
configuration; the fixed Microsoft first-party resource app ID, scope and public-cloud
service endpoints are protocol constants. Sovereign-cloud endpoints are not implemented.
Each tenant still needs the relevant service availability, licenses, administrator permissions
and audit/Defender configuration; a successful setup is not proof of tenant-side ingestion.

For the primary agent configured directly in the setup clone, run:

```powershell
.\scripts\setup-windows.ps1 -ConfigureA365Observability
```

```bash
./scripts/setup.sh --configure-a365-observability
```

Use `-AgentRoot`/`--agent-root` only to select an additional existing agent:

```powershell
.\scripts\setup-windows.ps1 -ConfigureA365Observability -AgentRoot C:\path\to\agent
```

```bash
./scripts/setup.sh --configure-a365-observability --agent-root=/path/to/agent
```

This standalone mode exits before ordinary setup, provisioning, certificate rotation, or tool
installation. Do not combine it with `--new`, `-UseBlueprint`, or Work IQ options. The existing
venv and this checkout's dependencies must already be installed.

Run the installed harness from that same environment, not an older globally installed copy.
Activate the venv once per terminal, then the short `entrabot` command uses this checkout:

```powershell
.\.venv\Scripts\Activate.ps1
entrabot
```

```bash
source .venv/bin/activate
entrabot
```

Both wrappers call `scripts/configure_a365_observability.py --authorize`. When the agent root is
omitted, the target is the Entrabot clone, not whichever unrelated directory the shell happens
to be in. Legacy clone-based primary agents do not need harness scaffolding; their Agent User
UPN supplies the telemetry service name.

Authorization:

- Resolves `global.env`, with the setup clone's combined `.env` as a backward-compatible
  shared-only fallback, plus the selected directory's combined/per-agent `.env`.
  Conflicting tenant or Blueprint IDs are rejected.
- Reuses `ensure_a365_observability_permissions.py`, checks the identity-parent chain,
  and applies the Blueprint and Agent User observability grants.
- Authenticates using the provisioner certificate and tenant saved for this checkout.
  Existing-provisioner authentication does not depend on the Azure CLI's current sign-in;
  mismatched provisioner state is rejected. Normal provisioning still requires `az login`.
- Acquires a delegated observability token with the existing certificate-based three-hop
  flow, then writes `ENTRABOT_A365_OBSERVABILITY_ENABLED=true` and
  `ENTRABOT_A365_EXPORT_ENABLED=true` to the selected agent's `.entrabot/.env`.
- Leaves shared `global.env` and unrelated agents unchanged. A failed grant or token
  acquisition does not change the runtime flags. Graph changes already completed are
  not rolled back; the grants helper is idempotent, so rerun after resolving the failure.

Restart the harness after authorization. Successful token acquisition is not proof of
telemetry ingestion: confirm activity in the tenant's observability destination separately.
This command does not submit to ALM or grant ALM approval.

The harness explicitly emits content-suppressed `InvokeAgent` lifecycle spans for CLI and
Teams turns and keeps the exporter token refreshed. Explicit tool, inference, and output span
instrumentation is not part of this branch.
Exported failure events use a fixed description, not raw provider error messages or their
tracebacks. Startup and `entrabot doctor` stop on selected-agent configuration errors rather
than falling back to another agent's identity.

There is no generated `PrepareAlm` artifact. The reusable ALM evidence is the implementation,
regression tests, permission inventory, setup documentation, and successful telemetry
validation in the tenant. A team submitting its own agent still supplies its deployment
manifest, approved permissions, security-group/scoping decisions, and observed Defender or
other destination evidence as required by its ALM process.

### Additional agents and runtime settings

Normal setup now saves the shared configuration automatically; `entrabot migrate` is not a
required onboarding step. Existing installations that still keep shared settings only in the
setup clone remain supported by the observability fallback. Each additional agent keeps the
existing shared Blueprint but starts with observability and export disabled in its own
configuration. Run explicit authorization for that agent directory when required; one agent's
approval does not automatically approve every agent under the Blueprint. Migration/splitting
keeps enable/export flags per-agent.

Telemetry `service.name` comes from `.entrabot/harness.json`'s `name`. No deployment-specific
agent name is baked into the SDK setup. Optionally set `ENTRABOT_A365_SERVICE_NAMESPACE`;
blank/unset supplies no namespace. `ENTRABOT_A365_CONSOLE_ENABLED` controls console telemetry
and defaults to false. Observability/export remain opt-in outside authorized onboarding.

CLI invocations do not require `ENTRABOT_HUMAN_USER_ID`. When a caller ID or configured
human ID is available it is included; otherwise human caller details are omitted.
Tenant, Blueprint, Agent Identity, and Agent User attribution remain required.

Long-running harness sessions refresh their observability tokens before the cache's
expiry cutoff. Refresh failures use the existing observability warning path, and the
refresh task is cancelled and awaited during session disposal.

### Implementation boundaries

| Component | Responsibility |
| --- | --- |
| `setup.sh` / `setup-windows.ps1` | Platform prerequisites and orchestration. Bootstrap the venv once; keep optional observability separate from identity provisioning. |
| `harness/config/globalcfg.py` | Shared dotenv parsing, tenant/Blueprint validation, certificate selection, per-agent composition and shared persistence. Runtime and observability use the same composition rules. |
| `configure_a365_observability.py` | Select the existing agent, apply grants, acquire its token, then enable its export flags. |
| `ensure_a365_observability_permissions.py` | Idempotent Graph validation and grant operations. Work IQ's distinct permission policy remains separate. |
| `observability/runtime.py`, `context.py`, `tokens.py` | SDK initialization, invocation scopes and token-cache/refresh lifetime, respectively. |
| `upload_blueprint_cert.py` | Idempotent public certificate registration; preserve other registered keys. |

## `setup_delegated.sh`

Browser-sign-in setup for `delegated` mode. Caches an MSAL token in the OS keystore so the MCP server can pick it up silently — no device-code flow.

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
scripts\setup-windows.cmd
```

```powershell
.\scripts\setup-windows.ps1 -NewChain -UpnSuffix my-agent
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
