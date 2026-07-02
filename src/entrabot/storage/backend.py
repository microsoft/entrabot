"""MemoryBackend abstraction for agent state (ADR-005, Phase 2).

Defines a small sync interface that hides whether a piece of agent state
lives on the local filesystem or in Azure Blob Storage. Existing call
sites (`tools/interaction_log.py`, `tools/daily_summary.py`) are sync,
so the interface matches that shape.

Two implementations:
- :class:`LocalBackend` — paths under a root dir on disk.
- :class:`BlobBackend` — wraps the async :class:`BlobStore` for sync
  callers via a small ``asyncio.run`` shim that tolerates being called
  from inside a running event loop.

Phase 2 ships the abstraction + both impls + a default-to-local
``get_backend()`` factory. Phase 3 adds caching + write-through;
Phase 5 wires the cloud branch into ``get_backend`` once
``setup.sh`` provisions the Storage Account.
"""

from __future__ import annotations

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from entrabot.config import get_config
from entrabot.errors import BackendMisconfiguredError
from entrabot.storage.blob import BlobStore, ConcurrencyError
from entrabot.tools.teams import acquire_agent_user_storage_token

if TYPE_CHECKING:
    from collections.abc import Coroutine


def _content_etag(content: str) -> str:
    """Deterministic content-hash ETag for local files.

    Mirrors Azure Blob ETag semantics closely enough for optimistic
    concurrency: identical content → identical ETag; any change → different
    ETag. Used by :class:`LocalBackend` so the same CAS code path works for
    both local and blob storage.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@runtime_checkable
class MemoryBackend(Protocol):
    """Sync key→text store for agent state.

    Keys are forward-slash separated paths (e.g. ``"interactions/2026-04-17.jsonl"``).
    Implementations decide where each key actually lives.
    """

    def read_text(self, key: str) -> str | None:
        """Return text at *key*, or ``None`` if it doesn't exist."""
        ...

    def read_text_with_etag(self, key: str) -> tuple[str | None, str | None]:
        """Return ``(text, etag)`` for *key*, or ``(None, None)`` if absent.

        The ETag is an opaque concurrency token to pass back to
        :meth:`write_text` as ``if_match`` for an optimistic-concurrency
        write (design F5). Backends without native ETags synthesize one from
        the content hash.
        """
        ...

    def write_text(self, key: str, content: str, *, if_match: str | None = None) -> str | None:
        """Replace *key*'s content with *content*. Creates parents as needed.

        When ``if_match`` is provided, the write is conditional: it succeeds
        only if *key*'s current ETag equals ``if_match``, otherwise it raises
        :class:`entrabot.storage.blob.ConcurrencyError`. ``if_match=None``
        (default) is an unconditional overwrite. Returns the new ETag (or
        ``None`` for backends that don't track one).
        """
        ...

    def append_text(self, key: str, content: str) -> None:
        """Append *content* to *key*. Creates the key (and parents) if missing."""
        ...

    def exists(self, key: str) -> bool: ...

    def list(self, prefix: str = "") -> list[str]:
        """Return keys whose path starts with *prefix*."""
        ...


# ---------------------------------------------------------------------------
# LocalBackend
# ---------------------------------------------------------------------------
class LocalBackend:
    """Filesystem-backed MemoryBackend rooted at *root*."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def _path(self, key: str) -> Path:
        return self._root / key

    def read_text(self, key: str) -> str | None:
        p = self._path(key)
        if not p.exists():
            return None
        return p.read_text()

    def read_text_with_etag(self, key: str) -> tuple[str | None, str | None]:
        p = self._path(key)
        if not p.exists():
            return None, None
        content = p.read_text()
        return content, _content_etag(content)

    def write_text(self, key: str, content: str, *, if_match: str | None = None) -> str | None:
        p = self._path(key)
        if if_match is not None:
            # Conditional write: current content hash must equal if_match.
            current = _content_etag(p.read_text()) if p.exists() else None
            if current != if_match:
                raise ConcurrencyError(
                    f"write_text({key!r}) refused: If-Match={if_match!r} is stale "
                    f"(current={current!r})"
                )
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return _content_etag(content)

    def append_text(self, key: str, content: str) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as fh:
            fh.write(content)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def list(self, prefix: str = "") -> list[str]:
        if not self._root.exists():
            return []
        results: list[str] = []
        for f in self._root.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(self._root).as_posix()
            if rel.startswith(prefix):
                results.append(rel)
        return results


# ---------------------------------------------------------------------------
# BlobBackend
# ---------------------------------------------------------------------------
def _run_sync(coro: Coroutine):
    """Run *coro* to completion from sync code.

    Uses ``asyncio.run`` when no loop is active; falls back to a worker
    thread when called from inside an existing loop (which would otherwise
    raise ``RuntimeError: asyncio.run() cannot be called from a running
    event loop``). The worker-thread path keeps the running loop free
    while the blob call blocks.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


class BlobBackend:
    """Sync MemoryBackend backed by an async :class:`BlobStore`.

    ``append_text`` is implemented as read+concat+put — fine for the
    daily JSONL files (a few KB) Phase 2 routes through this. The
    Phase 3 ``CachedBlobBackend`` will batch writes locally.
    """

    def __init__(self, store: BlobStore) -> None:
        self._store = store

    def read_text(self, key: str) -> str | None:
        try:
            data = _run_sync(self._store.get(key))
        except KeyError:
            return None
        return data.decode("utf-8")

    def read_text_with_etag(self, key: str) -> tuple[str | None, str | None]:
        try:
            data, etag = _run_sync(self._store.get_with_etag(key))
        except KeyError:
            return None, None
        return data.decode("utf-8"), etag

    def write_text(self, key: str, content: str, *, if_match: str | None = None) -> str | None:
        return _run_sync(
            self._store.put(key, content.encode("utf-8"), if_match=if_match or None)
        )

    def append_text(self, key: str, content: str) -> None:
        existing = self.read_text(key) or ""
        self.write_text(key, existing + content)

    def exists(self, key: str) -> bool:
        return bool(_run_sync(self._store.exists(key)))

    def list(self, prefix: str = "") -> list[str]:
        return list(_run_sync(self._store.list(prefix)))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def get_backend() -> MemoryBackend:
    """Return the configured MemoryBackend.

    Selection order (ADR-005 §"--keep-memory-local"):
      1. ``keep_memory_local`` flag set → :class:`LocalBackend` (escape hatch).
      2. ``blob_endpoint`` AND ``blob_container`` set → :class:`BlobBackend`
         wrapping a :class:`BlobStore` whose token provider is the Agent
         User's storage-scope three-hop token.
      3. Neither set → :class:`LocalBackend` rooted at ``cfg.data_dir``.

    Half-configured cloud (exactly one of endpoint/container set) is a HARD
    error (:class:`BackendMisconfiguredError`), not a silent Local fallback
    (design F2). A silent fallback is how a mis-enved fleet instance lands on
    an empty local store and re-bootstraps every chat → replay flood; failing
    loud surfaces the misconfiguration instead of letting the fleet diverge.
    """
    cfg = get_config()
    if cfg.keep_memory_local:
        return LocalBackend(cfg.data_dir)
    if cfg.blob_endpoint and cfg.blob_container:
        store = BlobStore(
            endpoint=cfg.blob_endpoint,
            container=cfg.blob_container,
            token_provider=lambda: acquire_agent_user_storage_token(get_config()),
        )
        return BlobBackend(store)
    if cfg.blob_endpoint or cfg.blob_container:
        raise BackendMisconfiguredError(
            endpoint=cfg.blob_endpoint,
            container=cfg.blob_container,
        )
    return LocalBackend(cfg.data_dir)


def assert_backend_config(logger=None) -> dict[str, str | None]:
    """Resolve + validate the storage backend once at boot (design F2).

    Raises :class:`BackendMisconfiguredError` on a half-configured blob env
    (so a mis-enved instance refuses to start instead of silently using an
    empty Local store), and logs the resolved backend + container so operators
    can confirm fleet-wide agreement in production logs.

    Returns a small summary ``{"backend": ..., "container": ..., "root": ...}``
    for callers/tests. Never resolves the storage token — construction is
    lazy, so this is safe to call before auth.
    """
    cfg = get_config()
    backend = get_backend()  # raises on half-config
    if isinstance(backend, BlobBackend):
        summary: dict[str, str | None] = {
            "backend": "BlobBackend",
            "endpoint": cfg.blob_endpoint,
            "container": cfg.blob_container,
            "root": None,
        }
        if logger:
            logger.info(
                "Resolved MemoryBackend=BlobBackend endpoint=%s container=%s",
                cfg.blob_endpoint,
                cfg.blob_container,
            )
    else:
        summary = {
            "backend": "LocalBackend",
            "endpoint": None,
            "container": None,
            "root": str(cfg.data_dir),
        }
        if logger:
            logger.info("Resolved MemoryBackend=LocalBackend root=%s", cfg.data_dir)
    return summary
