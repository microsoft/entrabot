"""Server-side outbound-discipline hooks for ``send_teams_message``.

Two pre-checks fire inside the MCP wrapper:

1. **Placeholder discipline** — substantive (>200 chars or >2 terminal
   punctuation marks) outbound Teams messages require a recent
   ``post_thinking_placeholder`` for the same chat_id. Hard-block;
   raises ``MissingPlaceholderError`` which the wrapper surfaces as a
   JSON ``{"error": ..., "error_type": "MissingPlaceholderError"}``.

2. **Commitment-language → promise** — scans the outbound text for
   "I'll do X" / "at the next pause" phrases. If matched and no recent
   ``add_promise`` covers it, the wrapper appends
   ``_discipline_warning`` to the result JSON. Warn-not-block by
   design (false positives on language detection are real).

Both hooks live in ``src/entrabot/mcp_server.py`` (the MCP tool
wrapper), not in ``.claude/settings.json`` — they apply on every host
that speaks MCP and every storage backend. See
``scripts/hooks/README.md``.
"""

from __future__ import annotations

import json as _json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def fake_state():
    """Reset ``mcp_server._state`` for each test, restore on teardown."""
    from entrabot import mcp_server

    old_state = mcp_server._state.copy()
    mcp_server._state.clear()
    fake_config = MagicMock()
    fake_config.mode = "agent_user"
    fake_config.agent_user_upn = "agent@example.com"
    mcp_server._state["config"] = fake_config
    mcp_server._state["recent_placeholders"] = {}
    yield mcp_server
    mcp_server._state.clear()
    mcp_server._state.update(old_state)


def _patch_send(monkeypatch, mcp_server, captured: dict | None = None):
    """Patch the send pipeline to capture args and return a fake result."""
    captured = captured if captured is not None else {}

    async def fake_with_token_retry(fn, **kwargs):
        captured.update(kwargs)
        return {
            "message_id": "msg-out-1",
            "sent_at": "2026-05-04T18:30:00+00:00",
        }

    monkeypatch.setattr(mcp_server, "_log_interaction_safe", lambda **kw: None)
    monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
    monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))
    monkeypatch.setattr(mcp_server, "_with_token_retry", fake_with_token_retry)
    # Force "has channel push" path so auto-wait is skipped.
    monkeypatch.setattr(mcp_server, "_current_host", lambda: "claude-code")
    return captured


# ---------------------------------------------------------------------------
# Placeholder discipline (HARD BLOCK)
# ---------------------------------------------------------------------------


class TestPlaceholderDiscipline:
    @pytest.mark.asyncio
    async def test_placeholder_required_for_substantive_message(self, monkeypatch, fake_state):
        """A long message with no prior placeholder is rejected."""
        mcp_server = fake_state
        _patch_send(monkeypatch, mcp_server)

        long_message = "<p>" + ("This is a long substantive reply. " * 10) + "</p>"
        assert len(long_message) > 200

        result = await mcp_server.send_teams_message(message=long_message, chat_id="chat-A")
        parsed = _json.loads(result)
        assert "error" in parsed
        assert parsed["error_type"] == "MissingPlaceholderError"
        assert "post_thinking_placeholder" in parsed["error"]
        assert parsed["remediation"] == "post_thinking_placeholder + retry"

    @pytest.mark.asyncio
    async def test_placeholder_grace_window_passes(self, monkeypatch, fake_state):
        """A recent placeholder (within grace window) lets the send proceed."""
        import time as _time

        mcp_server = fake_state
        captured = _patch_send(monkeypatch, mcp_server)

        # Pretend a placeholder was posted 30 seconds ago.
        mcp_server._state["recent_placeholders"] = {"chat-A": _time.monotonic() - 30}

        long_message = "<p>" + ("This is a long substantive reply. " * 10) + "</p>"

        result = await mcp_server.send_teams_message(message=long_message, chat_id="chat-A")
        parsed = _json.loads(result)
        assert "error" not in parsed
        assert parsed["message_id"] == "msg-out-1"
        # Verify the underlying send was actually invoked.
        assert captured.get("chat_id") == "chat-A"

    @pytest.mark.asyncio
    async def test_placeholder_grace_expired(self, monkeypatch, fake_state):
        """A stale placeholder (older than grace window) is rejected."""
        import time as _time

        mcp_server = fake_state
        _patch_send(monkeypatch, mcp_server)

        # Default grace is 300s; placeholder posted 600s ago is stale.
        mcp_server._state["recent_placeholders"] = {"chat-A": _time.monotonic() - 600}

        long_message = "<p>" + ("This is a long substantive reply. " * 10) + "</p>"

        result = await mcp_server.send_teams_message(message=long_message, chat_id="chat-A")
        parsed = _json.loads(result)
        assert parsed.get("error_type") == "MissingPlaceholderError"
        # Age must be reported in the message.
        assert "600s" in parsed["error"] or "exceeds grace" in parsed["error"]

    @pytest.mark.asyncio
    async def test_placeholder_skip_for_short_message(self, monkeypatch, fake_state):
        """A short, non-substantive message bypasses the placeholder check."""
        mcp_server = fake_state
        captured = _patch_send(monkeypatch, mcp_server)

        # Short, single-clause: ≤200 chars, ≤2 terminal punctuation.
        short_message = "<p>ack, on it</p>"

        result = await mcp_server.send_teams_message(message=short_message, chat_id="chat-A")
        parsed = _json.loads(result)
        assert "error" not in parsed
        assert parsed["message_id"] == "msg-out-1"
        assert captured.get("chat_id") == "chat-A"

    @pytest.mark.asyncio
    async def test_placeholder_bypass_env_var(self, monkeypatch, fake_state):
        """ENTRABOT_SKIP_PLACEHOLDER_CHECK=true disables the gate."""
        mcp_server = fake_state
        _patch_send(monkeypatch, mcp_server)
        monkeypatch.setenv("ENTRABOT_SKIP_PLACEHOLDER_CHECK", "true")

        long_message = "<p>" + ("This is a long substantive reply. " * 10) + "</p>"

        result = await mcp_server.send_teams_message(message=long_message, chat_id="chat-A")
        parsed = _json.loads(result)
        assert "error" not in parsed
        assert parsed["message_id"] == "msg-out-1"

    @pytest.mark.asyncio
    async def test_post_thinking_placeholder_records_state(self, monkeypatch, fake_state):
        """post_thinking_placeholder updates _state["recent_placeholders"]."""
        mcp_server = fake_state

        monkeypatch.setattr(mcp_server, "_log_interaction_safe", lambda **kw: None)
        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(
            mcp_server,
            "_with_token_retry",
            AsyncMock(return_value="msg-placeholder-1"),
        )

        before = mcp_server._state.get("recent_placeholders", {}).get("chat-X")
        assert before is None

        result = await mcp_server.post_thinking_placeholder(chat_id="chat-X", text="thinking…")
        parsed = _json.loads(result)
        assert parsed["message_id"] == "msg-placeholder-1"

        recent = mcp_server._state.get("recent_placeholders", {})
        assert "chat-X" in recent
        # Value is a monotonic timestamp (positive float).
        assert isinstance(recent["chat-X"], float)
        assert recent["chat-X"] > 0


# ---------------------------------------------------------------------------
# Commitment-language → promise (WARN, not block)
# ---------------------------------------------------------------------------


class TestCommitmentLanguageDiscipline:
    @pytest.mark.asyncio
    async def test_commitment_warning_emitted(self, monkeypatch, fake_state):
        """When commitment language is detected and no recent promise
        exists, the result JSON carries _discipline_warning."""
        import time as _time

        mcp_server = fake_state
        _patch_send(monkeypatch, mcp_server)

        # Stage a recent placeholder so the placeholder gate passes.
        mcp_server._state["recent_placeholders"] = {"chat-A": _time.monotonic()}

        # Patch list_promises (used by _has_recent_promise) to return empty.
        async def fake_list_promises(open_only=True):  # noqa: ARG001
            return []

        monkeypatch.setattr("entrabot.tools.promises.list_promises", fake_list_promises)

        # Commitment phrase included. Length is irrelevant — commitment
        # scan runs on every message; placeholder gate already passes
        # because we staged a recent placeholder above.
        message = (
            "<p>Got it. I'll write up the design doc tonight and ping you when it's ready.</p>"
        )

        result = await mcp_server.send_teams_message(message=message, chat_id="chat-A")
        parsed = _json.loads(result)

        assert "error" not in parsed
        assert "_discipline_warning" in parsed
        assert "add_promise" in parsed["_discipline_warning"]

    @pytest.mark.asyncio
    async def test_commitment_no_warning_when_promise_exists(self, monkeypatch, fake_state):
        """A matching recent promise suppresses the warning."""
        import time as _time

        mcp_server = fake_state
        _patch_send(monkeypatch, mcp_server)
        mcp_server._state["recent_placeholders"] = {"chat-A": _time.monotonic()}

        # Build a recent open promise tied to the same chat_id.
        from entrabot.tools.promises import Promise

        recent = Promise(
            id="p-1",
            created_at=datetime.now(UTC).isoformat(),
            chat_id="chat-A",
            description="write up design doc tonight",
            status="open",
        )

        async def fake_list_promises(open_only=True):  # noqa: ARG001
            return [recent]

        monkeypatch.setattr("entrabot.tools.promises.list_promises", fake_list_promises)

        message = (
            "<p>Got it. I'll write up the design doc tonight and ping you when "
            "it's ready. Should cover the trade-offs we discussed earlier this "
            "week.</p>"
        )
        result = await mcp_server.send_teams_message(message=message, chat_id="chat-A")
        parsed = _json.loads(result)

        assert "error" not in parsed
        assert "_discipline_warning" not in parsed

    @pytest.mark.asyncio
    async def test_commitment_no_warning_when_promise_terminal(self, monkeypatch, fake_state):
        """A recent promise with chat_id='terminal' also suppresses the
        warning — covers CLI-side commitments mirrored into Teams."""
        import time as _time

        mcp_server = fake_state
        _patch_send(monkeypatch, mcp_server)
        mcp_server._state["recent_placeholders"] = {"chat-A": _time.monotonic()}

        from entrabot.tools.promises import Promise

        terminal_promise = Promise(
            id="p-term",
            created_at=datetime.now(UTC).isoformat(),
            chat_id="terminal",
            description="write up design doc tonight",
            status="open",
        )

        async def fake_list_promises(open_only=True):  # noqa: ARG001
            return [terminal_promise]

        monkeypatch.setattr("entrabot.tools.promises.list_promises", fake_list_promises)

        message = (
            "<p>Got it. I'll write up the design doc tonight and ping you when "
            "it's ready. Should cover the trade-offs we discussed earlier this "
            "week.</p>"
        )
        result = await mcp_server.send_teams_message(message=message, chat_id="chat-A")
        parsed = _json.loads(result)
        assert "_discipline_warning" not in parsed

    @pytest.mark.asyncio
    async def test_commitment_old_promise_does_not_count(self, monkeypatch, fake_state):
        """A promise older than the lookback window does NOT suppress
        the warning."""
        import time as _time

        mcp_server = fake_state
        _patch_send(monkeypatch, mcp_server)
        mcp_server._state["recent_placeholders"] = {"chat-A": _time.monotonic()}

        from entrabot.tools.promises import Promise

        # 10 minutes ago — well past the 120s lookback.
        old_created = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        old_promise = Promise(
            id="p-old",
            created_at=old_created,
            chat_id="chat-A",
            description="something else",
            status="open",
        )

        async def fake_list_promises(open_only=True):  # noqa: ARG001
            return [old_promise]

        monkeypatch.setattr("entrabot.tools.promises.list_promises", fake_list_promises)

        message = (
            "<p>Got it. I'll write up the design doc tonight and ping you when "
            "it's ready. Should cover the trade-offs we discussed earlier this "
            "week.</p>"
        )
        result = await mcp_server.send_teams_message(message=message, chat_id="chat-A")
        parsed = _json.loads(result)
        assert "_discipline_warning" in parsed

    @pytest.mark.asyncio
    async def test_commitment_bypass_env_var(self, monkeypatch, fake_state):
        """ENTRABOT_SKIP_COMMITMENT_CHECK=true silences the warning."""
        import time as _time

        mcp_server = fake_state
        _patch_send(monkeypatch, mcp_server)
        mcp_server._state["recent_placeholders"] = {"chat-A": _time.monotonic()}
        monkeypatch.setenv("ENTRABOT_SKIP_COMMITMENT_CHECK", "true")

        # list_promises must NOT even be queried. Make it explode if called.
        called = {"hit": False}

        async def boom_list_promises(open_only=True):  # noqa: ARG001
            called["hit"] = True
            return []

        monkeypatch.setattr("entrabot.tools.promises.list_promises", boom_list_promises)

        message = (
            "<p>Got it. I'll write up the design doc tonight and ping you when "
            "it's ready. Should cover the trade-offs we discussed earlier this "
            "week.</p>"
        )
        result = await mcp_server.send_teams_message(message=message, chat_id="chat-A")
        parsed = _json.loads(result)
        assert "_discipline_warning" not in parsed
        # Bypass should short-circuit before touching the storage backend.
        assert called["hit"] is False

    @pytest.mark.asyncio
    async def test_no_commitment_no_warning(self, monkeypatch, fake_state):
        """A neutral message without commitment phrases gets no warning."""
        import time as _time

        mcp_server = fake_state
        _patch_send(monkeypatch, mcp_server)
        mcp_server._state["recent_placeholders"] = {"chat-A": _time.monotonic()}

        async def fake_list_promises(open_only=True):  # noqa: ARG001
            return []

        monkeypatch.setattr("entrabot.tools.promises.list_promises", fake_list_promises)

        # Long enough to be substantive but no commitment phrase.
        message = (
            "<p>Here is the data you asked for. The numbers came in this "
            "morning and the trend continues — Q3 outpaces Q2 by about 12 "
            "percent across all three product lines.</p>"
        )
        result = await mcp_server.send_teams_message(message=message, chat_id="chat-A")
        parsed = _json.loads(result)
        assert "_discipline_warning" not in parsed
