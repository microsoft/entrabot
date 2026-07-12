# `provision_blob_storage.py`

## Purpose

Provisions the Azure resources behind the cloud-hosted `BlobBackend` used for EntraBot's *operational* memory — the interaction log, daily summaries, promises, and chat cursors, each of which is routed through `MemoryBackend` (`get_backend()`). It is an Azure resource-management script, not a data-migration tool: it creates infrastructure (resource group, storage account, container, RBAC) and never reads, writes, or copies the JSON/JSONL content that lives inside `~/.entrabot/data/` or an existing container.

The watched-chats registration (`data_dir/watched_chats`) and the email poll cursor (`data_dir/email_cursor.txt`) are always plain local files under `cfg.data_dir` and are never routed through `get_backend()`, so enabling `BlobBackend` does not move their ongoing reads/writes into Blob — see [Storage and Memory](../../../architecture/storage-and-memory.md#what-actually-routes-through-memorybackend).

Moving existing local data into the container this script creates is a separate step, handled by `setup.sh`'s post-provision migration prompt or by calling `entrabot.storage.migration.migrate_local_to_backend` directly. That migration walks the whole local data directory, so it can copy `watched_chats` and `email_cursor.txt` into Blob as migration artifacts alongside the backend-routed keys — but copying them there doesn't change which component is authoritative at runtime; the MCP server still reads and writes both files locally regardless of backend configuration. This script also has no relationship to Claude Code / persona memory — that sync is owned by the `persona-sati` MCP server and, for one-off manual use, `claude_memory_sync.py`.

## Requirements

- The Azure CLI (`az`) installed and on `PATH`.
- An active `az login` session (user or service principal) with rights in the target subscription to create resource groups and storage accounts and to write role assignments (`Microsoft.Authorization/roleAssignments/write` — typically `Owner` or `User Access Administrator` at the subscription or resource group scope). Signing in with `az login` using a plain Azure CLI token is fine here — this script only calls Azure Resource Manager, not the Agent Identity Graph APIs that reject `az` tokens.
- The Entra tenant ID and the Agent User's object ID. Both are available from `.entrabot-state.json` after provisioning, or via `show_agent_status.py --json`.
- No EntraBot Python environment or package install is required — the script only uses the standard library and shells out to `az`.

## Usage

```bash
# Deterministic per-tenant account, container named for the Agent User
python3 scripts/provision_blob_storage.py \
  --tenant-id 11111111-2222-3333-4444-555555555555 \
  --agent-user-object-id 66666666-7777-8888-9999-000000000000

# Attach to (or create) a specific, named storage account
python3 scripts/provision_blob_storage.py \
  --tenant-id 11111111-2222-3333-4444-555555555555 \
  --agent-user-object-id 66666666-7777-8888-9999-000000000000 \
  --with-storage-account entclawteam01

# Force a brand-new, randomly-suffixed account even if the
# deterministic-name account already exists
python3 scripts/provision_blob_storage.py \
  --tenant-id 11111111-2222-3333-4444-555555555555 \
  --agent-user-object-id 66666666-7777-8888-9999-000000000000 \
  --create-new-storage

# Custom container name and region
python3 scripts/provision_blob_storage.py \
  --tenant-id 11111111-2222-3333-4444-555555555555 \
  --agent-user-object-id 66666666-7777-8888-9999-000000000000 \
  --with-container team-shared-agent \
  --location westus2
```

| Option | Default | Notes |
|---|---|---|
| `--tenant-id` | *(required)* | Entra tenant GUID. |
| `--agent-user-object-id` | *(required)* | Agent User object GUID; also used to derive the default container name and as the RBAC assignee. |
| `--location` | `eastus` | Azure region used only when the resource group or storage account is created for the first time; ignored for resources that already exist. |
| `--with-storage-account NAME` | unset | Use (creating if needed) the named account instead of the deterministic per-tenant name. Mutually exclusive with `--create-new-storage`. |
| `--with-container NAME` | unset | Use (creating if needed) the named container instead of `agent-<agent-user-object-id>`. |
| `--create-new-storage` | `false` | Forces a fresh, randomly-suffixed account even when the deterministic-name one already exists. Mutually exclusive with `--with-storage-account`. |

In normal operation you won't invoke this directly — `./scripts/setup.sh --use-cloud-memory` calls it for you and then wires the printed values into `.env`. Run it by hand when provisioning storage ahead of setup, attaching a second device to an existing account, or scripting infrastructure outside of `setup.sh`.

## Effects

1. **Resource group** `entrabot-rg` is created in `--location` if it doesn't already exist (`az group show` short-circuits `az group create`).
2. **Storage account** name resolution, in priority order: `--with-storage-account` (validated: 3–24 lowercase letters/digits) → a random per-tenant name if `--create-new-storage` (`entclaw<8-hex tenant hash><8-hex random>`) → the deterministic per-tenant default `entclaw<16-hex sha256(tenant_id)>`, so every developer in the same tenant converges on the same account without a global-uniqueness race. Created with `Standard_LRS`, kind `StorageV2`, public blob access disabled, and a minimum TLS version of 1.2.
3. **Container** name resolution: `--with-container` (validated: 3–63 lowercase alphanumeric/dash, no leading/trailing/consecutive dashes) or the default `agent-<agent-user-object-id, lowercased>`.
4. **RBAC**: `Storage Blob Data Contributor` is assigned to the Agent User's object ID, scoped to *the container*, not the account or resource group — so multiple Agent Users sharing a per-tenant account cannot read each other's data.

Every step checks for an existing resource before creating one, so re-running with the same arguments is safe and only fills in whatever is missing. A role assignment that already exists is treated as success rather than an error.

On success, exactly two `KEY=value` lines are printed to stdout for shell capture:

```
BLOB_ENDPOINT=https://entclaw<hash>.blob.core.windows.net
BLOB_CONTAINER=agent-<agent-user-object-id>
```

All progress messages and a monthly cost estimate go to stderr, keeping stdout script-friendly.

This script does **not** write `.env`, does **not** grant the Agent User's `user_impersonation` consent on Azure Storage (that's `grant_agent_user_storage_consent`, called from `create_entra_agent_ids.py` during setup), and does **not** copy any existing local or blob data.

## Exit behavior

- **Exit 0** — provisioning completed; `BLOB_ENDPOINT` and `BLOB_CONTAINER` are printed to stdout.
- **Exit 1** — an `az` command failed (surfaced as `ERROR: az ... failed: <az stderr>`) or a name/argument was invalid (e.g. an out-of-spec `--with-storage-account`/`--with-container` name).
- **Exit 2** — argparse usage error: a required argument (`--tenant-id`, `--agent-user-object-id`) is missing, or `--with-storage-account` and `--create-new-storage` were both passed on the command line.

Common failure causes and recovery:

- **Not signed in** — `az` calls fail with an authentication error. Run `az login` (and `az account set --subscription <id>` if you have more than one) and re-run; the script is idempotent and resumes from whatever hasn't been created yet.
- **Insufficient RBAC to assign roles** — the account/container are created but `az role assignment create` fails. You (or whoever runs this) needs `Microsoft.Authorization/roleAssignments/write` at the resource group or subscription scope; ask a subscription owner to grant it, then re-run.
- **`--with-storage-account` name already taken by another subscription** — storage account names are globally unique; pick a different name or omit the flag to use the deterministic per-tenant default.
- **RBAC propagation delay** — a freshly assigned role can take a few minutes to become effective for Graph/data-plane calls; this script itself will report success once the assignment call succeeds, but callers reading/writing blobs immediately afterward may need to retry.

## Related commands

- [`deprovision_blob_storage.py`](deprovision-blob-storage-py.md) — inverse: removes the resources this script creates.
- [`setup.sh`](../setup/setup-sh.md) — the usual caller, via `--use-cloud-memory`; also prompts to migrate existing local data into the new container afterward.
- [`create_entra_agent_ids.py`](../provisioning/create-entra-agent-ids-py.md) — grants the Agent User's `user_impersonation` consent on Azure Storage that the RBAC assignment here depends on at request time.

See also [Storage scripts](../index.md#storage), the [Storage Configuration guide](../../../guides/storage-configuration.md) (including its [Troubleshooting](../../../guides/storage-configuration.md#troubleshooting) section), and the [Storage and Memory](../../../architecture/storage-and-memory.md) architecture page.
