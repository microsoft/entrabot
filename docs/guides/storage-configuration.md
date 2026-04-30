# Storage configuration

EntraClaw writes *operational* data — interactions log, watched chats, email cursor — to a pluggable backend. This guide explains the two backends, how to choose between them, and how to migrate.

## TL;DR

- **Default: local filesystem** (`~/.entraclaw/data/`). Zero infra. Fine for single-machine research, offline demos, air-gapped dev loops.
- **Recommended: Azure Blob Storage.** Opt in via `./scripts/setup.sh --use-cloud-memory`. Durable, cross-device, RBAC-scoped per Agent User.
- Memory sync for *persona* (Claude Code memory, callbacks, relational context) is handled by the separate `persona-sati` MCP server, not by this project. EntraClaw's blob holds only operational data.

## The two backends

Both implement the `MemoryBackend` protocol in [`src/entraclaw/storage/backend.py`](../../src/entraclaw/storage/backend.py):

```
MemoryBackend
├── LocalBackend   — filesystem at ~/.entraclaw/data/
└── BlobBackend    — Azure Blob Storage container
```

`get_backend()` resolves which one to use on every call:

```python
if cfg.keep_memory_local:                      # explicit opt-out
    return LocalBackend(cfg.data_dir)
if cfg.blob_endpoint and cfg.blob_container:   # fully configured cloud
    return BlobBackend(BlobStore(...))
return LocalBackend(cfg.data_dir)              # safe fallback
```

A half-configured cloud (endpoint without container, or vice versa) falls through to local rather than raising — the hot path can't afford a startup failure over a missing env var.

## What gets stored

| Key | Written by | Read by |
|------|-----------|---------|
| `interactions/<YYYY-MM-DD>.jsonl` | every inbound/outbound Teams or email tool call | daily summary generator |
| `watched_chats` | `create_chat`, auto-discovery poll | MCP boot (rehydrates chat list) |
| `email_cursor.txt` | email poll | email poll (resumes after restart) |
| `chat_id` *(legacy)* | older code, pre-PR #11 | nothing active; safe to delete |

No persona memory, no behavioral rules, no secrets. Tokens live in the OS keystore; the blob container holds plain operational JSON/JSONL.

## Choosing local

Fine for:

- Single-machine research where the dev laptop is the only host
- Offline / air-gapped environments
- Evaluating EntraClaw before committing to Azure infra
- Demos where you want everything to reset with a `rm -rf ~/.entraclaw/data`

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

1. Ensures resource group `entraclaw-rg` exists (or reuses it)
2. Ensures a storage account named `entclaw<tenant-hash>` exists (one per tenant — multiple devs in the same tenant converge on the same account)
3. Ensures container `agent-<agent-user-oid>` exists (one per Agent User — multiple Agent Users in the same account stay cleanly isolated)
4. Assigns `Storage Blob Data Contributor` on *the container* (not the account) to the Agent User

Container-scoped RBAC means different Agent Users in the same tenant can't read each other's operational data even though they share a storage account.

### What goes in `.env`

```
ENTRACLAW_KEEP_MEMORY_LOCAL=false
ENTRACLAW_BLOB_ENDPOINT=https://entclaw<hash>.blob.core.windows.net
ENTRACLAW_BLOB_CONTAINER=agent-<agent-user-oid>
```

`setup.sh --use-cloud-memory` writes these for you. The backend reads them via `get_config()` on every call, so flipping between local and cloud is just an `.env` edit and an MCP server restart.

### The storage-scope token

The `BlobBackend` authenticates to Azure Blob via an Agent-User-scoped OAuth token for `https://storage.azure.com/.default`. The three-hop flow is parallel to the Graph-scoped flow used by the Teams tools, minted on demand via `acquire_agent_user_storage_token(config)`. Nothing about this is delegated back to you or to `az login`; the Agent User is the data plane principal.

If the setup wasn't run with `--use-cloud-memory` and you want to flip later, you'll need to re-run:

```bash
./scripts/setup.sh --use-cloud-memory
```

This grants the missing `user_impersonation` on Azure Storage, provisions the resources, and updates `.env`. It's idempotent.

## Migrating from local to cloud

If you've been running local and want to move your history to the cloud:

```bash
./scripts/setup.sh --use-cloud-memory
```

Near the end, the script will prompt you to migrate `~/.entraclaw/data` into the blob container. The migration:

- Is **non-destructive** — nothing is deleted from local. You end up with two copies.
- Is **idempotent** — running twice skips keys already present in the blob.
- Logs any per-file errors to the console rather than aborting the whole run.

To migrate manually (outside of `setup.sh`):

```bash
.venv/bin/python -c "
import asyncio
from pathlib import Path
from entraclaw.storage.backend import get_backend
from entraclaw.storage.migration import migrate_local_to_backend

async def main():
    backend = get_backend()
    report = migrate_local_to_backend(
        [(Path.home() / '.entraclaw' / 'data', '')],
        backend,
    )
    print(f'copied={report.copied} skipped={report.skipped} errors={len(report.errors)}')

asyncio.run(main())
"
```

## Troubleshooting

### Writes are still going to local after I switched `.env` to cloud

Your MCP server is still running the process that booted with the old config. Restart the MCP client (`/mcp` in Claude Code) to spin up a fresh server that reads the new `.env`. Look for multiple `entraclaw-mcp` processes in `ps aux | grep entraclaw` — kill any stale ones.

### `403 This request is not authorized to perform this operation`

Most common cause: **Azure RBAC propagation delay.** After `setup.sh --use-cloud-memory` grants `Storage Blob Data Contributor`, the role can take 1–5 minutes to take effect. Retry after a short wait.

Less common causes:

- The Agent User has the wrong (or no) `user_impersonation` consent on Azure Storage. Re-run `setup.sh --use-cloud-memory` which calls `grant_agent_user_storage_consent`.
- You're trying to read/write from a different principal (e.g. your `az login` user doesn't have a role on the container). `az storage blob list --auth-mode login` will hit this; the agent won't.

### I want to kill the cloud backend entirely

Easiest: pass `--keep-memory-local` to `setup.sh`, or remove `ENTRACLAW_BLOB_ENDPOINT`/`ENTRACLAW_BLOB_CONTAINER` from `.env` and set `ENTRACLAW_KEEP_MEMORY_LOCAL=true`. Restart the MCP server. The existing blob stays around until you `az storage container delete` it manually.

## See also

- [`docs/decisions/005-cloud-hosted-memory.md`](../decisions/005-cloud-hosted-memory.md) — the ADR driving this design
- [`src/entraclaw/storage/backend.py`](../../src/entraclaw/storage/backend.py) — the backend protocol + factory
- [`src/entraclaw/storage/blob.py`](../../src/entraclaw/storage/blob.py) — the async BlobStore client
- [`src/entraclaw/storage/migration.py`](../../src/entraclaw/storage/migration.py) — the migrator used by setup.sh and callable by hand
- [`scripts/provision_blob_storage.py`](../../scripts/provision_blob_storage.py) — the idempotent Azure provisioning routine
