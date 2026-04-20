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

    def test_unknown_is_not_leader(self) -> None:
        """Pre-initialize, default to slave. Safer: no accidental double-polling."""
        from entraclaw import mcp_server

        with patch.object(mcp_server, "_current_host", return_value="unknown"):
            assert mcp_server._is_leader_host() is False


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
# Background-task gating
#
# In slave mode ``_init_poll`` must not spawn any of the four background
# tasks (Teams poll, email poll, daily summary scheduler, chat discovery).
# The leader-mode test is the symmetric control — same state, just the
# leader predicate flipped — and the tasks DO spawn.
# ---------------------------------------------------------------------------
class TestBackgroundTaskGating:
    """_init_poll respects _is_leader_host."""

    @pytest.mark.asyncio
    async def test_background_tasks_not_spawned_in_slave_mode(
        self, monkeypatch
    ) -> None:
        from unittest.mock import MagicMock as _MM

        from entraclaw import mcp_server

        # Pretend we're in agent_user mode with one watched chat so that
        # the leader-path WOULD spawn tasks. Slave mode must short-circuit.
        fake_config = _MM()
        fake_config.mode = "agent_user"
        fake_config.data_dir = MagicMock()
        fake_config.data_dir.__truediv__ = (
            lambda self, other: MagicMock(is_file=lambda: False)
        )

        fake_identity = MagicMock()
        fake_identity.session.auth_mode = "agent_user"

        monkeypatch.setitem(mcp_server._state, "config", fake_config)
        monkeypatch.setattr(mcp_server, "_identity", fake_identity)
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

        assert created == [], (
            f"slave mode must not spawn background tasks; got {created!r}"
        )

    @pytest.mark.asyncio
    async def test_background_tasks_spawned_in_leader_mode(
        self, monkeypatch
    ) -> None:
        """Symmetric control: flip the leader predicate, tasks spawn."""
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

        # Leader + agent_user identity: 3 tasks (email, daily, discover).
        # No watched chats so no Teams poll. The point is: leader path
        # creates tasks, slave path does not.
        assert len(created) >= 3, (
            f"leader mode must spawn background tasks; got {len(created)}"
        )


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
