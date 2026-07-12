# Script reference

This is the index of every supported, operator-facing command in EntraBot: the scripts you run directly from a terminal to set up, operate, provision, authenticate, store, diagnose, or tear down an Agent Identity. Each command links to its own reference page with full usage, effects, and exit-behavior detail.

This index intentionally excludes internal migration helpers (for example, one-time state-format converters) and one-off research or spike scripts used to validate an approach before it became a supported command. If a script isn't listed here, it isn't part of the supported operator surface.

## Operations

| Command | Platforms | Purpose |
| --- | --- | --- |
| [`status.sh`](operations/status-sh.md) | macOS/Linux | Shell entry point that bootstraps the local Python venv if needed and delegates to show_agent_status.py for the consolidated Agent Identity status check. |
| [`status-windows.ps1`](operations/status-windows-ps1.md) | Windows | Windows PowerShell status wrapper — ensures the venv and deps exist, loads .env, and delegates to show_agent_status.py (supports -Json, -HealthOnly, -Strict). |
| [`show_agent_status.py`](operations/show-agent-status-py.md) | macOS/Linux/Windows | Shows consolidated Agent Identity status and health from local state and live Graph queries (Blueprint, Agent Identity, Agent User, Sponsors, Permissions, Certificates, Licenses, Storage). Supports --json and --health-only. |
| [`health_check.py`](operations/health-check-py.md) | macOS/Linux/Windows | Compatibility wrapper that forwards to show_agent_status.py with --health-only for callers that still invoke the old health_check.py entry point. |
| [`catch_up.py`](operations/catch-up-py.md) | macOS/Linux/Windows | Pulls recent messages from all watched chats and the inbox, authenticated as the Agent User via the three-hop token, to see what arrived while the MCP server was not polling. |
| [`dm.py`](operations/dm-py.md) | macOS/Linux/Windows | Sends a one-off Teams message to a chat (by chat_id or a locally defined alias) using the Agent User's three-hop token, outside of an MCP session. |
| [`read_email.py`](operations/read-email-py.md) | macOS/Linux/Windows | Fetches and prints the Agent User's most recent mailbox messages matching a subject substring, for a one-off check outside the MCP session. |
| [`show_permissions.py`](operations/show-permissions-py.md) | macOS/Linux/Windows | Shows the delegated permission grants (oauth2PermissionGrants) scoped to the Agent Identity's service principal and Agent User. Supports --json. |

## Setup

| Command | Platforms | Purpose |
| --- | --- | --- |
| [`setup.sh`](setup/setup-sh.md) | macOS/Linux | First-time or additional-device provisioning entry point — orchestrates the Python provisioning scripts to create or attach an Agent Identity chain, writes .env, and runs a post-setup token/Graph/Teams-scope smoke check (unless --skip-smoke). Operational storage stays local by default; pass --use-cloud-memory to opt into Azure Blob. |
| [`setup-windows.ps1`](setup/setup-windows-ps1.md) | Windows | Windows PowerShell counterpart of setup.sh — refuses to run under WSL, probes prereqs, bootstraps the venv, provisions the Agent Identity chain, generates the Blueprint cert (TPM-first, software fallback), writes .env, and registers the MCP server. |
| [`setup-windows.cmd`](setup/setup-windows-cmd.md) | Windows | cmd.exe one-liner wrapper that invokes setup-windows.ps1 with the right execution policy so users don't have to pass -ExecutionPolicy themselves. |
| [`prereqs-macos.sh`](setup/prereqs-macos-sh.md) | macOS | Installs macOS prerequisites (Python 3.12+, git, Azure CLI, and optionally the .NET SDK + Agent 365 a365 CLI + PowerShell 7 via Homebrew) before running setup.sh. Xcode Command Line Tools are triggered separately via `xcode-select --install`, not Homebrew. Safe to re-run. |
| [`prereqs-windows.ps1`](setup/prereqs-windows-ps1.md) | Windows | Installs Windows prerequisites via winget (PowerShell 7+, Python 3.12+, git, Azure CLI, .NET SDK, and VS Build Tools C++ workload) before running setup-windows.ps1. The Agent 365 a365 CLI is installed with `dotnet tool install`, not winget. Runs from Windows PowerShell 5.1 and is safe to re-run. |
| [`setup_delegated.sh`](setup/setup-delegated-sh.md) | macOS/Linux | Signs in interactively with MSAL (browser localhost redirect) using ENTRABOT_CLIENT_ID and persists the MSAL cache through OS-encrypted persistence (Keychain on macOS / Secret Service on Linux when available) so the MCP server picks it up silently — for delegated mode instead of the full Agent User chain. |
| [`setup_ado_credentials.sh`](setup/setup-ado-credentials-sh.md) | macOS | Prompts for an Azure DevOps PAT and stores it via `git credential approve`, which hands it to whichever `credential.helper` is configured — the macOS Keychain only receives it if that helper is `osxkeychain` — so git push/pull to dev.azure.com authenticates without further prompts. |
| [`deploy-windows.ps1`](setup/deploy-windows-ps1.md) | Windows | Windows Blueprint certificate-rotation deploy wrapper around rotate_cert_windows.py — captures the current cert DER, generates a new cert, runs the transactional rotation, and deletes the old cert only after a smoke check passes. A -Status flag instead delegates to status-windows.ps1. |

## Teardown

| Command | Platforms | Purpose |
| --- | --- | --- |
| [`teardown.sh`](teardown/teardown-sh.md) | macOS/Linux | Removes everything setup.sh creates — Agent User, Agent Identity, Blueprint (and its BlueprintPrincipal), the Provisioner app, and local state — with --dry-run, targeted --agent-user-upn, and --preserve-provisioner / --preserve-local-state flags. |
| [`teardown-windows.ps1`](teardown/teardown-windows-ps1.md) | Windows | Local-only Windows teardown — removes the Blueprint cert(s) from the CurrentUser store, the entrabot data dir, the BLUEPRINT_CERT_* lines from .env, the MSAL cache, and the MCP registration. It does NOT delete the Entra app registrations; clean those up separately. |
| [`deprovision_entra_agent_identity.py`](teardown/deprovision-entra-agent-identity-py.md) | macOS/Linux/Windows | Targeted teardown of one or more Agent User chains, identified by repeated --agent-user-upn flags — removes licenses, then deletes the Agent User, its Agent Identity, and the parent Blueprint, but refuses to delete a Blueprint still shared by other Agent Identities. Leaves local state and blob storage untouched. |
| [`cleanup-orphans.sh`](teardown/cleanup-orphans-sh.md) | macOS/Linux | Deletes orphaned Blueprint / Agent Identity resources by object ID using a Provisioner cert-auth Graph token, for cleanup after a teardown that failed because az CLI tokens are rejected by the Agent Identity APIs. |

## Provisioning

| Command | Platforms | Purpose |
| --- | --- | --- |
| [`create_entra_agent_ids.py`](provisioning/create-entra-agent-ids-py.md) | macOS/Linux/Windows | Creates the Blueprint, BlueprintPrincipal, Agent Identity, and Agent User in Entra (via the Provisioner cert token, never az CLI tokens) and persists the resulting IDs to .entrabot-state.json; idempotent on re-run. |
| [`add_agent_sponsor.py`](provisioning/add-agent-sponsor-py.md) | macOS/Linux/Windows | Resolves an email/UPN to a user and adds them as a sponsor on the configured Agent Identity — the fix for SponsorGate rejecting inbound messages when the operator is not in the sponsor list. |
| [`remove_agent_sponsor.py`](provisioning/remove-agent-sponsor-py.md) | macOS/Linux/Windows | Inverse of add_agent_sponsor.py — resolves an email/UPN and removes that user from the Agent Identity's sponsor list, then prints the remaining sponsors. |
| [`assign_agent_user_licenses.py`](provisioning/assign-agent-user-licenses-py.md) | macOS/Linux/Windows | Standalone Agent User license management — auto-selects the first available Teams/Copilot SKU candidate in Graph's returned order, or assigns a specific --sku, and can --list-available SKUs. |
| [`remove_agent_user_licenses.py`](provisioning/remove-agent-user-licenses-py.md) | macOS/Linux/Windows | Removes directly-assigned licenses from the Agent User (--all or a specific --sku-id). --all excludes group-inherited licenses from the removal set, but an explicit --sku-id is sent to Graph even if that SKU is group-inherited. |
| [`ensure_a365_work_iq_permissions.py`](provisioning/ensure-a365-work-iq-permissions-py.md) | macOS/Linux/Windows | Materializes the Microsoft Agent 365 Work IQ MCP first-party resource service principals and Blueprint-wide OAuth grants using the Provisioner token, working around the A365 CLI's silent OAuth2-grants-failed exit. |

## Auth and certs

| Command | Platforms | Purpose |
| --- | --- | --- |
| [`grant_consent.py`](auth-and-certs/grant-consent-py.md) | macOS/Linux/Windows | Creates or updates the oauth2PermissionGrant that lets the Agent Identity acquire delegated tokens with the specified --scopes (against Graph or another --resource-app-id) as the Agent User. The generalised CLI form of the consent logic in create_entra_agent_ids.py. |
| [`grant_files_consent.py`](auth-and-certs/grant-files-consent-py.md) | macOS/Linux/Windows | Ensures the Agent User's oauth2PermissionGrant carries the full default Agent User Graph scope set (Chat, User, Files, Sites, Mail — not only Files/Sites) by calling grant_agent_user_consent from create_entra_agent_ids.py, idempotently adding only the missing scopes via POST (new grant) or PATCH (existing grant); use when a Files MCP tool call raises MissingPermissionError. |
| [`revoke_consent.py`](auth-and-certs/revoke-consent-py.md) | macOS/Linux/Windows | Revokes specific --scopes from, or --all of (deleting), the oauth2PermissionGrant that lets the Agent Identity act as the Agent User. Inverse of grant_consent.py. |
| [`provisioner-token.py`](auth-and-certs/provisioner-token-py.md) | macOS/Linux/Windows | Prints a Graph API access token minted with the Provisioner app's certificate (private key read in memory only — from the OS keystore on macOS/Linux, or from an ACL-locked file under %LOCALAPPDATA%\entrabot\ on Windows) for manual Graph calls during debugging. Token on stdout, errors on stderr. |
| [`find_local_blueprint_cert.py`](auth-and-certs/find-local-blueprint-cert-py.md) | macOS/Linux | Recovers the registered Blueprint cert thumbprint (SHA-256 b64url) matching the local OS-keystore private key for a given Blueprint object ID, so setup.sh can reuse an existing cert instead of prompting to rotate. |
| [`list_blueprint_certs.py`](auth-and-certs/list-blueprint-certs-py.md) | macOS/Linux | Prints the count of certificates registered on a Blueprint app (stdout) plus one human-readable detail line per cert (stderr); used by setup.sh to show what will be replaced before generating a new cert. |
| [`verify_blueprint_cert.py`](auth-and-certs/verify-blueprint-cert-py.md) | macOS/Linux | Checks whether an expected thumbprint is still present on a Blueprint app's keyCredentials, so setup.sh's cached-thumbprint fast path can detect a stale cache before it fails at Hop 1. Exit 0 present, 1 stale, 2 usage error. |
| [`generate_windows_cert.py`](auth-and-certs/generate-windows-cert-py.md) | Windows | Generates the Blueprint cert on Windows by wrapping New-SelfSignedCertificate with locked crypto parameters (TPM-first, software-KSP fallback). A bare invocation prints only the SHA-1 thumbprint and the KSP used; the SHA-256 b64url JWT thumbprint and the public DER for upload to the Blueprint are emitted only when `--export-der` is passed. |
| [`rotate_cert_windows.py`](auth-and-certs/rotate-cert-windows-py.md) | Windows | Transactional Windows Blueprint cert rotation extracted from deploy-windows.ps1 for testable rollback — PATCHes the new cert onto the Blueprint and, if the smoke check fails, restores the old DER, restores the previous .env thumbprints, and invalidates the MSAL cache. |

## Storage

| Command | Platforms | Purpose |
| --- | --- | --- |
| [`provision_blob_storage.py`](storage/provision-blob-storage-py.md) | macOS/Linux/Windows | Idempotently provisions the resource group, storage account (deterministically named per tenant), per-Agent-User container, and Storage Blob Data Contributor RBAC for cloud-hosted operational memory. Requires az login. |
| [`deprovision_blob_storage.py`](storage/deprovision-blob-storage-py.md) | macOS/Linux/Windows | Removes cloud-memory resources created by provision_blob_storage.py — by default only the container, with --delete-account and --delete-resource-group to escalate. Requires az login. |

## Diagnostics

| Command | Platforms | Purpose |
| --- | --- | --- |
| [`entrabot-mcp-debug.sh`](diagnostics/entrabot-mcp-debug-sh.md) | macOS/Linux | Debug wrapper around entrabot-mcp that tees the server's stderr to a log file (while still passing it through to the parent) for post-crash inspection; carries a self-reference marker so efferent-copy peer discovery skips it and avoids a duplicate-server spawn. |
| [`diagnose-chat.py`](diagnostics/diagnose-chat-py.md) | macOS/Linux/Windows | Tests Teams chat creation directly against the Graph API, bypassing the MCP server and logging every detail, to diagnose chat connectivity and permission problems. |
| [`diagnose_sponsor_emails.py`](diagnostics/diagnose-sponsor-emails-py.md) | macOS/Linux/Windows | Read-only diagnostic that probes why an Agent Identity's sponsor email fields come back null (SponsorGate allowlist gaps), running nine Graph projections and token checks across the FIC and Agent User tokens. |
| [`list_agent_identities.py`](diagnostics/list-agent-identities-py.md) | macOS/Linux/Windows | Lists the Agent Identity service principals belonging to a Blueprint (from local state or an explicit --blueprint-app-id) via the Graph beta API. |
| [`list_sponsors.py`](diagnostics/list-sponsors-py.md) | macOS/Linux/Windows | Lists the sponsors assigned to the configured (or --agent-object-id specified) Agent Identity via the Graph beta API. Supports --json. |

