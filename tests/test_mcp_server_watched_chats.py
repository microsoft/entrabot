"""Tests for the shared watched-chat list in :mod:`entrabot.mcp_server`.

The list follows the configured backend. In cloud mode it never touches local
files, so a machine switched to another agent's container can't keep polling the
old agent's chats, and instances sharing a container share one list.

Blob calls are slow, and a past bug starved the MCP ``initialize`` handshake past
the host's timeout, so nothing here may run backend I/O on the event loop. The
``blob_calls_on_event_loop`` fixture proves that.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from entrabot import mcp_server
from entrabot.config import get_config
from entrabot.errors import BackendMisconfiguredError
from entrabot.identity.sponsors import SponsorGate
from entrabot.storage.backend import BlobBackend
from entrabot.storage.blob import ConcurrencyError
from entrabot.tools import chat_cursors, poll_state
from entrabot.tools.chat_cursors import CursorOutcome, CursorResolution
from entrabot.tools.poll_state import WATCHED_CHATS_KEY, read_watched_chats

CHAT_A = "19:chat-a@thread.v2"
CHAT_B = "19:chat-b@thread.v2"
OLD_AGENT_CHAT = "19:old-agent-chat@thread.v2"
ABSENT = CursorResolution(CursorOutcome.ABSENT, None)


@pytest.fixture(autouse=True)
def _server_state(monkeypatch: pytest.MonkeyPatch):
    old_state = mcp_server._state.copy()
    old_identity = mcp_server._identity
    old_logger = mcp_server.logger
    mcp_server._state.clear()
    mcp_server._state["config"] = get_config()
    mcp_server._identity = None
    mcp_server.logger = logging.getLogger("entrabot.mcp_server")
    monkeypatch.setattr(mcp_server, "_ensure_poll_task_running", lambda: None)
    yield
    mcp_server._state.clear()
    mcp_server._state.update(old_state)
    mcp_server._identity = old_identity
    mcp_server.logger = old_logger


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "agent-data"
    monkeypatch.setenv("ENTRABOT_DATA_DIR", str(path))
    return path


def _seed_list(store, *chat_ids: str) -> str:
    etag = BlobBackend(store).write_text(WATCHED_CHATS_KEY, "\n".join(chat_ids) + "\n")
    assert etag
    return etag


def _stored_list(store) -> list[str]:
    return store.data[WATCHED_CHATS_KEY].decode().splitlines()


async def _drain_persist_tasks() -> None:
    """Let any scheduled persist task and its worker thread finish."""
    for _ in range(50):
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


# ---------------------------------------------------------------------------
# _init_poll
# ---------------------------------------------------------------------------
class TestInitPollLoadsTheSharedList:
    async def test_cloud_list_is_loaded_and_stale_local_file_is_ignored(
        self, fake_cloud_store, data_dir: Path, blob_calls_on_event_loop
    ) -> None:
        """The near-miss: a machine on a new container still had 68 old chats."""
        _seed_list(fake_cloud_store, CHAT_A, CHAT_B)
        blob_calls_on_event_loop.clear()
        data_dir.mkdir(parents=True)
        (data_dir / "watched_chats").write_text(f"{OLD_AGENT_CHAT}\n")

        await mcp_server._init_poll()

        assert set(mcp_server._state["watched_chats"]) == {CHAT_A, CHAT_B}
        assert (data_dir / "watched_chats").read_text() == f"{OLD_AGENT_CHAT}\n"
        assert blob_calls_on_event_loop == []

    async def test_local_mode_loads_the_existing_file(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True)
        (data_dir / "watched_chats").write_text(f"{CHAT_A}\n{CHAT_B}\n")
        await mcp_server._init_poll()
        assert set(mcp_server._state["watched_chats"]) == {CHAT_A, CHAT_B}

    async def test_absent_list_starts_empty_with_no_retry(self, fake_cloud_store) -> None:
        await mcp_server._init_poll()
        assert mcp_server._state["watched_chats"] == {}
        assert mcp_server._state.get("_watched_list_retry_task") is None

    async def test_the_list_read_runs_off_the_loop(self, monkeypatch) -> None:
        loop_thread = threading.get_ident()
        read_threads: list[int] = []

        def slow_read() -> poll_state.WatchedChatsRead:
            read_threads.append(threading.get_ident())
            time.sleep(0.3)  # simulate a blocking blob read
            return poll_state.WatchedChatsRead(poll_state.PollStateOutcome.ABSENT)

        monkeypatch.setattr(poll_state, "read_watched_chats", slow_read)
        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        hb = asyncio.create_task(heartbeat())
        try:
            await mcp_server._init_poll()
        finally:
            hb.cancel()

        assert read_threads and loop_thread not in read_threads
        assert ticks >= 3, "event loop was blocked during the list read"

    async def test_unresolved_list_loads_nothing_and_never_falls_back_to_local(
        self, fake_cloud_store, data_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="entrabot.mcp_server")
        data_dir.mkdir(parents=True)
        (data_dir / "watched_chats").write_text(f"{OLD_AGENT_CHAT}\n")

        with patch.object(
            fake_cloud_store, "get_with_etag", AsyncMock(side_effect=OSError("outage"))
        ):
            await mcp_server._init_poll()

        task = mcp_server._state["_watched_list_retry_task"]
        task.cancel()
        assert mcp_server._state["watched_chats"] == {}
        assert "will retry" in caplog.text

    async def test_retry_registers_the_chats_once_the_read_recovers(
        self, fake_cloud_store, monkeypatch
    ) -> None:
        _seed_list(fake_cloud_store, CHAT_A)
        mcp_server._state["watched_chats"] = {}
        real_read = poll_state.read_watched_chats
        reads = {"n": 0}

        def flaky_read() -> poll_state.WatchedChatsRead:
            reads["n"] += 1
            if reads["n"] == 1:
                return poll_state.WatchedChatsRead(poll_state.PollStateOutcome.UNRESOLVED)
            return real_read()

        monkeypatch.setattr(poll_state, "read_watched_chats", flaky_read)
        monkeypatch.setattr(mcp_server, "WATCHED_LIST_RETRY_INTERVAL", 0)

        await asyncio.wait_for(mcp_server._background_retry_watched_list(), timeout=5)

        assert reads["n"] == 2
        assert set(mcp_server._state["watched_chats"]) == {CHAT_A}

    async def test_retry_keeps_chats_registered_in_the_meantime(self, fake_cloud_store) -> None:
        """Discovery may have registered a chat, with its live cursor state, while
        the list was unreadable. A late load must not reset that state."""
        _seed_list(fake_cloud_store, CHAT_A, CHAT_B)
        live = {"seen_ids": {"m1"}, "last_ts": "t", "bootstrapped": True}
        mcp_server._state["watched_chats"] = {CHAT_A: live}

        assert await mcp_server._load_persisted_watched_chats() is True

        assert mcp_server._state["watched_chats"][CHAT_A] is live
        assert CHAT_B in mcp_server._state["watched_chats"]

    async def test_legacy_copies_are_logged_once_at_info_in_cloud_mode(
        self, fake_cloud_store, data_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="entrabot.mcp_server")
        data_dir.mkdir(parents=True)
        (data_dir / "watched_chats").write_text(f"{OLD_AGENT_CHAT}\n")
        (data_dir / "email_cursor.txt").write_text("2026-05-01T00:00:00Z")

        await mcp_server._init_poll()

        lines = [r for r in caplog.records if "ignor" in r.getMessage().lower()]
        assert len(lines) == 1
        assert lines[0].levelno == logging.INFO
        assert "watched_chats" in lines[0].getMessage()
        assert "email_cursor.txt" in lines[0].getMessage()
        assert OLD_AGENT_CHAT not in caplog.text, "the file's content is never read"

    async def test_no_legacy_line_without_legacy_copies(
        self, fake_cloud_store, data_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="entrabot.mcp_server")
        await mcp_server._init_poll()
        assert not [r for r in caplog.records if "ignor" in r.getMessage().lower()]

    async def test_no_legacy_line_in_local_mode(
        self, data_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="entrabot.mcp_server")
        data_dir.mkdir(parents=True)
        (data_dir / "watched_chats").write_text(f"{CHAT_A}\n")
        await mcp_server._init_poll()
        assert not [r for r in caplog.records if "ignor" in r.getMessage().lower()]

    async def test_half_configured_backend_fails_before_any_poll_setup(self, monkeypatch) -> None:
        """Poll setup now depends on the backend, so a mis-enved instance must fail
        loud first instead of spawning background tasks against nothing."""
        monkeypatch.setenv("ENTRABOT_BLOB_ENDPOINT", "https://fake-account.invalid")
        monkeypatch.delenv("ENTRABOT_BLOB_CONTAINER", raising=False)
        # Local mode wins over a half-configured backend, and the Windows CI job runs
        # the whole suite with it on. Don't depend on what the environment holds.
        monkeypatch.delenv("ENTRABOT_KEEP_MEMORY_LOCAL", raising=False)
        monkeypatch.setattr(mcp_server, "_init_auth", AsyncMock())
        init_poll = AsyncMock()
        monkeypatch.setattr(mcp_server, "_init_poll", init_poll)

        with pytest.raises(BackendMisconfiguredError):
            await mcp_server._initialize_locked()

        init_poll.assert_not_awaited()


# ---------------------------------------------------------------------------
# _register_watched_chat(persist=True)
# ---------------------------------------------------------------------------
class TestRegisterPersists:
    async def test_write_runs_off_the_loop_and_lands(
        self, fake_cloud_store, blob_calls_on_event_loop
    ) -> None:
        mcp_server._register_watched_chat(CHAT_A, resolution=ABSENT)

        assert CHAT_A in mcp_server._state["watched_chats"], "registered at once, in memory"
        await _drain_persist_tasks()

        assert blob_calls_on_event_loop == []
        assert _stored_list(fake_cloud_store) == [CHAT_A]
        assert not mcp_server._state.get("_dirty_watched_chats")

    async def test_cloud_mode_writes_no_local_file(self, fake_cloud_store, data_dir: Path) -> None:
        mcp_server._register_watched_chat(CHAT_A, resolution=ABSENT)
        await _drain_persist_tasks()
        assert not (data_dir / "watched_chats").exists()

    async def test_local_mode_writes_the_existing_path(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True)
        (data_dir / "watched_chats").write_text(f"{CHAT_B}\n")
        mcp_server._register_watched_chat(CHAT_A, resolution=ABSENT)
        await _drain_persist_tasks()
        assert (data_dir / "watched_chats").read_text() == f"{CHAT_A}\n{CHAT_B}\n"

    async def test_persist_false_writes_nothing(self, fake_cloud_store) -> None:
        mcp_server._register_watched_chat(CHAT_A, persist=False, resolution=ABSENT)
        await _drain_persist_tasks()
        assert WATCHED_CHATS_KEY not in fake_cloud_store.data

    async def test_no_config_writes_nothing(self, fake_cloud_store) -> None:
        mcp_server._state.pop("config")
        mcp_server._register_watched_chat(CHAT_A, resolution=ABSENT)
        await _drain_persist_tasks()
        assert WATCHED_CHATS_KEY not in fake_cloud_store.data

    async def test_merges_with_chats_another_instance_added(self, fake_cloud_store) -> None:
        _seed_list(fake_cloud_store, CHAT_B)
        mcp_server._register_watched_chat(CHAT_A, resolution=ABSENT)
        await _drain_persist_tasks()
        assert _stored_list(fake_cloud_store) == [CHAT_A, CHAT_B]

    async def test_registration_during_a_write_lands_too(self, fake_cloud_store) -> None:
        mcp_server._register_watched_chat(CHAT_A, resolution=ABSENT)
        await asyncio.sleep(0)  # the task has started its first write
        mcp_server._register_watched_chat(CHAT_B, resolution=ABSENT)
        await _drain_persist_tasks()
        assert _stored_list(fake_cloud_store) == [CHAT_A, CHAT_B]
        assert not mcp_server._state.get("_dirty_watched_chats")

    def test_sync_fallback_without_a_running_loop(self, fake_cloud_store) -> None:
        mcp_server._register_watched_chat(CHAT_A, resolution=ABSENT)
        assert _stored_list(fake_cloud_store) == [CHAT_A]
        assert not mcp_server._state.get("_dirty_watched_chats")

    def test_sync_fallback_failure_stays_dirty(self, fake_cloud_store) -> None:
        with patch.object(fake_cloud_store, "put", AsyncMock(side_effect=OSError("outage"))):
            mcp_server._register_watched_chat(CHAT_A, resolution=ABSENT)
        assert mcp_server._state["_dirty_watched_chats"] == {CHAT_A}


class TestFailedPersistIsRetried:
    async def test_failed_write_stays_dirty_and_next_flush_retries(
        self, fake_cloud_store, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="entrabot.mcp_server")
        with patch.object(fake_cloud_store, "put", AsyncMock(side_effect=OSError("outage"))):
            mcp_server._register_watched_chat(CHAT_A, resolution=ABSENT)
            await _drain_persist_tasks()

        assert mcp_server._state["_dirty_watched_chats"] == {CHAT_A}
        assert CHAT_A in mcp_server._state["watched_chats"], "polling isn't affected"
        assert "retry" in caplog.text

        assert await mcp_server._persist_dirty_watched_chats() is True
        assert _stored_list(fake_cloud_store) == [CHAT_A]
        assert not mcp_server._state.get("_dirty_watched_chats")

    async def test_exhausted_cas_retries_do_not_raise_and_stay_dirty(
        self, fake_cloud_store
    ) -> None:
        """Discovery only registers chats not already in memory, so this chat would
        never reach the list unless the dirty set remembers it."""
        _seed_list(fake_cloud_store, CHAT_B)
        with patch.object(
            fake_cloud_store, "put", AsyncMock(side_effect=ConcurrencyError("simulated 412"))
        ):
            mcp_server._register_watched_chat(CHAT_A, resolution=ABSENT)
            await _drain_persist_tasks()
            assert await mcp_server._persist_dirty_watched_chats() is False

        assert mcp_server._state["_dirty_watched_chats"] == {CHAT_A}
        assert await mcp_server._persist_dirty_watched_chats() is True
        assert _stored_list(fake_cloud_store) == [CHAT_A, CHAT_B]

    def test_shutdown_flush_writes_dirty_chats(self, fake_cloud_store) -> None:
        mcp_server._state["_dirty_watched_chats"] = {CHAT_A}
        mcp_server._flush_watched_chats()
        assert _stored_list(fake_cloud_store) == [CHAT_A]
        assert not mcp_server._state["_dirty_watched_chats"]

    def test_shutdown_flush_failure_never_raises(
        self, fake_cloud_store, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="entrabot.mcp_server")
        mcp_server._state["_dirty_watched_chats"] = {CHAT_A}
        with patch.object(fake_cloud_store, "put", AsyncMock(side_effect=OSError("outage"))):
            mcp_server._flush_watched_chats()
        assert mcp_server._state["_dirty_watched_chats"] == {CHAT_A}
        assert CHAT_A in caplog.text or "watched" in caplog.text.lower()

    def test_shutdown_flush_with_nothing_dirty_touches_nothing(self, monkeypatch) -> None:
        monkeypatch.setattr(poll_state, "add_watched_chats", MagicMock(side_effect=AssertionError))
        mcp_server._flush_watched_chats()


class _FakeStdio:
    """``stdio_server()`` handing the runner two stand-in streams."""

    async def __aenter__(self) -> tuple[object, object]:
        return object(), object()

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class TestShutdownPersist:
    """The stdio runner saves the dirty watched chats on its way out, off the event loop.

    The write is storage I/O, so on the loop it froze every other task for as long as a slow
    backend took. It runs in a worker thread now, and the wait is bounded so a wedged backend
    can't hold shutdown open.
    """

    @pytest.fixture
    def runner(self, monkeypatch: pytest.MonkeyPatch):
        """``_run_stdio_with_write_stream`` against stubs whose server exits at once."""
        from entrabot import efferent_copy

        monkeypatch.setattr(mcp_server, "stdio_server", lambda: _FakeStdio())
        monkeypatch.setattr(efferent_copy, "discover_sinks", AsyncMock(return_value=[]))
        monkeypatch.setattr(efferent_copy, "install_into_fastmcp", MagicMock())
        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock())
        monkeypatch.setattr(mcp_server.mcp._mcp_server, "run", AsyncMock())
        return mcp_server._run_stdio_with_write_stream

    async def test_a_slow_persist_does_not_block_the_event_loop(self, runner, monkeypatch) -> None:
        mcp_server._state["_dirty_watched_chats"] = {CHAT_A}
        loop_ran = threading.Event()
        loop_was_free: list[bool] = []

        def slow_add_watched_chats(batch: list[str]) -> poll_state.WatchedChatsWrite:
            # The probe below can only run during this wait if the event loop is free.
            loop_was_free.append(loop_ran.wait(timeout=3))
            return poll_state.WatchedChatsWrite(persisted=True, added=tuple(batch))

        async def probe() -> None:
            loop_ran.set()

        monkeypatch.setattr(poll_state, "add_watched_chats", slow_add_watched_chats)
        probe_task = asyncio.create_task(probe())

        await runner()
        await probe_task

        assert loop_was_free == [True]
        assert not mcp_server._state["_dirty_watched_chats"]

    async def test_a_persist_that_outlasts_the_timeout_is_cut_off_with_a_warning(
        self, runner, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="entrabot.mcp_server")
        mcp_server._state["_dirty_watched_chats"] = {CHAT_A}
        monkeypatch.setattr(mcp_server, "SHUTDOWN_PERSIST_TIMEOUT_SECONDS", 0.05)
        release = threading.Event()
        finished = threading.Event()

        def wedged_add_watched_chats(batch: list[str]) -> poll_state.WatchedChatsWrite:
            release.wait(timeout=3)
            finished.set()
            return poll_state.WatchedChatsWrite(persisted=False)

        monkeypatch.setattr(poll_state, "add_watched_chats", wedged_add_watched_chats)
        try:
            await runner()  # returns although the write is still stuck
            assert not finished.is_set(), "shutdown waited for the stuck write"
        finally:
            release.set()  # let the worker thread go

        assert mcp_server._state["_dirty_watched_chats"] == {CHAT_A}, "still dirty, not lost"
        assert any(
            r.levelno == logging.WARNING and "shutdown" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param(RuntimeError("storage exploded"), id="error"),
            pytest.param(asyncio.CancelledError(), id="cancelled"),
        ],
    )
    async def test_a_failed_persist_does_not_stop_shutdown(
        self, runner, monkeypatch, caplog: pytest.LogCaptureFixture, failure: BaseException
    ) -> None:
        caplog.set_level(logging.WARNING, logger="entrabot.mcp_server")
        persist = AsyncMock(side_effect=failure)
        monkeypatch.setattr(mcp_server, "_persist_dirty_watched_chats", persist)

        await runner()  # must not raise

        persist.assert_awaited_once()
        assert any(
            r.levelno == logging.WARNING and "shutdown" in r.getMessage() for r in caplog.records
        )


class TestPersistAndTheCachedSponsorGate:
    """A rebuilt gate reads the stored list, so it must not be left caching a list
    that was missing a chat because the write hadn't landed (Learning #52)."""

    async def test_landing_a_new_chat_drops_a_gate_built_during_the_write(
        self, fake_cloud_store
    ) -> None:
        mcp_server._register_watched_chat(CHAT_A, resolution=ABSENT)
        # A gate rebuilt now reads a list that doesn't have CHAT_A yet.
        mcp_server._state["sponsor_gate"] = MagicMock(name="gate_built_too_early")
        await _drain_persist_tasks()
        assert mcp_server._state.get("sponsor_gate") is None

    async def test_relisting_a_chat_keeps_the_cached_gate(self, fake_cloud_store) -> None:
        _seed_list(fake_cloud_store, CHAT_A)
        mcp_server._state["watched_chats"] = {CHAT_A: {"seen_ids": set()}}
        gate = MagicMock(name="cached_gate")
        mcp_server._state["sponsor_gate"] = gate
        mcp_server._register_watched_chat(CHAT_A)
        await _drain_persist_tasks()
        assert mcp_server._state.get("sponsor_gate") is gate


def _generation() -> int:
    return mcp_server._state.get("sponsor_gate_generation", 0)


class TestSponsorGateGeneration:
    """Every place that drops the cached gate also moves a generation counter.

    Dropping the cache isn't enough on its own. A worker thread builds the gate while
    the event loop keeps registering chats, so a worker that read the list BEFORE a
    registration landed can finish AFTER the drop and cache its stale result. A
    federated sponsor in the new chat would then never be matched. Callers cache only
    if the generation they started under is still current.
    """

    def test_registering_a_new_chat_moves_the_generation(self) -> None:
        before = _generation()
        mcp_server._register_watched_chat(CHAT_A, persist=False, resolution=ABSENT)
        assert _generation() > before

    def test_registering_a_known_chat_leaves_the_generation_alone(self) -> None:
        mcp_server._state["watched_chats"] = {CHAT_A: {"seen_ids": set()}}
        before = _generation()
        mcp_server._register_watched_chat(CHAT_A, persist=False)
        assert _generation() == before

    async def test_a_write_that_lands_a_chat_moves_the_generation(self, fake_cloud_store) -> None:
        mcp_server._mark_watched_chats_dirty([CHAT_A])
        before = _generation()
        assert await mcp_server._persist_dirty_watched_chats() is True
        assert _generation() > before

    async def test_a_write_that_adds_nothing_leaves_the_generation_alone(
        self, fake_cloud_store
    ) -> None:
        _seed_list(fake_cloud_store, CHAT_A)
        mcp_server._mark_watched_chats_dirty([CHAT_A])
        before = _generation()
        assert await mcp_server._persist_dirty_watched_chats() is True
        assert _generation() == before

    def test_invalidating_reports_whether_a_gate_was_cached(self) -> None:
        assert mcp_server._invalidate_sponsor_gate() is False
        mcp_server._state["sponsor_gate"] = _gate()
        assert mcp_server._invalidate_sponsor_gate() is True
        assert "sponsor_gate" not in mcp_server._state


class TestLoadSponsorGate:
    """The one helper both gate load sites use."""

    @staticmethod
    async def _load(loader) -> SponsorGate:
        with patch("entrabot.identity.sponsors.load_agent_identity_sponsor_gate", loader):
            return await mcp_server._load_sponsor_gate(get_config())

    async def test_complete_gate_is_cached_and_returned(self) -> None:
        gate = _gate()
        assert await self._load(lambda _config: gate) is gate
        assert mcp_server._state["sponsor_gate"] is gate

    async def test_provisional_gate_is_returned_but_not_cached(self) -> None:
        gate = _gate(provisional=True)
        assert await self._load(lambda _config: gate) is gate
        assert "sponsor_gate" not in mcp_server._state

    async def test_gate_invalidated_while_loading_is_returned_but_not_cached(self) -> None:
        gate = _gate()

        def loader(_config) -> SponsorGate:
            mcp_server._invalidate_sponsor_gate()  # a registration lands mid-load
            return gate

        assert await self._load(loader) is gate
        assert "sponsor_gate" not in mcp_server._state

    async def test_a_real_registration_while_loading_is_not_overwritten_by_the_stale_gate(
        self,
    ) -> None:
        """The race itself: the worker has read the list, then a chat is registered
        and the cache dropped, then the worker finishes."""
        gate = _gate()

        def loader(_config) -> SponsorGate:
            mcp_server._register_watched_chat(CHAT_B, persist=False, resolution=ABSENT)
            return gate

        assert await self._load(loader) is gate
        assert "sponsor_gate" not in mcp_server._state

    async def test_invalidation_before_the_load_started_does_not_block_caching(self) -> None:
        mcp_server._invalidate_sponsor_gate()
        gate = _gate()
        assert await self._load(lambda _config: gate) is gate
        assert mcp_server._state["sponsor_gate"] is gate

    async def test_the_next_load_after_a_discarded_one_caches(self) -> None:
        calls = {"n": 0}
        gate = _gate()

        def loader(_config) -> SponsorGate:
            calls["n"] += 1
            if calls["n"] == 1:
                mcp_server._invalidate_sponsor_gate()
            return gate

        await self._load(loader)
        assert "sponsor_gate" not in mcp_server._state
        await self._load(loader)
        assert mcp_server._state["sponsor_gate"] is gate
        assert calls["n"] == 2

    async def test_load_failure_propagates_and_caches_nothing(self) -> None:
        def loader(_config) -> SponsorGate:
            raise RuntimeError("graph is down")

        with pytest.raises(RuntimeError, match="graph is down"):
            await self._load(loader)
        assert "sponsor_gate" not in mcp_server._state

    async def test_load_runs_off_the_loop(self) -> None:
        loop_thread = threading.get_ident()
        threads: list[int] = []

        def loader(_config) -> SponsorGate:
            threads.append(threading.get_ident())
            return _gate()

        await self._load(loader)
        assert threads and loop_thread not in threads


# ---------------------------------------------------------------------------
# create_chat
# ---------------------------------------------------------------------------
class TestCreateChat:
    @pytest.fixture(autouse=True)
    def _create_chat_stubs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_retry(fn, **kwargs):
            return {"chat_id": CHAT_A, "created_at": "2026-11-02T12:00:00Z"}

        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock())
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock())
        monkeypatch.setattr(mcp_server, "_with_token_retry", fake_retry)

    async def _create(self) -> None:
        await mcp_server.create_chat("alice@contoso.example", target_tenant_id="fake-tenant")

    async def test_no_blob_io_on_the_loop(self, fake_cloud_store, blob_calls_on_event_loop) -> None:
        await self._create()
        assert blob_calls_on_event_loop == []

    async def test_chat_is_in_the_list_before_the_tool_returns(self, fake_cloud_store) -> None:
        """The next call can rebuild the gate from the list, so the write must be
        done by then or a federated sponsor is never matched (Learning #52)."""
        await self._create()
        assert read_watched_chats().chat_ids == (CHAT_A,)
        assert CHAT_A in mcp_server._state["watched_chats"]

    async def test_write_failure_does_not_fail_the_tool(self, fake_cloud_store) -> None:
        with patch.object(fake_cloud_store, "put", AsyncMock(side_effect=OSError("outage"))):
            await self._create()
        assert CHAT_A in mcp_server._state["watched_chats"]
        assert mcp_server._state["_dirty_watched_chats"] == {CHAT_A}

    async def test_existing_cursor_is_rehydrated_off_the_loop(
        self, fake_cloud_store, blob_calls_on_event_loop
    ) -> None:
        await asyncio.to_thread(
            chat_cursors.save_cursor,
            CHAT_A,
            {"last_ts": "2026-11-01T00:00:00Z", "seen_ids_tail": ["m1"], "bootstrapped": True},
        )
        await self._create()
        assert mcp_server._state["watched_chats"][CHAT_A]["bootstrapped"] is True
        assert blob_calls_on_event_loop == []


# ---------------------------------------------------------------------------
# Sponsor gate callers
# ---------------------------------------------------------------------------
def _gate(*, provisional: bool = False) -> SponsorGate:
    return SponsorGate(
        user_ids=frozenset({"sponsor-id"}),
        upns=frozenset(),
        mails=frozenset(),
        provisional=provisional,
    )


_PICKED = {
    "chat_id": CHAT_A,
    "message_id": "m1",
    "sender": "alice@contoso.example",
    "sender_id": "sponsor-id",
    "sent_at": "2026-11-02T12:00:00Z",
    "content": "<p>hi</p>",
}


class TestWaitForSponsorDmGateLoading:
    @pytest.fixture(autouse=True)
    def _stubs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock())
        mcp_server._state["watched_chats"] = {CHAT_A: {}}
        mcp_server._state["wait_tool_dedup"] = deque()
        mcp_server._state["wait_tool_chat_type_cache"] = {CHAT_A: "oneOnOne"}

    async def _wait(self, loader) -> AsyncMock:
        wait_loop = AsyncMock(return_value=_PICKED)
        with (
            patch("entrabot.identity.sponsors.load_agent_identity_sponsor_gate", loader),
            patch("entrabot.tools.audit.log_event", MagicMock()),
            patch("entrabot.tools.wait_tool.wait_loop", wait_loop),
        ):
            await mcp_server.wait_for_sponsor_dm(timeout_seconds=1)
        return wait_loop

    async def test_gate_load_runs_off_the_loop(self) -> None:
        loop_thread = threading.get_ident()
        threads: list[int] = []

        def loader(_config) -> SponsorGate:
            threads.append(threading.get_ident())
            return _gate()

        await self._wait(loader)
        assert threads and loop_thread not in threads

    async def test_complete_gate_is_cached(self) -> None:
        gate = _gate()
        await self._wait(lambda _config: gate)
        assert mcp_server._state["sponsor_gate"] is gate

    async def test_provisional_gate_is_used_but_not_cached(self) -> None:
        """One blob blip must not pin a narrowed gate for the rest of the session,
        or a federated sponsor could never be matched until a restart."""
        await self._wait(lambda _config: _gate(provisional=True))
        assert "sponsor_gate" not in mcp_server._state

    async def test_gate_built_while_a_chat_was_registered_is_used_but_not_cached(self) -> None:
        """The worker read the list, a chat was registered and the cache dropped, and
        only then did the worker finish. Its gate is fine for this wait but is already
        stale, so the next wait must rebuild rather than reuse it."""
        gate = _gate()

        def loader(_config) -> SponsorGate:
            mcp_server._register_watched_chat(CHAT_B, persist=False, resolution=ABSENT)
            return gate

        wait_loop = await self._wait(loader)

        assert wait_loop.call_args.kwargs["gate"] is gate
        assert "sponsor_gate" not in mcp_server._state

    async def test_load_failure_is_audited_and_raised(self) -> None:
        def loader(_config) -> SponsorGate:
            raise RuntimeError("graph is down")

        log_event = MagicMock()
        with (
            patch("entrabot.identity.sponsors.load_agent_identity_sponsor_gate", loader),
            patch("entrabot.tools.audit.log_event", log_event),
            patch("entrabot.tools.wait_tool.wait_loop", AsyncMock(return_value=_PICKED)),
            pytest.raises(RuntimeError, match="graph is down"),
        ):
            await mcp_server.wait_for_sponsor_dm(timeout_seconds=1)

        reasons = [
            call.kwargs.get("metadata", {}).get("reason") for call in log_event.call_args_list
        ]
        assert "sponsor_gate_load_failed" in reasons
        assert "sponsor_gate" not in mcp_server._state


class TestSendTeamsMessageGateLoading:
    @pytest.fixture(autouse=True)
    def _stubs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_retry(fn, **kwargs):
            return {"message_id": "out-1", "sent_at": "2026-11-02T12:00:00Z"}

        monkeypatch.setattr(mcp_server, "_current_host", lambda: "copilot")
        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock())
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock())
        monkeypatch.setattr(mcp_server, "_with_token_retry", fake_retry)
        monkeypatch.setattr(mcp_server, "_log_interaction_safe", lambda **kw: None)
        monkeypatch.setattr(mcp_server, "_check_placeholder_discipline", lambda *a, **k: None)
        mcp_server._state["watched_chats"] = {CHAT_A: {}}
        mcp_server._state["wait_tool_dedup"] = deque()

    async def _send(self, loader) -> AsyncMock:
        wait_loop = AsyncMock(return_value=_PICKED)
        with (
            patch("entrabot.identity.sponsors.load_agent_identity_sponsor_gate", loader),
            patch("entrabot.tools.wait_tool.wait_loop", wait_loop),
        ):
            await mcp_server.send_teams_message("hello", chat_id=CHAT_A)
        return wait_loop

    async def test_gate_load_runs_off_the_loop(self) -> None:
        loop_thread = threading.get_ident()
        threads: list[int] = []

        def loader(_config) -> SponsorGate:
            threads.append(threading.get_ident())
            return _gate()

        await self._send(loader)
        assert threads and loop_thread not in threads

    async def test_provisional_gate_is_used_but_not_cached(self) -> None:
        await self._send(lambda _config: _gate(provisional=True))
        assert "sponsor_gate" not in mcp_server._state

    async def test_complete_gate_is_cached(self) -> None:
        gate = _gate()
        await self._send(lambda _config: gate)
        assert mcp_server._state["sponsor_gate"] is gate

    async def test_gate_built_while_a_chat_was_registered_is_used_but_not_cached(self) -> None:
        gate = _gate()

        def loader(_config) -> SponsorGate:
            mcp_server._register_watched_chat(CHAT_B, persist=False, resolution=ABSENT)
            return gate

        wait_loop = await self._send(loader)

        assert wait_loop.call_args.kwargs["gate"] is gate
        assert "sponsor_gate" not in mcp_server._state

    async def test_load_failure_returns_the_send_result_without_waiting(self) -> None:
        """The existing degrade-gracefully path survives the refactor."""

        def loader(_config) -> SponsorGate:
            raise RuntimeError("graph is down")

        wait_loop = await self._send(loader)

        wait_loop.assert_not_awaited()
        assert "sponsor_gate" not in mcp_server._state


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------
GRAPH_CHATS = "https://graph.microsoft.com/v1.0/me/chats"


def _next_link(token: str) -> str:
    return f"{GRAPH_CHATS}?$top=50&$skiptoken={token}"


def _page(*chat_ids: str, next_token: str | None = None) -> httpx.Response:
    body: dict = {"value": [{"id": cid} for cid in chat_ids]}
    if next_token:
        body["@odata.nextLink"] = _next_link(next_token)
    return httpx.Response(200, json=body)


def _writes_to_list(store_put_calls: list[str]) -> int:
    return store_put_calls.count(WATCHED_CHATS_KEY)


@pytest.fixture
def list_writes(fake_cloud_store, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the key of every write that reaches the store."""
    keys: list[str] = []
    real_put = fake_cloud_store.put

    async def recording_put(path: str, data: bytes, **kwargs) -> str:
        keys.append(path)
        return await real_put(path, data, **kwargs)

    monkeypatch.setattr(fake_cloud_store, "put", recording_put)
    return keys


class TestDiscoveryPagination:
    async def test_follows_next_link_and_registers_every_page_in_one_write(
        self, fake_cloud_store, list_writes, blob_calls_on_event_loop
    ) -> None:
        with respx.mock:
            route = respx.get(GRAPH_CHATS).mock(
                side_effect=[
                    _page("19:p1a@thread.v2", "19:p1b@thread.v2", next_token="two"),
                    _page("19:p2a@thread.v2", next_token="three"),
                    _page("19:p3a@thread.v2"),
                ]
            )
            count = await mcp_server._discover_chats_once("fake-token")

        expected = {
            "19:p1a@thread.v2",
            "19:p1b@thread.v2",
            "19:p2a@thread.v2",
            "19:p3a@thread.v2",
        }
        assert count == 4
        assert route.call_count == 3
        assert set(mcp_server._state["watched_chats"]) == expected
        assert set(_stored_list(fake_cloud_store)) == expected
        assert _writes_to_list(list_writes) == 1, "one batched write for the whole sweep"
        assert blob_calls_on_event_loop == []

    async def test_first_page_asks_for_50_and_later_pages_use_the_link_as_given(
        self, fake_cloud_store
    ) -> None:
        with respx.mock:
            route = respx.get(GRAPH_CHATS).mock(
                side_effect=[_page("19:a@thread.v2", next_token="tok-2"), _page("19:b@thread.v2")]
            )
            await mcp_server._discover_chats_once("fake-token")

        first, second = (call.request.url.params for call in route.calls)
        assert first["$top"] == "50"
        assert second["$skiptoken"] == "tok-2"
        for call in route.calls:
            assert call.request.headers["Authorization"] == "Bearer fake-token"

    async def test_respects_the_page_bound(
        self, fake_cloud_store, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A server that never stops paging can't keep a sweep running forever."""
        caplog.set_level(logging.WARNING, logger="entrabot.mcp_server")
        monkeypatch.setattr(mcp_server, "CHAT_DISCOVER_MAX_PAGES", 3)
        counter = {"n": 0}

        def endless(_request: httpx.Request) -> httpx.Response:
            counter["n"] += 1
            return _page(f"19:page-{counter['n']}@thread.v2", next_token=f"t{counter['n']}")

        with respx.mock:
            route = respx.get(GRAPH_CHATS).mock(side_effect=endless)
            count = await mcp_server._discover_chats_once("fake-token")

        assert route.call_count == 3
        assert count == 3, "what was fetched before the bound is still registered"
        assert "page" in caplog.text.lower()

    def test_default_page_bound(self) -> None:
        assert mcp_server.CHAT_DISCOVER_MAX_PAGES == 20

    async def test_error_key_on_a_200_stops_but_keeps_earlier_pages(
        self, fake_cloud_store, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="entrabot.mcp_server")
        error = httpx.Response(200, json={"error": {"code": "TooManyRequests"}})
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(side_effect=[_page(CHAT_A, next_token="x"), error])
            count = await mcp_server._discover_chats_once("fake-token")

        assert count == 1
        assert read_watched_chats().chat_ids == (CHAT_A,)
        assert "TooManyRequests" in caplog.text

    async def test_error_key_on_the_first_page_registers_nothing(self, fake_cloud_store) -> None:
        error = httpx.Response(200, json={"error": {"code": "Forbidden"}})
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(return_value=error)
            assert await mcp_server._discover_chats_once("fake-token") == 0
        assert mcp_server._state.get("watched_chats", {}) == {}

    @pytest.mark.parametrize("status", [401, 403, 429, 500])
    async def test_failure_status_stops_but_keeps_earlier_pages(
        self, fake_cloud_store, status: int, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="entrabot.mcp_server")
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(
                side_effect=[_page(CHAT_A, next_token="x"), httpx.Response(status)]
            )
            assert await mcp_server._discover_chats_once("fake-token") == 1
        assert str(status) in caplog.text

    async def test_invalid_json_stops_but_keeps_earlier_pages(self, fake_cloud_store) -> None:
        proxy_page = httpx.Response(200, text="<html>proxy</html>")
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(side_effect=[_page(CHAT_A, next_token="x"), proxy_page])
            assert await mcp_server._discover_chats_once("fake-token") == 1

    async def test_network_error_stops_but_keeps_earlier_pages(self, fake_cloud_store) -> None:
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(
                side_effect=[_page(CHAT_A, next_token="x"), httpx.ConnectError("no route")]
            )
            assert await mcp_server._discover_chats_once("fake-token") == 1

    @pytest.mark.parametrize(
        "next_link",
        [
            "https://evil.example/v1.0/me/chats?$skiptoken=x",
            "http://graph.microsoft.com/v1.0/me/chats?$skiptoken=x",
            "https://graph.microsoft.com.evil.example/v1.0/me/chats",
            "/v1.0/me/chats?$skiptoken=x",
        ],
    )
    async def test_next_link_off_graph_is_not_sent_the_token(
        self, fake_cloud_store, next_link: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """respx raises on any request it doesn't know, so following this would fail."""
        caplog.set_level(logging.WARNING, logger="entrabot.mcp_server")
        body = {"value": [{"id": CHAT_A}], "@odata.nextLink": next_link}
        with respx.mock:
            route = respx.get(GRAPH_CHATS).mock(return_value=httpx.Response(200, json=body))
            assert await mcp_server._discover_chats_once("fake-token") == 1
        assert route.call_count == 1
        assert "nextLink" in caplog.text
        assert next_link not in caplog.text

    @pytest.mark.parametrize(
        "body",
        [
            {"value": [{"id": None}, {}, "not-an-object"]},
            {"value": "not-a-list"},
            {},
            [],
        ],
    )
    async def test_unexpected_shapes_register_nothing(self, fake_cloud_store, body) -> None:
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(return_value=httpx.Response(200, json=body))
            assert await mcp_server._discover_chats_once("fake-token") == 0

    async def test_token_never_reaches_the_logs(
        self, fake_cloud_store, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(return_value=httpx.Response(500, text="boom"))
            await mcp_server._discover_chats_once("super-secret-token")
        assert "super-secret-token" not in caplog.text


class TestDiscoveryRegistration:
    async def test_rebuilds_the_full_list_from_an_empty_store(
        self, fake_cloud_store, data_dir: Path
    ) -> None:
        """Nothing local survives in cloud mode, so a sweep must be able to rebuild."""
        pages = [
            _page(*[f"19:chat-{p}-{i}@thread.v2" for i in range(50)], next_token=f"t{p}")
            for p in range(2)
        ]
        pages.append(_page(*[f"19:chat-2-{i}@thread.v2" for i in range(10)]))
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(side_effect=pages)
            assert await mcp_server._discover_chats_once("fake-token") == 110

        assert len(read_watched_chats().chat_ids) == 110
        assert not (data_dir / "watched_chats").exists()

    async def test_chats_on_two_pages_register_once(self, fake_cloud_store) -> None:
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(
                side_effect=[_page(CHAT_A, next_token="x"), _page(CHAT_A, CHAT_B)]
            )
            assert await mcp_server._discover_chats_once("fake-token") == 2

    async def test_chats_already_in_memory_are_not_reset(self, fake_cloud_store) -> None:
        live = {"seen_ids": {"m1"}, "last_ts": "t", "bootstrapped": True}
        mcp_server._state["watched_chats"] = {CHAT_A: live}
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(return_value=_page(CHAT_A))
            assert await mcp_server._discover_chats_once("fake-token") == 0
        assert mcp_server._state["watched_chats"][CHAT_A] is live

    async def test_nothing_new_writes_nothing(self, fake_cloud_store, list_writes) -> None:
        mcp_server._state["watched_chats"] = {CHAT_A: {"seen_ids": set()}}
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(return_value=_page(CHAT_A))
            await mcp_server._discover_chats_once("fake-token")
        assert list_writes == []

    async def test_existing_cursor_rehydrates_off_the_loop(
        self, fake_cloud_store, blob_calls_on_event_loop
    ) -> None:
        await asyncio.to_thread(
            chat_cursors.save_cursor,
            CHAT_A,
            {"last_ts": "2026-11-01T00:00:00Z", "seen_ids_tail": ["m1"], "bootstrapped": True},
        )
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(return_value=_page(CHAT_A))
            await mcp_server._discover_chats_once("fake-token")
        state = mcp_server._state["watched_chats"][CHAT_A]
        assert state["bootstrapped"] is True
        assert state["seen_ids"] == {"m1"}
        assert blob_calls_on_event_loop == []

    async def test_unresolved_cursor_fails_closed(self, fake_cloud_store, monkeypatch) -> None:
        monkeypatch.setattr(
            chat_cursors,
            "resolve_cursor",
            lambda _cid: CursorResolution(CursorOutcome.UNRESOLVED, None),
        )
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(return_value=_page(CHAT_A))
            await mcp_server._discover_chats_once("fake-token")
        assert mcp_server._state["watched_chats"][CHAT_A]["needs_resolution"] is True

    async def test_sibling_chats_not_on_this_sweep_survive(self, fake_cloud_store) -> None:
        _seed_list(fake_cloud_store, CHAT_B)
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(return_value=_page(CHAT_A))
            await mcp_server._discover_chats_once("fake-token")
        assert _stored_list(fake_cloud_store) == [CHAT_A, CHAT_B]

    async def test_a_new_chat_drops_the_cached_sponsor_gate(self, fake_cloud_store) -> None:
        mcp_server._state["sponsor_gate"] = MagicMock(name="cached_gate")
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(return_value=_page(CHAT_A))
            await mcp_server._discover_chats_once("fake-token")
        assert mcp_server._state.get("sponsor_gate") is None


class TestDiscoveryRetriesFailedWrites:
    async def test_failed_write_is_retried_on_the_next_sweep(self, fake_cloud_store) -> None:
        """The second sweep finds nothing new, since the chat is already in memory.
        Only the dirty set gets it onto the list."""
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(return_value=_page(CHAT_A))
            with patch.object(fake_cloud_store, "put", AsyncMock(side_effect=OSError("outage"))):
                await mcp_server._discover_chats_once("fake-token")
            assert WATCHED_CHATS_KEY not in fake_cloud_store.data

            assert await mcp_server._discover_chats_once("fake-token") == 0

        assert _stored_list(fake_cloud_store) == [CHAT_A]
        assert not mcp_server._state["_dirty_watched_chats"]

    async def test_exhausted_cas_is_retried_on_the_next_sweep(self, fake_cloud_store) -> None:
        _seed_list(fake_cloud_store, CHAT_B)
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(return_value=_page(CHAT_A))
            with patch.object(
                fake_cloud_store, "put", AsyncMock(side_effect=ConcurrencyError("simulated 412"))
            ):
                await mcp_server._discover_chats_once("fake-token")
            await mcp_server._discover_chats_once("fake-token")
        assert _stored_list(fake_cloud_store) == [CHAT_A, CHAT_B]

    async def test_graph_failure_does_not_stop_the_retry(self, fake_cloud_store) -> None:
        mcp_server._state["_dirty_watched_chats"] = {CHAT_A}
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(return_value=httpx.Response(503))
            await mcp_server._discover_chats_once("fake-token")
        assert _stored_list(fake_cloud_store) == [CHAT_A]

    async def test_retry_is_batched_with_new_chats(self, fake_cloud_store, list_writes) -> None:
        mcp_server._state["_dirty_watched_chats"] = {CHAT_A}
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(return_value=_page(CHAT_B))
            await mcp_server._discover_chats_once("fake-token")
        assert _stored_list(fake_cloud_store) == [CHAT_A, CHAT_B]
        assert _writes_to_list(list_writes) == 1


class TestBackgroundDiscoverTask:
    async def test_sweeps_with_the_current_token(self, fake_cloud_store, monkeypatch) -> None:
        mcp_server._state["token"] = "fake-token"
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock())

        calls = {"n": 0}

        async def stop_after_one(_seconds: float) -> None:
            calls["n"] += 1
            if calls["n"] > 1:
                raise asyncio.CancelledError

        with (
            respx.mock,
            patch.object(asyncio, "sleep", stop_after_one),
            pytest.raises(asyncio.CancelledError),
        ):
            respx.get(GRAPH_CHATS).mock(return_value=_page(CHAT_A))
            await mcp_server._background_discover_chats()

        assert read_watched_chats().chat_ids == (CHAT_A,)


# ---------------------------------------------------------------------------
# Old chats must not replay when they are first seen
# ---------------------------------------------------------------------------
def _message(message_id: str, sent_at: datetime) -> dict:
    return {
        "message_id": message_id,
        "sent_at": sent_at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "from": "Alice Example",
        "content": "<external_content>ship it</external_content>",
    }


class TestFirstSightDoesNotReplay:
    """Paging reaches chats that were never polled before, and a chat with no cursor
    looks brand new. Surfacing each one's newest message would replay up to a thousand
    old imperative messages on upgrade: the flood the design doc treats as a security
    problem. A chat first seen by discovery or on the shared list surfaces its newest
    message only if it is recent, so a genuine new-chat intro still gets through.
    """

    @pytest.fixture(autouse=True)
    def _stubs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock())
        monkeypatch.setattr(mcp_server, "CURSOR_SAVE_DEBOUNCE_SECONDS", 0)
        monkeypatch.setattr(
            "entrabot.tools.teams.filter_human_messages",
            lambda messages, **kwargs: messages,
        )

    def _messages_are(self, monkeypatch, messages: list[dict]) -> None:
        async def fake_retry(fn, **kwargs):
            return messages

        monkeypatch.setattr(mcp_server, "_with_token_retry", fake_retry)

    async def _poll(self, chat_id: str) -> None:
        await mcp_server._poll_watched_chat(
            chat_id,
            mcp_server._state["watched_chats"][chat_id],
            agent_upn="entra-agent@contoso.onmicrosoft.com",
            agent_object_id="agent-oid",
        )

    def _record_pushes(self, monkeypatch) -> list[str]:
        pushed: list[str] = []

        async def record(msg: dict, chat_id: str) -> None:
            pushed.append(msg["message_id"])

        monkeypatch.setattr(mcp_server, "_push_channel_notification", record)
        return pushed

    async def test_discovered_chat_with_an_old_newest_message_is_silent(
        self, fake_cloud_store, monkeypatch
    ) -> None:
        pushed = self._record_pushes(monkeypatch)
        old = datetime.now(UTC) - timedelta(days=90)
        self._messages_are(monkeypatch, [_message("m-old", old)])
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(return_value=_page(CHAT_A))
            await mcp_server._discover_chats_once("fake-token")

        await self._poll(CHAT_A)  # bootstrap
        await self._poll(CHAT_A)  # first real cycle

        assert pushed == []

    async def test_discovered_chat_with_a_recent_message_surfaces_it_once(
        self, fake_cloud_store, monkeypatch
    ) -> None:
        """A human adds the agent and posts an intro: that must not be swallowed."""
        pushed = self._record_pushes(monkeypatch)
        recent = datetime.now(UTC) - timedelta(minutes=2)
        self._messages_are(monkeypatch, [_message("m-intro", recent)])
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(return_value=_page(CHAT_A))
            await mcp_server._discover_chats_once("fake-token")

        await self._poll(CHAT_A)
        await self._poll(CHAT_A)
        await self._poll(CHAT_A)

        assert pushed == ["m-intro"]

    async def test_message_arriving_after_a_silent_bootstrap_is_pushed(
        self, fake_cloud_store, monkeypatch
    ) -> None:
        pushed = self._record_pushes(monkeypatch)
        old = datetime.now(UTC) - timedelta(days=90)
        self._messages_are(monkeypatch, [_message("m-old", old)])
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(return_value=_page(CHAT_A))
            await mcp_server._discover_chats_once("fake-token")
        await self._poll(CHAT_A)

        new = _message("m-new", datetime.now(UTC))
        self._messages_are(monkeypatch, [_message("m-old", old), new])
        await self._poll(CHAT_A)

        assert pushed == ["m-new"]

    async def test_chat_from_the_shared_list_with_no_cursor_is_silent_when_old(
        self, fake_cloud_store, monkeypatch
    ) -> None:
        pushed = self._record_pushes(monkeypatch)
        _seed_list(fake_cloud_store, CHAT_A)
        await mcp_server._init_poll()
        old = datetime.now(UTC) - timedelta(days=90)
        self._messages_are(monkeypatch, [_message("m-old", old)])

        await self._poll(CHAT_A)
        await self._poll(CHAT_A)

        assert pushed == []

    async def test_first_boot_on_a_fresh_container_does_not_flood(
        self, fake_cloud_store, monkeypatch
    ) -> None:
        pushed = self._record_pushes(monkeypatch)
        old = datetime.now(UTC) - timedelta(days=90)
        self._messages_are(monkeypatch, [_message("m-old", old)])
        chats = [f"19:chat-{i}@thread.v2" for i in range(120)]
        pages = [
            _page(*chats[:50], next_token="a"),
            _page(*chats[50:100], next_token="b"),
            _page(*chats[100:]),
        ]
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(side_effect=pages)
            await mcp_server._discover_chats_once("fake-token")

        for chat_id in chats:
            await self._poll(chat_id)
            await self._poll(chat_id)

        assert pushed == []

    async def test_chat_created_by_the_agent_still_surfaces_its_newest(
        self, fake_cloud_store, monkeypatch
    ) -> None:
        """create_chat keeps today's behaviour; the gate is only for first sight."""
        pushed = self._record_pushes(monkeypatch)
        mcp_server._register_watched_chat(CHAT_A, persist=False, resolution=ABSENT)
        old = datetime.now(UTC) - timedelta(days=90)
        self._messages_are(monkeypatch, [_message("m-old", old)])

        await self._poll(CHAT_A)
        await self._poll(CHAT_A)

        assert pushed == ["m-old"]

    async def test_chat_with_a_real_cursor_is_unaffected(
        self, fake_cloud_store, monkeypatch
    ) -> None:
        pushed = self._record_pushes(monkeypatch)
        recent = datetime.now(UTC) - timedelta(hours=1)
        chat_cursors.save_cursor(
            CHAT_A,
            {
                "last_ts": recent.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "seen_ids_tail": ["m-seen"],
                "bootstrapped": True,
            },
        )
        with respx.mock:
            respx.get(GRAPH_CHATS).mock(return_value=_page(CHAT_A))
            await mcp_server._discover_chats_once("fake-token")
        self._messages_are(monkeypatch, [_message("m-new", datetime.now(UTC))])

        await self._poll(CHAT_A)

        assert pushed == ["m-new"]

    @pytest.mark.parametrize("quiet", [True, False])
    async def test_failed_bootstrap_read_pushes_nothing_and_retries(
        self, fake_cloud_store, monkeypatch, quiet: bool
    ) -> None:
        """With no watermark the steady-state gate calls everything new, so one
        throttled read during a big sweep used to push up to 10 old messages from
        the chat. A failed read must leave the chat un-bootstrapped."""
        pushed = self._record_pushes(monkeypatch)
        mcp_server._register_watched_chat(
            CHAT_A, persist=False, resolution=ABSENT, quiet_bootstrap=quiet
        )

        async def throttled(fn, **kwargs):
            raise httpx.HTTPStatusError(
                "429",
                request=httpx.Request("GET", "https://graph.invalid"),
                response=httpx.Response(429),
            )

        monkeypatch.setattr(mcp_server, "_with_token_retry", throttled)
        await self._poll(CHAT_A)

        state = mcp_server._state["watched_chats"][CHAT_A]
        assert state["bootstrapped"] is False
        assert state["last_ts"] is None
        assert chat_cursors.load_cursor(CHAT_A) is None, "no cursor written for a failed read"

        now = datetime.now(UTC)
        older = [_message(f"m-{i}", now - timedelta(days=91 + i)) for i in range(9)]
        newest = _message("m-newest", now - timedelta(days=90))
        self._messages_are(monkeypatch, [*older, newest])
        await self._poll(CHAT_A)  # the retry bootstraps
        await self._poll(CHAT_A)  # first real cycle

        assert state["bootstrapped"] is True
        assert pushed == ([] if quiet else ["m-newest"]), "at most the newest, never the history"

    async def test_unparseable_timestamp_leaves_the_chat_unbootstrapped(
        self, fake_cloud_store, monkeypatch
    ) -> None:
        """A message with no usable timestamp can't give the chat a watermark. Treating it as
        merely old used to finish the bootstrap without one, so nothing changes now and the
        next cycle reads again."""
        pushed = self._record_pushes(monkeypatch)
        mcp_server._register_watched_chat(
            CHAT_A, persist=False, resolution=ABSENT, quiet_bootstrap=True
        )
        bad = {"message_id": "m-bad", "sent_at": "not-a-timestamp"}
        self._messages_are(monkeypatch, [bad])

        await self._poll(CHAT_A)
        await self._poll(CHAT_A)

        state = mcp_server._state["watched_chats"][CHAT_A]
        assert state["bootstrapped"] is False
        assert state["last_ts"] is None
        assert state["seen_ids"] == set()
        assert pushed == []
        assert chat_cursors.load_cursor(CHAT_A) is None, "no cursor written"
