# `scripts/setup-windows.ps1`

Platforms: Windows.

## Purpose

`setup-windows.ps1` is the native-Windows counterpart of
[`setup.sh`](setup-sh.md). It provisions the Agent Identity chain on a Windows
host: it refuses to run under WSL, probes prerequisites, bootstraps the venv,
runs the legacy data-directory migration, provisions the Blueprint / Agent
Identity / Agent User, generates the Blueprint certificate with a TPM-backed CNG
key (software fallback), writes `.env` with both certificate thumbprints, and
registers the `entrabot` MCP server. It writes state to `.entrabot-state.json`
and is safe to re-run.

## Requirements

- **Native Windows** — the script refuses to run under WSL (`$IsLinux` or
  `WSL_DISTRO_NAME`) and refuses non-Windows hosts.
- **PowerShell 7+ (`pwsh`, the black icon), not Windows PowerShell 5.1** — it
  exits early with an actionable message if launched from 5.1. Use
  [`setup-windows.cmd`](setup-windows-cmd.md) to launch it with the right
  execution policy.
- `python` 3.12+, `az` CLI (signed in via `az login`), `git`, `pwsh`, and `a365`
  must all be on `PATH`. A missing tool aborts at the prerequisite probe.
- A C/C++ toolchain for native Python packages and (for Work IQ) a .NET SDK.

Install everything first with
[`prereqs-windows.ps1`](prereqs-windows-ps1.md). See also
[Windows installation](../../../getting-started/windows.md).

## Usage

```powershell
# First-time provisioning — new chain
.\scripts\setup-windows.ps1 -NewChain -UpnSuffix winagent

# Attach this machine to an existing Blueprint
.\scripts\setup-windows.ps1 -UseBlueprint <BLUEPRINT_APP_ID> -AgentUserUpn <UPN>

# New chain with cloud-hosted operational memory
.\scripts\setup-windows.ps1 -NewChain -UpnSuffix winagent -UseCloudMemory `
    -WithStorageAccount mycorpstg -WithContainer winagent-mem

# Status / health instead of setup
.\scripts\setup-windows.ps1 -Status -Json
```

## Options

- `-NewChain` — create a completely new Agent Identity chain.
- `-UseBlueprint <APP_ID>` — attach to an existing Blueprint by App ID.
- `-UpnSuffix <NAME>` — Agent User UPN suffix (required with `-NewChain`; also
  selects an existing suffixed Agent User with `-UseBlueprint`).
- `-AgentUserUpn <UPN>` — explicit existing Agent User UPN to reuse with
  `-UseBlueprint`.
- `-UseCloudMemory` — provision Azure Blob Storage for operational data (local
  is the default).
- `-WithStorageAccount <NAME>` — target a named storage account; created if
  missing. Mutually exclusive with `-CreateNewStorage`. Only meaningful with
  `-UseCloudMemory`.
- `-WithContainer <NAME>` — target a named blob container instead of the
  `agent-<oid>` default.
- `-CreateNewStorage` — force a fresh randomly-suffixed storage account.
  Mutually exclusive with `-WithStorageAccount`.
- `-ConfigureA365WorkIq` — run the interactive Work IQ Word developer setup
  against the existing Entrabot Blueprint and validate the tooling manifest.
- `-A365AgentName <NAME>` — deprecated compatibility parameter; Work IQ setup
  reuses the Blueprint from state.
- `-Migrate` — accepted for compatibility but currently unused: the script
  never reads its value. The one-shot legacy `~/.entrabot` data-directory
  migration always runs unconditionally on every invocation, whether or not
  this switch is supplied.
- `-Status` — skip setup and delegate to `status-windows.ps1`. Combine with
  `-Json`, `-HealthOnly`, `-Strict`, which are forwarded to the status command.
- `-Help` — show detailed help via `Get-Help`.

## Effects

- Probes for `python`, `az`, `git`, `pwsh`, and `a365`, and verifies Python
  ≥ 3.12.
- Runs the one-shot, idempotent legacy `~/.entrabot` data-directory migration.
- Creates `.venv` and installs the package with dev extras.
- Verifies the Azure login, then provisions the identity chain via
  `entra_provisioning.py` and
  [`create_entra_agent_ids.py`](../provisioning/create-entra-agent-ids-py.md).
- Generates the Blueprint certificate via
  [`generate_windows_cert.py`](../auth-and-certs/generate-windows-cert-py.md):
  it probes the TPM and uses the *Microsoft Platform Crypto Provider* (a
  non-exportable CNG/TPM key) when the TPM is ready, otherwise the *Microsoft
  Software Key Storage Provider*. The chosen key-storage provider is recorded as
  `ksp`.
- Writes `.env` (appended) with `ENTRABOT_TENANT_ID`,
  `ENTRABOT_BLUEPRINT_CERT_THUMBPRINT` (the **SHA-256 base64url `x5t#S256`** JWT
  thumbprint used for the client assertion), `ENTRABOT_BLUEPRINT_CERT_SHA1` (the
  **SHA-1 hex** thumbprint that locates the cert in `Cert:\CurrentUser\My`), and
  `ENTRABOT_BLUEPRINT_KSP`. The file is locked with `icacls ... :M` (modify, not
  read-only) so re-runs and rotation can still update it.
- With `-UseCloudMemory` and an Agent User present, provisions Azure Blob Storage
  via [`provision_blob_storage.py`](../storage/provision-blob-storage-py.md) and
  appends the blob endpoint/container to `.env`; falls back to local-only memory
  on failure. Without `-UseCloudMemory`, writes `ENTRABOT_KEEP_MEMORY_LOCAL=true`.
- Registers the `entrabot` MCP server for Claude Code and Copilot CLI via
  `mcp_config.py`.
- Does **not** run a smoke test and does **not** create or watch any Teams chat.

## Exit behavior

- `0` — setup completed, or `-Help` / `-Status` (with a healthy status result)
  finished.
- `1` — launched from Windows PowerShell 5.1, invoked under WSL or a non-Windows
  host, or any `Fail` step (missing tools, no `az` login, a failed provisioning /
  cert / `mcp_config` step). `Set-StrictMode`/`$ErrorActionPreference = 'Stop'`
  turn unexpected errors into non-zero terminating failures.
- `2` — `-CreateNewStorage` and `-WithStorageAccount` were both supplied.
- `-Status` — exits with the underlying `status-windows.ps1` exit code.

## Common failures

- **`This script needs PowerShell 7+`** — you launched it from Windows
  PowerShell 5.1 (blue icon). Open `pwsh` (black icon) or use
  [`setup-windows.cmd`](setup-windows-cmd.md).
- **`invoked from inside WSL`** — run [`setup.sh`](setup-sh.md) in WSL, or run
  `setup-windows.cmd` from a native Windows terminal.
- **`Missing tools`** — run [`prereqs-windows.ps1`](prereqs-windows-ps1.md), open
  a new terminal, and retry.
- **`Not logged in to az`** — run `az login` and retry.

## Related commands

- [Script reference — Setup](../index.md#setup)
- [`setup-windows.cmd`](setup-windows-cmd.md) — the `cmd.exe` launcher wrapper.
- [`prereqs-windows.ps1`](prereqs-windows-ps1.md) — install the Windows
  prerequisites.
- [`generate_windows_cert.py`](../auth-and-certs/generate-windows-cert-py.md) —
  the TPM-first certificate generator used here.
- [`deploy-windows.ps1`](deploy-windows-ps1.md) — certificate rotation on
  Windows.
- [`create_entra_agent_ids.py`](../provisioning/create-entra-agent-ids-py.md) —
  the underlying identity-chain provisioner.
- [`status-windows.ps1`](../operations/status-windows-ps1.md) — status/health
  (reached via `-Status`).
- [`teardown-windows.ps1`](../teardown/teardown-windows-ps1.md) — local Windows
  teardown.
- [Windows platform notes](../../../platform-docs/platform-windows.md).
