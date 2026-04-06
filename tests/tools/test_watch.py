"""Tests for watch_teams_replies and supporting functions."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from openclaw.tools.teams import GRAPH_BASE, filter_human_messages


class TestFilterHumanMessages:
    def test_filters_agent_messages(self) -> None:
        messages = [
            {
                "message_id": "m1",
                "from": "Human User",
                "content": "hello",
                "sent_at": "2026-04-06T12:00:00Z",
            },
            {
                "message_id": "m2",
                "from": "Openclaw Agent",
                "content": "hi back",
                "sent_at": "2026-04-06T12:00:01Z",
            },
            {
                "message_id": "m3",
                "from": "Human User",
                "content": "do something",
                "sent_at": "2026-04-06T12:00:02Z",
            },
        ]
        result = filter_human_messages(messages, agent_user_display_name="Openclaw Agent")
        assert len(result) == 2
        assert result[0]["message_id"] == "m1"
        assert result[1]["message_id"] == "m3"

    def test_filters_no_from_field(self) -> None:
        messages = [
            {
                "message_id": "m1",
                "from": "Human User",
                "content": "hello",
                "sent_at": "2026-04-06T12:00:00Z",
            },
            {
                "message_id": "m2",
                "from": "unknown",
                "content": "",
                "sent_at": "2026-04-06T12:00:01Z",
            },
        ]
        # "unknown" is what teams.read() returns when from is None — see existing code
        # System messages have from="unknown", so filter those out too
        result = filter_human_messages(messages, agent_user_display_name="Openclaw Agent")
        assert len(result) == 1
        assert result[0]["message_id"] == "m1"

    def test_empty_list(self) -> None:
        result = filter_human_messages([], agent_user_display_name="Openclaw Agent")
        assert result == []

    def test_all_agent_messages(self) -> None:
        messages = [
            {
                "message_id": "m1",
                "from": "Openclaw Agent",
                "content": "hi",
                "sent_at": "2026-04-06T12:00:00Z",
            },
        ]
        result = filter_human_messages(messages, agent_user_display_name="Openclaw Agent")
        assert result == []


class TestEagerTokenRefresh:
    @pytest.mark.asyncio
    async def test_refreshes_when_expired(self) -> None:
        """Token older than 55 min should be refreshed."""
        from openclaw import mcp_server

        mock_acquire = MagicMock(return_value="new-token")
        mock_config = MagicMock()

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state["token"] = "old-token"
            mcp_server._state["config"] = mock_config
            mcp_server._state["token_acquired_at"] = time.monotonic() - 3400

            with patch("openclaw.mcp_server.acquire_agent_user_token", mock_acquire):
                await mcp_server._ensure_valid_token()

            assert mcp_server._state["token"] == "new-token"
            mock_acquire.assert_called_once_with(mock_config)
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)

    @pytest.mark.asyncio
    async def test_no_refresh_when_fresh(self) -> None:
        """Token younger than 55 min should NOT be refreshed."""
        from openclaw import mcp_server

        mock_acquire = MagicMock(return_value="new-token")
        mock_config = MagicMock()

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state["token"] = "fresh-token"
            mcp_server._state["config"] = mock_config
            mcp_server._state["token_acquired_at"] = time.monotonic() - 100

            with patch("openclaw.mcp_server.acquire_agent_user_token", mock_acquire):
                await mcp_server._ensure_valid_token()

            assert mcp_server._state["token"] == "fresh-token"
            mock_acquire.assert_not_called()
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)


class TestLazyTokenRetry:
    @pytest.mark.asyncio
    async def test_retry_on_401(self) -> None:
        """TokenExpiredError on first call should trigger refresh + retry."""
        from openclaw import mcp_server
        from openclaw.errors import TokenExpiredError

        call_count = 0

        async def flaky_fn(*, token: str) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TokenExpiredError("expired")
            return f"result-with-{token}"

        mock_acquire = MagicMock(return_value="refreshed-token")
        mock_config = MagicMock()

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state["token"] = "old-token"
            mcp_server._state["config"] = mock_config
            mcp_server._state["token_acquired_at"] = time.monotonic()

            with patch("openclaw.mcp_server.acquire_agent_user_token", mock_acquire):
                result = await mcp_server._with_token_retry(flaky_fn)

            assert result == "result-with-refreshed-token"
            assert mcp_server._state["token"] == "refreshed-token"
            mock_acquire.assert_called_once()
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)

    @pytest.mark.asyncio
    async def test_raises_if_retry_also_fails(self) -> None:
        """If both attempts fail with TokenExpiredError, propagate the error."""
        from openclaw import mcp_server
        from openclaw.errors import TokenExpiredError

        async def always_fails(*, token: str) -> str:
            raise TokenExpiredError("still expired")

        mock_acquire = MagicMock(return_value="refreshed-token")
        mock_config = MagicMock()

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state["token"] = "old-token"
            mcp_server._state["config"] = mock_config
            mcp_server._state["token_acquired_at"] = time.monotonic()

            with (
                patch("openclaw.mcp_server.acquire_agent_user_token", mock_acquire),
                pytest.raises(TokenExpiredError),
            ):
                await mcp_server._with_token_retry(always_fails)
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)


class TestExistingToolsRetrofitted:
    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retries_on_401(self) -> None:
        """send_teams_message should retry once on TokenExpiredError."""
        from openclaw import mcp_server

        respx.post(f"{GRAPH_BASE}/chats/c1/messages").mock(
            side_effect=[
                httpx.Response(401),
                httpx.Response(
                    201,
                    json={
                        "id": "msg-1",
                        "createdDateTime": "2026-04-06T12:00:00Z",
                    },
                ),
            ]
        )

        mock_acquire = MagicMock(return_value="refreshed-token")
        mock_config = MagicMock()

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state.update({
                "initialized": True,
                "token": "old-token",
                "config": mock_config,
                "chat_id": "c1",
                "token_acquired_at": time.monotonic(),
            })

            with patch(
                "openclaw.mcp_server.acquire_agent_user_token",
                mock_acquire,
            ):
                result_json = await mcp_server.send_teams_message("hello")

            result = json.loads(result_json)
            assert result["message_id"] == "msg-1"
            mock_acquire.assert_called_once()
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)

    @respx.mock
    @pytest.mark.asyncio
    async def test_read_retries_on_401(self) -> None:
        """read_teams_messages should retry once on TokenExpiredError."""
        from openclaw import mcp_server

        respx.get(f"{GRAPH_BASE}/chats/c1/messages").mock(
            side_effect=[
                httpx.Response(401),
                httpx.Response(200, json={"value": [
                    {
                        "id": "m1",
                        "from": {"user": {"displayName": "Human"}},
                        "body": {"content": "reply"},
                        "createdDateTime": "2026-04-06T12:00:00Z",
                    },
                ]}),
            ]
        )

        mock_acquire = MagicMock(return_value="refreshed-token")
        mock_config = MagicMock()

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state.update({
                "initialized": True,
                "token": "old-token",
                "config": mock_config,
                "chat_id": "c1",
                "token_acquired_at": time.monotonic(),
            })

            with patch(
                "openclaw.mcp_server.acquire_agent_user_token",
                mock_acquire,
            ):
                result_json = await mcp_server.read_teams_messages(count=5)

            result = json.loads(result_json)
            assert len(result) == 1
            mock_acquire.assert_called_once()
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)
