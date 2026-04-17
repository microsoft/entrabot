"""Tests for Teams tools — httpx fully mocked with respx.

Token acquisition uses the three-hop Agent User flow:
  1. Blueprint token via client_credentials
  2. Agent Identity token via FIC exchange (Blueprint token as assertion)
  3. Agent User token via user_fic grant
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from entraclaw.errors import (
    AgentIDNotAvailable,
    ChatNotFound,
    MessageTooLong,
    RateLimitError,
    TeamsNotLicensed,
    TokenExchangeError,
    TokenExpiredError,
)
from entraclaw.tools.teams import (
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
    "ENTRACLAW_BLUEPRINT_APP_ID": "bp-id",
    "ENTRACLAW_BLUEPRINT_CERT_THUMBPRINT": "fake-thumbprint",
    "ENTRACLAW_TENANT_ID": "tid",
    "ENTRACLAW_AGENT_ID": "agent-id",
    "ENTRACLAW_AGENT_USER_ID": "agent-user-oid",
}


def _mock_credential_store():
    store = MagicMock()
    store.retrieve.return_value = "fake-pem-key"
    return store


_P_STORE = "entraclaw.tools.teams.get_credential_store"
_P_ASSERT = "entraclaw.tools.teams.build_client_assertion"

TOKEN_URL = TOKEN_ENDPOINT.format(tenant="tid")


class TestAcquireAgentUserToken:
    def test_missing_config_raises(self) -> None:
        cleaned = {k: v for k, v in os.environ.items() if not k.startswith("ENTRACLAW_")}
        with (
            patch.dict(os.environ, cleaned, clear=True),
            pytest.raises(AgentIDNotAvailable),
        ):
            from entraclaw.config import get_config

            acquire_agent_user_token(get_config())

    def test_missing_agent_user_id_raises(self) -> None:
        env = {k: v for k, v in FULL_ENV.items() if k != "ENTRACLAW_AGENT_USER_ID"}
        # Clear all ENTRACLAW_ vars to avoid interference, then set only ours
        cleaned = {k: v for k, v in os.environ.items() if not k.startswith("ENTRACLAW_")}
        cleaned.update(env)
        with (
            patch.dict(os.environ, cleaned, clear=True),
            pytest.raises(AgentIDNotAvailable),
        ):
            from entraclaw.config import get_config

            acquire_agent_user_token(get_config())

    @respx.mock
    def test_hop1_failure_raises(self) -> None:
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={
            "error": "invalid_client",
            "error_description": "Bad secret",
        }))
        with (
            patch.dict(os.environ, FULL_ENV, clear=False),
            patch(_P_STORE, return_value=_mock_credential_store()),
            patch(_P_ASSERT, return_value="mocked-jwt-assertion"),
            pytest.raises(TokenExchangeError, match="hop1:blueprint"),
        ):
            from entraclaw.config import get_config

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
            patch(_P_STORE, return_value=_mock_credential_store()),
            patch(_P_ASSERT, return_value="mocked-jwt-assertion"),
            pytest.raises(TokenExchangeError, match="hop2:agent_identity"),
        ):
            from entraclaw.config import get_config

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
            patch(_P_STORE, return_value=_mock_credential_store()),
            patch(_P_ASSERT, return_value="mocked-jwt-assertion"),
            pytest.raises(TokenExchangeError, match="hop3:agent_user"),
        ):
            from entraclaw.config import get_config

            acquire_agent_user_token(get_config())

    @respx.mock
    def test_success(self) -> None:
        respx.post(TOKEN_URL).mock(side_effect=[
            httpx.Response(200, json={"access_token": "bp-token"}),
            httpx.Response(200, json={"access_token": "agent-id-token"}),
            httpx.Response(200, json={"access_token": "agent-user-token-123"}),
        ])
        with (
            patch.dict(os.environ, FULL_ENV, clear=False),
            patch(_P_STORE, return_value=_mock_credential_store()),
            patch(_P_ASSERT, return_value="mocked-jwt-assertion"),
        ):
            from entraclaw.config import get_config

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
        with (
            patch.dict(os.environ, FULL_ENV, clear=False),
            patch(_P_STORE, return_value=_mock_credential_store()),
            patch(_P_ASSERT, return_value="mocked-jwt-assertion"),
        ):
            from entraclaw.config import get_config

            acquire_agent_user_token(get_config())

        # Hop 1: client_credentials with certificate assertion
        hop1_body = dict(x.split("=") for x in route.calls[0].request.content.decode().split("&"))
        assert hop1_body["grant_type"] == "client_credentials"
        assert hop1_body["client_id"] == "bp-id"
        assert hop1_body["fmi_path"] == "agent-id"
        expected_type = (
            "urn%3Aietf%3Aparams%3Aoauth%3A"
            "client-assertion-type%3Ajwt-bearer"
        )
        assert hop1_body["client_assertion_type"] == expected_type
        assert hop1_body["client_assertion"] == "mocked-jwt-assertion"
        assert "client_secret" not in hop1_body

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
            human_user_ids=["human-uid"],
        )
        assert result["chat_id"] == "19:chat-id@thread.v2"

    @respx.mock
    @pytest.mark.asyncio
    async def test_expired_token(self) -> None:
        respx.post(f"{GRAPH_BASE}/chats").mock(return_value=httpx.Response(401))
        with pytest.raises(TokenExpiredError):
            await create_or_find_chat(
                token="expired",
                human_user_ids=["h"],
            )

    @respx.mock
    @pytest.mark.asyncio
    async def test_teams_not_licensed(self) -> None:
        respx.post(f"{GRAPH_BASE}/chats").mock(return_value=httpx.Response(403))
        with pytest.raises(TeamsNotLicensed):
            await create_or_find_chat(
                token="tok",
                human_user_ids=["h"],
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
                human_user_ids=["h"],
            )
        assert exc_info.value.retry_after == 30

    @respx.mock
    @pytest.mark.asyncio
    async def test_group_chat_multiple_users(self) -> None:
        route = respx.post(f"{GRAPH_BASE}/chats").mock(
            return_value=httpx.Response(
                201,
                json={"id": "19:group-chat@thread.v2", "createdDateTime": "2024-01-01"},
            )
        )
        result = await create_or_find_chat(
            token="agent-token",
            human_user_ids=["user-1", "user-2"],
        )
        assert result["chat_id"] == "19:group-chat@thread.v2"
        # Verify group chat type was used
        body = route.calls.last.request.content
        assert b'"group"' in body
        assert b"EntraClaw Agent Chat" in body

    @respx.mock
    @pytest.mark.asyncio
    async def test_federated_user_includes_tenant_id(self) -> None:
        """External/guest users must have tenantId in the member payload (Example 7)."""
        route = respx.post(f"{GRAPH_BASE}/chats").mock(
            return_value=httpx.Response(
                201,
                json={"id": "19:fed-chat@thread.v2", "createdDateTime": "2024-01-01"},
            )
        )
        result = await create_or_find_chat(
            token="agent-token",
            human_user_ids=["guest-obj-id"],
            human_user_tenant_ids=["72f988bf-86f1-41af-91ab-2d7cd011db47"],
            human_user_mails=["adrumea@microsoft.com"],
        )
        assert result["chat_id"] == "19:fed-chat@thread.v2"
        import json

        body = json.loads(route.calls.last.request.content)
        # Find the human member (not the agent)
        human_members = [
            m for m in body["members"] if "adrumea@microsoft.com" in m.get("user@odata.bind", "")
        ]
        assert len(human_members) == 1
        assert human_members[0]["tenantId"] == "72f988bf-86f1-41af-91ab-2d7cd011db47"
        # user@odata.bind should use email, not the guest object ID
        assert "adrumea@microsoft.com" in human_members[0]["user@odata.bind"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_mixed_members_and_guests(self) -> None:
        """In-tenant members should NOT get tenantId; guests should."""
        route = respx.post(f"{GRAPH_BASE}/chats").mock(
            return_value=httpx.Response(
                201,
                json={"id": "19:mixed-chat@thread.v2", "createdDateTime": "2024-01-01"},
            )
        )
        await create_or_find_chat(
            token="agent-token",
            human_user_ids=["member-uid", "guest-obj-id"],
            human_user_tenant_ids=["", "ext-tenant-id"],
            human_user_mails=["brandon@werner.ac", "guest@external.com"],
        )
        import json

        body = json.loads(route.calls.last.request.content)
        # Find the in-tenant member (uses object ID, no tenantId)
        member_entry = [
            m for m in body["members"] if "member-uid" in m.get("user@odata.bind", "")
        ]
        assert len(member_entry) == 1
        assert "tenantId" not in member_entry[0]
        # Find the guest (uses email, has tenantId)
        guest_entry = [
            m for m in body["members"] if "guest@external.com" in m.get("user@odata.bind", "")
        ]
        assert len(guest_entry) == 1
        assert guest_entry[0]["tenantId"] == "ext-tenant-id"

    @respx.mock
    @pytest.mark.asyncio
    async def test_member_user_no_tenant_id(self) -> None:
        """In-tenant members with empty tenant_id get no tenantId field (backward compat)."""
        route = respx.post(f"{GRAPH_BASE}/chats").mock(
            return_value=httpx.Response(
                201,
                json={"id": "19:member-chat@thread.v2", "createdDateTime": "2024-01-01"},
            )
        )
        await create_or_find_chat(
            token="agent-token",
            human_user_ids=["local-uid"],
            human_user_tenant_ids=[""],
            human_user_mails=["brandon@werner.ac"],
        )
        import json

        body = json.loads(route.calls.last.request.content)
        human_members = [
            m for m in body["members"] if "local-uid" in m.get("user@odata.bind", "")
        ]
        assert len(human_members) == 1
        assert "tenantId" not in human_members[0]
        # Uses object ID, not email
        assert "local-uid" in human_members[0]["user@odata.bind"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_guest_user_uses_federated_with_tenant_id(self) -> None:
        """B2B guest users must use Example 7 (federated): email + tenantId + role='owner'.

        Using the guest object ID with role='guest' (Example 6) creates chats
        that are invisible to the guest's Teams client.  The correct approach
        is to reference the user by their home tenant email + tenantId so
        Graph resolves their real identity cross-tenant.
        """
        route = respx.post(f"{GRAPH_BASE}/chats").mock(
            return_value=httpx.Response(
                201,
                json={"id": "19:fed-guest@thread.v2", "createdDateTime": "2024-01-01"},
            )
        )
        result = await create_or_find_chat(
            token="agent-token",
            human_user_ids=["guest-obj-id"],
            agent_user_id="agent-user-id",
            human_user_types=["Guest"],
            human_user_tenant_ids=["72f988bf-86f1-41af-91ab-2d7cd011db47"],
            human_user_mails=["user@microsoft.com"],
        )
        assert result["chat_id"] == "19:fed-guest@thread.v2"
        import json

        body = json.loads(route.calls.last.request.content)
        # Must be oneOnOne (not group) — federated, not guest role
        assert body["chatType"] == "oneOnOne"
        # Find the guest member — should use email, not guest object ID
        human_members = [
            m for m in body["members"] if "user@microsoft.com" in m.get("user@odata.bind", "")
        ]
        assert len(human_members) == 1
        assert human_members[0]["roles"] == ["owner"]
        assert human_members[0]["tenantId"] == "72f988bf-86f1-41af-91ab-2d7cd011db47"
        # Agent user should still be owner
        agent_members = [
            m for m in body["members"] if "agent-user-id" in m.get("user@odata.bind", "")
        ]
        assert len(agent_members) == 1
        assert agent_members[0]["roles"] == ["owner"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_mixed_member_and_guest_types(self) -> None:
        """Group chat with both a Member and a Guest user — guest uses federated."""
        route = respx.post(f"{GRAPH_BASE}/chats").mock(
            return_value=httpx.Response(
                201,
                json={"id": "19:mixed@thread.v2", "createdDateTime": "2024-01-01"},
            )
        )
        await create_or_find_chat(
            token="agent-token",
            human_user_ids=["member-uid", "guest-uid"],
            human_user_types=["Member", "Guest"],
            human_user_tenant_ids=["", "ext-tenant-id"],
            human_user_mails=["brandon@werner.ac", "guest@external.com"],
        )
        import json

        body = json.loads(route.calls.last.request.content)
        # Group because 2 humans
        assert body["chatType"] == "group"
        member_entry = [
            m for m in body["members"] if "member-uid" in m.get("user@odata.bind", "")
        ]
        assert member_entry[0]["roles"] == ["owner"]
        assert "tenantId" not in member_entry[0]
        # Guest uses federated: email + tenantId
        guest_entry = [
            m for m in body["members"] if "guest@external.com" in m.get("user@odata.bind", "")
        ]
        assert guest_entry[0]["roles"] == ["owner"]
        assert guest_entry[0]["tenantId"] == "ext-tenant-id"

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_user_types_defaults_to_owner(self) -> None:
        """When human_user_types is not provided, all users default to 'owner' role."""
        route = respx.post(f"{GRAPH_BASE}/chats").mock(
            return_value=httpx.Response(
                201,
                json={"id": "19:default@thread.v2", "createdDateTime": "2024-01-01"},
            )
        )
        await create_or_find_chat(
            token="agent-token",
            human_user_ids=["user-1"],
        )
        import json

        body = json.loads(route.calls.last.request.content)
        # Single user without types → oneOnOne, owner role
        assert body["chatType"] == "oneOnOne"
        human_members = [
            m for m in body["members"] if "user-1" in m.get("user@odata.bind", "")
        ]
        assert human_members[0]["roles"] == ["owner"]


# ---------------------------------------------------------------------------
# add_member
# ---------------------------------------------------------------------------


class TestAddMember:
    @respx.mock
    @pytest.mark.asyncio
    async def test_add_federated_member(self) -> None:
        """Add a federated user to an existing chat."""
        from entraclaw.tools.teams import add_member

        route = respx.post(
            f"{GRAPH_BASE}/chats/19:chat-id@thread.v2/members"
        ).mock(
            return_value=httpx.Response(
                201,
                json={
                    "id": "member-id-123",
                    "displayName": "New User",
                    "roles": ["owner"],
                },
            )
        )
        result = await add_member(
            chat_id="19:chat-id@thread.v2",
            token="agent-token",
            email="newuser@microsoft.com",
            tenant_id="72f988bf-86f1-41af-91ab-2d7cd011db47",
        )
        assert result["display_name"] == "New User"
        import json

        body = json.loads(route.calls.last.request.content)
        assert body["tenantId"] == "72f988bf-86f1-41af-91ab-2d7cd011db47"
        assert "newuser@microsoft.com" in body["user@odata.bind"]
        assert body["roles"] == ["owner"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_add_in_tenant_member(self) -> None:
        """Add an in-tenant member by object ID (no tenantId)."""
        from entraclaw.tools.teams import add_member

        route = respx.post(
            f"{GRAPH_BASE}/chats/19:chat-id@thread.v2/members"
        ).mock(
            return_value=httpx.Response(
                201,
                json={
                    "id": "member-id-456",
                    "displayName": "Local User",
                    "roles": ["owner"],
                },
            )
        )
        result = await add_member(
            chat_id="19:chat-id@thread.v2",
            token="agent-token",
            email="local@werner.ac",
        )
        assert result["display_name"] == "Local User"
        import json

        body = json.loads(route.calls.last.request.content)
        assert "tenantId" not in body
        assert "local@werner.ac" in body["user@odata.bind"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_add_member_404(self) -> None:
        """404 when user email doesn't resolve."""
        from entraclaw.tools.teams import add_member

        respx.post(
            f"{GRAPH_BASE}/chats/19:chat-id@thread.v2/members"
        ).mock(
            return_value=httpx.Response(
                404,
                json={"error": {"message": "User not found"}},
            )
        )
        with pytest.raises(ChatNotFound):
            await add_member(
                chat_id="19:chat-id@thread.v2",
                token="agent-token",
                email="nobody@microsoft.com",
                tenant_id="72f988bf-86f1-41af-91ab-2d7cd011db47",
            )


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

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_with_mentions(self) -> None:
        """Mentions array is included in the Graph API payload."""
        route = respx.post(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(201, json={"id": "msg-m", "createdDateTime": "2024-01-01"})
        )
        mentions = [
            {"id": 0, "name": "Alice Example", "user_id": "user-guid-eric"},
        ]
        result = await send(
            chat_id="c1",
            message='<at id="0">Alice Example</at> check this out',
            token="tok",
            content_type="html",
            mentions=mentions,
        )
        assert result["message_id"] == "msg-m"
        import json as _json

        body = _json.loads(route.calls.last.request.content)
        assert "mentions" in body
        assert body["mentions"][0]["mentioned"]["user"]["id"] == "user-guid-eric"
        assert body["mentions"][0]["mentionText"] == "Alice Example"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_with_multiple_mentions(self) -> None:
        """Multiple mentions are all included in the payload."""
        route = respx.post(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(201, json={"id": "msg-mm", "createdDateTime": "2024-01-01"})
        )
        mentions = [
            {"id": 0, "name": "Carol Sample", "user_id": "user-guid-ayse"},
            {"id": 1, "name": "Alice Example", "user_id": "user-guid-eric"},
        ]
        await send(
            chat_id="c1",
            message='<at id="0">Carol Sample</at> and <at id="1">Alice Example</at>',
            token="tok",
            content_type="html",
            mentions=mentions,
        )
        import json as _json

        body = _json.loads(route.calls.last.request.content)
        assert len(body["mentions"]) == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_without_mentions_omits_field(self) -> None:
        """When no mentions are passed, the payload has no mentions key."""
        route = respx.post(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(201, json={"id": "msg-nm", "createdDateTime": "2024-01-01"})
        )
        await send(chat_id="c1", message="no tags here", token="tok")
        import json as _json

        body = _json.loads(route.calls.last.request.content)
        assert "mentions" not in body


# ---------------------------------------------------------------------------
# list_members
# ---------------------------------------------------------------------------


class TestListMembers:
    @respx.mock
    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        from entraclaw.tools.teams import list_members

        respx.get(f"{GRAPH_BASE}/chats/c1/members").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "userId": "user-1",
                            "displayName": "Alice Example",
                            "email": "user@example.com",
                            "roles": ["owner"],
                        },
                        {
                            "userId": "user-2",
                            "displayName": "Brandon Werner",
                            "email": "brandon@werner.ac",
                            "roles": ["owner"],
                        },
                    ]
                },
            )
        )
        result = await list_members(chat_id="c1", token="tok")
        assert len(result) == 2
        assert result[0]["user_id"] == "user-1"
        assert result[0]["name"] == "Alice Example"
        assert result[1]["name"] == "Brandon Werner"

    @respx.mock
    @pytest.mark.asyncio
    async def test_token_expired(self) -> None:
        from entraclaw.tools.teams import list_members

        respx.get(f"{GRAPH_BASE}/chats/c1/members").mock(
            return_value=httpx.Response(401)
        )
        with pytest.raises(TokenExpiredError):
            await list_members(chat_id="c1", token="tok")


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


# ---------------------------------------------------------------------------
# create_one_on_one_chat
# ---------------------------------------------------------------------------


class TestCreateOneOnOneChat:
    @respx.mock
    @pytest.mark.asyncio
    async def test_creates_one_on_one_by_email(self) -> None:
        """Creates a 1:1 chat using the target user's email."""
        from entraclaw.tools.teams import create_one_on_one_chat

        route = respx.post(f"{GRAPH_BASE}/chats").mock(
            return_value=httpx.Response(
                201,
                json={
                    "id": "19:dm-chat@thread.v2",
                    "chatType": "oneOnOne",
                    "createdDateTime": "2024-01-01",
                },
            )
        )
        result = await create_one_on_one_chat(
            token="agent-token",
            target_email="brandon@werner.ac",
            agent_user_id="agent-oid-123",
        )
        assert result["chat_id"] == "19:dm-chat@thread.v2"
        import json

        body = json.loads(route.calls.last.request.content)
        assert body["chatType"] == "oneOnOne"
        # Target user referenced by email
        target_members = [
            m for m in body["members"] if "brandon@werner.ac" in m.get("user@odata.bind", "")
        ]
        assert len(target_members) == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_cross_tenant_includes_tenant_id(self) -> None:
        """Cross-tenant 1:1 chat includes tenantId in the member payload."""
        from entraclaw.tools.teams import create_one_on_one_chat

        route = respx.post(f"{GRAPH_BASE}/chats").mock(
            return_value=httpx.Response(
                201,
                json={
                    "id": "19:xt-dm@thread.v2",
                    "chatType": "oneOnOne",
                    "createdDateTime": "2024-01-01",
                },
            )
        )
        result = await create_one_on_one_chat(
            token="agent-token",
            target_email="user@microsoft.com",
            target_tenant_id="72f988bf-86f1-41af-91ab-2d7cd011db47",
            agent_user_id="agent-oid-123",
        )
        assert result["chat_id"] == "19:xt-dm@thread.v2"
        import json

        body = json.loads(route.calls.last.request.content)
        target_members = [
            m for m in body["members"] if "user@microsoft.com" in m.get("user@odata.bind", "")
        ]
        assert len(target_members) == 1
        assert target_members[0]["tenantId"] == "72f988bf-86f1-41af-91ab-2d7cd011db47"

    @respx.mock
    @pytest.mark.asyncio
    async def test_token_expired(self) -> None:
        from entraclaw.tools.teams import create_one_on_one_chat

        respx.post(f"{GRAPH_BASE}/chats").mock(return_value=httpx.Response(401))
        with pytest.raises(TokenExpiredError):
            await create_one_on_one_chat(
                token="expired",
                target_email="someone@example.com",
                agent_user_id="agent-oid-123",
            )

    @respx.mock
    @pytest.mark.asyncio
    async def test_not_licensed(self) -> None:
        from entraclaw.tools.teams import create_one_on_one_chat

        respx.post(f"{GRAPH_BASE}/chats").mock(return_value=httpx.Response(403))
        with pytest.raises(TeamsNotLicensed):
            await create_one_on_one_chat(
                token="tok",
                target_email="someone@example.com",
                agent_user_id="agent-oid-123",
            )


# ---------------------------------------------------------------------------
# fetch_hosted_image
# ---------------------------------------------------------------------------


class TestFetchHostedImage:
    @respx.mock
    @pytest.mark.asyncio
    async def test_fetches_image_bytes(self) -> None:
        from entraclaw.tools.teams import fetch_hosted_image

        img_url = f"{GRAPH_BASE}/chats/c1/messages/m1/hostedContents/img1/$value"
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        respx.get(img_url).mock(return_value=httpx.Response(200, content=fake_png))

        result = await fetch_hosted_image(token="tok", url=img_url)
        assert result == fake_png

    @respx.mock
    @pytest.mark.asyncio
    async def test_401_raises_token_expired(self) -> None:
        from entraclaw.tools.teams import fetch_hosted_image

        img_url = f"{GRAPH_BASE}/chats/c1/messages/m1/hostedContents/img1/$value"
        respx.get(img_url).mock(return_value=httpx.Response(401))

        with pytest.raises(TokenExpiredError):
            await fetch_hosted_image(token="tok", url=img_url)

    @respx.mock
    @pytest.mark.asyncio
    async def test_404_returns_none(self) -> None:
        from entraclaw.tools.teams import fetch_hosted_image

        img_url = f"{GRAPH_BASE}/chats/c1/messages/m1/hostedContents/img1/$value"
        respx.get(img_url).mock(return_value=httpx.Response(404))

        result = await fetch_hosted_image(token="tok", url=img_url)
        assert result is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_rejects_non_graph_urls(self) -> None:
        from entraclaw.tools.teams import fetch_hosted_image

        with pytest.raises(ValueError, match="not a Graph API"):
            await fetch_hosted_image(
                token="tok", url="https://evil.com/steal-token"
            )
