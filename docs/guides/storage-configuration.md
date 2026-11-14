# Storage configuration

EntraBot writes its *operational* data to a pluggable backend. Interaction logs, daily summaries, promises, per-chat delivery cursors, the watched-chat list, and the email cursor all use `MemoryBackend`.

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
| `watched_chats` | selected `MemoryBackend` |
| `poll_state/email_cursor.txt` | selected `MemoryBackend` |
| `.singleton.lock` | always local, under `ENTRABOT_DATA_DIR` (a per-machine process lock) |

With a cloud backend, the watched-chat list and email cursor never touch local disk, so instances sharing a container share them. Any old local `watched_chats` or `email_cursor.txt` is ignored, and startup logs a line saying so. No persona memory, behavioral rules, or secrets are stored through this operational backend.

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

`setup.sh --use-cloud-memory` writes these for you. The backend reads them via `get_config()` on every call, so flipping between local and cloud is just an `.env` edit and an MCP server restart. Switching from cloud back to local has one catch: see [I need to switch back to local storage](../troubleshooting/storage.md#i-need-to-switch-back-to-local-storage).

### Re-running setup

Setup reads `.env` before it changes it, replaces only its own lines, and keeps the memory settings when the run is for the same Agent User:

- With neither `--use-cloud-memory` nor `--keep-memory-local`, an agent on cloud memory stays on it. Setup leaves the blob endpoint and container as they are and provisions nothing. That is why `./scripts/setup.sh` is safe to run on a configured machine.
- `--keep-memory-local` switches to local. `--use-cloud-memory` provisions again and writes the new values.
- If `--use-cloud-memory` fails to provision, setup shows the provisioner's output and keeps the cloud settings that were already there. On a machine that was not on cloud memory, it says so and asks whether to continue with local memory (yes is suggested, and is the answer unattended); it never falls back silently.

Cloud memory counts as set only when both `ENTRABOT_BLOB_ENDPOINT` and `ENTRABOT_BLOB_CONTAINER` are present and `ENTRABOT_KEEP_MEMORY_LOCAL` is not `true`, `1`, or `yes`. A run for a different Agent User, such as `--new`, carries nothing over; pass `--use-cloud-memory` for it. Lines you added to `.env` by hand are kept.

### The wizard's memory question

The setup wizard (on macOS and Linux as on Windows) asks where the agent should keep its memory. Cloud memory is offered only when the signed-in account has an Azure subscription in the tenant. The same choices as options:

| `setup.sh` | `scripts\setup-windows.cmd` |
|---|---|
| `--use-cloud-memory` | `-UseCloudMemory` |
| `--keep-memory-local` | `-KeepMemoryLocal` |
| `--with-storage-account=NAME` | `-WithStorageAccount NAME` |
| `--with-container=NAME` | `-WithContainer NAME` |
| `--create-new-storage` | `-CreateNewStorage` |
| `--location=REGION`, `--subscription=ID_OR_NAME` | `-Location REGION`, `-Subscription ID_OR_NAME` |

The rules above apply unchanged: a re-run for the same agent keeps its memory setting, and a different agent inherits nothing. When cloud memory is set up in a run and local memory already holds data, the wizard offers to copy it to the cloud (no is suggested; the local copy is kept). The storage options need `--use-cloud-memory` (`-UseCloudMemory`).

Setting up cloud memory through the wizard has not yet been tested against a real subscription, on any system.

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

Once the storage is set up, setup asks whether to copy what this computer already holds (`~/.entrabot/data`, and Claude Code's persona memory) into the blob container. It asks only at a terminal; unattended, nothing is copied. The migration:

- Is **non-destructive** — nothing is deleted from local. You end up with two copies.
- Is **idempotent** — running twice skips keys already present in the blob.
- Treats cloud as authoritative on rerun — an existing target key is not overwritten.
- Collects per-file errors while continuing other files.
- Leaves local poll state behind: your `watched_chats` list, both email cursor files, the legacy `chat_id` key, and the per-chat cursors under `chat_cursors/`. A machine's copies could be stale or belong to another agent, and adopting them could replay old mail or messages as new. The cloud email cursor starts at "now", so mail that arrived before the first boot on the container isn't pushed, but stays readable through the `read_email` tool. Discovery rebuilds the chat list from Graph about two minutes after boot, and a chat with no cursor surfaces its newest message only if it is under 24 hours old.

When migration errors occur inside setup, setup says "Copying memory to the cloud failed." and exits with code 1 (earlier versions of `setup.sh` reported `Setup INCOMPLETE` and exited with code 2). Fix the reported problem and run the same command again, answering yes to the copy; successful copies remain in Blob and local sources remain untouched.

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

## Upgrading an existing deployment

If you're upgrading to the release that moved the watched-chat list and email cursor into the backend, see [Upgrading to shared poll state](../troubleshooting/migrations-and-upgrades.md#upgrading-to-shared-poll-state).

## Troubleshooting

See [Storage troubleshooting](../troubleshooting/storage.md) for backend selection, half-configured environments, storage-scope 401s, RBAC/consent 403s, migration recovery, and ETag cursor behavior.

## See also

- [Storage and Memory](../architecture/storage-and-memory.md) — the architecture behind this design
- [`src/entrabot/storage/backend.py`](https://github.com/microsoft/entrabot/blob/main/src/entrabot/storage/backend.py) — the backend protocol + factory
- [`src/entrabot/storage/blob.py`](https://github.com/microsoft/entrabot/blob/main/src/entrabot/storage/blob.py) — the async BlobStore client
- [`src/entrabot/storage/migration.py`](https://github.com/microsoft/entrabot/blob/main/src/entrabot/storage/migration.py) — the migrator used by setup.sh and callable by hand
- [`scripts/provision_blob_storage.py`](https://github.com/microsoft/entrabot/blob/main/scripts/provision_blob_storage.py) — the idempotent Azure provisioning routine
