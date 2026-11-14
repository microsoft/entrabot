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

Almost all operational state uses `MemoryBackend`:

| State | Location |
|---|---|
| Interaction logs | Selected `MemoryBackend` |
| Daily summaries | Selected `MemoryBackend` |
| Promises | Selected `MemoryBackend` |
| Per-chat delivery cursors | Selected `MemoryBackend` |
| `watched_chats` list | Selected `MemoryBackend` |
| Email cursor (`poll_state/email_cursor.txt`) | Selected `MemoryBackend` |
| `.singleton.lock` | Always local under `ENTRABOT_DATA_DIR` |

With a cloud backend, `watched_chats` and `email_cursor.txt` files under
`ENTRABOT_DATA_DIR` are ignored, and startup logs an info line naming any that
exist. They can be removed by hand. An `email_cursor.txt` blob in the container
is also ignored: it is a stale copy from setup's migration.

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

When migration errors occur during setup, setup prints "Copying memory to the
cloud failed. Cloud memory is not in sync with this computer." and the files
that failed, and exits with code 1 (earlier versions of `setup.sh` printed
`Setup INCOMPLETE` and exited with code 2). Fix storage scope, RBAC,
connectivity, or the unreadable local file, then run setup again with cloud
memory (`./scripts/setup.sh --use-cloud-memory` on macOS and Linux) and answer
yes to the copy. Do not delete successful Blob copies before retrying.

See [Migrations and upgrades](migrations-and-upgrades.md).

## Teams delivery pauses during a storage incident

Per-chat delivery cursors use ETags and conditional writes. Concurrent pollers
read the shared cursor, claim unseen message IDs with `If-Match`, and retry a
bounded number of conflicts.

Ambiguous cursor reads, corrupt cursor content, storage failures, and exhausted
ETag retries fail closed: no message is pushed. This prevents duplicate or
historical delivery. Restore backend health first; deleting cursors can cause a
bootstrap or replay and should not be the first recovery step.

The watched-chat list and email cursor behave the same way during an outage:

- If the list can't be read at startup, the instance starts with no chats and logs
  `Watched-chat list unreadable at startup`. It retries every 60 seconds, and
  discovery can still register chats in the meantime. Neither can overwrite the
  stored list, because every write is a merge.
- If a list write fails, the chat keeps polling and the write is retried on the
  next discovery sweep.
- If the email cursor can't be read, the poll logs `Email cursor unresolved` and
  skips the cycle. See
  [Email notifications do not arrive](teams-and-email.md#email-notifications-do-not-arrive).
- If the list can't be read while a sponsor wait is starting, the sponsor gate is
  narrowed to the Agent Identity's own sponsors for that wait. It is never widened.

## I need to switch back to local storage

Set:

```text
ENTRABOT_KEEP_MEMORY_LOCAL=true
```

Restart the MCP host. Existing Blob data is left intact. Switching the runtime
backend does not automatically copy newer cloud state back to local.

Before restarting, delete `email_cursor.txt` and `poll_state/email_cursor.txt`
under `ENTRABOT_DATA_DIR` if they exist. Cloud mode doesn't update those files, so
they are as old as your last local run. Local mode would use one of them, and its
first poll could push up to 50 emails that had already arrived as if they were new.
With both gone, the cursor starts at the current time. Any mail in between stays
readable through the `read_email` tool.

See [Storage Configuration](../guides/storage-configuration.md),
[Storage and Memory](../architecture/storage-and-memory.md), and
[Storage Backends](../reference/api/storage-backends.md).
