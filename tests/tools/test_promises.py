"""Tests for durable promise store — outstanding human-facing commitments.

Promises live in ONE blob object per Agent Identity (``promises.jsonl``),
append-only JSONL. Each ``add_promise`` appends an "open" line; each
``resolve_promise`` appends a new line with the same ``id`` and
``status="resolved"``. ``list_promises`` folds the file by taking the
last line per id.

Storage model rules under test:
- ETag optimistic concurrency on blob writes; one retry on contention.
- Compaction kicks in when the JSONL exceeds ~1000 lines: open promises
  plus resolved-in-last-30-days are preserved, everything else is folded.
- ``resolve_promise`` on an unknown id raises ``PromiseNotFound``.
- ``resolve_promise`` on an already-resolved id is idempotent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from entraclaw.tools.promises import (
    PROMISES_KEY,
    Promise,
    PromiseNotFound,
    PromiseStoreConflict,
    add_promise,
    list_promises,
    resolve_promise,
)


class InMemoryBackend:
    """Tiny dict-backed MemoryBackend for tests.

    Does not simulate ETag contention on its own — tests that need
    ETag semantics use the ``ETaggedBackend`` variant below.
    """

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def read_text(self, key: str) -> str | None:
        return self._store.get(key)

    def write_text(self, key: str, content: str) -> None:
        self._store[key] = content

    def append_text(self, key: str, content: str) -> None:
        self._store[key] = (self._store.get(key) or "") + content

    def exists(self, key: str) -> bool:
        return key in self._store

    def list(self, prefix: str = "") -> list[str]:
        return [k for k in self._store if k.startswith(prefix)]


# ---------------------------------------------------------------------------
# add_promise
# ---------------------------------------------------------------------------
class TestAddPromise:
    @pytest.mark.asyncio
    async def test_appends_open_jsonl_line(self) -> None:
        backend = InMemoryBackend()
        promise = await add_promise(
            chat_id="c1",
            description="announce PR landing",
            backend=backend,
        )
        assert isinstance(promise, Promise)
        assert promise.chat_id == "c1"
        assert promise.description == "announce PR landing"
        assert promise.status == "open"
        assert promise.id  # uuid4 hex
        assert promise.created_at  # ISO-8601

        # Stored as JSONL — one line, parseable.
        raw = backend.read_text(PROMISES_KEY)
        assert raw is not None
        lines = [line for line in raw.splitlines() if line.strip()]
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["id"] == promise.id
        assert entry["chat_id"] == "c1"
        assert entry["description"] == "announce PR landing"
        assert entry["status"] == "open"

    @pytest.mark.asyncio
    async def test_due_by_recorded_when_set(self) -> None:
        backend = InMemoryBackend()
        promise = await add_promise(
            chat_id="c1",
            description="ship the fix",
            due_by="2026-04-25T17:00:00+00:00",
            backend=backend,
        )
        assert promise.due_by == "2026-04-25T17:00:00+00:00"
        entry = json.loads(backend.read_text(PROMISES_KEY).splitlines()[0])
        assert entry["due_by"] == "2026-04-25T17:00:00+00:00"

    @pytest.mark.asyncio
    async def test_multiple_adds_append(self) -> None:
        backend = InMemoryBackend()
        p1 = await add_promise(chat_id="c1", description="first", backend=backend)
        p2 = await add_promise(chat_id="c2", description="second", backend=backend)
        lines = [line for line in backend.read_text(PROMISES_KEY).splitlines() if line.strip()]
        assert len(lines) == 2
        ids = [json.loads(line)["id"] for line in lines]
        assert ids == [p1.id, p2.id]


# ---------------------------------------------------------------------------
# list_promises
# ---------------------------------------------------------------------------
class TestListPromises:
    @pytest.mark.asyncio
    async def test_empty_when_no_file(self) -> None:
        backend = InMemoryBackend()
        assert await list_promises(backend=backend) == []

    @pytest.mark.asyncio
    async def test_open_only_filters_resolved(self) -> None:
        backend = InMemoryBackend()
        p1 = await add_promise(chat_id="c1", description="first", backend=backend)
        p2 = await add_promise(chat_id="c2", description="second", backend=backend)
        await resolve_promise(promise_id=p1.id, resolution="shipped", backend=backend)

        open_only = await list_promises(backend=backend, open_only=True)
        assert len(open_only) == 1
        assert open_only[0].id == p2.id
        assert open_only[0].status == "open"

    @pytest.mark.asyncio
    async def test_all_includes_resolved(self) -> None:
        backend = InMemoryBackend()
        p1 = await add_promise(chat_id="c1", description="first", backend=backend)
        p2 = await add_promise(chat_id="c2", description="second", backend=backend)
        await resolve_promise(promise_id=p1.id, resolution="shipped", backend=backend)

        all_promises = await list_promises(backend=backend, open_only=False)
        assert len(all_promises) == 2
        by_id = {p.id: p for p in all_promises}
        assert by_id[p1.id].status == "resolved"
        assert by_id[p1.id].resolution == "shipped"
        assert by_id[p2.id].status == "open"

    @pytest.mark.asyncio
    async def test_last_line_wins_per_id(self) -> None:
        """Multiple entries for same id → most recent reflects state."""
        backend = InMemoryBackend()
        p = await add_promise(chat_id="c1", description="first", backend=backend)
        # Manually craft a second "update" line to simulate a resolve.
        newer = json.dumps(
            {
                "id": p.id,
                "created_at": p.created_at,
                "chat_id": "c1",
                "description": "first",
                "status": "resolved",
                "resolved_at": "2026-04-20T12:00:00+00:00",
                "resolution": "wrapped up",
            }
        )
        backend.append_text(PROMISES_KEY, newer + "\n")

        all_promises = await list_promises(backend=backend, open_only=False)
        assert len(all_promises) == 1
        assert all_promises[0].status == "resolved"
        assert all_promises[0].resolution == "wrapped up"


# ---------------------------------------------------------------------------
# resolve_promise
# ---------------------------------------------------------------------------
class TestResolvePromise:
    @pytest.mark.asyncio
    async def test_appends_resolved_entry(self) -> None:
        backend = InMemoryBackend()
        promise = await add_promise(chat_id="c1", description="ship it", backend=backend)
        resolved = await resolve_promise(
            promise_id=promise.id,
            resolution="PR #42 merged",
            backend=backend,
        )
        assert resolved.id == promise.id
        assert resolved.status == "resolved"
        assert resolved.resolution == "PR #42 merged"
        assert resolved.resolved_at

        lines = [line for line in backend.read_text(PROMISES_KEY).splitlines() if line.strip()]
        assert len(lines) == 2
        # Final line carries the resolution.
        final = json.loads(lines[-1])
        assert final["id"] == promise.id
        assert final["status"] == "resolved"
        assert final["resolution"] == "PR #42 merged"

    @pytest.mark.asyncio
    async def test_unknown_id_raises(self) -> None:
        backend = InMemoryBackend()
        with pytest.raises(PromiseNotFound):
            await resolve_promise(
                promise_id="does-not-exist",
                resolution="nope",
                backend=backend,
            )

    @pytest.mark.asyncio
    async def test_idempotent_on_already_resolved(self) -> None:
        backend = InMemoryBackend()
        promise = await add_promise(chat_id="c1", description="ship it", backend=backend)
        first = await resolve_promise(
            promise_id=promise.id,
            resolution="done",
            backend=backend,
        )
        second = await resolve_promise(
            promise_id=promise.id,
            resolution="still done",
            backend=backend,
        )
        # Idempotent: returns the existing resolved Promise, no new line.
        assert second.id == promise.id
        assert second.status == "resolved"
        assert second.resolution == first.resolution == "done"

        lines = [line for line in backend.read_text(PROMISES_KEY).splitlines() if line.strip()]
        assert len(lines) == 2  # open + resolved, not a third.


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------
class TestCompaction:
    @pytest.mark.asyncio
    async def test_compacts_when_over_threshold(self) -> None:
        """>1000 lines + a new write → compact-and-write retains open +
        resolved-in-last-30-days."""
        backend = InMemoryBackend()

        now = datetime.now(UTC)
        old_resolved_at = (now - timedelta(days=60)).isoformat()
        recent_resolved_at = (now - timedelta(days=5)).isoformat()

        lines: list[str] = []
        # 800 ancient resolved (should be dropped by compaction).
        for i in range(800):
            pid = f"old-{i:04d}"
            lines.append(
                json.dumps(
                    {
                        "id": pid,
                        "created_at": old_resolved_at,
                        "chat_id": "c1",
                        "description": f"old {i}",
                        "status": "open",
                    }
                )
            )
            lines.append(
                json.dumps(
                    {
                        "id": pid,
                        "created_at": old_resolved_at,
                        "chat_id": "c1",
                        "description": f"old {i}",
                        "status": "resolved",
                        "resolved_at": old_resolved_at,
                        "resolution": "ancient",
                    }
                )
            )
        # 100 recent resolved (kept — inside 30-day window).
        for i in range(100):
            pid = f"recent-{i:04d}"
            lines.append(
                json.dumps(
                    {
                        "id": pid,
                        "created_at": recent_resolved_at,
                        "chat_id": "c1",
                        "description": f"recent {i}",
                        "status": "open",
                    }
                )
            )
            lines.append(
                json.dumps(
                    {
                        "id": pid,
                        "created_at": recent_resolved_at,
                        "chat_id": "c1",
                        "description": f"recent {i}",
                        "status": "resolved",
                        "resolved_at": recent_resolved_at,
                        "resolution": "recent done",
                    }
                )
            )
        # 3 open (kept).
        for i in range(3):
            pid = f"open-{i:04d}"
            lines.append(
                json.dumps(
                    {
                        "id": pid,
                        "created_at": recent_resolved_at,
                        "chat_id": "c1",
                        "description": f"open {i}",
                        "status": "open",
                    }
                )
            )
        backend.write_text(PROMISES_KEY, "\n".join(lines) + "\n")
        pre_count = len(backend.read_text(PROMISES_KEY).splitlines())
        assert pre_count > 1000

        # Trigger: the next add should compact on its way through.
        new_promise = await add_promise(
            chat_id="c1",
            description="new after compaction",
            backend=backend,
        )

        post_lines = [line for line in backend.read_text(PROMISES_KEY).splitlines() if line.strip()]
        # Should now carry: 100 recent resolved + 3 open + 1 new = 104.
        # Ancient resolved should be gone.
        assert len(post_lines) == 104
        entries = [json.loads(line) for line in post_lines]
        ids = {e["id"] for e in entries}
        assert new_promise.id in ids
        assert not any(i.startswith("old-") for i in ids)
        for i in range(100):
            assert f"recent-{i:04d}" in ids
        for i in range(3):
            assert f"open-{i:04d}" in ids


# ---------------------------------------------------------------------------
# ETag optimistic concurrency
# ---------------------------------------------------------------------------
class ETaggedBackend:
    """Backend wrapping BlobStore semantics: writes carry an ``if_match``.

    Simulates one round of ETag contention. The first write after
    ``trip=True`` raises ``ConcurrencyError``; the second succeeds.

    The promise module writes via ``BlobBackend``'s underlying ``BlobStore``
    directly for conditional writes — this stand-in exposes the same
    async ``get`` / ``put`` + ``if_match`` surface.
    """

    def __init__(self) -> None:
        self._data: bytes = b""
        self._etag: str = ""
        self.trip_next = False
        self.put_calls: list[tuple[bytes, str | None]] = []

    async def get(self, path: str) -> tuple[bytes, str]:
        return self._data, self._etag

    async def put(
        self,
        path: str,
        data: bytes,
        *,
        if_match: str | None = None,
    ) -> str:
        from entraclaw.storage.blob import ConcurrencyError

        self.put_calls.append((data, if_match))
        if self.trip_next:
            self.trip_next = False
            # Simulate a concurrent writer bumping the ETag under us.
            self._etag = "etag-bumped"
            raise ConcurrencyError("simulated 412")
        self._data = data
        self._etag = f"etag-{len(self.put_calls)}"
        return self._etag


class TestETagContention:
    @pytest.mark.asyncio
    async def test_retry_on_412_succeeds(self) -> None:
        """When a conditional put hits 412, the module re-reads the blob
        and retries once. Second try succeeds."""
        from entraclaw.tools import promises as promises_mod

        fake_store = ETaggedBackend()

        # Seed one existing open line so add_promise re-reads correctly.
        seed_line = json.dumps(
            {
                "id": "seed",
                "created_at": "2026-04-01T00:00:00+00:00",
                "chat_id": "c1",
                "description": "seed",
                "status": "open",
            }
        )
        fake_store._data = (seed_line + "\n").encode("utf-8")
        fake_store._etag = "etag-0"
        fake_store.trip_next = True

        with patch.object(promises_mod, "_get_conditional_store", lambda: fake_store):
            promise = await add_promise(
                chat_id="c2",
                description="after contention",
                backend=None,  # conditional path kicks in
            )

        # Two put attempts: one failed with ConcurrencyError, one succeeded.
        assert len(fake_store.put_calls) == 2
        # Final blob data carries both the seed and the new promise.
        final_lines = fake_store._data.decode("utf-8").splitlines()
        ids = [json.loads(line)["id"] for line in final_lines if line.strip()]
        assert "seed" in ids
        assert promise.id in ids

    @pytest.mark.asyncio
    async def test_exhausted_retry_raises(self) -> None:
        """After one failed retry, a second 412 raises PromiseStoreConflict."""
        from entraclaw.storage.blob import ConcurrencyError
        from entraclaw.tools import promises as promises_mod

        class AlwaysConflictBackend:
            async def get(self, path):
                return b"", ""

            async def put(self, path, data, *, if_match=None):
                raise ConcurrencyError("always conflicts")

        with (
            patch.object(
                promises_mod,
                "_get_conditional_store",
                lambda: AlwaysConflictBackend(),
            ),
            pytest.raises(PromiseStoreConflict),
        ):
            await add_promise(
                chat_id="c1",
                description="doomed",
                backend=None,
            )
