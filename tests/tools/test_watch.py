"""Tests for watch_teams_replies and supporting functions."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from entraclaw.tools.teams import GRAPH_BASE, filter_human_messages


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
                "from": "EntraClaw Agent",
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
        result = filter_human_messages(messages, agent_user_display_name="EntraClaw Agent")
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
        result = filter_human_messages(messages, agent_user_display_name="EntraClaw Agent")
        assert len(result) == 1
        assert result[0]["message_id"] == "m1"

    def test_empty_list(self) -> None:
        result = filter_human_messages([], agent_user_display_name="EntraClaw Agent")
        assert result == []

    def test_all_agent_messages(self) -> None:
        messages = [
            {
                "message_id": "m1",
                "from": "EntraClaw Agent",
                "content": "hi",
                "sent_at": "2026-04-06T12:00:00Z",
            },
        ]
        result = filter_human_messages(messages, agent_user_display_name="EntraClaw Agent")
        assert result == []


class TestEagerTokenRefresh:
    @pytest.mark.asyncio
    async def test_refreshes_when_expired(self) -> None:
        """Token older than 55 min should be refreshed via identity-aware dispatch."""
        from entraclaw import mcp_server
        from entraclaw.identity.state_machine import IdentityStateMachine
        from entraclaw.models import IdentityState

        mock_acquire = MagicMock(return_value="new-token")
        mock_config = MagicMock()

        old_state = mcp_server._state.copy()
        old_identity = mcp_server._identity
        try:
            # Set up identity state machine in AGENT_USER state
            sm = IdentityStateMachine()
            await sm.transition(IdentityState.AGENT_USER)
            sm.update_session(
                token="old-token",
                token_acquired_at=time.monotonic() - 3400,
            )
            mcp_server._identity = sm
            mcp_server._state["token"] = "old-token"
            mcp_server._state["config"] = mock_config

            with patch("entraclaw.mcp_server.acquire_agent_user_token", mock_acquire):
                await mcp_server._ensure_valid_token()

            assert mcp_server._identity.session.token == "new-token"
            assert mcp_server._state["token"] == "new-token"
            mock_acquire.assert_called_once_with(mock_config)
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)
            mcp_server._identity = old_identity

    @pytest.mark.asyncio
    async def test_no_refresh_when_fresh(self) -> None:
        """Token younger than 55 min should NOT be refreshed."""
        from entraclaw import mcp_server
        from entraclaw.identity.state_machine import IdentityStateMachine
        from entraclaw.models import IdentityState

        mock_acquire = MagicMock(return_value="new-token")
        mock_config = MagicMock()

        old_state = mcp_server._state.copy()
        old_identity = mcp_server._identity
        try:
            sm = IdentityStateMachine()
            await sm.transition(IdentityState.AGENT_USER)
            sm.update_session(
                token="fresh-token",
                token_acquired_at=time.monotonic() - 100,
            )
            mcp_server._identity = sm
            mcp_server._state["token"] = "fresh-token"
            mcp_server._state["config"] = mock_config

            with patch("entraclaw.mcp_server.acquire_agent_user_token", mock_acquire):
                await mcp_server._ensure_valid_token()

            assert mcp_server._identity.session.token == "fresh-token"
            mock_acquire.assert_not_called()
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)
            mcp_server._identity = old_identity


class TestLazyTokenRetry:
    @pytest.mark.asyncio
    async def test_retry_on_401(self) -> None:
        """TokenExpiredError on first call should trigger refresh + retry."""
        from entraclaw import mcp_server
        from entraclaw.errors import TokenExpiredError
        from entraclaw.identity.state_machine import IdentityStateMachine
        from entraclaw.models import IdentityState

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
        old_identity = mcp_server._identity
        try:
            sm = IdentityStateMachine()
            await sm.transition(IdentityState.AGENT_USER)
            sm.update_session(
                token="old-token",
                token_acquired_at=time.monotonic(),
            )
            mcp_server._identity = sm
            mcp_server._state["token"] = "old-token"
            mcp_server._state["config"] = mock_config

            with patch("entraclaw.mcp_server.acquire_agent_user_token", mock_acquire):
                result = await mcp_server._with_token_retry(flaky_fn)

            assert result == "result-with-refreshed-token"
            assert mcp_server._identity.session.token == "refreshed-token"
            mock_acquire.assert_called_once()
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)
            mcp_server._identity = old_identity

    @pytest.mark.asyncio
    async def test_raises_if_retry_also_fails(self) -> None:
        """If both attempts fail with TokenExpiredError, propagate the error."""
        from entraclaw import mcp_server
        from entraclaw.errors import TokenExpiredError
        from entraclaw.identity.state_machine import IdentityStateMachine
        from entraclaw.models import IdentityState

        async def always_fails(*, token: str) -> str:
            raise TokenExpiredError("still expired")

        mock_acquire = MagicMock(return_value="refreshed-token")
        mock_config = MagicMock()

        old_state = mcp_server._state.copy()
        old_identity = mcp_server._identity
        try:
            sm = IdentityStateMachine()
            await sm.transition(IdentityState.AGENT_USER)
            sm.update_session(
                token="old-token",
                token_acquired_at=time.monotonic(),
            )
            mcp_server._identity = sm
            mcp_server._state["token"] = "old-token"
            mcp_server._state["config"] = mock_config

            with (
                patch("entraclaw.mcp_server.acquire_agent_user_token", mock_acquire),
                pytest.raises(TokenExpiredError),
            ):
                await mcp_server._with_token_retry(always_fails)
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)
            mcp_server._identity = old_identity


class TestExistingToolsRetrofitted:
    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retries_on_401(self) -> None:
        """send_teams_message should retry once on TokenExpiredError."""
        from entraclaw import mcp_server
        from entraclaw.identity.state_machine import IdentityStateMachine
        from entraclaw.models import IdentityState

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
        old_identity = mcp_server._identity
        try:
            sm = IdentityStateMachine()
            await sm.transition(IdentityState.AGENT_USER)
            sm.update_session(
                token="old-token",
                token_acquired_at=time.monotonic(),
            )
            mcp_server._identity = sm
            mcp_server._state.update(
                {
                    "initialized": True,
                    "token": "old-token",
                    "config": mock_config,
                    "chat_id": "c1",
                }
            )

            with patch(
                "entraclaw.mcp_server.acquire_agent_user_token",
                mock_acquire,
            ):
                result_json = await mcp_server.send_teams_message("hello")

            result = json.loads(result_json)
            assert result["message_id"] == "msg-1"
            mock_acquire.assert_called_once()
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)
            mcp_server._identity = old_identity

    @respx.mock
    @pytest.mark.asyncio
    async def test_read_retries_on_401(self) -> None:
        """read_teams_messages should retry once on TokenExpiredError."""
        from entraclaw import mcp_server
        from entraclaw.identity.state_machine import IdentityStateMachine
        from entraclaw.models import IdentityState

        respx.get(f"{GRAPH_BASE}/chats/c1/messages").mock(
            side_effect=[
                httpx.Response(401),
                httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "id": "m1",
                                "from": {"user": {"displayName": "Human"}},
                                "body": {"content": "reply"},
                                "createdDateTime": "2026-04-06T12:00:00Z",
                            },
                        ]
                    },
                ),
            ]
        )

        mock_acquire = MagicMock(return_value="refreshed-token")
        mock_config = MagicMock()

        old_state = mcp_server._state.copy()
        old_identity = mcp_server._identity
        try:
            sm = IdentityStateMachine()
            await sm.transition(IdentityState.AGENT_USER)
            sm.update_session(
                token="old-token",
                token_acquired_at=time.monotonic(),
            )
            mcp_server._identity = sm
            mcp_server._state.update(
                {
                    "initialized": True,
                    "token": "old-token",
                    "config": mock_config,
                    "chat_id": "c1",
                }
            )

            with patch(
                "entraclaw.mcp_server.acquire_agent_user_token",
                mock_acquire,
            ):
                result_json = await mcp_server.read_teams_messages(count=5)

            result = json.loads(result_json)
            assert len(result) == 1
            mock_acquire.assert_called_once()
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)
            mcp_server._identity = old_identity


class TestDedupHelpers:
    def test_new_messages_detected(self) -> None:
        """Messages newer than cursor and not in seen-set should be returned."""
        from entraclaw.mcp_server import _filter_new_messages

        messages = [
            {"message_id": "m1", "sent_at": "2026-04-06T12:00:05Z"},
            {"message_id": "m2", "sent_at": "2026-04-06T12:00:10Z"},
        ]
        result = _filter_new_messages(
            messages=messages,
            last_seen_timestamp="2026-04-06T12:00:00Z",
            seen_ids=set(),
        )
        assert len(result) == 2

    def test_skips_already_seen(self) -> None:
        """Messages in seen-set should be excluded even if timestamp is newer."""
        from entraclaw.mcp_server import _filter_new_messages

        messages = [
            {"message_id": "m1", "sent_at": "2026-04-06T12:00:05Z"},
            {"message_id": "m2", "sent_at": "2026-04-06T12:00:10Z"},
        ]
        result = _filter_new_messages(
            messages=messages,
            last_seen_timestamp="2026-04-06T12:00:00Z",
            seen_ids={"m1"},
        )
        assert len(result) == 1
        assert result[0]["message_id"] == "m2"

    def test_overlap_window_catches_boundary(self) -> None:
        """Messages within the 2s overlap window should be included (if not seen)."""
        from entraclaw.mcp_server import _overlap_timestamp

        ts = "2026-04-06T12:00:10Z"
        overlap_ts = _overlap_timestamp(ts)
        assert overlap_ts == "2026-04-06T12:00:08Z"

    def test_seen_set_cleanup(self) -> None:
        """Seen-set should be pruned when it exceeds 500 entries."""
        from entraclaw.mcp_server import _prune_seen_set

        seen = {f"msg-{i}" for i in range(501)}
        now = datetime.now(UTC)
        timestamps = {}
        for i in range(501):
            if i >= 451:
                timestamps[f"msg-{i}"] = (now - timedelta(minutes=1)).isoformat()
            else:
                timestamps[f"msg-{i}"] = (now - timedelta(minutes=20)).isoformat()

        pruned = _prune_seen_set(seen, timestamps)
        assert len(pruned) <= 500
        assert f"msg-{500}" in pruned


class TestWatchTeamsReplies:
    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_new_human_messages(self) -> None:
        """Should return only new human messages, not agent or system messages."""
        from entraclaw import mcp_server

        respx.get(f"{GRAPH_BASE}/chats/c1/messages").mock(
            side_effect=[
                # First call: bootstrap — sets cursor
                httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "id": "old-1",
                                "from": {"user": {"displayName": "Human"}},
                                "body": {"content": "old msg"},
                                "createdDateTime": "2026-04-06T11:59:00Z",
                            },
                        ]
                    },
                ),
                # Second call: new messages
                httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "id": "new-1",
                                "from": {"user": {"displayName": "Human"}},
                                "body": {"content": "do something"},
                                "createdDateTime": "2026-04-06T12:00:05Z",
                            },
                            {
                                "id": "agent-1",
                                "from": {"user": {"displayName": "EntraClaw Agent"}},
                                "body": {"content": "sure thing"},
                                "createdDateTime": "2026-04-06T12:00:06Z",
                            },
                        ]
                    },
                ),
            ]
        )

        mock_acquire = MagicMock(return_value="token")
        mock_config = MagicMock()
        mock_config.agent_user_upn = "EntraClaw Agent"

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state.update(
                {
                    "initialized": True,
                    "token": "token",
                    "config": mock_config,
                    "chat_id": "c1",
                    "token_acquired_at": time.monotonic(),
                    "last_seen_timestamp": None,
                    "seen_message_ids": set(),
                    "seen_id_timestamps": {},
                }
            )

            with patch("entraclaw.mcp_server.acquire_agent_user_token", mock_acquire):
                result_json = await mcp_server.watch_teams_replies(timeout=5, interval=0)

            result = json.loads(result_json)
            assert result["timed_out"] is False
            assert len(result["messages"]) == 1
            assert result["messages"][0]["message_id"] == "new-1"
            assert result["messages"][0]["content"] == "do something"
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)

    @respx.mock
    @pytest.mark.asyncio
    async def test_timeout_returns_empty(self) -> None:
        """Should return timed_out=true when no new messages arrive."""
        from entraclaw import mcp_server

        respx.get(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "old-1",
                            "from": {"user": {"displayName": "Human"}},
                            "body": {"content": "old"},
                            "createdDateTime": "2026-04-06T11:59:00Z",
                        },
                    ]
                },
            )
        )

        mock_config = MagicMock()
        mock_config.agent_user_upn = "EntraClaw Agent"

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state.update(
                {
                    "initialized": True,
                    "token": "token",
                    "config": mock_config,
                    "chat_id": "c1",
                    "token_acquired_at": time.monotonic(),
                    "last_seen_timestamp": None,
                    "seen_message_ids": set(),
                    "seen_id_timestamps": {},
                }
            )

            with patch("entraclaw.mcp_server.acquire_agent_user_token", MagicMock()):
                result_json = await mcp_server.watch_teams_replies(timeout=1, interval=0)

            result = json.loads(result_json)
            assert result["timed_out"] is True
            assert result["messages"] == []
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)

    @respx.mock
    @pytest.mark.asyncio
    async def test_cursor_advances(self) -> None:
        """After returning messages, cursor should advance to newest timestamp."""
        from entraclaw import mcp_server

        respx.get(f"{GRAPH_BASE}/chats/c1/messages").mock(
            side_effect=[
                # Bootstrap call
                httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "id": "m0",
                                "from": {"user": {"displayName": "Human"}},
                                "body": {"content": "seed"},
                                "createdDateTime": "2026-04-06T11:59:00Z",
                            },
                        ]
                    },
                ),
                # First watch: new message
                httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "id": "m1",
                                "from": {"user": {"displayName": "Human"}},
                                "body": {"content": "first"},
                                "createdDateTime": "2026-04-06T12:00:05Z",
                            },
                        ]
                    },
                ),
            ]
        )

        mock_config = MagicMock()
        mock_config.agent_user_upn = "EntraClaw Agent"

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state.update(
                {
                    "initialized": True,
                    "token": "token",
                    "config": mock_config,
                    "chat_id": "c1",
                    "token_acquired_at": time.monotonic(),
                    "last_seen_timestamp": None,
                    "seen_message_ids": set(),
                    "seen_id_timestamps": {},
                }
            )

            with patch("entraclaw.mcp_server.acquire_agent_user_token", MagicMock()):
                await mcp_server.watch_teams_replies(timeout=5, interval=0)

            assert mcp_server._state["last_seen_timestamp"] == "2026-04-06T12:00:05Z"
            assert "m1" in mcp_server._state["seen_message_ids"]
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)


class TestRateLimitHandling:
    @respx.mock
    @pytest.mark.asyncio
    async def test_429_propagates_from_watch(self) -> None:
        """watch_teams_replies should propagate RateLimitError from read()."""
        from entraclaw import mcp_server
        from entraclaw.errors import RateLimitError

        respx.get(f"{GRAPH_BASE}/chats/c1/messages").mock(
            side_effect=[
                # Bootstrap call succeeds
                httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "id": "m0",
                                "from": {"user": {"displayName": "Human"}},
                                "body": {"content": "seed"},
                                "createdDateTime": "2026-04-06T11:59:00Z",
                            },
                        ]
                    },
                ),
                # Second call: 429 (retry transport retries up to 3 more times)
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(429, headers={"Retry-After": "0"}),
            ]
        )

        mock_config = MagicMock()
        mock_config.agent_user_upn = "EntraClaw Agent"

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state.update(
                {
                    "initialized": True,
                    "token": "token",
                    "config": mock_config,
                    "chat_id": "c1",
                    "token_acquired_at": time.monotonic(),
                    "last_seen_timestamp": None,
                    "seen_message_ids": set(),
                    "seen_id_timestamps": {},
                }
            )

            with (
                patch("entraclaw.mcp_server.acquire_agent_user_token", MagicMock()),
                pytest.raises(RateLimitError) as exc_info,
            ):
                await mcp_server.watch_teams_replies(timeout=5, interval=0)

            assert exc_info.value.retry_after == 0
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)
