# `setup.sh` reference

The `./scripts/setup.sh` script provisions and configures an EntraClaw agent end to end. It's idempotent — re-run it after any failure.

## Usage

```bash
./scripts/setup.sh [OPTIONS]
```

## Options

### Identity chain

| Flag | Purpose |
|------|---------|
| *(none)* | Reuse existing Blueprint / Agent Identity / Agent User from `.entraclaw-state.json`. This is the common case on a machine that's already been set up. |
| `--new` | Provision a brand-new identity chain (Blueprint + Agent Identity + Agent User). Does not touch the existing chain; the current `.env` is backed up. Must be paired with `--with-upn-suffix` or you'll be prompted. |
| `--use-blueprint=<app-id>` | Attach to an existing Blueprint from a different machine. Generates a new cert locally and uploads its public key to the Blueprint. Reuses the existing Agent Identity and Agent User. Also handles the "switch this machine to a different Blueprint" case — stale Agent Identity / User / cert thumbprint are wiped from local state. |
| `--with-upn-suffix=<name>` | Required with `--new`; also supported with `--use-blueprint` to select an existing suffixed Agent User under the Blueprint. Example: `--with-upn-suffix=sati-agent` produces or selects `entraclaw-agent-sati-agent@yourdomain.com`. |
| `--agent-user-upn=<upn>` | Explicit Agent User UPN. With `--use-blueprint`, selects an existing Agent User to reuse, e.g. `entraclaw-agent-sati-agent@werner.ac`. With `--new`, creates exactly that UPN, e.g. `entraclaw-agent@werner.ac`. |

### User identity

| Flag | Purpose |
|------|---------|
| `--switch-user` | Sign in as a different Azure CLI user before setup. The new user becomes the agent's sponsor (Blueprint principal). |
| `--teams-user=<email[,email...]>` | Set a different user (or group of users) as the Teams chat recipient. The signed-in `az` user remains the admin/provisioner. Supports comma-separated list for group chats; cross-tenant guests are auto-detected and their home tenant is resolved via OpenID discovery. |

### Operational storage

| Flag | Purpose |
|------|---------|
| *(none)* | **Default: local filesystem.** Operational data stays at `~/.entraclaw/data`. No Azure storage is provisioned. |
| `--use-cloud-memory` | Opt in to Azure Blob Storage. Provisions resource group `entraclaw-rg`, a tenant-scoped storage account, a container scoped to this Agent User (`agent-<OID>`), and `Storage Blob Data Contributor` RBAC on the container. Sets the `ENTRACLAW_BLOB_*` env vars. Recommended for production-like setups and cross-device continuity. |
| `--keep-memory-local` | Backward-compat alias for the default behavior. Explicit opt-out from cloud storage. No-op unless you also pass `--use-cloud-memory` on the same line. |

### Misc

| Flag | Purpose |
|------|---------|
| `--with-a365-work-iq` | Install/update the Microsoft Agent 365 DevTools CLI prerequisite used for Work IQ setup. |
| `--configure-a365-work-iq` | Configure Work IQ Word for the existing Entraclaw Blueprint. Writes `a365.config.json`, adds `mcp_WordServer`, ensures the A365 resource service principals and OAuth grants exist, then runs `a365 setup permissions mcp`. |
| `--help`, `-h` | Show the built-in help. |

## Examples

### First-time setup on a fresh machine

```bash
./scripts/setup.sh --new --with-upn-suffix=my-agent
```

Creates a new identity chain, stores everything locally, no cloud storage.

To create a specific unsuffixed Agent User UPN:

```bash
./scripts/setup.sh --new --agent-user-upn=entraclaw-agent@werner.ac
```

### Fresh setup with cloud storage from the start

```bash
./scripts/setup.sh --new --with-upn-suffix=my-agent --use-cloud-memory
```

### Add this machine to an existing Blueprint

```bash
./scripts/setup.sh --use-blueprint=9bfb75b3-e65f-4e56-bdbe-3ed213135c3b
```

The Blueprint's Agent Identity and Agent User are reused; this machine gets its own cert.

If the Blueprint has a suffixed Agent User, pin that chain explicitly:

```bash
./scripts/setup.sh --use-blueprint=9bfb75b3-e65f-4e56-bdbe-3ed213135c3b \
  --agent-user-upn=entraclaw-agent-sati-agent@werner.ac
```

If the local OS keystore already has the matching Blueprint private key, setup recovers the registered cert thumbprint and does not prompt to rotate the Blueprint cert.

### Configure Work IQ Word for an existing Agent User

```bash
./scripts/setup.sh --use-blueprint=9bfb75b3-e65f-4e56-bdbe-3ed213135c3b \
  --agent-user-upn=entraclaw-agent-sati-agent@werner.ac \
  --with-a365-work-iq \
  --configure-a365-work-iq
```

This path self-heals the A365 tenant bootstrap case where Agent Tools or Work IQ Word MCP service principals are missing. Before `a365 setup permissions mcp`, setup calls `scripts/ensure_a365_work_iq_permissions.py` to create the first-party service principals and grant the Blueprint `McpServersMetadata.Read.All` and `Tools.ListInvoke.All`.

### Promote an existing local setup to cloud

```bash
./scripts/setup.sh --use-cloud-memory
```

Grants the missing `user_impersonation` consent on Azure Storage, provisions the resource group / account / container, and prompts to migrate `~/.entraclaw/data` into the blob (non-destructive). Idempotent — re-runnable.

### Start a group chat that includes an external guest

```bash
./scripts/setup.sh --teams-user=you@yourorg.com,partner@external.com
```

Auto-detects the external UPN, resolves their home tenant, and creates a federated group chat.

## Environment outcomes

After a successful run, `.env` will have the following entries (at minimum):

```
ENTRACLAW_TENANT_ID=...
ENTRACLAW_BLUEPRINT_APP_ID=...
ENTRACLAW_BLUEPRINT_OBJECT_ID=...
ENTRACLAW_BLUEPRINT_CERT_THUMBPRINT=...
ENTRACLAW_AGENT_ID=...
ENTRACLAW_AGENT_OBJECT_ID=...
ENTRACLAW_AGENT_USER_ID=...
ENTRACLAW_AGENT_USER_UPN=...
ENTRACLAW_HUMAN_USER_ID=...
ENTRACLAW_HUMAN_UPN=...
ENTRACLAW_PROVISIONER_APP_ID=...
ENTRACLAW_LOG_LEVEL=INFO
```

Without `--use-cloud-memory` you'll also see:

```
ENTRACLAW_KEEP_MEMORY_LOCAL=true
```

With `--use-cloud-memory`:

```
ENTRACLAW_KEEP_MEMORY_LOCAL=false
ENTRACLAW_BLOB_ENDPOINT=https://entclaw<hash>.blob.core.windows.net
ENTRACLAW_BLOB_CONTAINER=agent-<agent-user-oid>
```

Private keys are **never** written to `.env` — they live in the OS keystore (Keychain on macOS, TPM on Windows, Secret Service on Linux). Only the cert thumbprint is persisted as config.

## What it does not do

- Does not remove anything. Use `./scripts/teardown.sh` for that.
- Does not manage the `persona-sati` MCP server. That's a separate project.
- Does not modify your Azure subscription beyond what's listed above (tenant-scoped storage account, Agent User, Agent Identity, Blueprint, provisioner app, consent grants).

## Targeted teardown

`./scripts/teardown.sh` can deprovision an Agent User chain by UPN:

```bash
./scripts/teardown.sh --agent-user-upn=entraclaw-agent-sati-agent@werner.ac --dry-run
./scripts/teardown.sh --agent-user-upn=entraclaw-agent-sati-agent@werner.ac --yes
```

Targeted teardown removes assigned licenses first, then deletes the Agent User,
parent Agent Identity, and parent Blueprint. **It does not delete Azure Blob
Storage accounts or containers.** Storage deletion is intentionally separate and
manual until a dedicated storage teardown path exists.

## See also

- [`scripts/setup.sh`](../../scripts/setup.sh) — the script itself
- [`scripts/provision_blob_storage.py`](../../scripts/provision_blob_storage.py) — the Python callable that `--use-cloud-memory` invokes
- [`scripts/create_entra_agent_ids.py`](../../scripts/create_entra_agent_ids.py) — Agent Identity provisioning
- [`docs/guides/storage-configuration.md`](../guides/storage-configuration.md) — more on the storage choice
- [`docs/decisions/003-certificate-auth-over-client-secrets.md`](../decisions/003-certificate-auth-over-client-secrets.md) — why we use cert auth
- [`docs/decisions/005-cloud-hosted-memory.md`](../decisions/005-cloud-hosted-memory.md) — the ADR behind `--use-cloud-memory`
