"""Tests for Teams tools — httpx fully mocked with respx.

Token acquisition uses the three-hop Agent User flow:
  1. Blueprint token via client_credentials
  2. Agent Identity token via FIC exchange (Blueprint token as assertion)
  3. Agent User token via user_fic grant
"""

from __future__ import annotations

import os
from unittest.mock import patch

import httpx
import pytest
import respx

from openclaw.errors import (
    AgentIDNotAvailable,
    ChatNotFound,
    MessageTooLong,
    RateLimitError,
    TeamsNotLicensed,
    TokenExchangeError,
    TokenExpiredError,
)
from openclaw.tools.teams import (
    GRAPH_BASE,
    MAX_MESSAGE_LENGTH,
    TOKEN_ENDPOINT,
    acquire_agent_user_token,
    create_or_find_chat,
    read,
    send,
)

# ---------------------------------------------------------------------------
# acquire_agent_user_token (three-hop flow)
# ---------------------------------------------------------------------------

FULL_ENV = {
    "OPENCLAW_BLUEPRINT_APP_ID": "bp-id",
    "OPENCLAW_BLUEPRINT_SECRET": "bp-secret",
    "OPENCLAW_TENANT_ID": "tid",
    "OPENCLAW_AGENT_ID": "agent-id",
    "OPENCLAW_AGENT_USER_ID": "agent-user-oid",
}

TOKEN_URL = TOKEN_ENDPOINT.format(tenant="tid")


class TestAcquireAgentUserToken:
    def test_missing_config_raises(self) -> None:
        cleaned = {k: v for k, v in os.environ.items() if not k.startswith("OPENCLAW_")}
        with (
            patch.dict(os.environ, cleaned, clear=True),
            pytest.raises(AgentIDNotAvailable),
        ):
            from openclaw.config import get_config

            acquire_agent_user_token(get_config())

    def test_missing_agent_user_id_raises(self) -> None:
        env = {k: v for k, v in FULL_ENV.items() if k != "OPENCLAW_AGENT_USER_ID"}
        # Clear all OPENCLAW_ vars to avoid interference, then set only ours
        cleaned = {k: v for k, v in os.environ.items() if not k.startswith("OPENCLAW_")}
        cleaned.update(env)
        with (
            patch.dict(os.environ, cleaned, clear=True),
            pytest.raises(AgentIDNotAvailable),
        ):
            from openclaw.config import get_config

            acquire_agent_user_token(get_config())

    @respx.mock
    def test_hop1_failure_raises(self) -> None:
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={
            "error": "invalid_client",
            "error_description": "Bad secret",
        }))
        with (
            patch.dict(os.environ, FULL_ENV, clear=False),
            pytest.raises(TokenExchangeError, match="hop1:blueprint"),
        ):
            from openclaw.config import get_config

            acquire_agent_user_token(get_config())

    @respx.mock
    def test_hop2_failure_raises(self) -> None:
        # Hop 1 succeeds
        respx.post(TOKEN_URL).mock(side_effect=[
            httpx.Response(200, json={"access_token": "bp-token"}),
            httpx.Response(200, json={
                "error": "invalid_grant",
                "error_description": "FIC not configured",
            }),
        ])
        with (
            patch.dict(os.environ, FULL_ENV, clear=False),
            pytest.raises(TokenExchangeError, match="hop2:agent_identity"),
        ):
            from openclaw.config import get_config

            acquire_agent_user_token(get_config())

    @respx.mock
    def test_hop3_failure_raises(self) -> None:
        respx.post(TOKEN_URL).mock(side_effect=[
            httpx.Response(200, json={"access_token": "bp-token"}),
            httpx.Response(200, json={"access_token": "agent-id-token"}),
            httpx.Response(200, json={
                "error": "invalid_grant",
                "error_description": "Agent User not found",
            }),
        ])
        with (
            patch.dict(os.environ, FULL_ENV, clear=False),
            pytest.raises(TokenExchangeError, match="hop3:agent_user"),
        ):
            from openclaw.config import get_config

            acquire_agent_user_token(get_config())

    @respx.mock
    def test_success(self) -> None:
        respx.post(TOKEN_URL).mock(side_effect=[
            httpx.Response(200, json={"access_token": "bp-token"}),
            httpx.Response(200, json={"access_token": "agent-id-token"}),
            httpx.Response(200, json={"access_token": "agent-user-token-123"}),
        ])
        with patch.dict(os.environ, FULL_ENV, clear=False):
            from openclaw.config import get_config

            token = acquire_agent_user_token(get_config())
        assert token == "agent-user-token-123"

    @respx.mock
    def test_correct_hop_payloads(self) -> None:
        """Verify each hop sends the right grant_type and parameters."""
        route = respx.post(TOKEN_URL).mock(side_effect=[
            httpx.Response(200, json={"access_token": "bp-token"}),
            httpx.Response(200, json={"access_token": "agent-id-token"}),
            httpx.Response(200, json={"access_token": "final-token"}),
        ])
        with patch.dict(os.environ, FULL_ENV, clear=False):
            from openclaw.config import get_config

            acquire_agent_user_token(get_config())

        # Hop 1: client_credentials with fmi_path
        hop1_body = dict(x.split("=") for x in route.calls[0].request.content.decode().split("&"))
        assert hop1_body["grant_type"] == "client_credentials"
        assert hop1_body["client_id"] == "bp-id"
        assert hop1_body["fmi_path"] == "agent-id"

        # Hop 2: client_credentials with T1 as assertion
        hop2_body = dict(x.split("=") for x in route.calls[1].request.content.decode().split("&"))
        assert hop2_body["grant_type"] == "client_credentials"
        assert hop2_body["client_id"] == "agent-id"
        assert hop2_body["client_assertion"] == "bp-token"

        # Hop 3: user_fic with T1 + T2
        hop3_body = dict(x.split("=") for x in route.calls[2].request.content.decode().split("&"))
        assert hop3_body["grant_type"] == "user_fic"
        assert hop3_body["user_id"] == "agent-user-oid"
        assert hop3_body["client_assertion"] == "bp-token"
        assert hop3_body["user_federated_identity_credential"] == "agent-id-token"
        assert hop3_body["requested_token_use"] == "on_behalf_of"


# ---------------------------------------------------------------------------
# create_or_find_chat
# ---------------------------------------------------------------------------


class TestCreateOrFindChat:
    @respx.mock
    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        respx.post(f"{GRAPH_BASE}/chats").mock(
            return_value=httpx.Response(
                201,
                json={"id": "19:chat-id@thread.v2", "createdDateTime": "2024-01-01"},
            )
        )
        result = await create_or_find_chat(
            token="agent-token",
            human_user_id="human-uid",
        )
        assert result["chat_id"] == "19:chat-id@thread.v2"

    @respx.mock
    @pytest.mark.asyncio
    async def test_expired_token(self) -> None:
        respx.post(f"{GRAPH_BASE}/chats").mock(return_value=httpx.Response(401))
        with pytest.raises(TokenExpiredError):
            await create_or_find_chat(
                token="expired",
                human_user_id="h",
            )

    @respx.mock
    @pytest.mark.asyncio
    async def test_teams_not_licensed(self) -> None:
        respx.post(f"{GRAPH_BASE}/chats").mock(return_value=httpx.Response(403))
        with pytest.raises(TeamsNotLicensed):
            await create_or_find_chat(
                token="tok",
                human_user_id="h",
            )

    @respx.mock
    @pytest.mark.asyncio
    async def test_rate_limited(self) -> None:
        respx.post(f"{GRAPH_BASE}/chats").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "30"})
        )
        with pytest.raises(RateLimitError) as exc_info:
            await create_or_find_chat(
                token="tok",
                human_user_id="h",
            )
        assert exc_info.value.retry_after == 30


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


class TestTeamsSend:
    @respx.mock
    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        respx.post(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(
                201,
                json={"id": "msg-1", "createdDateTime": "2024-01-01"},
            )
        )
        result = await send(chat_id="c1", message="hello!", token="tok")
        assert result["message_id"] == "msg-1"

    @respx.mock
    @pytest.mark.asyncio
    async def test_html_content(self) -> None:
        route = respx.post(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(201, json={"id": "msg-2", "createdDateTime": "2024-01-01"})
        )
        await send(chat_id="c1", message="<b>hi</b>", token="tok", content_type="html")
        body = route.calls.last.request.content
        assert b"html" in body

    @pytest.mark.asyncio
    async def test_message_too_long(self) -> None:
        long_msg = "x" * (MAX_MESSAGE_LENGTH + 1)
        with pytest.raises(MessageTooLong):
            await send(chat_id="c1", message=long_msg, token="tok")

    @respx.mock
    @pytest.mark.asyncio
    async def test_token_expired(self) -> None:
        respx.post(f"{GRAPH_BASE}/chats/c1/messages").mock(return_value=httpx.Response(401))
        with pytest.raises(TokenExpiredError):
            await send(chat_id="c1", message="hello", token="tok")

    @respx.mock
    @pytest.mark.asyncio
    async def test_rate_limited(self) -> None:
        respx.post(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "42"})
        )
        with pytest.raises(RateLimitError) as exc_info:
            await send(chat_id="c1", message="hello", token="tok")
        assert exc_info.value.retry_after == 42

    @respx.mock
    @pytest.mark.asyncio
    async def test_chat_not_found(self) -> None:
        respx.post(f"{GRAPH_BASE}/chats/nope/messages").mock(return_value=httpx.Response(404))
        with pytest.raises(ChatNotFound):
            await send(chat_id="nope", message="hello", token="tok")


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


class TestTeamsRead:
    @respx.mock
    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        respx.get(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "m1",
                            "from": {"user": {"displayName": "Human"}},
                            "body": {"content": "hi agent"},
                            "createdDateTime": "2024-01-01T12:00:00Z",
                        }
                    ]
                },
            )
        )
        result = await read(chat_id="c1", token="tok", count=5)
        assert len(result) == 1
        assert result[0]["message_id"] == "m1"
        assert result[0]["from"] == "Human"
        assert result[0]["content"] == "hi agent"

    @respx.mock
    @pytest.mark.asyncio
    async def test_token_expired(self) -> None:
        respx.get(f"{GRAPH_BASE}/chats/c1/messages").mock(return_value=httpx.Response(401))
        with pytest.raises(TokenExpiredError):
            await read(chat_id="c1", token="expired")

    @respx.mock
    @pytest.mark.asyncio
    async def test_chat_not_found(self) -> None:
        respx.get(f"{GRAPH_BASE}/chats/nope/messages").mock(return_value=httpx.Response(404))
        with pytest.raises(ChatNotFound):
            await read(chat_id="nope", token="tok")

    @respx.mock
    @pytest.mark.asyncio
    async def test_empty_messages(self) -> None:
        respx.get(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        result = await read(chat_id="c1", token="tok")
        assert result == []
