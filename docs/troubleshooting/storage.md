# Storage

## Entrabot selected the wrong backend

Backend selection is deterministic and evaluated from current environment
values:

1. `ENTRABOT_KEEP_MEMORY_LOCAL=true` selects `LocalBackend`.
2. Both `ENTRABOT_BLOB_ENDPOINT` and `ENTRABOT_BLOB_CONTAINER` select
   `BlobBackend`.
3. Neither Blob variable selects `LocalBackend`.
4. Exactly one Blob variable raises `BackendMisconfiguredError`.

A half-configured Blob environment never silently falls back to local. Fix the
missing variable, or deliberately set `ENTRABOT_KEEP_MEMORY_LOCAL=true`, then
restart the MCP host.

## Data appears in different places

Not all operational state uses `MemoryBackend`:

| State | Location |
|---|---|
| Interaction logs | Selected `MemoryBackend` |
| Daily summaries | Selected `MemoryBackend` |
| Promises | Selected `MemoryBackend` |
| Per-chat delivery cursors | Selected `MemoryBackend` |
| `watched_chats` registry | Always `ENTRABOT_DATA_DIR/watched_chats` |
| Email cursor | Always `ENTRABOT_DATA_DIR/email_cursor.txt` |

The local-to-Blob migration walks the data directory and may copy
`watched_chats` and `email_cursor.txt` as artifacts, but runtime reads and
writes for those two files remain local.

## Blob requests return 401

A Blob 401 means the token is expired or does not carry the Azure Storage
resource scope. Blob access requires an Agent User token for:

```text
https://storage.azure.com/.default
```

This is distinct from the Graph token used by Teams and email. Re-run setup
with cloud memory enabled so the Storage `user_impersonation` consent is
present:

```bash
./scripts/setup.sh --use-blueprint=<blueprint-app-id> --use-cloud-memory
```

Do not use a human Azure CLI token as the Entrabot Blob data-plane identity.

## Blob requests return 403

Check both authorization layers:

1. The Agent Identity has per-principal Storage consent for the Agent User.
2. The Agent User has `Storage Blob Data Contributor` scoped to the configured
   container.

After a new role assignment, allow time for Azure RBAC propagation, then
retry. Also confirm the endpoint and container belong to the same resources
setup provisioned.

## Local-to-Blob migration reports errors

The migration is:

- **Source-preserving**: local files are never deleted.
- **Idempotent**: existing target keys are skipped.
- **Cloud-authoritative on rerun**: an existing Blob is not overwritten by the
  local copy.
- **Best effort per file**: individual failures are collected while other
  files continue.

When selected migration errors occur during `setup.sh`, setup prints
`Setup INCOMPLETE` and exits with code 2. Fix storage scope, RBAC, connectivity,
or the unreadable local file, then re-run the same setup command. Do not delete
successful Blob copies before retrying.

See [Migrations and upgrades](migrations-and-upgrades.md).

## Teams delivery pauses during a storage incident

Per-chat delivery cursors use ETags and conditional writes. Concurrent pollers
read the shared cursor, claim unseen message IDs with `If-Match`, and retry a
bounded number of conflicts.

Ambiguous cursor reads, corrupt cursor content, storage failures, and exhausted
ETag retries fail closed: no message is pushed. This prevents duplicate or
historical delivery. Restore backend health first; deleting cursors can cause a
bootstrap or replay and should not be the first recovery step.

## I need to switch back to local storage

Set:

```text
ENTRABOT_KEEP_MEMORY_LOCAL=true
```

Restart the MCP host. Existing Blob data is left intact. Switching the runtime
backend does not automatically copy newer cloud state back to local.

See [Storage Configuration](../guides/storage-configuration.md),
[Storage and Memory](../architecture/storage-and-memory.md), and
[Storage Backends](../reference/api/storage-backends.md).
