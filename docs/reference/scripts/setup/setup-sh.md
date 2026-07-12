# `scripts/setup.sh`

Platforms: macOS, Linux.

## Purpose

`setup.sh` is the first-time and additional-device provisioning entry point for
macOS and Linux. It is a thin orchestrator around the Python provisioning
scripts: it verifies prerequisites, bootstraps the dedicated Provisioner app,
creates or attaches the full Agent Identity chain (Blueprint → Agent Identity →
Agent User), generates and registers the Blueprint certificate, writes `.env`,
optionally provisions cloud-hosted operational memory, registers the `entrabot`
MCP server, and runs a post-setup smoke check. State is persisted to
`.entrabot-state.json`, so re-runs are idempotent.

## Requirements

- macOS or Linux with Bash.
- Azure CLI (`az`) on `PATH`, already signed in (`az login`). Without an active
  login the script fails at Step 2.
- Python 3.12 or newer (`python3.12`, `python3.13`, or a `python3` that reports
  ≥ 3.12).
- `git`.
- An OS credential store reachable by `keyring` (Keychain on macOS, Secret
  Service / Keyring on Linux). The certificate step fails closed if the active
  keyring backend is not OS-native, rather than writing the private key to disk.
- A Teams-capable M365 license for the Agent User is required for end-to-end
  Teams operation; license replication can take 10–15 minutes after assignment.

Install the macOS prerequisites with [`prereqs-macos.sh`](prereqs-macos-sh.md).
On Linux, install `python3.12`, `git`, and the Azure CLI with your package
manager. See also [macOS / Linux installation](../../../getting-started/macos-linux.md).

## Usage

```bash
# First-time provisioning — create a brand-new identity chain
./scripts/setup.sh --new --with-upn-suffix=workstation

# Create an explicit Agent User UPN
./scripts/setup.sh --new --agent-user-upn=entrabot-agent@yourtenant.onmicrosoft.com

# Attach this machine to an existing Blueprint (multi-device / re-install)
./scripts/setup.sh --use-blueprint=<BLUEPRINT_APP_ID> --agent-user-upn=<UPN>

# Opt in to cloud-hosted operational memory (Azure Blob Storage)
./scripts/setup.sh --new --with-upn-suffix=workstation --use-cloud-memory

# Skip the post-setup smoke test (useful in CI with no live tenant)
./scripts/setup.sh --new --with-upn-suffix=ci --skip-smoke

# Re-run diagnostics or the consolidated status view instead of provisioning
./scripts/setup.sh --diagnose
./scripts/setup.sh --status --json
```

An identity mode is required: pass exactly one of `--new` or
`--use-blueprint=<APP_ID>`. `--new` and `--use-blueprint` are mutually
exclusive.

## Options

Identity mode (choose one):

- `--new` — create a completely fresh chain. Existing state is backed up and the
  identity keys are cleared (the Provisioner app is preserved). Requires a UPN
  suffix (`--with-upn-suffix`) or an explicit `--agent-user-upn`; if neither is
  given, the script prompts for a suffix.
- `--use-blueprint=ID` — attach to an existing Blueprint by App ID. A new cert is
  generated for this machine and added to the Blueprint; the existing Agent
  Identity and Agent User are reused. If local state points at a *different*
  Blueprint, the stale identity-derived state is wiped (after a timestamped
  backup) so discovery re-runs cleanly against the new Blueprint. The old
  machine's cert on the previous Blueprint is **not** revoked automatically.

Agent User selection:

- `--with-upn-suffix=NAME` — UPN suffix for the Agent User (required with `--new`
  unless `--agent-user-upn` is given). With `--use-blueprint`, selects an
  existing suffixed Agent User.
- `--agent-user-upn=UPN` — explicit Agent User UPN. With `--new`, creates exactly
  that UPN; with `--use-blueprint`, selects an existing Agent User to reuse.
- `--switch-user` — run `az login` first so a different signed-in user owns and
  sponsors the agent.
- `--teams-user=EMAIL` — set a different user (or comma-separated users) as the
  Teams recipient(s); the `az` CLI user remains the admin/provisioner. Guests
  (B2B) are detected and resolved to their home tenant.

Operational memory:

- `--use-cloud-memory` — opt in to Azure Blob Storage for operational data
  (interaction log, daily summaries, promises, and per-chat delivery cursors).
  The watched-chat registry and the email-poll cursor stay in local files
  regardless of this flag. Provisions a resource group, storage account, and
  container scoped to the Agent User, and unsets `ENTRABOT_KEEP_MEMORY_LOCAL`.
- `--keep-memory-local` — the default. Operational data stays on the local
  filesystem. Kept as an explicit, backwards-compatible flag; sets
  `ENTRABOT_KEEP_MEMORY_LOCAL=true`.
- `--with-storage-account=NAME` — target a named storage account instead of the
  deterministic per-tenant default (used with `--use-cloud-memory`). Mutually
  exclusive with `--create-new-storage`.
- `--with-container=NAME` — target a named blob container instead of the
  `agent-<oid>` default.
- `--create-new-storage` — force a fresh randomly-suffixed storage account even
  when the deterministic-name one exists. Mutually exclusive with
  `--with-storage-account`.

Agent 365 Work IQ:

- `--with-a365-work-iq` — install or update the Microsoft Agent 365 DevTools CLI
  (`a365`) via `dotnet` global tools (requires a .NET SDK on `PATH`).
- `--configure-a365-work-iq` — run the interactive Work IQ Word developer setup
  against the existing Entrabot Blueprint, materialize the Work IQ OAuth grants,
  and validate the tooling manifest. Requires `pwsh`.
- `--a365-agent-name=NAME` — deprecated compatibility flag; Work IQ setup now
  reuses the existing Entrabot Blueprint from state.

Diagnostics and control:

- `--status` — skip setup and delegate to `status.sh`; extra arguments are
  forwarded to `show_agent_status.py` (for example `--json`, `--health-only`,
  `--strict`).
- `--diagnose` — skip provisioning and run a full health check (state file,
  certificate, three-hop token, Graph identity, Teams scope, MCP wiring). Exits
  non-zero if any check fails.
- `--skip-smoke` — skip the post-setup smoke test (token + Graph identity +
  Teams scope).
- `--help`, `-h` — print the full option matrix.

## Effects

- Verifies `az`, Python ≥ 3.12, and required CLI tools; verifies the Azure login.
- Bootstraps the Provisioner app via `entra_provisioning.py` and creates or
  reuses the Blueprint, Agent Identity, and Agent User via
  [`create_entra_agent_ids.py`](../provisioning/create-entra-agent-ids-py.md)
  (using the Provisioner certificate token, never `az` CLI tokens).
- Generates an RSA-2048 self-signed Blueprint certificate, stores the private
  key in the OS credential store, and uploads only the public certificate to the
  Blueprint via a Graph `PATCH keyCredentials` (a list-replace, which warns
  before overwriting existing certs). Reuses a cached or locally recoverable
  certificate when one is already registered.
- Creates `.venv`, installs the package with dev extras, and writes `.env`
  (`chmod 600`) with the resulting tenant, Blueprint, Agent Identity, Agent User,
  and human-user identifiers plus the certificate thumbprint.
- With `--use-cloud-memory` and an Agent User present, provisions Azure Blob
  Storage via [`provision_blob_storage.py`](../storage/provision-blob-storage-py.md)
  and appends `ENTRABOT_BLOB_ENDPOINT` / `ENTRABOT_BLOB_CONTAINER` to `.env`;
  offers an idempotent, source-preserving migration prompt that copies existing
  local data (interaction log, daily summaries, promises, per-chat delivery
  cursors) and Claude Code persona memory as a one-time artifact copy.
  Provisioning failures fall back to local-only memory. The migration copies
  the watched-chat registry and email-poll cursor as files too, but those two
  stay local-file-only afterward — `MemoryBackend` never becomes their runtime
  reader or writer.
- Registers the `entrabot` MCP server in the project-local `.mcp.json` (Claude
  Code) and `~/.copilot/mcp-config.json` (Copilot CLI) via `mcp_config.py`; both
  entries are byte-identical.
- Runs a post-setup smoke test (token + Graph identity + Teams scope) unless
  `--skip-smoke` is passed.
- Does **not** create or watch any Teams chat. There is no default chat; every
  Teams tool requires an explicit chat id supplied at runtime. See
  [Messaging and delivery](../../../architecture/messaging-and-delivery.md).

## Exit behavior

- `0` — setup (or `--status` / `--help`) completed successfully; the smoke test
  either passed or was skipped.
- `1` — a fatal setup error (for example a missing prerequisite, no Azure login,
  a failed provisioning step, an aborted cert replacement, `--new` /
  `--use-blueprint` conflict, or a missing required identity mode).
- `2` — mutually exclusive storage flags, or setup completed provisioning but the
  cloud-memory **migration failed** (cloud memory is not in sync with local
  disk; re-run after fixing the underlying consent error).
- `3` — provisioning finished but the post-setup **smoke test failed** (the agent
  can't yet authenticate end-to-end, most often because a just-assigned Teams
  license has not replicated). Re-run `./scripts/setup.sh --diagnose` after
  replication completes.
- `--diagnose` exits with the diagnostics' own overall exit code (`0` when all
  checks pass, non-zero otherwise).

## Common failures

- **`Not logged in to Azure CLI`** — run `az login` (or `./scripts/setup.sh
  --switch-user`) and retry.
- **`No identity mode specified`** — pass `--new --with-upn-suffix=NAME` or
  `--use-blueprint=APP_ID`.
- **Cert replacement prompt** — the Blueprint already has registered
  certificate(s); confirming replaces them (Graph `PATCH` list semantics), which
  stops other machines authenticating until they re-run setup.
- **Smoke test failed (exit 3)** — usually license replication latency; wait
  10–15 minutes and re-run with `--diagnose`.
- **Migration failed (exit 2)** — most often missing storage consent for the
  Agent Identity; re-run `setup.sh` (idempotent) or
  [`create_entra_agent_ids.py`](../provisioning/create-entra-agent-ids-py.md) to
  grant it, then retry.

## Related commands

- [Script reference — Setup](../index.md#setup)
- [`prereqs-macos.sh`](prereqs-macos-sh.md) — install the macOS prerequisites.
- [`setup_delegated.sh`](setup-delegated-sh.md) — delegated (MSAL) sign-in
  instead of the full Agent User chain.
- [`setup-windows.ps1`](setup-windows-ps1.md) — the Windows counterpart.
- [`create_entra_agent_ids.py`](../provisioning/create-entra-agent-ids-py.md) —
  the underlying identity-chain provisioner.
- [`provision_blob_storage.py`](../storage/provision-blob-storage-py.md) —
  cloud-memory provisioning invoked by `--use-cloud-memory`.
- [`status.sh`](../operations/status-sh.md) /
  [`show_agent_status.py`](../operations/show-agent-status-py.md) — status and
  health checks (reached via `--status`).
- [`teardown.sh`](../teardown/teardown-sh.md) — remove everything `setup.sh`
  creates.
- [Identity and token flow](../../../architecture/identity-and-token-flow.md).
