# Storage scripts

Scripts that provision, deprovision, and sync the Azure Blob Storage backend used for agent memory (ADR-005) and the host-portable state archive.

Cloud storage is opt-in. `LocalBackend` is the default; `BlobBackend` is enabled when `setup.sh --use-cloud-memory` is passed.

## `provision_blob_storage.py`

Provision Azure Blob Storage for agent memory. Idempotent — designed to be called from `setup.sh` on every run.

### Usage

```bash
python scripts/provision_blob_storage.py
python scripts/provision_blob_storage.py --with-storage-account <NAME>
python scripts/provision_blob_storage.py --with-container <NAME>
python scripts/provision_blob_storage.py --create-new-storage
```

### What it does

1. Ensures `entrabot-rg` resource group exists in the user's default subscription.
2. Ensures a Storage Account exists — one per tenant, named from the tenant ID so multiple devs in the same tenant converge on the same account without a global-unique-name race.
3. Ensures a container exists for this Agent User (named with the Agent User's object ID per ADR-005).
4. Assigns `Storage Blob Data Contributor` to the Agent User on the container — scoped to the container, not the account, so each Agent User only sees its own slice.

### Idempotency

Detects already-provisioned resources and reuses them. Only missing pieces get created. Prints two `KEY=value` lines on stdout so the calling shell can read them.

See ADR-005 for the full design.

## `deprovision_blob_storage.py`

Remove Azure Blob Storage resources provisioned by `provision_blob_storage.py`. Inverse with safe defaults.

### Usage

```bash
# Default: container only
python3 scripts/deprovision_blob_storage.py \
  --storage-account <NAME> --container <NAME>

# Also delete the account
python3 scripts/deprovision_blob_storage.py \
  --storage-account <NAME> --container <NAME> --delete-account

# Also delete the resource group (implies account)
python3 scripts/deprovision_blob_storage.py \
  --storage-account <NAME> --container <NAME> \
  --delete-account --delete-resource-group
```

### What it does

- Default: deletes the named container only.
- `--delete-account`: also deletes the storage account.
- `--delete-resource-group`: also deletes the resource group (implies account).

Requires `az login` with appropriate permissions.

## `claude_memory_sync.py`

Claude Code memory ⇄ blob storage sync.

!!! warning "Deprecated"
    Memory sync is now handled by the **persona-sati** MCP server. The `SessionStart` and `PostToolUse` hooks that called this script have been removed from `.claude/settings.json`. This script is kept as a **manual migration / one-off sync tool**.

### Usage

```bash
# Pull every claude_memory/ blob into the local memory directory
python3 scripts/claude_memory_sync.py pull

# Upload every local file not already in the cloud
python3 scripts/claude_memory_sync.py push

# Upload a single file
python3 scripts/claude_memory_sync.py push-one <PATH>
```

### What it does

- All three commands are idempotent and respect `ENTRABOT_PERSONA_SYNC` — when unset or not `on`, the script exits 0 without touching the backend.
- Every error path returns 0 and logs to stderr (the original SessionStart hook ran on every Claude session and could not block boot).

Use cases now: one-off bulk migrations after switching machines, or manual recovery when persona-sati's sync state is broken.

See ADR-005 Phase 6a and `docs/decisions/005-cloud-hosted-memory.md` for context.

## `export-state.sh`

Export everything needed to run the MCP server on a new machine without re-provisioning. Creates an encrypted archive.

### Usage

```bash
./scripts/export-state.sh                       # prompts for password
./scripts/export-state.sh --password <PASS>     # non-interactive
```

### What's in the archive

- `.env` (MCP server config).
- `.entrabot-state.json` (provisioning state).
- Persisted Teams chat IDs.
- Blueprint private key from the OS keystore.
- Claude Code memory files.

The archive is encrypted (openssl AES) so it can be safely committed to a private repo branch for transfer. Use for moving the agent between machines without burning the existing chain.

## `import-state.sh`

Import an archive produced by `export-state.sh`. Inverse.

### Usage

```bash
./scripts/import-state.sh                                  # prompts for password
./scripts/import-state.sh --password <PASS>
./scripts/import-state.sh --archive <PATH> --password <PASS>
```

### What it does

- Decrypts the archive into the project root.
- Imports the private key into the local OS keystore.
- Writes `.env`, `.entrabot-state.json`, and watched chat state in place.

After import:

```bash
python3.12 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
claude --dangerously-load-development-channels server:entrabot
```
