"""Tests for chat-cursor wiring in :mod:`entrabot.mcp_server`.

Covers the rehydration + persistence behavior added to fix issue #17:

- ``_register_watched_chat`` / ``_init_poll`` rehydrate a watched chat's
  in-memory ``chat_state`` from the persisted cursor when it exists and is
  fresh, skipping ``_bootstrap_chat``.
- A stale cursor (older than the staleness cap) falls through to the
  bootstrap path — better to re-baseline than to surface a 3-day-old message
  as live.
- A debounced save fires after the poll loop advances ``last_ts`` /
  ``seen_ids``, and per-chat saves are independent.
- ``_flush_chat_cursors`` writes every dirty chat to the backend on graceful
  shutdown.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from entrabot import mcp_server
from entrabot.tools import chat_cursors


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Each test gets a fresh data_dir, clean blob env, clean _state.

    ``_register_watched_chat`` calls ``_ensure_poll_task_running`` which
    eagerly invokes ``asyncio.get_event_loop().create_task(...)``. In sync
    test contexts running under pytest there is no current loop (especially
    after earlier async tests have torn theirs down). Install a fresh one
    for the duration of the test so the sync-shaped tests can drive the
    registration helper without crashing on the eager poll-task spawn.
    """
    monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ENTRABOT_BLOB_ENDPOINT", raising=False)
    monkeypatch.delenv("ENTRABOT_BLOB_CONTAINER", raising=False)
    monkeypatch.delenv("ENTRABOT_KEEP_MEMORY_LOCAL", raising=False)
    # The config is cached in _state["config"]; clear so tests pick up the
    # freshly-overridden ENTRABOT_DATA_DIR.
    mcp_server._state.pop("config", None)
    mcp_server._state.pop("watched_chats", None)
    mcp_server._state.pop("_dirty_cursor_chats", None)
    mcp_server._state.pop("_cursor_save_tasks", None)
    mcp_server._state.pop("poll_task", None)
    # Ensure a current event loop exists. asyncio_mode=auto creates one for
    # @pytest.mark.asyncio tests but not for sync tests in this module.
    try:
        asyncio.get_event_loop()
        installed_loop = None
    except RuntimeError:
        installed_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(installed_loop)
    yield
    mcp_server._state.pop("config", None)
    mcp_server._state.pop("watched_chats", None)
    mcp_server._state.pop("_dirty_cursor_chats", None)
    mcp_server._state.pop("_cursor_save_tasks", None)
    mcp_server._state.pop("poll_task", None)
    if installed_loop is not None:
        installed_loop.close()
        asyncio.set_event_loop(None)


# ---------------------------------------------------------------------------
# Rehydration via _register_watched_chat
# ---------------------------------------------------------------------------
class TestRehydrateOnRegister:
    def test_no_cursor_present_leaves_state_unbootstrapped(self, tmp_path) -> None:
        """ABSENT (successful empty read) → new chat, may bootstrap-push once."""
        mcp_server._register_watched_chat("19:fresh@thread.v2", persist=False)
        state = mcp_server._state["watched_chats"]["19:fresh@thread.v2"]
        # Default fresh state: not bootstrapped, empty seen, no last_ts, and NOT
        # flagged for re-resolution (it resolved cleanly to ABSENT).
        assert state["bootstrapped"] is False
        assert state["last_ts"] is None
        assert state["seen_ids"] == set()
        assert state.get("needs_resolution") is False

    def test_fresh_cursor_present_rehydrates_and_skips_bootstrap(
        self, tmp_path
    ) -> None:
        """A fresh cursor → rehydrate ``chat_state`` and mark as bootstrapped."""
        recent = (datetime.now(UTC) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        chat_cursors.save_cursor(
            "19:rehydrate@thread.v2",
            {
                "last_ts": recent,
                "seen_ids_tail": ["msg-a", "msg-b"],
                "bootstrapped": True,
            },
        )

        mcp_server._register_watched_chat("19:rehydrate@thread.v2", persist=False)

        state = mcp_server._state["watched_chats"]["19:rehydrate@thread.v2"]
        assert state["bootstrapped"] is True
        assert state["last_ts"] == recent
        assert state["seen_ids"] == {"msg-a", "msg-b"}

    def test_stale_cursor_rehydrates_for_catch_up(self, tmp_path) -> None:
        """F4: a stale-but-present cursor rehydrates (NOT re-bootstrap).

        The steady-state timestamp gate does catch-up (surfaces only messages
        newer than ``last_ts``); an idle chat with no new messages emits
        nothing. Re-bootstrapping a stale cursor is exactly the replay-flood
        bug this fix removes.
        """
        old = (
            datetime.now(UTC)
            - timedelta(seconds=chat_cursors.CURSOR_STALENESS_SECONDS + 3600)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        chat_cursors.save_cursor(
            "19:stale@thread.v2",
            {
                "last_ts": old,
                "seen_ids_tail": ["msg-a"],
                "bootstrapped": True,
            },
        )

        mcp_server._register_watched_chat("19:stale@thread.v2", persist=False)

        state = mcp_server._state["watched_chats"]["19:stale@thread.v2"]
        # Present cursor → rehydrate + catch up; never re-baseline-and-push.
        assert state["bootstrapped"] is True
        assert state["last_ts"] == old
        assert state["seen_ids"] == {"msg-a"}
        assert state.get("needs_resolution") is False

    def test_corrupt_cursor_marks_needs_resolution(self, tmp_path) -> None:
        """F1: corrupt JSON is ambiguous → fail closed, do NOT bootstrap-push.

        A partial write could be hiding a live cursor. The chat is flagged
        ``needs_resolution`` so the poll loop re-reads before delivering
        anything — it must never surface the newest message on a corrupt read.
        """
        from entrabot.storage.backend import get_backend

        get_backend().write_text(
            chat_cursors.cursor_key("19:corrupt@thread.v2"),
            "{not-valid-json",
        )

        mcp_server._register_watched_chat("19:corrupt@thread.v2", persist=False)

        state = mcp_server._state["watched_chats"]["19:corrupt@thread.v2"]
        assert state["bootstrapped"] is False
        assert state["last_ts"] is None
        assert state["needs_resolution"] is True

    def test_read_error_marks_needs_resolution(self, tmp_path, monkeypatch) -> None:
        """F1 core: a transient read failure must fail closed, never bootstrap.

        This is the exact fleet-replay trigger — a blob 401/timeout/throttle at
        registration used to fall through to ``_bootstrap_chat`` and push the
        newest (weeks-old) message. Now it flags ``needs_resolution``.
        """
        import entrabot.tools.chat_cursors as cc

        class ExplodingBackend:
            def read_text(self, key: str) -> str | None:
                raise OSError("simulated transient blob read failure")

        monkeypatch.setattr(cc, "get_backend", lambda: ExplodingBackend())

        mcp_server._register_watched_chat("19:boom@thread.v2", persist=False)

        state = mcp_server._state["watched_chats"]["19:boom@thread.v2"]
        assert state["bootstrapped"] is False
        assert state["needs_resolution"] is True


# ---------------------------------------------------------------------------
# Poll loop — fail-closed handling of a needs_resolution chat (F1)
# ---------------------------------------------------------------------------
class TestPollFailClosed:
    """The per-chat poll step must never push while a chat is unresolved.

    A chat flagged ``needs_resolution`` (transient read failure or corrupt
    cursor at registration) must re-read the cursor and, until it resolves,
    push NOTHING and NOT bootstrap. This is the security-relevant fail-closed
    guarantee: an ambiguous read never re-injects stale messages.
    """

    @pytest.mark.asyncio
    async def test_unresolved_chat_does_not_push_or_bootstrap(
        self, monkeypatch
    ) -> None:
        chat_id = "19:unresolved@thread.v2"
        chat_state = {
            "seen_ids": set(),
            "last_ts": None,
            "bootstrapped": False,
            "needs_resolution": True,
        }
        mcp_server._state["watched_chats"] = {chat_id: chat_state}

        # Cursor read still failing → resolve_cursor returns UNRESOLVED.
        import entrabot.tools.chat_cursors as cc

        monkeypatch.setattr(
            cc,
            "resolve_cursor",
            lambda cid: cc.CursorResolution(cc.CursorOutcome.UNRESOLVED, None),
        )

        pushed: list = []
        bootstrapped: list = []
        monkeypatch.setattr(
            mcp_server,
            "_push_channel_notification",
            _async_recorder(pushed),
        )
        monkeypatch.setattr(mcp_server, "_bootstrap_chat", _async_recorder(bootstrapped))

        await mcp_server._poll_watched_chat(chat_id, chat_state, "EntraBot Agent")

        assert pushed == []
        assert bootstrapped == []
        # Still unresolved → stays flagged for retry next cycle.
        assert chat_state["needs_resolution"] is True
        assert chat_state["bootstrapped"] is False

    @pytest.mark.asyncio
    async def test_unresolved_chat_rehydrates_when_read_recovers(
        self, monkeypatch
    ) -> None:
        chat_id = "19:recovers@thread.v2"
        chat_state = {
            "seen_ids": set(),
            "last_ts": None,
            "bootstrapped": False,
            "needs_resolution": True,
        }
        mcp_server._state["watched_chats"] = {chat_id: chat_state}

        recovered = {
            "last_ts": "2026-06-09T18:00:00Z",
            "seen_ids_tail": ["m1", "m2"],
            "bootstrapped": True,
        }
        import entrabot.tools.chat_cursors as cc

        monkeypatch.setattr(
            cc,
            "resolve_cursor",
            lambda cid: cc.CursorResolution(cc.CursorOutcome.PRESENT, recovered),
        )

        pushed: list = []
        bootstrapped: list = []
        monkeypatch.setattr(
            mcp_server, "_push_channel_notification", _async_recorder(pushed)
        )
        monkeypatch.setattr(mcp_server, "_bootstrap_chat", _async_recorder(bootstrapped))

        await mcp_server._poll_watched_chat(chat_id, chat_state, "EntraBot Agent")

        # Resolution cycle rehydrates but does NOT push (fail-closed): the
        # steady-state gate handles delivery on the NEXT cycle.
        assert pushed == []
        assert bootstrapped == []
        assert chat_state["needs_resolution"] is False
        assert chat_state["bootstrapped"] is True
        assert chat_state["last_ts"] == "2026-06-09T18:00:00Z"
        assert chat_state["seen_ids"] == {"m1", "m2"}


def _async_recorder(sink: list):
    async def _rec(*args, **kwargs):
        sink.append((args, kwargs))

    return _rec


# ---------------------------------------------------------------------------
# Marking a chat dirty + flushing
# ---------------------------------------------------------------------------
class TestMarkDirtyAndFlush:
    def test_mark_dirty_records_chat(self) -> None:
        mcp_server._register_watched_chat("19:dirty@thread.v2", persist=False)
        state = mcp_server._state["watched_chats"]["19:dirty@thread.v2"]
        state["last_ts"] = "2026-06-09T18:00:00Z"
        state["seen_ids"] = {"msg-x"}

        mcp_server._mark_cursor_dirty("19:dirty@thread.v2")

        dirty = mcp_server._state.get("_dirty_cursor_chats", set())
        assert "19:dirty@thread.v2" in dirty

    def test_flush_writes_every_dirty_chat_through_backend(self) -> None:
        mcp_server._register_watched_chat("chat-a", persist=False)
        mcp_server._register_watched_chat("chat-b", persist=False)
        mcp_server._state["watched_chats"]["chat-a"]["last_ts"] = "2026-06-09T18:00:00Z"
        mcp_server._state["watched_chats"]["chat-a"]["seen_ids"] = {"a1", "a2"}
        mcp_server._state["watched_chats"]["chat-a"]["bootstrapped"] = True
        mcp_server._state["watched_chats"]["chat-b"]["last_ts"] = "2026-06-09T18:30:00Z"
        mcp_server._state["watched_chats"]["chat-b"]["seen_ids"] = {"b1"}
        mcp_server._state["watched_chats"]["chat-b"]["bootstrapped"] = True
        mcp_server._mark_cursor_dirty("chat-a")
        mcp_server._mark_cursor_dirty("chat-b")

        mcp_server._flush_chat_cursors()

        a = chat_cursors.load_cursor("chat-a")
        b = chat_cursors.load_cursor("chat-b")
        assert a is not None and b is not None
        assert a["last_ts"] == "2026-06-09T18:00:00Z"
        assert set(a["seen_ids_tail"]) == {"a1", "a2"}
        assert b["last_ts"] == "2026-06-09T18:30:00Z"
        assert b["seen_ids_tail"] == ["b1"]
        # Dirty set is cleared after flush.
        assert not mcp_server._state.get("_dirty_cursor_chats")

    def test_flush_swallows_backend_errors_per_chat(self, monkeypatch) -> None:
        """One chat's write failure must not block other chats' flushes."""
        mcp_server._register_watched_chat("chat-ok", persist=False)
        mcp_server._register_watched_chat("chat-bad", persist=False)
        mcp_server._state["watched_chats"]["chat-ok"]["last_ts"] = "2026-06-09T18:00:00Z"
        mcp_server._state["watched_chats"]["chat-ok"]["seen_ids"] = {"ok1"}
        mcp_server._state["watched_chats"]["chat-ok"]["bootstrapped"] = True
        mcp_server._state["watched_chats"]["chat-bad"]["last_ts"] = "2026-06-09T18:30:00Z"
        mcp_server._state["watched_chats"]["chat-bad"]["bootstrapped"] = True
        mcp_server._mark_cursor_dirty("chat-ok")
        mcp_server._mark_cursor_dirty("chat-bad")

        real_save = chat_cursors.save_cursor

        def maybe_explode(chat_id: str, state: dict) -> None:
            if chat_id == "chat-bad":
                raise OSError("simulated disk failure")
            return real_save(chat_id, state)

        monkeypatch.setattr(mcp_server, "_chat_cursor_save", maybe_explode)

        # Must not raise.
        mcp_server._flush_chat_cursors()

        # The healthy chat still got persisted.
        assert chat_cursors.load_cursor("chat-ok") is not None


# ---------------------------------------------------------------------------
# Debounced async save
# ---------------------------------------------------------------------------
class TestDebouncedSave:
    @pytest.mark.asyncio
    async def test_schedule_save_writes_after_debounce_window(
        self, monkeypatch
    ) -> None:
        """A scheduled save fires after the debounce delay, not synchronously."""
        # Make the debounce window negligible so the test isn't slow.
        monkeypatch.setattr(mcp_server, "CURSOR_SAVE_DEBOUNCE_SECONDS", 0.05)

        mcp_server._register_watched_chat("chat-debounce", persist=False)
        st = mcp_server._state["watched_chats"]["chat-debounce"]
        st["last_ts"] = "2026-06-09T18:00:00Z"
        st["seen_ids"] = {"x"}
        st["bootstrapped"] = True

        mcp_server._schedule_cursor_save("chat-debounce")

        # Not yet written — debounce in flight.
        # (Allow a tiny yield in case the test runner is slow.)
        await asyncio.sleep(0)
        # Wait past the debounce window.
        await asyncio.sleep(0.2)

        loaded = chat_cursors.load_cursor("chat-debounce")
        assert loaded is not None
        assert loaded["last_ts"] == "2026-06-09T18:00:00Z"

    @pytest.mark.asyncio
    async def test_multiple_schedules_coalesce_to_one_write(
        self, monkeypatch
    ) -> None:
        """Burst of schedule calls in the debounce window → exactly one save."""
        monkeypatch.setattr(mcp_server, "CURSOR_SAVE_DEBOUNCE_SECONDS", 0.1)

        call_count = {"n": 0}
        real_save = chat_cursors.save_cursor

        def counting_save(chat_id: str, state: dict) -> None:
            call_count["n"] += 1
            return real_save(chat_id, state)

        monkeypatch.setattr(mcp_server, "_chat_cursor_save", counting_save)

        mcp_server._register_watched_chat("chat-burst", persist=False)
        st = mcp_server._state["watched_chats"]["chat-burst"]
        st["last_ts"] = "2026-06-09T18:00:00Z"
        st["seen_ids"] = {"x"}
        st["bootstrapped"] = True

        # Fire several schedules in quick succession.
        for _ in range(10):
            mcp_server._schedule_cursor_save("chat-burst")
            await asyncio.sleep(0)

        # Wait past the debounce.
        await asyncio.sleep(0.25)

        assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# _serialize_chat_state — deterministic seen_ids_tail
# ---------------------------------------------------------------------------
class TestSerializeChatStateDeterministic:
    """Regression for PR #18 review comment #1.

    ``seen_ids`` is a Python ``set`` in memory; iteration order is not
    guaranteed across runs (depends on insertion order, hash collisions, and
    Python's hash randomization). When the serializer slices ``[-50:]`` to
    bound the tail, a non-deterministic ``list(seen)`` can drop the actual
    most-recent IDs that the overlap-window dedupe needs after restart —
    causing duplicate notifications.

    Teams message IDs are numeric-as-string (e.g. ``"1781031555261"``) so
    lexicographic sort on the string is monotonic with sent-at; that's the
    correct ordering key.
    """

    def test_seen_ids_tail_keeps_highest_value_ids_when_over_cap(self) -> None:
        """Over-cap set must yield the lexicographically-highest IDs as the tail."""
        # Construct a set larger than the 50-id cap with a mix of low and high
        # numeric IDs in arbitrary insertion order.
        high_ids = [f"178103155{i:04d}" for i in range(60)]
        # Shuffle insertion to make it more likely that list(set) ordering
        # would NOT happen to produce the right tail by accident.
        scrambled_order = high_ids[30:] + high_ids[:30]
        seen = set(scrambled_order)
        chat_state = {
            "last_ts": "2026-06-09T18:59:15.261Z",
            "seen_ids": seen,
            "bootstrapped": True,
        }

        serialized = mcp_server._serialize_chat_state(chat_state)
        tail = serialized["seen_ids_tail"]

        # The tail must be bounded to 50 (matches MAX_SEEN_IDS_TAIL).
        assert len(tail) == 50
        # The tail must end with the 50 highest IDs — the ones we actually
        # need to dedupe against in the 2-second overlap window after restart.
        expected_highest = sorted(high_ids)[-50:]
        assert tail == expected_highest

    def test_seen_ids_tail_is_deterministic_across_calls(self) -> None:
        """Same input set → identical serialized output, every time."""
        # Mix of IDs designed to exercise hash collisions / insertion-order
        # sensitivity. Build the same set twice via different insertion paths.
        ids = [f"id-{i:03d}" for i in range(100)]
        a = set()
        for i in ids:
            a.add(i)
        b = set()
        for i in reversed(ids):
            b.add(i)
        result_a = mcp_server._serialize_chat_state(
            {"last_ts": "t", "seen_ids": a, "bootstrapped": True}
        )
        result_b = mcp_server._serialize_chat_state(
            {"last_ts": "t", "seen_ids": b, "bootstrapped": True}
        )
        assert result_a["seen_ids_tail"] == result_b["seen_ids_tail"]


# ---------------------------------------------------------------------------
# _schedule_cursor_save sync fallback — dirty-set cleanup
# ---------------------------------------------------------------------------
class TestScheduleCursorSaveSyncFallback:
    """Regression for PR #18 review comment #2.

    When ``_schedule_cursor_save`` runs without a current event loop (test
    contexts / shutdown), it falls back to a synchronous backend write. The
    async branch removes the chat from ``_dirty_cursor_chats`` after a
    successful write; the sync fallback must mirror that behavior or the
    chat stays "dirty" forever, causing redundant flush attempts.
    """

    def test_sync_fallback_clears_dirty_set_on_success(self, monkeypatch) -> None:
        # Pre-register the chat and stage some advanced state.
        mcp_server._register_watched_chat("chat-syncfb", persist=False)
        st = mcp_server._state["watched_chats"]["chat-syncfb"]
        st["last_ts"] = "2026-06-09T18:00:00Z"
        st["seen_ids"] = {"x"}
        st["bootstrapped"] = True

        # Force the no-running-loop fallback path. ``_schedule_cursor_save``
        # calls ``asyncio.get_event_loop()`` and treats ``RuntimeError`` as
        # the no-loop signal.
        import asyncio as _asyncio

        def boom() -> None:
            raise RuntimeError("no running event loop")

        monkeypatch.setattr(_asyncio, "get_event_loop", boom)

        mcp_server._schedule_cursor_save("chat-syncfb")

        # After a successful sync write the chat must NOT remain dirty.
        dirty = mcp_server._state.get("_dirty_cursor_chats") or set()
        assert "chat-syncfb" not in dirty
        # And the write itself must have landed.
        assert chat_cursors.load_cursor("chat-syncfb") is not None
