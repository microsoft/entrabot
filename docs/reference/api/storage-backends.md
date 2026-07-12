# Storage backends

Defined in `src/entrabot/storage/`. The `MemoryBackend` protocol hides whether a piece of agent state lives on the local filesystem or in Azure Blob Storage. Three implementations ship: `LocalBackend`, `BlobBackend`, `PersonaBackend`.

## `MemoryBackend` protocol

```python
@runtime_checkable
class MemoryBackend(Protocol):
    def read_text(self, key: str) -> str | None: ...
    def write_text(self, key: str, content: str) -> None: ...
    def append_text(self, key: str, content: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    def list(self, prefix: str = "") -> list[str]: ...
```

Keys are forward-slash separated paths (e.g. `"interactions/2026-04-17.jsonl"`). Implementations decide where each key actually lives.

The interface is sync — call sites (`tools/interaction_log.py`, `tools/daily_summary.py`) are sync. Async impls use a small `asyncio.run` shim that tolerates being called from inside a running event loop.

## `LocalBackend`

```python
class LocalBackend:
    def __init__(self, root: Path) -> None
```

Filesystem-backed. Keys map directly to paths under `root`. `root` is `cfg.data_dir`, which is `~/.entrabot/data` on macOS / Linux and `%LOCALAPPDATA%\entrabot\data` on Windows.

Use when:

- `ENTRABOT_KEEP_MEMORY_LOCAL=true` is set.
- `BLOB_ENDPOINT` / `BLOB_CONTAINER` are unset (half-configured cloud falls back to local for safety).
- Running locally without an Azure subscription.

## `BlobBackend`

```python
class BlobBackend:
    def __init__(self, store: BlobStore) -> None
```

Wraps an async `BlobStore` (`storage/blob.py`) for sync callers via a small `asyncio.run` shim. `BlobStore.put` is ETag-aware and raises `ConcurrencyError` on lost races; `401` from Azure raises `TokenExpiredError` so the MCP server can refresh the storage-scope token.

`append_text` is implemented as read+concat+put — fine for the daily JSONL files (a few KB) Phase 2 routes through this. A future `CachedBlobBackend` will batch writes locally.

Token provider: `acquire_agent_user_storage_token(cfg)` — Hop 3 of the three-hop chain, swapping the resource scope from Graph to `https://storage.azure.com/.default`. See ADR-005 §5.

Use when:

- `BLOB_ENDPOINT` and `BLOB_CONTAINER` are both set.
- `ENTRABOT_KEEP_MEMORY_LOCAL` is unset.
- Cross-device durability matters.

## `PersonaBackend`

```python
class PersonaBackend:
    def __init__(self, backend: MemoryBackend, *, local_root: Path) -> None

    def push_one(self, path: Path) -> PersonaReport: ...
    def push_all(self) -> PersonaReport: ...
    def pull_all(self) -> PersonaReport: ...
```

Thin wrapper over an existing `MemoryBackend`, scoped to the `claude_memory/` key prefix. Adds the directory-level operations the per-key protocol does not have: `pull_all` (cloud → local), `push_all` (local → cloud), `push_one` (single file → cloud).

No caching — the hot path is exactly "one file write → one blob PUT" and "session start → list + fetch each key."

Used by `scripts/claude_memory_sync.py` as a manual migration tool. Runtime sync is now owned by persona-sati (see `docs/architecture/DESIGN-persona-sati-integration.md`).

### `claude_code_memory_dir`

```python
def claude_code_memory_dir(project_root: Path, *, home: Path | None = None) -> Path
```

Resolve the Claude Code per-project auto-memory directory: `~/.claude/projects/<slug>/memory`, where `<slug>` replaces both POSIX `/` and Windows `\` separators (and spaces) with `-`.

The directory may not exist (a project Claude Code has never seen, or a host without Claude Code). Caller must `.exists()` before reading.

## `get_backend()` factory

```python
def get_backend() -> MemoryBackend
```

Selection order (ADR-005 §"--keep-memory-local"):

1. `cfg.keep_memory_local` → `LocalBackend(cfg.data_dir)` (explicit escape hatch).
2. `cfg.blob_endpoint` AND `cfg.blob_container` set → `BlobBackend` wrapping a `BlobStore` whose token provider is `acquire_agent_user_storage_token`.
3. Otherwise → `LocalBackend(cfg.data_dir)`.

Half-configured cloud (endpoint without container, or vice versa) is treated as not-configured, not as an error — the local fallback is safer for the hot path.

## Migration

`src/entrabot/storage/migration.py` ships a one-shot `migrate_local_to_backend()` helper used by `setup.sh --use-cloud-memory` to upload an existing local data dir to the freshly-provisioned blob container.

```python
@dataclass
class MigrationReport:
    copied: int
    skipped: int
    errors: list[tuple[str, str]]

def migrate_local_to_backend(
    local_root: Path,
    backend: MemoryBackend,
    *,
    sources: list[str] | None = None,
) -> MigrationReport
```

Idempotent and source-preserving — never deletes local files. `setup.sh` exits non-zero on migration failure (Learning #36 reminded us to surface this loudly).
