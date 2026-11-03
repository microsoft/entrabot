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
import contextlib
import hashlib
import logging
import os
import re
import secrets
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from entrabot.config import get_config
from entrabot.errors import BackendMisconfiguredError
from entrabot.storage.blob import BlobStore, ConcurrencyError
from entrabot.tools.teams import (
    acquire_agent_user_storage_token,
    invalidate_agent_user_storage_token,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine

logger = logging.getLogger("entrabot.storage.backend")

# Held across ``LocalBackend.write_text``, so the ETag check and the write that follows
# it are one step as far as every other thread in this process is concerned. Without it
# two writers holding the same ETag can both pass the check, and the later write replaces
# the earlier one. Module-level on purpose: ``get_backend()`` builds a new instance per
# call, so a lock on the instance would guard nothing. It does not reach across
# processes; two processes sharing one data directory can still race, a known limit of
# the local backend. ``BlobBackend`` needs none of this, because the service makes the
# conditional write atomic.
_LOCAL_WRITE_LOCK = threading.Lock()


def _content_etag(content: str) -> str:
    """Deterministic content-hash ETag for local files.

    Mirrors Azure Blob ETag semantics closely enough for optimistic
    concurrency: identical content → identical ETag; any change → different
    ETag. Used by :class:`LocalBackend` so the same CAS code path works for
    both local and blob storage.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _check_write_conditions(if_match: str | None, if_absent: bool) -> None:
    """Refuse a contradictory write: exists-with-this-ETag AND does-not-exist."""
    if if_match is not None and if_absent:
        raise ValueError("write_text: if_match and if_absent are mutually exclusive")


# A write goes to a temp file beside its key and is then moved into place (see
# ``_publish_atomically``). The name is ``.tmp-`` and 16 hex digits. It leaves the key's name
# out on purpose: a key near the 255-byte file name limit would leave no room for it.
# ``LocalBackend.list`` hides anything shaped like it, because a list taken during a write
# can see the temp file in flight, and a process that dies mid-write leaves one behind.
_TEMP_FILE_NAME = re.compile(r"\.tmp-[0-9a-f]{16}")

# Readers take no lock, and Python opens files without FILE_SHARE_DELETE, so on Windows a
# replace fails with PermissionError while something is reading the file. Reads are short, so
# it is retried briefly. Elsewhere that error means what it says, so it isn't.
_REPLACE_CAN_BE_BLOCKED = sys.platform == "win32"
_REPLACE_ATTEMPTS = 5
_REPLACE_RETRY_SECONDS = 0.05


def is_temp_file_name(name: str) -> bool:
    """True if a file name is a write's temp file rather than a key of its own."""
    return _TEMP_FILE_NAME.fullmatch(name) is not None


def _replace_into_place(temp: Path, path: Path) -> None:
    """``os.replace``, allowing for a Windows reader that has *path* open for a moment."""
    for attempt in range(1, _REPLACE_ATTEMPTS + 1):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if not _REPLACE_CAN_BE_BLOCKED or attempt == _REPLACE_ATTEMPTS:
                raise
            time.sleep(_REPLACE_RETRY_SECONDS)


def _publish_atomically(path: Path, content: str, key: str, *, create_only: bool) -> None:
    """Make *content* visible at *path* in one step: all of it, or nothing.

    Readers take no lock, so a value written in place can be read empty or half written,
    and an overwrite that fails partway truncates what was there. Instead *content* is
    written and synced to a temp file in the same directory, then moved into place:

    * Overwrite: ``os.replace``, which is atomic. The target's permission bits are copied
      onto the temp file first, once it is written, so a mode the operator chose (wider or
      narrower than the temp file's own) survives the replacement.
    * Create-only: ``os.link``, which is atomic too and fails if the target exists. That
      ``FileExistsError`` becomes the ``ConcurrencyError`` callers expect. A volume
      without hard links raises another ``OSError``, and :func:`_create_exclusively`
      does the job there instead.

    The temp file is created owner-only (0o600) rather than with the process umask, so a
    value the operator restricted is never readable by others while it is being written, and a
    crash can't leave a readable copy behind. A key created this way is 0o600 as well.

    The temp file is always removed, so a failed write leaves the old value (or no key)
    and nothing else behind. The caller holds ``_LOCAL_WRITE_LOCK``. The text encoding is
    the default one, exactly as ``Path.write_text`` used.
    """
    temp = path.with_name(f".tmp-{secrets.token_hex(8)}")
    try:
        # Owner-only from the first byte, and exclusive so an existing name is refused. O_BINARY
        # exists only on Windows, where it stops the C runtime translating newlines a second time
        # under the text wrapper (``open`` adds it itself).
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        if create_only:
            try:
                os.link(temp, path)
            except FileExistsError as exc:
                raise ConcurrencyError(f"write_text({key!r}) refused: key exists") from exc
            except OSError as exc:
                logger.debug(
                    "Could not hard-link %r (%s); creating it exclusively", key, type(exc).__name__
                )
                _create_exclusively(path, content, key)
        else:
            with contextlib.suppress(FileNotFoundError):
                shutil.copymode(path, temp)
            _replace_into_place(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass  # os.replace has already moved it into place.
        except OSError as unlink_exc:
            # Report it rather than mask the error that got us here.
            logger.warning(
                "Could not remove the temp file left by a write of %r: %s: %s",
                key,
                type(unlink_exc).__name__,
                unlink_exc,
            )


def _create_exclusively(path: Path, content: str, key: str) -> None:
    """Create *path* holding *content*, refusing if it already exists.

    This is the fallback for a volume that can't hard-link (see ``_publish_atomically``).
    The exclusive create publishes an empty file before any content is written. If
    the write or the close then fails, the file this call created is removed and the
    error re-raised, so a later attempt can create the key instead of every reader
    finding an empty value that nothing can replace. A file that was already there is
    never touched: that case raises ``ConcurrencyError`` before anything is created.
    """
    try:
        # O_EXCL: atomic create-only, and owner-only like the temp file. Closed below, because
        # the create and the write fail differently and only the second may remove the file.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    except FileExistsError as exc:
        raise ConcurrencyError(f"write_text({key!r}) refused: key exists") from exc
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
    except BaseException:
        try:
            path.unlink()
        except OSError as unlink_exc:
            # Report it, because now the empty file stays and blocks later creates.
            logger.warning(
                "Could not remove the empty file left by a failed write of %r: %s: %s",
                key,
                type(unlink_exc).__name__,
                unlink_exc,
            )
        raise


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

    def write_text(
        self,
        key: str,
        content: str,
        *,
        if_match: str | None = None,
        if_absent: bool = False,
    ) -> str | None:
        """Replace *key*'s content with *content*. Creates parents as needed.

        When ``if_match`` is provided, the write is conditional: it succeeds
        only if *key*'s current ETag equals ``if_match``, otherwise it raises
        :class:`entrabot.storage.blob.ConcurrencyError`. ``if_absent=True`` is
        the compare-and-swap for a key that doesn't exist yet: it creates *key*
        and raises ``ConcurrencyError`` if another writer got there first. The
        two are mutually exclusive. With neither, the write is an unconditional
        overwrite. Returns the new ETag (or ``None`` for backends that don't
        track one).
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

    def write_text(
        self,
        key: str,
        content: str,
        *,
        if_match: str | None = None,
        if_absent: bool = False,
    ) -> str | None:
        _check_write_conditions(if_match, if_absent)
        p = self._path(key)
        # Every mode takes the lock, not just the conditional one: an unconditional
        # write landing between a compare-and-swap's check and its write would be
        # replaced just the same.
        with _LOCAL_WRITE_LOCK:
            if if_match is not None:
                # Conditional write: current content hash must equal if_match.
                current = _content_etag(p.read_text()) if p.exists() else None
                if current != if_match:
                    raise ConcurrencyError(
                        f"write_text({key!r}) refused: If-Match={if_match!r} is stale "
                        f"(current={current!r})"
                    )
            p.parent.mkdir(parents=True, exist_ok=True)
            _publish_atomically(p, content, key, create_only=if_absent)
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
            if not f.is_file() or is_temp_file_name(f.name):
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

    def write_text(
        self,
        key: str,
        content: str,
        *,
        if_match: str | None = None,
        if_absent: bool = False,
    ) -> str | None:
        _check_write_conditions(if_match, if_absent)
        data = content.encode("utf-8")
        if if_absent:
            return _run_sync(self._store.put(key, data, if_none_match="*"))
        return _run_sync(self._store.put(key, data, if_match=if_match or None))

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
            on_unauthorized=lambda rejected: invalidate_agent_user_storage_token(
                get_config(), rejected_token=rejected
            ),
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
