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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from entrabot.config import get_config
from entrabot.storage.blob import BlobStore
from entrabot.tools.teams import acquire_agent_user_storage_token

if TYPE_CHECKING:
    from collections.abc import Coroutine


@runtime_checkable
class MemoryBackend(Protocol):
    """Sync key→text store for agent state.

    Keys are forward-slash separated paths (e.g. ``"interactions/2026-04-17.jsonl"``).
    Implementations decide where each key actually lives.
    """

    def read_text(self, key: str) -> str | None:
        """Return text at *key*, or ``None`` if it doesn't exist."""
        ...

    def write_text(self, key: str, content: str) -> None:
        """Replace *key*'s content with *content*. Creates parents as needed."""
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

    def write_text(self, key: str, content: str) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

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

    def write_text(self, key: str, content: str) -> None:
        _run_sync(self._store.put(key, content.encode("utf-8")))

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
      3. Otherwise → :class:`LocalBackend` rooted at ``cfg.data_dir``.

    Half-configured cloud (endpoint without container, or vice versa) is
    treated as not-configured rather than raising — the local fallback is
    safer for the hot path.
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
    return LocalBackend(cfg.data_dir)
