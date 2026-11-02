# Storage and Memory

Entrabot has two separate things that could be called "memory," and they are owned by different systems:

1. **Operational state** — the agent's own bookkeeping. Interaction logs, daily summaries, chat cursors, promises, the watched-chat list, and the email cursor all route through `MemoryBackend` and may use local files or opt-in Azure Blob Storage.
2. **Persona / mind memory** — personality, relationships, running context, behavioral rules. When a `persona-sati` MCP server is attached, the host calls its tools (`write_memory_file`, `read_memory_file`, `bootstrap_session`, and related) for this. Entrabot is not that system; it does not read or write persona content as part of its normal operation.

## The `MemoryBackend` protocol

`src/entrabot/storage/backend.py` defines a small sync protocol that hides where a piece of operational state actually lives:

```python
class MemoryBackend(Protocol):
    def read_text(self, key: str) -> str | None: ...
    def read_text_with_etag(self, key: str) -> tuple[str | None, str | None]: ...
    def write_text(
        self, key: str, content: str, *, if_match: str | None = None, if_absent: bool = False
    ) -> str | None: ...
    def append_text(self, key: str, content: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    def list(self, prefix: str = "") -> list[str]: ...
```

Keys are forward-slash-separated paths, e.g. `"interactions/2026-04-17.jsonl"`. Two implementations ship:

- **`LocalBackend`** — reads and writes files under `cfg.data_dir` (`~/.entrabot/data` by default). ETags are synthesized as a SHA-256 hash of the file's content, so identical content always produces the same ETag and any change produces a different one.
- **`BlobBackend`** — wraps the async `BlobStore` client (`storage/blob.py`) for these sync call sites via a small `asyncio.run` shim that falls back to a worker thread when called from inside an already-running event loop (so it can't raise `asyncio.run() cannot be called from a running event loop`).

### Resolution order

`get_backend()` resolves which implementation to use, and it resolves this **on every call**, not once at import time — so a config change (or a test fixture) takes effect immediately without restarting anything but the process that reads the new environment:

1. `ENTRABOT_KEEP_MEMORY_LOCAL=true` → always `LocalBackend`. This is an explicit escape hatch and takes priority over everything else.
2. Both `ENTRABOT_BLOB_ENDPOINT` and `ENTRABOT_BLOB_CONTAINER` set → `BlobBackend`, wrapping a `BlobStore` whose token provider is `acquire_agent_user_storage_token()`.
3. Neither set → `LocalBackend` (the default).
4. **Exactly one** of `ENTRABOT_BLOB_ENDPOINT` / `ENTRABOT_BLOB_CONTAINER` set → raises `BackendMisconfiguredError`. This is deliberate: a half-configured cloud environment must fail loud rather than silently fall back to an empty local store — a fleet instance that quietly re-bootstraps against a fresh local store would look "up" while replaying every chat from scratch.

`assert_backend_config()` calls `get_backend()` during MCP initialization to surface a half-config error and log which backend resolved (`BlobBackend` + endpoint/container, or `LocalBackend` + root path). This validation runs after authentication and before poll setup, which reads through the backend, so a misconfigured instance starts no background tasks.

### What actually routes through `MemoryBackend`

Modules that call `get_backend()`:

- `tools/interaction_log.py` — appends to and reads `interactions/<YYYY-MM-DD>.jsonl`.
- `tools/daily_summary.py` — reads the day's interaction log to build the 17:00 (fixed UTC-7) summary.
- `tools/promises.py` — reads/writes the single key `promises.jsonl`.
- `tools/chat_cursors.py` — one key per chat under `chat_cursors/<url-quoted chat_id>.json`.
- `tools/poll_state.py` — the watched-chat list and the email cursor, described next.

`.singleton.lock` deliberately stays local under `cfg.data_dir`: it is a per-machine process lock, not shared state.

### Poll state

`tools/poll_state.py` owns the two values that decide what the background polls deliver:

| Value | Key | Format |
|---|---|---|
| Watched-chat list | `watched_chats` | One chat ID per line, sorted |
| Email cursor | `poll_state/email_cursor.txt` | One RFC 3339 timestamp |

**With a cloud backend, neither value touches local disk.** Instances on one container share them, and a machine moved to another agent's container can't carry over the old agent's chats or a stale cursor. If `data_dir/watched_chats` or `data_dir/email_cursor.txt` exist, they are never read or written; startup logs one info line saying they are being ignored.

**With a local backend, little changes.** `watched_chats` is the same file at the same path. The email cursor moves to the new key, and the first run adopts the value in the legacy `email_cursor.txt` once, so there is no gap in delivery. The legacy file stays in place.

**The email cursor has a new key on purpose.** Existing cloud containers hold a stale `email_cursor.txt` that setup's migration copied once and nothing has updated since, because runtime never read it. Reading that key would replay up to 50 old emails on upgrade, and replayed imperative messages are a security problem, not a nuisance (see `DESIGN-multi-instance-cursor-consistency.md`). So on a cloud backend, a missing cursor starts at "now", and neither the old key nor any local file is consulted. Mail that arrives while no instance runs isn't pushed, but stays readable through the `read_email` tool.

**The rules that make this fleet-safe:**

- Reads classify as `PRESENT`, `ABSENT`, or `UNRESOLVED`, like chat cursors. A failed read is never treated as absent: an unreadable list starts with no chats and retries, and an unreadable email cursor skips the cycle. Neither falls back to a local copy.
- Every write is a compare-and-swap, including the first. Adding a chat merges into the list, so it can't remove another instance's chats. A failed write stays in a dirty set and is retried on the next discovery sweep, because discovery only registers chats that aren't in memory yet.
- A value with no ETag is treated as a failure, never overwritten unconditionally.
- All of this is blocking network I/O, so async code runs it in a worker thread. A past bug that did blob reads on the event loop starved the MCP `initialize` handshake past the host's 30-second timeout.

## Blob authentication

`BlobBackend` authenticates every request with an Agent-User-scoped OAuth token. `acquire_agent_user_storage_token()` (in `tools/teams.py`) is the same three-hop Blueprint → Agent Identity → Agent User flow used for Graph, with Hop 3's `scope` swapped to `https://storage.azure.com/.default` instead of Graph — Hops 1 and 2 are unchanged. See [Identity and Token Flow](identity-and-token-flow.md) for the full three-hop mechanics.

`BlobStore` (`storage/blob.py`) checks every response for HTTP 401 before doing anything else and raises `TokenExpiredError` when it sees one. Storage call sites are not universally wrapped in `_with_token_retry()`, so this mapping does not imply that every storage operation is automatically refreshed and retried.

## Optimistic concurrency

`write_text(key, content, if_match=etag)` is a compare-and-swap: it succeeds only if the key's current ETag equals `if_match`, and raises `ConcurrencyError` otherwise. `LocalBackend` and `BlobBackend` both honor this — Local via the content-hash ETag, Blob via Azure's real ETag header (and its native `If-Match` precondition, surfaced as HTTP 412).

`write_text(key, content, if_absent=True)` is the same guarantee for a key that doesn't exist yet: it creates the key and raises `ConcurrencyError` if another writer got there first. `if_match=None` alone is an unconditional overwrite, so without this two instances that both read "absent" would both write, and the last one would win. `LocalBackend` uses an exclusive create; `BlobBackend` sends `If-None-Match: *`, and `BlobStore` treats both 412 and 409 as a conflict on that path, since Azure's docs list both.

Three call sites build real logic on top of this:

- **`chat_cursors.claim_delivery()`** — the fleet-safe delivery ledger for inbound Teams messages. It reads the shared cursor with its ETag, computes which candidate message IDs aren't already recorded as delivered, and writes the merged cursor back with `if_match=etag`. On `ConcurrencyError` it re-reads and retries, up to `CLAIM_MAX_ATTEMPTS` (4) times; exhausting the retries returns an empty claim (push nothing) rather than risk double-delivering a message across multiple polling instances.
- **`promises.py`** — the same read-with-etag / write-with-if_match / retry-once-on-`ConcurrencyError` shape for the shared promises ledger.
- **`poll_state.py`** — merges into the watched-chat list with bounded retries, and claims each window of email by advancing the cursor before pushing it. See [Messaging and Delivery](messaging-and-delivery.md#fleet-safe-delivery).

### Fail-closed cursor reads

`chat_cursors.resolve_cursor()` classifies every cursor read into one of three outcomes, and the distinction is load-bearing: `ABSENT` (the read succeeded and no cursor exists — only this case allows a poll to treat a chat as brand-new), `PRESENT` (a cursor exists and parsed, fresh or stale), or `UNRESOLVED` (the read raised, or the payload was corrupt or the wrong shape). `UNRESOLVED` is treated the same as "do not push anything this cycle" — an ambiguous read (a transient Blob 401, a timeout, a partial write) must never be mistaken for "this is a genuinely new chat," because that would replay old messages as if they were new.

## Migration

`src/entrabot/storage/migration.py` provides `migrate_local_to_backend(sources, target)`, where `sources` is an iterable of `(local_root, blob_prefix)` pairs and `target` is the destination `MemoryBackend`:

```python
def migrate_local_to_backend(
    sources: Iterable[tuple[Path, str]],
    target: MemoryBackend,
) -> MigrationReport
```

Behavior:

- **Source-preserving.** Files are copied, never deleted — the local copy remains as a rollback path if `ENTRABOT_KEEP_MEMORY_LOCAL` is set afterward.
- **The email cursor is not copied.** The migration skips `poll_state/email_cursor.txt`, so a local value can't become the cloud cursor, and the cloud cursor starts at "now". A legacy `email_cursor.txt` may still be copied, but nothing reads that key. `watched_chats` is copied, which is correct for a real local-to-cloud conversion.
- **Idempotent.** A key already present at the target is skipped, so re-running the migration only copies what's missing.
- **Per-file error report, not an abort.** A file that fails to read or write is recorded in `report.errors` as `(key, message)`; one bad file does not stop the rest of the migration.
- **Missing source roots are skipped silently** — a user with no Claude Code persona-memory directory still migrates their agent data cleanly.

`scripts/setup.sh --use-cloud-memory` calls this after provisioning Blob Storage, and treats any non-empty `report.errors` as a hard failure: the setup script prints the errors and **exits non-zero**, rather than reporting success while cloud memory is silently out of sync with local disk.

For what changes when an existing deployment picks this up, see [Migrations and upgrades](../troubleshooting/migrations-and-upgrades.md#upgrading-to-shared-poll-state).

## Persona memory: `PersonaBackend`

`storage/persona.py`'s `PersonaBackend` is a thin wrapper around an existing `MemoryBackend`, scoped to the `claude_memory/` key prefix:

```python
class PersonaBackend:
    def __init__(self, backend: MemoryBackend, *, local_root: Path) -> None: ...
    def push_one(self, path: Path) -> None: ...
    def push_all(self) -> PersonaReport: ...
    def pull_all(self) -> PersonaReport: ...
```

It adds directory-level operations the per-key `MemoryBackend` protocol doesn't have: `push_all` uploads every local file not already present in the cloud, `pull_all` downloads every blob under `claude_memory/` back to disk (cloud is authoritative on pull, so this overwrites local), and `push_one` uploads a single file, rejecting any path that resolves outside `local_root`.

`PersonaBackend` and its CLI wrapper, `scripts/claude_memory_sync.py`, are **manual migration and compatibility utilities** — a one-off way to move persona-memory files into or out of the same Blob container the operational backend uses, and a fallback for hosts without a `persona-sati` MCP server. Neither runs automatically as part of normal operation: there is no hook or background task that keeps `claude_memory/` in sync on every write. When `persona-sati` is attached, it owns persona memory directly through its own MCP tools, independent of this path.

## See also

- [Storage Configuration guide](../guides/storage-configuration.md) — how to choose and configure a backend.
- [Storage Backends reference](../reference/api/storage-backends.md) — full API surface.
- [MCP Runtime](mcp-runtime.md) — how backend resolution fits into server boot.
- [Messaging and Delivery](messaging-and-delivery.md) — how chat cursors and delivery claims fit into the poll loop.
- [Security Boundaries](security-boundaries.md) — the fail-closed principle applied across storage, cursors, and auth.
