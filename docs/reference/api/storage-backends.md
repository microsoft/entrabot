# Storage backends

Defined in `src/entrabot/storage/`. The `MemoryBackend` protocol hides whether a piece of agent *operational* state lives on the local filesystem or in Azure Blob Storage. Three implementations ship: `LocalBackend`, `BlobBackend`, and `PersonaBackend` (a compatibility/migration wrapper — see below).

This is a distinct system from persona-sati's own memory. See [Storage and Memory](../../architecture/storage-and-memory.md) for the full picture of what's operational state versus persona/mind memory.

## `MemoryBackend` protocol

```python
@runtime_checkable
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

Keys are forward-slash separated paths (e.g. `"interactions/2026-04-17.jsonl"`). Implementations decide where each key actually lives.

The interface is sync — call sites (`tools/interaction_log.py`, `tools/daily_summary.py`, `tools/promises.py`, `tools/chat_cursors.py`, `tools/poll_state.py`) are sync. On `BlobBackend` every call is blocking network I/O, so async code should run these in a worker thread. `_run_sync` avoids the `asyncio.run()` error when a loop is running, but it still waits for the result on the calling thread.

## `LocalBackend`

```python
class LocalBackend:
    def __init__(self, root: Path) -> None
```

Filesystem-backed. Keys map directly to paths under `root`. ETags are synthesized as a SHA-256 hex digest of the content (`_content_etag`) — identical content always produces the same ETag, and any change produces a different one, so the same compare-and-swap code path works against both `LocalBackend` and `BlobBackend`.

`root` is `cfg.data_dir`: `~/.entrabot/data` on macOS/Linux, or the Windows per-user data root under `%LOCALAPPDATA%\entrabot\data` (configurable via `ENTRABOT_DATA_DIR`).

## `BlobBackend`

```python
class BlobBackend:
    def __init__(self, store: BlobStore) -> None
```

Wraps an async `BlobStore` (`storage/blob.py`) for sync callers via `_run_sync`, a small shim that calls `asyncio.run` directly when no event loop is running, and falls back to a one-worker `ThreadPoolExecutor` when called from inside an already-running loop (so it never raises `asyncio.run() cannot be called from a running event loop`).

`append_text` is implemented as read-then-concat-then-put — fine for the daily JSONL files this backend routes today (a few KB); there is no batching or write coalescing.

Token provider: `acquire_agent_user_storage_token(cfg)` (in `tools/teams.py`) — the same three-hop Blueprint → Agent Identity → Agent User flow used for Graph, with Hop 3's scope swapped to `https://storage.azure.com/.default`. `BlobStore` translates a `401` response into `TokenExpiredError` on every call (`_check_auth`), so the same token-refresh handling used for Graph calls can apply here too.

## ETag concurrency

`write_text(key, content, if_match=etag)` is compare-and-swap: it only succeeds if the key's current ETag equals `if_match`, and raises `entrabot.storage.blob.ConcurrencyError` otherwise. Both backends honor this — `LocalBackend` compares content hashes, `BlobBackend` relies on Azure's real ETag header and its native `If-Match` precondition (surfaced by Azure as HTTP 412, translated to `ConcurrencyError` by `BlobStore.put`). `if_match=None` (the default) is an unconditional overwrite.

`write_text(key, content, if_absent=True)` is the compare-and-swap for a key that doesn't exist yet: it creates the key, and raises `ConcurrencyError` if another writer got there first. `LocalBackend` uses an exclusive create. `BlobBackend` sends `If-None-Match: *`, and `BlobStore.put` translates both 412 and 409 to `ConcurrencyError` on that path, since Azure's docs list both. `if_match` and `if_absent` together raise `ValueError`.

Three call sites build retry logic on top of this:

- **`chat_cursors.claim_delivery()`** — reads the shared per-chat cursor with its ETag, computes which candidate message IDs aren't already recorded delivered, and writes the merged cursor back with `if_match=etag`. On `ConcurrencyError` it re-reads and retries, up to `CLAIM_MAX_ATTEMPTS = 4` times; exhausting the retries returns an empty claim (push nothing) rather than risk double-delivering a message across concurrent pollers.
- **`promises.py`** — the same read-with-etag / write-with-`if_match` shape, but retries only **once** on `ConcurrencyError` before raising `PromiseStoreConflict`.
- **`poll_state.add_watched_chats()`** — merges a batch of chat IDs into the shared list, up to `WATCHED_CHATS_MAX_ATTEMPTS = 4` times. Exhausting the retries reports failure without raising, so the caller can keep those chats dirty and try again later.

`poll_state.claim_email_window()` uses the same primitive without a retry: losing the swap means another instance owns that window of mail, so there is nothing to retry.

## `get_backend()` factory

```python
def get_backend() -> MemoryBackend
```

Resolves on every call (not cached at import time), in this order:

1. `cfg.keep_memory_local` (`ENTRABOT_KEEP_MEMORY_LOCAL=true`/`1`/`yes`) → `LocalBackend(cfg.data_dir)` — explicit escape hatch, takes priority over everything else.
2. Both `cfg.blob_endpoint` (`ENTRABOT_BLOB_ENDPOINT`) and `cfg.blob_container` (`ENTRABOT_BLOB_CONTAINER`) set → `BlobBackend` wrapping a `BlobStore` whose token provider is `acquire_agent_user_storage_token`.
3. Neither set → `LocalBackend(cfg.data_dir)` (the default).

**Exactly one** of `ENTRABOT_BLOB_ENDPOINT` / `ENTRABOT_BLOB_CONTAINER` set raises `BackendMisconfiguredError` — this is a hard failure, not a silent fallback to Local. A silent fallback is how a mis-configured fleet instance would land on an empty local store and re-bootstrap every chat from scratch (a replay flood); failing loud surfaces the misconfiguration instead of letting instances diverge.

`assert_backend_config(logger=None)` calls `get_backend()` once at MCP initialization to force this validation early and log which backend resolved. It never resolves a storage token itself, so it's safe to call before auth completes.

## What actually routes through `MemoryBackend`

- `tools/interaction_log.py` — appends to / reads `interactions/<YYYY-MM-DD>.jsonl`.
- `tools/daily_summary.py` — reads the day's interaction log to build the summary; archives the rendered summary.
- `tools/promises.py` — reads/writes the single key `promises.jsonl`.
- `tools/chat_cursors.py` — one key per chat, `chat_cursors/<url-quoted chat_id>.json`.
- `tools/poll_state.py` — the watched-chat list at `watched_chats`, and the email cursor at `poll_state/email_cursor.txt`.

`.singleton.lock` stays a plain local file under `cfg.data_dir`: it is a per-machine process lock.

### `poll_state`

```python
class PollStateOutcome(Enum):  # ABSENT, PRESENT, UNRESOLVED
class EmailClaim(Enum):        # WON, LOST, FAILED

def read_watched_chats() -> WatchedChatsRead                  # .outcome, .chat_ids, .etag
def add_watched_chats(chat_ids) -> WatchedChatsWrite          # .persisted, .added
def read_email_cursor() -> EmailCursorRead                    # .outcome, .cursor, .etag
def initialize_email_cursor(now: str | None = None) -> bool
def claim_email_window(new_cursor: str, etag: str | None) -> EmailClaim
def ignored_legacy_local_copies() -> list[Path]
```

None of these raise for a storage failure. A failed read is `UNRESOLVED`, never `ABSENT`. A cursor that isn't a timestamp is `UNRESOLVED`, and so is any value with no ETag, since it couldn't be swapped safely. List entries that can't be chat IDs (containing whitespace, `/`, `\`, `?`, `#`, `%`, or control characters) are dropped on parse, because each ID ends up in a Graph URL path. Being on the list grants nothing: `SponsorGate` trusts a chat only after Graph confirms a sponsor is a member.

`initialize_email_cursor()` starts the cursor at "now" on any backend except `LocalBackend`, which first adopts a usable legacy `email_cursor.txt`. It never consults the old key on a cloud backend. See [Poll state](../../architecture/storage-and-memory.md#poll-state) for why.

## Migration

`src/entrabot/storage/migration.py`:

```python
def migrate_local_to_backend(
    sources: Iterable[tuple[Path, str]],
    target: MemoryBackend,
) -> MigrationReport
```

`sources` is an iterable of `(local_root, blob_prefix)` pairs; an empty prefix (`""`) means "root of the container." For each pair, every file under `local_root` is copied to `target` at key `{blob_prefix}/{relative_path}` (or just `{relative_path}` when the prefix is empty).

```python
@dataclass
class MigrationReport:
    copied: int = 0
    skipped: int = 0
    bytes_copied: int = 0
    keys_copied: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
```

Behavior:

- **Idempotent and source-preserving** — a key already present at the target is skipped (`skipped` +1); local files are never deleted.
- **Local poll state is not copied** — `poll_state/email_cursor.txt`, the legacy `email_cursor.txt`, `watched_chats`, the legacy `chat_id` key, everything under `chat_cursors/`, and the process-lock files are skipped and not counted, since a machine's copies may be stale or another agent's. A copied per-chat cursor would be read back as present and skip the 24-hour first-sight gate. The cloud cursor starts at "now" and discovery rebuilds the list. The exclusions match keys at the container root (an empty prefix) only; a source copied under a prefix such as `claude_memory` keeps every file.
- **Missing source roots are skipped silently** — a user with no Claude Code persona-memory directory still migrates their agent data cleanly.
- **Per-file errors are recorded, not raised** — a file that fails to read or write is appended to `errors` as `(key, message)`; one bad file doesn't abort the rest.

`scripts/setup.sh --use-cloud-memory` calls this after provisioning Blob Storage and treats any non-empty `report.errors` as a hard failure, exiting non-zero rather than reporting success while cloud memory is silently out of sync.

## `PersonaBackend` — compatibility/migration wrapper, not the runtime owner of persona memory

```python
class PersonaBackend:
    def __init__(self, backend: MemoryBackend, *, local_root: Path) -> None

    def push_one(self, path: Path) -> None: ...
    def push_all(self) -> PersonaReport: ...
    def pull_all(self) -> PersonaReport: ...
```

A thin wrapper over an existing `MemoryBackend`, scoped to the `claude_memory/` key prefix. It adds directory-level operations the per-key protocol doesn't have:

- `push_one(path)` — uploads a single file. Raises `ValueError` if `path` resolves outside `local_root`. A missing file is a silent no-op (a hook may fire after a rapid create+delete).
- `push_all()` — uploads every local file not already present at its key in the backend; skips symlinks (recorded as errors) and anything outside `local_root`.
- `pull_all()` — downloads every blob under `claude_memory/` into `local_root`, overwriting local files (cloud is authoritative on pull); skips keys whose relative path would traverse outside `local_root`.

```python
@dataclass
class PersonaReport:
    copied: int = 0
    skipped: int = 0
    pulled: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    skipped_keys: list[str] = field(default_factory=list)
```

`PersonaBackend` and its CLI wrapper, `scripts/claude_memory_sync.py`, are **manual migration and compatibility utilities** — a one-off way to move persona-memory files into or out of the same Blob container the operational backend uses, and a fallback for hosts without a `persona-sati` MCP server attached. Neither runs automatically: there is no hook or background task that keeps `claude_memory/` in sync during normal operation. When a `persona-sati` MCP server is attached, it owns persona memory directly through its own MCP tools, independent of this path.

### `claude_code_memory_dir`

```python
def claude_code_memory_dir(project_root: Path, *, home: Path | None = None) -> Path
```

Resolves the Claude Code per-project auto-memory directory: `~/.claude/projects/<slug>/memory`, where `<slug>` replaces `/`, `\`, and spaces in the absolute project path with `-`. The directory may not exist (a project Claude Code has never opened, or a host without Claude Code) — callers must check `.exists()` before reading.

## Related

- [Storage and Memory](../../architecture/storage-and-memory.md) — the full architecture, including fail-closed cursor semantics and the operational-vs-persona memory split.
- [Storage Configuration guide](../../guides/storage-configuration.md) — how to choose and configure a backend.
- [Security Boundaries](../../architecture/security-boundaries.md) — why half-configured Blob storage fails closed instead of silently falling back.
- [Configuration](../configuration.md) — the full `ENTRABOT_*` environment variable reference.
