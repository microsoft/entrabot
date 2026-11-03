# `scripts/setup-windows.ps1`

Platforms: Windows.

## Purpose

`setup-windows.ps1` is the native-Windows counterpart of
[`setup.sh`](setup-sh.md). It provisions the Agent Identity chain on a Windows
host: it refuses to run under WSL, probes prerequisites, bootstraps the venv,
runs the legacy data-directory migration, provisions the Blueprint / Agent
Identity / Agent User, makes sure this machine's Blueprint certificate exists and
is registered on the Blueprint (generating one with a TPM-backed CNG key, software
fallback, when it has none), writes `.env` with both certificate thumbprints, and
registers the `entrabot` MCP server. It writes state to `.entrabot-state.json`
and is safe to re-run: re-running it also repairs a machine whose certificate was
never registered.

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
- Stops before touching any certificate if `BLUEPRINT_OBJECT_ID` is missing from
  `.entrabot-state.json`.
- Makes sure this machine's certificate is on the Blueprint. If `.env` names a
  certificate (`ENTRABOT_BLUEPRINT_CERT_SHA1` and `ENTRABOT_BLUEPRINT_CERT_THUMBPRINT`)
  that is in `Cert:\CurrentUser\My`, in date, and has its private key, it asks
  [`verify_blueprint_cert.py`](../auth-and-certs/verify-blueprint-cert-py.md):
    - **Registered** — the certificate is reused. Nothing is generated.
    - **Not registered** — the state of a machine set up before this step existed.
      That same certificate is registered, and nothing is generated.
    - **Couldn't check** — setup stops. A failed check says nothing about the
      Blueprint, so no certificate is generated.
- With no usable certificate, it generates one via
  [`generate_windows_cert.py`](../auth-and-certs/generate-windows-cert-py.md): it
  probes the TPM and uses the *Microsoft Platform Crypto Provider* (a
  non-exportable CNG/TPM key) when the TPM is ready, otherwise the *Microsoft
  Software Key Storage Provider*. The chosen key-storage provider is recorded as
  `ksp`.
- Registers the certificate with
  [`register_windows_blueprint_cert.py`](../auth-and-certs/register-windows-blueprint-cert-py.md),
  which adds it to the Blueprint and keeps every other machine's certificate. If
  nothing could be written, a certificate generated in this run is removed from the
  store again. If the write may have landed but couldn't be confirmed, the
  certificate is kept and recorded in `.env`, and setup stops so the next run
  checks the Blueprint again. The temporary `.cer` file is always deleted.
- Writes `.env` on every run, since the runtime reads identity only from the
  environment and `.env`, never from the state file: `ENTRABOT_TENANT_ID`, the
  Blueprint, Agent Identity and Agent User IDs and UPN from the state file,
  `ENTRABOT_PROVISIONER_APP_ID`, `ENTRABOT_BLUEPRINT_CERT_THUMBPRINT` (the **SHA-256
  base64url `x5t#S256`** JWT thumbprint used for the client assertion),
  `ENTRABOT_BLUEPRINT_CERT_SHA1` (the **SHA-1 hex** thumbprint that locates the cert
  in `Cert:\CurrentUser\My`), and `ENTRABOT_BLUEPRINT_KSP`. Earlier lines for these
  keys are replaced rather than appended to, because `.env` loading keeps the first
  value of a repeated key. The sponsor (`ENTRABOT_HUMAN_*`) defaults to the signed-in
  account only when `.env` doesn't already name one through its singular or plural ID or
  UPN keys, so a re-run under another account doesn't change it. A run for a different
  Agent User keeps neither the sponsor nor the blob settings. The file is locked with `icacls ... :M` (modify, not read-only)
  so re-runs and rotation can still update it.
- With `-UseCloudMemory` and an Agent User present, provisions Azure Blob Storage
  via [`provision_blob_storage.py`](../storage/provision-blob-storage-py.md) and
  writes the blob endpoint/container to `.env`, replacing any earlier memory lines.
  If that can't be done, a machine already on cloud memory stays there and any other
  falls back to local-only memory.
- Without `-UseCloudMemory`, a machine already on cloud memory stays there, so
  re-running setup to repair a certificate can't move it to local memory. Any other
  machine gets `ENTRABOT_KEEP_MEMORY_LOCAL=true`, which is the default.
- Registers the `entrabot` MCP server for Claude Code and Copilot CLI via
  `mcp_config.py`.
- Does **not** run a smoke test and does **not** create or watch any Teams chat.

## Exit behavior

- `0` — setup completed, or `-Help` / `-Status` (with a healthy status result)
  finished.
- `1` — launched from Windows PowerShell 5.1, invoked under WSL or a non-Windows
  host, or any `Fail` step (missing tools, no `az` login, a failed provisioning /
  cert / `mcp_config` step, a certificate that couldn't be checked or registered
  on the Blueprint). `Set-StrictMode`/`$ErrorActionPreference = 'Stop'`
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
- **`Could not check whether the certificate is registered`** — the Graph or token
  failure is printed above it. Nothing was generated or changed; fix the cause and
  re-run.
- **`The certificate was not registered on the Blueprint`** — Graph refused the
  update or the Blueprint couldn't be read, and nothing was changed. Fix the cause
  (often Provisioner permissions) and re-run.
- **`Could not confirm the certificate is on the Blueprint`** — the write may have
  landed. Re-run setup: it checks the Blueprint for the certificate recorded in `.env`
  and registers it only if it is missing.

## Related commands

- [Script reference — Setup](../index.md#setup)
- [`setup-windows.cmd`](setup-windows-cmd.md) — the `cmd.exe` launcher wrapper.
- [`prereqs-windows.ps1`](prereqs-windows-ps1.md) — install the Windows
  prerequisites.
- [`generate_windows_cert.py`](../auth-and-certs/generate-windows-cert-py.md) —
  the TPM-first certificate generator used here.
- [`register_windows_blueprint_cert.py`](../auth-and-certs/register-windows-blueprint-cert-py.md) —
  adds the certificate to the Blueprint.
- [`verify_blueprint_cert.py`](../auth-and-certs/verify-blueprint-cert-py.md) —
  checks whether the certificate in `.env` is already registered.
- [`deploy-windows.ps1`](deploy-windows-ps1.md) — certificate rotation on
  Windows. It can't repair a certificate that was never registered; re-run
  `setup-windows.ps1` for that.
- [`create_entra_agent_ids.py`](../provisioning/create-entra-agent-ids-py.md) —
  the underlying identity-chain provisioner.
- [`status-windows.ps1`](../operations/status-windows-ps1.md) — status/health
  (reached via `-Status`).
- [`teardown-windows.ps1`](../teardown/teardown-windows-ps1.md) — local Windows
  teardown.
- [Windows platform notes](../../../platform-docs/platform-windows.md).
