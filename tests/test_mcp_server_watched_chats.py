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
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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

    async def _wait(self, loader) -> None:
        with (
            patch("entrabot.identity.sponsors.load_agent_identity_sponsor_gate", loader),
            patch("entrabot.tools.audit.log_event", MagicMock()),
            patch("entrabot.tools.wait_tool.wait_loop", AsyncMock(return_value=_PICKED)),
        ):
            await mcp_server.wait_for_sponsor_dm(timeout_seconds=1)

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

    async def _send(self, loader) -> None:
        with (
            patch("entrabot.identity.sponsors.load_agent_identity_sponsor_gate", loader),
            patch("entrabot.tools.wait_tool.wait_loop", AsyncMock(return_value=_PICKED)),
        ):
            await mcp_server.send_teams_message("hello", chat_id=CHAT_A)

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
