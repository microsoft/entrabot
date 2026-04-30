"""Durable, identity-scoped outstanding-promises store.

One blob object per Agent Identity (``promises.jsonl``), append-only
JSONL. Each ``add_promise`` appends one "open" line; each
``resolve_promise`` appends a new line with the same ``id`` and
``status="resolved"``. ``list_promises`` reads the whole file and folds
by ``id`` — the last line per id wins.

Why JSONL-append instead of overwrite: tolerant of concurrent writers
and trivial to reason about. Compaction runs when the file exceeds
~1000 lines: keep every open promise, keep resolved-in-last-30-days,
drop everything else.

When the promises file lives in Azure Blob Storage, writes use
``BlobStore.put`` with ``if_match=<etag>`` for optimistic concurrency.
On ``ConcurrencyError`` the module re-reads and retries once; a second
conflict raises :class:`PromiseStoreConflict`.

When no conditional store is available (``LocalBackend`` or test
``MemoryBackend``) the module falls back to plain read+append via the
``MemoryBackend`` interface — ETag-less but still correct under a single
writer, which is the only realistic local scenario.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from entraclaw.config import get_config
from entraclaw.storage.backend import MemoryBackend, get_backend

if TYPE_CHECKING:
    from entraclaw.storage.blob import BlobStore

logger = logging.getLogger("entraclaw.tools.promises")

PROMISES_KEY = "promises.jsonl"

# Compaction thresholds — see module docstring.
_COMPACT_LINE_THRESHOLD = 1000
_RESOLVED_RETENTION_DAYS = 30


class PromiseNotFound(Exception):
    """Raised by ``resolve_promise`` when the id isn't in the store."""


class PromiseStoreConflict(Exception):
    """Raised when optimistic-concurrency retry is exhausted."""


@dataclass
class Promise:
    id: str
    created_at: str
    chat_id: str
    description: str
    due_by: str | None = None
    status: str = "open"
    resolved_at: str | None = None
    resolution: str | None = None

    @classmethod
    def from_entry(cls, entry: dict[str, Any]) -> Promise:
        return cls(
            id=entry["id"],
            created_at=entry["created_at"],
            chat_id=entry.get("chat_id", ""),
            description=entry.get("description", ""),
            due_by=entry.get("due_by"),
            status=entry.get("status", "open"),
            resolved_at=entry.get("resolved_at"),
            resolution=entry.get("resolution"),
        )

    def to_entry(self) -> dict[str, Any]:
        raw = asdict(self)
        # Drop keys whose value is None to keep the JSONL lean.
        return {k: v for k, v in raw.items() if v is not None}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_lines(raw: str | None) -> list[dict[str, Any]]:
    """Parse a JSONL payload, skipping corrupt lines."""
    if not raw:
        return []
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("skipping corrupt promise line: %s", line[:80])
    return out


def _fold_by_id(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return the latest entry per id (last write wins)."""
    folded: dict[str, dict[str, Any]] = {}
    for entry in entries:
        pid = entry.get("id")
        if not pid:
            continue
        folded[pid] = entry
    return folded


def _compact_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the log to (open) ∪ (resolved in last N days).

    The output is one entry per surviving id, each representing the
    current folded state.
    """
    folded = _fold_by_id(entries)
    cutoff = datetime.now(UTC) - timedelta(days=_RESOLVED_RETENTION_DAYS)
    kept: list[dict[str, Any]] = []
    for entry in folded.values():
        status = entry.get("status", "open")
        if status == "open":
            kept.append(entry)
            continue
        resolved_at = entry.get("resolved_at") or entry.get("created_at")
        try:
            ts = datetime.fromisoformat(resolved_at)
        except (TypeError, ValueError):
            continue
        if ts >= cutoff:
            kept.append(entry)
    return kept


# ---------------------------------------------------------------------------
# Conditional (ETag) write path
# ---------------------------------------------------------------------------
def _get_conditional_store() -> Any | None:
    """Return a BlobStore-shaped object for ETag-conditional writes.

    Returns None when the active backend doesn't support conditional
    writes (LocalBackend, test in-memory backends). Callers MUST handle
    the None case by falling back to the plain ``MemoryBackend`` path.
    """
    cfg = get_config()
    if cfg.keep_memory_local:
        return None
    if cfg.blob_endpoint and cfg.blob_container:
        from entraclaw.storage.blob import BlobStore
        from entraclaw.tools.teams import acquire_agent_user_storage_token

        return _ConditionalBlobAdapter(
            BlobStore(
                endpoint=cfg.blob_endpoint,
                container=cfg.blob_container,
                token_provider=lambda: acquire_agent_user_storage_token(get_config()),
            )
        )
    return None


class _ConditionalBlobAdapter:
    """Narrow BlobStore → (get→(bytes, etag), put with if_match) adapter.

    Kept as a thin shim so tests can stub ``_get_conditional_store`` with
    a ``ETaggedBackend`` that exposes the same shape without needing a
    full httpx mock.
    """

    def __init__(self, store: BlobStore) -> None:
        self._store = store

    async def get(self, path: str) -> tuple[bytes, str]:
        # Use a HEAD-then-GET pattern? httpx's AsyncClient already returns
        # the ETag on the GET response, but BlobStore.get drops it. Do one
        # raw GET here to preserve it.
        import httpx

        async with httpx.AsyncClient() as client:
            url = self._store._url(path)  # noqa: SLF001 — thin shim
            resp = await client.get(url, headers=self._store._headers())  # noqa: SLF001
            if resp.status_code == 404:
                return b"", ""
            if resp.status_code == 401:
                from entraclaw.errors import TokenExpiredError

                raise TokenExpiredError("Storage token expired or missing scope")
            resp.raise_for_status()
            return resp.content, resp.headers.get("ETag", "")

    async def put(
        self,
        path: str,
        data: bytes,
        *,
        if_match: str | None = None,
    ) -> str:
        return await self._store.put(path, data, if_match=if_match or None)


async def _run_conditional_write(
    mutator,
) -> tuple[list[dict[str, Any]], Promise | None]:
    """Read → mutate → write-with-If-Match, retry once on contention.

    ``mutator`` is a callable ``(entries: list[dict]) -> (new_entries,
    promise_or_none)`` that produces the post-mutation state. Returns
    the final entries and whatever promise ``mutator`` emitted.

    Raises :class:`PromiseStoreConflict` when retry is exhausted.
    """
    from entraclaw.storage.blob import ConcurrencyError

    store = _get_conditional_store()
    assert store is not None  # only called when conditional path is available

    for attempt in range(2):
        raw_bytes, etag = await store.get(PROMISES_KEY)
        entries = _parse_lines(raw_bytes.decode("utf-8") if raw_bytes else "")
        new_entries, result = mutator(entries)
        payload = "".join(json.dumps(e) + "\n" for e in new_entries)
        try:
            await store.put(
                PROMISES_KEY,
                payload.encode("utf-8"),
                if_match=etag or None,
            )
            return new_entries, result
        except ConcurrencyError:
            if attempt == 0:
                logger.warning("promises.jsonl ETag mismatch; re-reading and retrying")
                continue
            raise PromiseStoreConflict(
                "promises.jsonl: optimistic concurrency retry exhausted"
            ) from None
    raise PromiseStoreConflict(  # defensive — loop always returns/raises
        "promises.jsonl: unreachable"
    )


# ---------------------------------------------------------------------------
# Plain (non-conditional) write path
# ---------------------------------------------------------------------------
def _load_entries(backend: MemoryBackend) -> list[dict[str, Any]]:
    return _parse_lines(backend.read_text(PROMISES_KEY))


def _needs_compaction(entries: list[dict[str, Any]]) -> bool:
    return len(entries) >= _COMPACT_LINE_THRESHOLD


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def add_promise(
    *,
    chat_id: str,
    description: str,
    due_by: str | None = None,
    backend: MemoryBackend | None = None,
) -> Promise:
    """Record a new outstanding promise. Returns the persisted Promise."""
    if not chat_id:
        raise ValueError("chat_id is required")
    if not description or not description.strip():
        raise ValueError("description is required")

    promise = Promise(
        id=uuid.uuid4().hex,
        created_at=_now_iso(),
        chat_id=chat_id,
        description=description.strip(),
        due_by=due_by,
        status="open",
    )

    if backend is None:
        store = _get_conditional_store()
        if store is not None:

            def mutator(
                entries: list[dict[str, Any]],
            ) -> tuple[list[dict[str, Any]], Promise]:
                new = list(entries)
                new.append(promise.to_entry())
                if _needs_compaction(new):
                    new = _compact_entries(new)
                return new, promise

            await _run_conditional_write(mutator)
            return promise
        backend = get_backend()

    entries = _load_entries(backend)
    if _needs_compaction(entries + [promise.to_entry()]):
        compacted = _compact_entries(entries + [promise.to_entry()])
        payload = "".join(json.dumps(e) + "\n" for e in compacted)
        backend.write_text(PROMISES_KEY, payload)
    else:
        backend.append_text(PROMISES_KEY, json.dumps(promise.to_entry()) + "\n")
    return promise


async def list_promises(
    *,
    open_only: bool = True,
    backend: MemoryBackend | None = None,
) -> list[Promise]:
    """Return the current set of promises, folded by id."""
    if backend is None:
        store = _get_conditional_store()
        if store is not None:
            raw_bytes, _etag = await store.get(PROMISES_KEY)
            entries = _parse_lines(raw_bytes.decode("utf-8") if raw_bytes else "")
        else:
            backend = get_backend()
            entries = _load_entries(backend)
    else:
        entries = _load_entries(backend)

    folded = _fold_by_id(entries)
    promises = [Promise.from_entry(e) for e in folded.values()]
    if open_only:
        promises = [p for p in promises if p.status == "open"]
    return promises


async def resolve_promise(
    *,
    promise_id: str,
    resolution: str,
    backend: MemoryBackend | None = None,
) -> Promise:
    """Mark ``promise_id`` resolved. Idempotent. Raises PromiseNotFound."""
    if not promise_id:
        raise ValueError("promise_id is required")
    if not resolution or not resolution.strip():
        raise ValueError("resolution is required")

    now = _now_iso()
    resolution = resolution.strip()

    if backend is None:
        store = _get_conditional_store()
        if store is not None:
            captured: dict[str, Promise] = {}

            def mutator(
                entries: list[dict[str, Any]],
            ) -> tuple[list[dict[str, Any]], Promise]:
                folded = _fold_by_id(entries)
                if promise_id not in folded:
                    raise PromiseNotFound(promise_id)
                current = folded[promise_id]
                if current.get("status") == "resolved":
                    captured["promise"] = Promise.from_entry(current)
                    return entries, captured["promise"]
                resolved_entry = {
                    **current,
                    "status": "resolved",
                    "resolved_at": now,
                    "resolution": resolution,
                }
                new = list(entries) + [resolved_entry]
                if _needs_compaction(new):
                    new = _compact_entries(new)
                captured["promise"] = Promise.from_entry(resolved_entry)
                return new, captured["promise"]

            await _run_conditional_write(mutator)
            return captured["promise"]
        backend = get_backend()

    entries = _load_entries(backend)
    folded = _fold_by_id(entries)
    if promise_id not in folded:
        raise PromiseNotFound(promise_id)
    current = folded[promise_id]
    if current.get("status") == "resolved":
        return Promise.from_entry(current)
    resolved_entry = {
        **current,
        "status": "resolved",
        "resolved_at": now,
        "resolution": resolution,
    }
    new_entries = entries + [resolved_entry]
    if _needs_compaction(new_entries):
        compacted = _compact_entries(new_entries)
        payload = "".join(json.dumps(e) + "\n" for e in compacted)
        backend.write_text(PROMISES_KEY, payload)
    else:
        backend.append_text(PROMISES_KEY, json.dumps(resolved_entry) + "\n")
    return Promise.from_entry(resolved_entry)
