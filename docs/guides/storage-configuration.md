# Storage configuration

EntraBot writes selected *operational* data to a pluggable backend. Interaction logs, daily summaries, promises, and per-chat delivery cursors use `MemoryBackend`. The watched-chat registry and email cursor always remain local under `ENTRABOT_DATA_DIR`.

## TL;DR

- **Default: local filesystem** (`~/.entrabot/data/`). Zero infra. Fine for single-machine research, offline demos, air-gapped dev loops.
- **Recommended: Azure Blob Storage.** Opt in via `./scripts/setup.sh --use-cloud-memory`. Durable, cross-device, RBAC-scoped per Agent User.
- Memory sync for *persona* (Claude Code memory, callbacks, relational context) is handled by the separate `persona-sati` MCP server, not by this project. EntraBot's blob holds only operational data.

## The two backends

Both implement the `MemoryBackend` protocol in [`src/entrabot/storage/backend.py`](https://github.com/microsoft/entrabot/blob/main/src/entrabot/storage/backend.py):

```
MemoryBackend
├── LocalBackend   — filesystem at ~/.entrabot/data/
└── BlobBackend    — Azure Blob Storage container
```

`get_backend()` resolves which one to use on every call:

```python
if cfg.keep_memory_local:                      # explicit opt-out
    return LocalBackend(cfg.data_dir)
if cfg.blob_endpoint and cfg.blob_container:   # fully configured cloud
    return BlobBackend(BlobStore(...))
if cfg.blob_endpoint or cfg.blob_container:    # exactly one is configured
    raise BackendMisconfiguredError(...)
return LocalBackend(cfg.data_dir)              # neither is configured
```

A half-configured cloud environment is a hard failure. Exactly one of `ENTRABOT_BLOB_ENDPOINT` or `ENTRABOT_BLOB_CONTAINER` raises `BackendMisconfiguredError` instead of silently selecting an empty local store. Set both variables for Blob, neither for the default local backend, or set `ENTRABOT_KEEP_MEMORY_LOCAL=true` to force local storage.

## What gets stored

| State | Storage location |
|------|-----------|
| `interactions/<YYYY-MM-DD>.jsonl` | selected `MemoryBackend` |
| Daily-summary archives | selected `MemoryBackend` |
| `promises.jsonl` | selected `MemoryBackend` |
| `chat_cursors/<encoded-chat-id>.json` | selected `MemoryBackend` |
| `watched_chats` | always `ENTRABOT_DATA_DIR/watched_chats` |
| `email_cursor.txt` | always `ENTRABOT_DATA_DIR/email_cursor.txt` |

The local-to-Blob migration walks the data directory, so it may copy `watched_chats` and `email_cursor.txt` as migration artifacts. Normal runtime reads and writes for those two files remain local. No persona memory, behavioral rules, or secrets are stored through this operational backend.

## Choosing local

Fine for:

- Single-machine research where the dev laptop is the only host
- Offline / air-gapped environments
- Evaluating EntraBot before committing to Azure infra
- Demos where you want everything to reset with a `rm -rf ~/.entrabot/data`

Drawbacks:

- Lost on machine change; no cross-device continuity
- No remote backup
- Two MCP instances on different machines write to different local stores — there's no canonical truth

## Choosing cloud (recommended)

Fine for:

- Production-like setups where the Agent User outlives any one machine
- Teams of developers who share an Agent User (each machine has a cert; the blob is shared)
- Regular daily-summary generation where you want history to survive restarts
- Any scenario where `interactions/*.jsonl` is an audit artifact

### What gets provisioned

`setup.sh --use-cloud-memory` calls `scripts/provision_blob_storage.py`, which:

1. Ensures resource group `entrabot-rg` exists (or reuses it)
2. Ensures a storage account named `entclaw<tenant-hash>` exists (one per tenant — multiple devs in the same tenant converge on the same account)
3. Ensures container `agent-<agent-user-oid>` exists (one per Agent User — multiple Agent Users in the same account stay cleanly isolated)
4. Assigns `Storage Blob Data Contributor` on *the container* (not the account) to the Agent User

Container-scoped RBAC means different Agent Users in the same tenant can't read each other's operational data even though they share a storage account.

### What goes in `.env`

```
ENTRABOT_KEEP_MEMORY_LOCAL=false
ENTRABOT_BLOB_ENDPOINT=https://entclaw<hash>.blob.core.windows.net
ENTRABOT_BLOB_CONTAINER=agent-<agent-user-oid>
```

`setup.sh --use-cloud-memory` writes these for you. The backend reads them via `get_config()` on every call, so flipping between local and cloud is just an `.env` edit and an MCP server restart.

### The storage-scope token

The `BlobBackend` authenticates to Azure Blob via an Agent-User-scoped OAuth token for `https://storage.azure.com/.default`. The three-hop flow is parallel to the Graph-scoped flow used by the Teams tools, minted on demand via `acquire_agent_user_storage_token(config)`. Nothing about this is delegated back to you or to `az login`; the Agent User is the data plane principal.

If the setup wasn't run with `--use-cloud-memory` and you want to flip later, you'll need to re-run:

```bash
./scripts/setup.sh --use-blueprint=<blueprint-app-id> --use-cloud-memory
```

This grants the missing `user_impersonation` on Azure Storage, provisions the resources, and updates `.env`. It's idempotent.

## Migrating from local to cloud

If you've been running local and want to move your history to the cloud:

```bash
./scripts/setup.sh --use-blueprint=<blueprint-app-id> --use-cloud-memory
```

Near the end, the script will prompt you to migrate `~/.entrabot/data` into the blob container. The migration:

- Is **non-destructive** — nothing is deleted from local. You end up with two copies.
- Is **idempotent** — running twice skips keys already present in the blob.
- Treats cloud as authoritative on rerun — an existing target key is not overwritten.
- Collects per-file errors while continuing other files.

When selected migration errors occur inside `setup.sh`, setup reports `Setup INCOMPLETE` and exits with code 2. Fix the reported problem and re-run the same setup command; successful copies remain in Blob and local sources remain untouched.

To migrate manually (outside of `setup.sh`):

```bash
.venv/bin/python -c "
import asyncio
from pathlib import Path
from entrabot.storage.backend import get_backend
from entrabot.storage.migration import migrate_local_to_backend

async def main():
    backend = get_backend()
    report = migrate_local_to_backend(
        [(Path.home() / '.entrabot' / 'data', '')],
        backend,
    )
    print(f'copied={report.copied} skipped={report.skipped} errors={len(report.errors)}')

asyncio.run(main())
"
```

## Troubleshooting

See [Storage troubleshooting](../troubleshooting/storage.md) for backend selection, half-configured environments, storage-scope 401s, RBAC/consent 403s, migration recovery, and ETag cursor behavior.

## See also

- [Storage and Memory](../architecture/storage-and-memory.md) — the architecture behind this design
- [`src/entrabot/storage/backend.py`](https://github.com/microsoft/entrabot/blob/main/src/entrabot/storage/backend.py) — the backend protocol + factory
- [`src/entrabot/storage/blob.py`](https://github.com/microsoft/entrabot/blob/main/src/entrabot/storage/blob.py) — the async BlobStore client
- [`src/entrabot/storage/migration.py`](https://github.com/microsoft/entrabot/blob/main/src/entrabot/storage/migration.py) — the migrator used by setup.sh and callable by hand
- [`scripts/provision_blob_storage.py`](https://github.com/microsoft/entrabot/blob/main/scripts/provision_blob_storage.py) — the idempotent Azure provisioning routine
