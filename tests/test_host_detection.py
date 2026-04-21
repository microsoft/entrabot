"""Tests for host detection and leader/slave mode gating.

The EntraClaw MCP server supports multiple MCP clients, but it was originally
built around Claude Code, which exposes a custom ``notifications/claude/channel``
for pushing inbound events to the model mid-turn. Hosts without a channel
mechanism (like GitHub Copilot CLI) would double-poll Teams and silently drop
every push notification if they ran the same background loops.

**Design decision (Brandon, 2026-04-20):** Claude Code is always the leader;
all other MCP hosts run in slave mode. Static designation based on the
``clientInfo.name`` the host sends at session initialize — not dynamic
election. Slaves run ZERO background tasks and get a per-response disclosure
on tools that expect an asynchronous reply (e.g. ``send_teams_message``).

This file covers:

- ``_current_host()`` — reads the active FastMCP context, returns the
  client-info name normalized to a well-known set, and falls back to
  ``"unknown"`` before session initialize completes.
- ``_is_leader_host()`` — convenience predicate for gating background
  task spawning and channel pushes.
- ``_slave_disclosure_suffix()`` — returns the disclosure string when the
  current host is NOT a leader, empty string otherwise.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _current_host
# ---------------------------------------------------------------------------
class TestCurrentHost:
    """``_current_host()`` returns the lowercased client name or ``unknown``."""

    def test_returns_claude_code_when_client_is_claude_code(self) -> None:
        from entraclaw import mcp_server

        fake_ctx = MagicMock()
        fake_ctx.session.client_params.clientInfo.name = "claude-code"

        with patch.object(mcp_server.mcp, "get_context", return_value=fake_ctx):
            assert mcp_server._current_host() == "claude-code"

    def test_returns_claude_code_when_client_is_capitalized(self) -> None:
        """Case-insensitive: ``Claude Code`` and ``claude-code`` normalize."""
        from entraclaw import mcp_server

        fake_ctx = MagicMock()
        fake_ctx.session.client_params.clientInfo.name = "Claude Code"

        with patch.object(mcp_server.mcp, "get_context", return_value=fake_ctx):
            # Normalized to lowercase + hyphenated for the compare below.
            assert mcp_server._current_host() == "claude code"

    def test_returns_github_copilot_cli_when_client_is_copilot(self) -> None:
        from entraclaw import mcp_server

        fake_ctx = MagicMock()
        fake_ctx.session.client_params.clientInfo.name = "github-copilot-cli"

        with patch.object(mcp_server.mcp, "get_context", return_value=fake_ctx):
            assert mcp_server._current_host() == "github-copilot-cli"

    def test_returns_unknown_when_client_info_absent(self) -> None:
        """Before session initialize, client_params is None."""
        from entraclaw import mcp_server

        fake_ctx = MagicMock()
        fake_ctx.session.client_params = None

        with patch.object(mcp_server.mcp, "get_context", return_value=fake_ctx):
            assert mcp_server._current_host() == "unknown"

    def test_returns_unknown_when_get_context_raises(self) -> None:
        """Outside a request context, FastMCP.get_context() raises. Treat as unknown."""
        from entraclaw import mcp_server

        def boom():
            raise LookupError("no active request context")

        with patch.object(mcp_server.mcp, "get_context", side_effect=boom):
            assert mcp_server._current_host() == "unknown"


# ---------------------------------------------------------------------------
# _is_leader_host
# ---------------------------------------------------------------------------
class TestIsLeaderHost:
    """Canonical leader set: ``{"claude-code", "claude code"}``.

    Any other host (including ``"unknown"``) is a slave. Static designation —
    no dynamic election, no config switch.

    ``_is_leader_host`` prefers the live request context but falls back to a
    cached host populated by prior tool invocations, so background tasks
    (which run outside any request context) still get the right answer.
    """

    def test_claude_code_is_leader(self) -> None:
        from entraclaw import mcp_server

        with patch.object(mcp_server, "_current_host", return_value="claude-code"):
            assert mcp_server._is_leader_host() is True

    def test_claude_code_with_space_is_leader(self) -> None:
        from entraclaw import mcp_server

        with patch.object(mcp_server, "_current_host", return_value="claude code"):
            assert mcp_server._is_leader_host() is True

    def test_copilot_cli_is_not_leader(self) -> None:
        from entraclaw import mcp_server

        with patch.object(
            mcp_server, "_current_host", return_value="github-copilot-cli"
        ):
            assert mcp_server._is_leader_host() is False

    def test_unknown_with_empty_cache_is_not_leader(self) -> None:
        """Pre-initialize AND no cached host: default to slave (fail closed)."""
        from entraclaw import mcp_server

        prior = mcp_server._state.get("cached_host", "")
        mcp_server._state["cached_host"] = ""
        try:
            with patch.object(mcp_server, "_current_host", return_value="unknown"):
                assert mcp_server._is_leader_host() is False
        finally:
            mcp_server._state["cached_host"] = prior

    def test_unknown_with_cached_leader_is_leader(self) -> None:
        """The fix: background pushes see no live context but cached host wins.

        This is the exact failure mode PR #27 exposed — _push_channel_notification
        runs in a detached asyncio task so _current_host() returns "unknown".
        After any prior tool call from Claude Code, the cache holds "claude-code"
        and the predicate correctly returns True.
        """
        from entraclaw import mcp_server

        prior = mcp_server._state.get("cached_host", "")
        mcp_server._state["cached_host"] = "claude-code"
        try:
            with patch.object(mcp_server, "_current_host", return_value="unknown"):
                assert mcp_server._is_leader_host() is True
        finally:
            mcp_server._state["cached_host"] = prior

    def test_unknown_with_cached_slave_is_not_leader(self) -> None:
        """Cache faithfully records the last-seen host, even if it's a slave."""
        from entraclaw import mcp_server

        prior = mcp_server._state.get("cached_host", "")
        mcp_server._state["cached_host"] = "github-copilot-cli"
        try:
            with patch.object(mcp_server, "_current_host", return_value="unknown"):
                assert mcp_server._is_leader_host() is False
        finally:
            mcp_server._state["cached_host"] = prior

    def test_live_context_wins_over_stale_cache(self) -> None:
        """A live request context always overrides the cache — correctness over staleness."""
        from entraclaw import mcp_server

        prior = mcp_server._state.get("cached_host", "")
        mcp_server._state["cached_host"] = "github-copilot-cli"
        try:
            with patch.object(mcp_server, "_current_host", return_value="claude-code"):
                assert mcp_server._is_leader_host() is True
        finally:
            mcp_server._state["cached_host"] = prior


# ---------------------------------------------------------------------------
# _capture_host_from_context
# ---------------------------------------------------------------------------
class TestCaptureHostFromContext:
    """``_capture_host_from_context()`` reads the live request context and
    updates ``_state['cached_host']`` so background tasks (which run outside
    any request context) can still answer the leader/slave question.

    The cache is a no-op when there's no live context, preserving any value
    set by a prior request.
    """

    def test_captures_live_host_into_cache(self) -> None:
        from entraclaw import mcp_server

        prior = mcp_server._state.get("cached_host", "")
        mcp_server._state["cached_host"] = ""
        try:
            with patch.object(
                mcp_server, "_current_host", return_value="claude-code"
            ):
                mcp_server._capture_host_from_context()
            assert mcp_server._state["cached_host"] == "claude-code"
        finally:
            mcp_server._state["cached_host"] = prior

    def test_no_live_context_preserves_existing_cache(self) -> None:
        """Tool calls outside a request context (or during shutdown) must
        not clobber a previously-captured leader host.
        """
        from entraclaw import mcp_server

        prior = mcp_server._state.get("cached_host", "")
        mcp_server._state["cached_host"] = "claude-code"
        try:
            with patch.object(mcp_server, "_current_host", return_value="unknown"):
                mcp_server._capture_host_from_context()
            assert mcp_server._state["cached_host"] == "claude-code"
        finally:
            mcp_server._state["cached_host"] = prior

    def test_empty_live_host_preserves_existing_cache(self) -> None:
        from entraclaw import mcp_server

        prior = mcp_server._state.get("cached_host", "")
        mcp_server._state["cached_host"] = "claude-code"
        try:
            with patch.object(mcp_server, "_current_host", return_value=""):
                mcp_server._capture_host_from_context()
            assert mcp_server._state["cached_host"] == "claude-code"
        finally:
            mcp_server._state["cached_host"] = prior

    def test_slave_host_overwrites_cache(self) -> None:
        """If the currently connected host really is a slave, the cache must
        reflect that. Otherwise a stale leader value would mask the change.
        """
        from entraclaw import mcp_server

        prior = mcp_server._state.get("cached_host", "")
        mcp_server._state["cached_host"] = "claude-code"
        try:
            with patch.object(
                mcp_server, "_current_host", return_value="github-copilot-cli"
            ):
                mcp_server._capture_host_from_context()
            assert mcp_server._state["cached_host"] == "github-copilot-cli"
        finally:
            mcp_server._state["cached_host"] = prior


# ---------------------------------------------------------------------------
# _slave_disclosure_suffix
# ---------------------------------------------------------------------------
class TestSlaveDisclosureSuffix:
    """Returns disclosure text in slave mode, empty string in leader mode."""

    def test_leader_returns_empty_string(self) -> None:
        from entraclaw import mcp_server

        with patch.object(mcp_server, "_is_leader_host", return_value=True):
            assert mcp_server._slave_disclosure_suffix() == ""

    def test_slave_returns_disclosure(self) -> None:
        from entraclaw import mcp_server

        with patch.object(mcp_server, "_is_leader_host", return_value=False):
            suffix = mcp_server._slave_disclosure_suffix()
            assert suffix  # non-empty
            assert "Reply channel unavailable" in suffix
            assert "Claude Code" in suffix


# ---------------------------------------------------------------------------
# Background-task lifecycle — start unconditionally
#
# Regression guard (2026-04-20): the previous gating made _init_poll() skip
# all background tasks when _is_leader_host() returned False. At boot time,
# no MCP initialize request has been processed yet so _current_host() reads
# "unknown" from a missing clientInfo and the leader predicate is False —
# even for a real Claude Code client about to connect. Result: the server
# silently ran no polls for 19+ hours in production.
#
# Background reads are process-level observability (they write to the audit
# log and interaction log; they don't leak anything a slave shouldn't see),
# so they must start unconditionally. Slave-mode gating now only applies to
# channel pushes and outbound-tool disclosure.
# ---------------------------------------------------------------------------
class TestBackgroundTaskGating:
    """_init_poll starts tasks regardless of host identity."""

    @pytest.mark.asyncio
    async def test_background_tasks_spawned_when_host_is_unknown(
        self, monkeypatch
    ) -> None:
        """Regression for the boot-time lifecycle bug.

        At ``_init_poll()`` time the MCP initialize request has not yet
        populated ``clientInfo``, so ``_current_host()`` returns "unknown"
        and ``_is_leader_host()`` returns False. Background tasks must
        still start — they're process-level, not per-connection.
        """
        from unittest.mock import MagicMock as _MM

        from entraclaw import mcp_server

        fake_config = _MM()
        fake_config.mode = "agent_user"
        fake_config.data_dir = MagicMock()
        fake_config.data_dir.__truediv__ = (
            lambda self, other: MagicMock(is_file=lambda: False)
        )

        fake_identity = MagicMock()
        fake_identity.session.auth_mode = "agent_user"

        monkeypatch.setitem(mcp_server._state, "config", fake_config)
        monkeypatch.setitem(mcp_server._state, "watched_chats", {})
        monkeypatch.setattr(mcp_server, "_identity", fake_identity)
        # Boot-time state: no initialize yet, _is_leader_host() is False.
        monkeypatch.setattr(mcp_server, "_is_leader_host", lambda: False)

        created: list[object] = []

        class FakeLoop:
            def create_task(self, coro):
                created.append(coro)
                coro.close()  # drop so no RuntimeWarning
                return MagicMock()

        monkeypatch.setattr(
            "asyncio.get_event_loop", lambda: FakeLoop()
        )

        await mcp_server._init_poll()

        # agent_user identity: 3 tasks (email, daily summary, chat discovery).
        # No watched chats so no Teams poll. Host-identity-independent.
        assert len(created) >= 3, (
            f"background tasks must start regardless of host; "
            f"got {len(created)} (created={created!r})"
        )

    @pytest.mark.asyncio
    async def test_background_tasks_spawned_when_host_is_leader(
        self, monkeypatch
    ) -> None:
        """Symmetric control: leader also spawns tasks (same path, same result)."""
        from unittest.mock import MagicMock as _MM

        from entraclaw import mcp_server

        fake_config = _MM()
        fake_config.mode = "agent_user"
        fake_config.data_dir = MagicMock()
        fake_config.data_dir.__truediv__ = (
            lambda self, other: MagicMock(is_file=lambda: False)
        )

        fake_identity = MagicMock()
        fake_identity.session.auth_mode = "agent_user"

        monkeypatch.setitem(mcp_server._state, "config", fake_config)
        monkeypatch.setitem(mcp_server._state, "watched_chats", {})
        monkeypatch.setattr(mcp_server, "_identity", fake_identity)
        monkeypatch.setattr(mcp_server, "_is_leader_host", lambda: True)

        created: list[object] = []

        class FakeLoop:
            def create_task(self, coro):
                created.append(coro)
                coro.close()
                return MagicMock()

        monkeypatch.setattr(
            "asyncio.get_event_loop", lambda: FakeLoop()
        )

        await mcp_server._init_poll()

        assert len(created) >= 3, (
            f"leader mode must spawn background tasks; got {len(created)}"
        )


# ---------------------------------------------------------------------------
# _push_channel_notification — slave gates the push, not the observation
#
# The push target (``notifications/claude/channel``) is a Claude-Code-specific
# JSON-RPC extension. Slaves have no such channel, so the push would be a
# no-op at best and a noisy error at worst. But the interaction log write
# is process-level observability — daily summaries must see every inbound
# message regardless of which host is connected. Test both branches.
# ---------------------------------------------------------------------------
class TestPushChannelNotificationSlaveGating:
    @pytest.mark.asyncio
    async def test_slave_skips_push_but_still_logs_interaction(
        self, tmp_path, monkeypatch
    ) -> None:
        """Slave mode: interaction log still written, write_stream.send NOT called.

        This preserves the "observe unconditionally, push conditionally"
        invariant. Daily summary must not lose visibility just because the
        connected host can't receive pushes.
        """
        from unittest.mock import AsyncMock

        monkeypatch.setenv("ENTRACLAW_DATA_DIR", str(tmp_path))

        from datetime import UTC, datetime

        from entraclaw import mcp_server
        from entraclaw.tools.interaction_log import read_day

        monkeypatch.setattr(mcp_server, "_is_leader_host", lambda: False)

        mock_stream = AsyncMock()
        mcp_server._state["_write_stream"] = mock_stream
        try:
            await mcp_server._push_channel_notification(
                {
                    "message_id": "m-slave-1",
                    "from": "Brandon",
                    "content": "slave-mode inbound",
                    "sent_at": "2026-04-20T23:40:32Z",
                },
                chat_id="19:abc@unq.gbl.spaces",
            )
        finally:
            mcp_server._state.pop("_write_stream", None)

        mock_stream.send.assert_not_awaited()

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        entries = read_day(today)
        assert any(
            e.get("content_ref") == "m-slave-1" for e in entries
        ), f"slave mode must still write interaction log; got: {entries}"

    @pytest.mark.asyncio
    async def test_leader_pushes_notification(
        self, tmp_path, monkeypatch
    ) -> None:
        """Leader mode: push is sent. Regression guard for the happy path."""
        from unittest.mock import AsyncMock

        monkeypatch.setenv("ENTRACLAW_DATA_DIR", str(tmp_path))

        from entraclaw import mcp_server

        monkeypatch.setattr(mcp_server, "_is_leader_host", lambda: True)

        mock_stream = AsyncMock()
        mcp_server._state["_write_stream"] = mock_stream
        try:
            await mcp_server._push_channel_notification(
                {
                    "message_id": "m-leader-1",
                    "from": "Brandon",
                    "content": "leader-mode inbound",
                    "sent_at": "2026-04-20T23:41:00Z",
                },
                chat_id="19:abc@unq.gbl.spaces",
            )
        finally:
            mcp_server._state.pop("_write_stream", None)

        mock_stream.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_push_succeeds_from_detached_context_when_cache_has_leader(
        self, tmp_path, monkeypatch
    ) -> None:
        """Production regression (PR #27): background pushes from _background_poll
        run in a detached asyncio task where ``_current_host()`` returns "unknown"
        because no MCP request context exists. The live-only gate dropped every
        push silently. With the cache populated by a prior tool call, the gate
        must correctly allow the push through.
        """
        from unittest.mock import AsyncMock

        monkeypatch.setenv("ENTRACLAW_DATA_DIR", str(tmp_path))

        from entraclaw import mcp_server

        # Simulate the exact production state: detached context, prior tool
        # call left "claude-code" in the cache.
        monkeypatch.setattr(mcp_server, "_current_host", lambda: "unknown")
        prior_cache = mcp_server._state.get("cached_host", "")
        mcp_server._state["cached_host"] = "claude-code"

        mock_stream = AsyncMock()
        mcp_server._state["_write_stream"] = mock_stream
        try:
            await mcp_server._push_channel_notification(
                {
                    "message_id": "m-bg-1",
                    "from": "Brandon",
                    "content": "background inbound",
                    "sent_at": "2026-04-21T20:06:00Z",
                },
                chat_id="19:abc@unq.gbl.spaces",
            )
        finally:
            mcp_server._state.pop("_write_stream", None)
            mcp_server._state["cached_host"] = prior_cache

        mock_stream.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_push_skipped_from_detached_context_when_cache_has_slave(
        self, tmp_path, monkeypatch
    ) -> None:
        """Slave host connected then a background poll fires: the cache says
        "github-copilot-cli" and the push must be skipped (no leader channel).
        Interaction log write still happens above the gate.
        """
        from unittest.mock import AsyncMock

        monkeypatch.setenv("ENTRACLAW_DATA_DIR", str(tmp_path))

        from datetime import UTC, datetime

        from entraclaw import mcp_server
        from entraclaw.tools.interaction_log import read_day

        monkeypatch.setattr(mcp_server, "_current_host", lambda: "unknown")
        prior_cache = mcp_server._state.get("cached_host", "")
        mcp_server._state["cached_host"] = "github-copilot-cli"

        mock_stream = AsyncMock()
        mcp_server._state["_write_stream"] = mock_stream
        try:
            await mcp_server._push_channel_notification(
                {
                    "message_id": "m-bg-slave-1",
                    "from": "Brandon",
                    "content": "background inbound (slave cached)",
                    "sent_at": "2026-04-21T20:07:00Z",
                },
                chat_id="19:abc@unq.gbl.spaces",
            )
        finally:
            mcp_server._state.pop("_write_stream", None)
            mcp_server._state["cached_host"] = prior_cache

        mock_stream.send.assert_not_awaited()

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        entries = read_day(today)
        assert any(
            e.get("content_ref") == "m-bg-slave-1" for e in entries
        ), "interaction log write must still happen above the gate"


# ---------------------------------------------------------------------------
# Capture-on-tool-entry wiring
#
# Every @mcp.tool() wrapper calls await _initialize() at entry. _initialize()
# must call _capture_host_from_context() so the cache is populated before the
# tool does its work — that way, any subsequent background push (running in
# a detached asyncio task with no live request context) can still answer the
# leader/slave question correctly.
# ---------------------------------------------------------------------------
class TestInitializeCapturesHost:
    @pytest.mark.asyncio
    async def test_initialize_populates_cached_host(self, monkeypatch) -> None:
        """After _initialize() runs inside a request context, the cache
        must reflect the live ``clientInfo.name``.
        """
        from unittest.mock import AsyncMock

        from entraclaw import mcp_server

        # Stub out the real auth/poll work — we're testing the capture wiring.
        monkeypatch.setattr(mcp_server, "_init_auth", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_init_poll", AsyncMock(return_value=None))

        # Force a fresh init path so the capture runs even if already initialized.
        prior_initialized = mcp_server._state.get("initialized", False)
        prior_cache = mcp_server._state.get("cached_host", "")
        mcp_server._state["initialized"] = False
        mcp_server._state["cached_host"] = ""

        try:
            with patch.object(
                mcp_server, "_current_host", return_value="claude-code"
            ):
                await mcp_server._initialize()
            assert mcp_server._state["cached_host"] == "claude-code"
        finally:
            mcp_server._state["initialized"] = prior_initialized
            mcp_server._state["cached_host"] = prior_cache

    @pytest.mark.asyncio
    async def test_initialize_captures_even_when_already_initialized(
        self, monkeypatch
    ) -> None:
        """Fast path: if the server already initialized, capture must still
        run on every subsequent tool call (that's the whole point — the cache
        must stay warm across the server's lifetime).
        """
        from entraclaw import mcp_server

        prior_initialized = mcp_server._state.get("initialized", False)
        prior_cache = mcp_server._state.get("cached_host", "")
        mcp_server._state["initialized"] = True
        mcp_server._state["cached_host"] = ""

        try:
            with patch.object(
                mcp_server, "_current_host", return_value="claude-code"
            ):
                await mcp_server._initialize()
            assert mcp_server._state["cached_host"] == "claude-code"
        finally:
            mcp_server._state["initialized"] = prior_initialized
            mcp_server._state["cached_host"] = prior_cache


# ---------------------------------------------------------------------------
# send_teams_message — slave-mode disclosure
#
# The semantic of send_teams_message is "fire-and-forget with a reply expected
# asynchronously via the channel mechanism". In leader mode the channel push
# delivers the reply into Claude Code's turn; in slave mode there is no
# channel, so the tool response MUST tell the model to advise the user their
# reply won't surface. The disclosure lives inside the JSON response so the
# model sees it as tool output.
# ---------------------------------------------------------------------------
class TestSendTeamsMessageDisclosure:
    @pytest.mark.asyncio
    async def test_slave_send_teams_message_response_includes_disclosure(
        self, monkeypatch
    ) -> None:
        import json as _json

        from entraclaw import mcp_server

        monkeypatch.setattr(mcp_server, "_is_leader_host", lambda: False)

        # Bypass auth init and token ensure — we just care about the wrapper.
        async def _noop():
            return None

        async def _fake_init():
            mcp_server._state["initialized"] = True

        async def _fake_with_retry(_fn, **kwargs):
            return {"message_id": "abc", "sent_at": "2026-04-20T00:00:00Z"}

        monkeypatch.setattr(mcp_server, "_initialize", _fake_init)
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", _noop)
        monkeypatch.setattr(mcp_server, "_with_token_retry", _fake_with_retry)
        monkeypatch.setattr(mcp_server, "_log_interaction_safe", lambda **_kw: None)

        fake_config = MagicMock()
        fake_config.mode = "agent_user"
        monkeypatch.setitem(mcp_server._state, "config", fake_config)

        fake_identity = MagicMock()
        fake_identity.session.auth_mode = "agent_user"
        monkeypatch.setattr(mcp_server, "_identity", fake_identity)

        raw = await mcp_server.send_teams_message(
            "hello", chat_id="19:abc@thread.v2"
        )
        payload = _json.loads(raw)

        assert "notice" in payload, (
            f"slave-mode response must include a 'notice' field: {payload}"
        )
        assert "Reply channel unavailable" in payload["notice"]

    @pytest.mark.asyncio
    async def test_leader_send_teams_message_response_excludes_disclosure(
        self, monkeypatch
    ) -> None:
        import json as _json

        from entraclaw import mcp_server

        monkeypatch.setattr(mcp_server, "_is_leader_host", lambda: True)

        async def _noop():
            return None

        async def _fake_init():
            mcp_server._state["initialized"] = True

        async def _fake_with_retry(_fn, **kwargs):
            return {"message_id": "abc", "sent_at": "2026-04-20T00:00:00Z"}

        monkeypatch.setattr(mcp_server, "_initialize", _fake_init)
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", _noop)
        monkeypatch.setattr(mcp_server, "_with_token_retry", _fake_with_retry)
        monkeypatch.setattr(mcp_server, "_log_interaction_safe", lambda **_kw: None)

        fake_config = MagicMock()
        fake_config.mode = "agent_user"
        monkeypatch.setitem(mcp_server._state, "config", fake_config)

        fake_identity = MagicMock()
        fake_identity.session.auth_mode = "agent_user"
        monkeypatch.setattr(mcp_server, "_identity", fake_identity)

        raw = await mcp_server.send_teams_message(
            "hello", chat_id="19:abc@thread.v2"
        )
        payload = _json.loads(raw)

        assert "notice" not in payload, (
            f"leader-mode response must NOT include a slave notice: {payload}"
        )
