"""The MCP server's Agent 365 runs follow what it pushes to the model.

A Teams message or email that reached the model starts a run; one whose push failed does not,
because the model never saw it. With telemetry off nothing is tracked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from entrabot import mcp_server
from tests.test_mcp_push_envelope import (  # noqa: F401 - the fixture is used by name
    OTHER_ID,
    _email,
    _gate,
    _teams_message,
    _use_gate,
    stream,
)


class Tracker:
    def __init__(self) -> None:
        self.inbound_calls: list[dict] = []

    def inbound(self, **kwargs: object) -> None:
        self.inbound_calls.append(kwargs)


@pytest.fixture
def tracker(monkeypatch: pytest.MonkeyPatch) -> Tracker:
    fake = Tracker()
    monkeypatch.setitem(mcp_server._state, "a365_runs", fake)
    return fake


async def test_a_pushed_teams_message_starts_a_run_attributed_to_its_sender(
    stream: AsyncMock,  # noqa: F811
    tracker: Tracker,
) -> None:
    _use_gate(_gate())
    await mcp_server._push_channel_notification(_teams_message(), chat_id="19:chat@thread.v2")
    assert tracker.inbound_calls == [
        {"conversation_id": "19:chat@thread.v2", "channel": "msteams", "caller_id": OTHER_ID}
    ]


async def test_a_push_that_failed_starts_no_run(
    stream: AsyncMock,  # noqa: F811
    tracker: Tracker,
) -> None:
    _use_gate(_gate())
    stream.send.side_effect = RuntimeError("closed")
    await mcp_server._push_channel_notification(_teams_message(), chat_id="19:chat@thread.v2")
    assert tracker.inbound_calls == []


async def test_a_pushed_email_starts_an_outlook_run_with_no_caller_id(
    stream: AsyncMock,  # noqa: F811
    tracker: Tracker,
) -> None:
    await mcp_server._push_email_notification(_email(conversationId="conv-1"))
    assert tracker.inbound_calls == [
        {"conversation_id": "conv-1", "channel": "outlook", "caller_id": None}
    ]


async def test_with_telemetry_off_nothing_is_tracked_or_started(
    stream: AsyncMock,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from entrabot.config import EntraBotConfig

    monkeypatch.delitem(mcp_server._state, "a365_runs", raising=False)
    monkeypatch.setitem(mcp_server._state, "config", EntraBotConfig())
    assert mcp_server._start_a365_telemetry() == []
    assert "a365_runs" not in mcp_server._state
    _use_gate(_gate())
    await mcp_server._push_channel_notification(_teams_message(), chat_id="19:chat@thread.v2")
