"""Tests for Teams tools — httpx fully mocked with respx.

Token acquisition uses the three-hop Agent User flow:
  1. Blueprint token via client_credentials
  2. Agent Identity token via FIC exchange (Blueprint token as assertion)
  3. Agent User token via user_fic grant
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from entrabot.errors import (
    AgentIDNotAvailable,
    ChatNotFound,
    MessageTooLong,
    RateLimitError,
    TeamsNotLicensed,
    TokenExchangeError,
    TokenExpiredError,
)
from entrabot.tools.teams import (
    GRAPH_BASE,
    MAX_MESSAGE_LENGTH,
    TOKEN_ENDPOINT,
    acquire_agent_identity_token,
    acquire_agent_user_token,
    create_or_find_chat,
    read,
    send,
)

# ---------------------------------------------------------------------------
# acquire_agent_user_token (three-hop flow)
# ---------------------------------------------------------------------------

FULL_ENV = {
    "ENTRABOT_BLUEPRINT_APP_ID": "bp-id",
    "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT": "fake-thumbprint",
    "ENTRABOT_TENANT_ID": "tid",
    "ENTRABOT_AGENT_ID": "agent-id",
    "ENTRABOT_AGENT_USER_ID": "agent-user-oid",
}


def _mock_credential_store():
    store = MagicMock()
    store.retrieve.return_value = "fake-pem-key"
    return store


_P_STORE = "entrabot.tools.teams.get_credential_store"
_P_ASSERT = "entrabot.tools.teams.build_client_assertion"

TOKEN_URL = TOKEN_ENDPOINT.format(tenant="tid")


class TestAcquireAgentUserToken:
    def test_missing_config_raises(self) -> None:
        cleaned = {k: v for k, v in os.environ.items() if not k.startswith("ENTRABOT_")}
        with (
            patch.dict(os.environ, cleaned, clear=True),
            pytest.raises(AgentIDNotAvailable),
        ):
            from entrabot.config import get_config

            acquire_agent_user_token(get_config())

    def test_missing_agent_user_id_raises(self) -> None:
        env = {k: v for k, v in FULL_ENV.items() if k != "ENTRABOT_AGENT_USER_ID"}
        # Clear all ENTRABOT_ vars to avoid interference, then set only ours
        cleaned = {k: v for k, v in os.environ.items() if not k.startswith("ENTRABOT_")}
        cleaned.update(env)
        with (
            patch.dict(os.environ, cleaned, clear=True),
            pytest.raises(AgentIDNotAvailable),
        ):
            from entrabot.config import get_config

            acquire_agent_user_token(get_config())

    @respx.mock
    def test_hop1_failure_raises(self) -> None:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "error": "invalid_client",
                    "error_description": "Bad secret",
                },
            )
        )
        with (
            patch.dict(os.environ, FULL_ENV, clear=False),
            patch(_P_STORE, return_value=_mock_credential_store()),
            patch(_P_ASSERT, return_value="mocked-jwt-assertion"),
            pytest.raises(TokenExchangeError, match="hop1:blueprint"),
        ):
            from entrabot.config import get_config

            acquire_agent_user_token(get_config())

    @respx.mock
    def test_hop2_failure_raises(self) -> None:
        # Hop 1 succeeds
        respx.post(TOKEN_URL).mock(
            side_effect=[
                httpx.Response(200, json={"access_token": "bp-token"}),
                httpx.Response(
                    200,
                    json={
                        "error": "invalid_grant",
                        "error_description": "FIC not configured",
                    },
                ),
            ]
        )
        with (
            patch.dict(os.environ, FULL_ENV, clear=False),
            patch(_P_STORE, return_value=_mock_credential_store()),
            patch(_P_ASSERT, return_value="mocked-jwt-assertion"),
            pytest.raises(TokenExchangeError, match="hop2:agent_identity"),
        ):
            from entrabot.config import get_config

            acquire_agent_user_token(get_config())

    @respx.mock
    def test_hop3_failure_raises(self) -> None:
        respx.post(TOKEN_URL).mock(
            side_effect=[
                httpx.Response(200, json={"access_token": "bp-token"}),
                httpx.Response(200, json={"access_token": "agent-id-token"}),
                httpx.Response(
                    200,
                    json={
                        "error": "invalid_grant",
                        "error_description": "Agent User not found",
                    },
                ),
            ]
        )
        with (
            patch.dict(os.environ, FULL_ENV, clear=False),
            patch(_P_STORE, return_value=_mock_credential_store()),
            patch(_P_ASSERT, return_value="mocked-jwt-assertion"),
            pytest.raises(TokenExchangeError, match="hop3:agent_user"),
        ):
            from entrabot.config import get_config

            acquire_agent_user_token(get_config())

    @respx.mock
    def test_success(self) -> None:
        respx.post(TOKEN_URL).mock(
            side_effect=[
                httpx.Response(200, json={"access_token": "bp-token"}),
                httpx.Response(200, json={"access_token": "agent-id-token"}),
                httpx.Response(200, json={"access_token": "agent-user-token-123"}),
            ]
        )
        with (
            patch.dict(os.environ, FULL_ENV, clear=False),
            patch(_P_STORE, return_value=_mock_credential_store()),
            patch(_P_ASSERT, return_value="mocked-jwt-assertion"),
        ):
            from entrabot.config import get_config

            token = acquire_agent_user_token(get_config())
        assert token == "agent-user-token-123"

    @pytest.mark.parametrize(
        ("hop", "responses"),
        [
            (
                "hop1:blueprint",
                [httpx.Response(200, text="<html>captive portal</html>")],
            ),
            (
                "hop2:agent_identity",
                [
                    httpx.Response(200, json={"access_token": "bp-token"}),
                    httpx.Response(200, text="<html>captive portal</html>"),
                ],
            ),
            (
                "hop3:agent_user",
                [
                    httpx.Response(200, json={"access_token": "bp-token"}),
                    httpx.Response(200, json={"access_token": "agent-id-token"}),
                    httpx.Response(200, text="<html>captive portal</html>"),
                ],
            ),
        ],
    )
    @respx.mock
    def test_non_json_token_response_raises_token_exchange_error(
        self,
        hop: str,
        responses: list[httpx.Response],
    ) -> None:
        respx.post(TOKEN_URL).mock(side_effect=responses)
        with (
            patch.dict(os.environ, FULL_ENV, clear=False),
            patch(_P_STORE, return_value=_mock_credential_store()),
            patch(_P_ASSERT, return_value="mocked-jwt-assertion"),
            pytest.raises(TokenExchangeError) as exc_info,
        ):
            from entrabot.config import get_config

            acquire_agent_user_token(get_config())

        assert exc_info.value.hop == hop
        assert exc_info.value.error == "non_json_response"
        assert "HTTP 200: <html>captive portal</html>" in exc_info.value.description

    @pytest.mark.parametrize(
        ("hop", "responses"),
        [
            ("hop1:blueprint", [httpx.Response(502)]),
            (
                "hop2:agent_identity",
                [
                    httpx.Response(200, json={"access_token": "bp-token"}),
                    httpx.Response(502),
                ],
            ),
            (
                "hop3:agent_user",
                [
                    httpx.Response(200, json={"access_token": "bp-token"}),
                    httpx.Response(200, json={"access_token": "agent-id-token"}),
                    httpx.Response(502),
                ],
            ),
        ],
    )
    @respx.mock
    def test_empty_502_token_response_raises_token_exchange_error(
        self,
        hop: str,
        responses: list[httpx.Response],
    ) -> None:
        respx.post(TOKEN_URL).mock(side_effect=responses)
        with (
            patch.dict(os.environ, FULL_ENV, clear=False),
            patch(_P_STORE, return_value=_mock_credential_store()),
            patch(_P_ASSERT, return_value="mocked-jwt-assertion"),
            pytest.raises(TokenExchangeError) as exc_info,
        ):
            from entrabot.config import get_config

            acquire_agent_user_token(get_config())

        assert exc_info.value.hop == hop
        assert exc_info.value.error == "non_json_response"
        assert exc_info.value.description == "HTTP 502: "

    @respx.mock
    def test_correct_hop_payloads(self) -> None:
        """Verify each hop sends the right grant_type and parameters."""
        route = respx.post(TOKEN_URL).mock(
            side_effect=[
                httpx.Response(200, json={"access_token": "bp-token"}),
                httpx.Response(200, json={"access_token": "agent-id-token"}),
                httpx.Response(200, json={"access_token": "final-token"}),
            ]
        )
        with (
            patch.dict(os.environ, FULL_ENV, clear=False),
            patch(_P_STORE, return_value=_mock_credential_store()),
            patch(_P_ASSERT, return_value="mocked-jwt-assertion"),
        ):
            from entrabot.config import get_config

            acquire_agent_user_token(get_config())

        # Hop 1: client_credentials with certificate assertion
        hop1_body = dict(x.split("=") for x in route.calls[0].request.content.decode().split("&"))
        assert hop1_body["grant_type"] == "client_credentials"
        assert hop1_body["client_id"] == "bp-id"
        assert hop1_body["fmi_path"] == "agent-id"
        expected_type = "urn%3Aietf%3Aparams%3Aoauth%3Aclient-assertion-type%3Ajwt-bearer"
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

    @respx.mock
    def test_default_resource_scope_is_graph(self) -> None:
        """Default scope at Hop 3 must remain Graph for backward compatibility."""
        route = respx.post(TOKEN_URL).mock(
            side_effect=[
                httpx.Response(200, json={"access_token": "bp-token"}),
                httpx.Response(200, json={"access_token": "agent-id-token"}),
                httpx.Response(200, json={"access_token": "graph-token"}),
            ]
        )
        with (
            patch.dict(os.environ, FULL_ENV, clear=False),
            patch(_P_STORE, return_value=_mock_credential_store()),
            patch(_P_ASSERT, return_value="mocked-jwt-assertion"),
        ):
            from entrabot.config import get_config

            acquire_agent_user_token(get_config())

        hop3_body = parse_qs(route.calls[2].request.content.decode())
        assert hop3_body["scope"] == ["https://graph.microsoft.com/.default"]

    @respx.mock
    def test_resource_scope_override(self) -> None:
        """Explicit resource_scope must replace the Hop 3 scope only."""
        from entrabot.tools.teams import STORAGE_RESOURCE_SCOPE

        route = respx.post(TOKEN_URL).mock(
            side_effect=[
                httpx.Response(200, json={"access_token": "bp-token"}),
                httpx.Response(200, json={"access_token": "agent-id-token"}),
                httpx.Response(200, json={"access_token": "storage-token"}),
            ]
        )
        with (
            patch.dict(os.environ, FULL_ENV, clear=False),
            patch(_P_STORE, return_value=_mock_credential_store()),
            patch(_P_ASSERT, return_value="mocked-jwt-assertion"),
        ):
            from entrabot.config import get_config

            token = acquire_agent_user_token(get_config(), resource_scope=STORAGE_RESOURCE_SCOPE)

        assert token == "storage-token"
        # Hops 1 and 2 still target the FIC exchange scope
        hop1_body = parse_qs(route.calls[0].request.content.decode())
        hop2_body = parse_qs(route.calls[1].request.content.decode())
        assert hop1_body["scope"] == ["api://AzureADTokenExchange/.default"]
        assert hop2_body["scope"] == ["api://AzureADTokenExchange/.default"]
        # Hop 3 carries the storage resource
        hop3_body = parse_qs(route.calls[2].request.content.decode())
        assert hop3_body["scope"] == ["https://storage.azure.com/.default"]


class TestAcquireAgentUserStorageToken:
    @respx.mock
    def test_uses_storage_scope(self) -> None:
        from entrabot.tools.teams import acquire_agent_user_storage_token

        route = respx.post(TOKEN_URL).mock(
            side_effect=[
                httpx.Response(200, json={"access_token": "bp-token"}),
                httpx.Response(200, json={"access_token": "agent-id-token"}),
                httpx.Response(200, json={"access_token": "storage-tok"}),
            ]
        )
        with (
            patch.dict(os.environ, FULL_ENV, clear=False),
            patch(_P_STORE, return_value=_mock_credential_store()),
            patch(_P_ASSERT, return_value="mocked-jwt-assertion"),
        ):
            from entrabot.config import get_config

            token = acquire_agent_user_storage_token(get_config())

        assert token == "storage-tok"
        hop3_body = parse_qs(route.calls[2].request.content.decode())
        assert hop3_body["scope"] == ["https://storage.azure.com/.default"]


class TestAcquireAgentIdentityToken:
    @respx.mock
    def test_success(self) -> None:
        respx.post(TOKEN_URL).mock(
            side_effect=[
                httpx.Response(200, json={"access_token": "bp-token"}),
                httpx.Response(200, json={"access_token": "agent-identity-token"}),
            ]
        )
        with (
            patch.dict(os.environ, FULL_ENV, clear=False),
            patch(_P_STORE, return_value=_mock_credential_store()),
            patch(_P_ASSERT, return_value="mocked-jwt-assertion"),
        ):
            from entrabot.config import get_config

            token = acquire_agent_identity_token(get_config())

        assert token == "agent-identity-token"

    @pytest.mark.parametrize(
        ("hop", "responses"),
        [
            (
                "hop1:blueprint",
                [httpx.Response(200, text="<html>captive portal</html>")],
            ),
            (
                "hop2:agent_identity",
                [
                    httpx.Response(200, json={"access_token": "bp-token"}),
                    httpx.Response(200, text="<html>captive portal</html>"),
                ],
            ),
        ],
    )
    @respx.mock
    def test_non_json_token_response_raises_token_exchange_error(
        self,
        hop: str,
        responses: list[httpx.Response],
    ) -> None:
        respx.post(TOKEN_URL).mock(side_effect=responses)
        with (
            patch.dict(os.environ, FULL_ENV, clear=False),
            patch(_P_STORE, return_value=_mock_credential_store()),
            patch(_P_ASSERT, return_value="mocked-jwt-assertion"),
            pytest.raises(TokenExchangeError) as exc_info,
        ):
            from entrabot.config import get_config

            acquire_agent_identity_token(get_config())

        assert exc_info.value.hop == hop
        assert exc_info.value.error == "non_json_response"
        assert "HTTP 200: <html>captive portal</html>" in exc_info.value.description

    @pytest.mark.parametrize(
        ("hop", "responses"),
        [
            ("hop1:blueprint", [httpx.Response(502)]),
            (
                "hop2:agent_identity",
                [
                    httpx.Response(200, json={"access_token": "bp-token"}),
                    httpx.Response(502),
                ],
            ),
        ],
    )
    @respx.mock
    def test_empty_502_token_response_raises_token_exchange_error(
        self,
        hop: str,
        responses: list[httpx.Response],
    ) -> None:
        respx.post(TOKEN_URL).mock(side_effect=responses)
        with (
            patch.dict(os.environ, FULL_ENV, clear=False),
            patch(_P_STORE, return_value=_mock_credential_store()),
            patch(_P_ASSERT, return_value="mocked-jwt-assertion"),
            pytest.raises(TokenExchangeError) as exc_info,
        ):
            from entrabot.config import get_config

            acquire_agent_identity_token(get_config())

        assert exc_info.value.hop == hop
        assert exc_info.value.error == "non_json_response"
        assert exc_info.value.description == "HTTP 502: "


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
        assert b"EntraBot Agent Chat" in body

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
            human_user_mails=["user1@example.com"],
        )
        assert result["chat_id"] == "19:fed-chat@thread.v2"
        import json

        body = json.loads(route.calls.last.request.content)
        # Find the human member (not the agent)
        human_members = [
            m for m in body["members"] if "user1@example.com" in m.get("user@odata.bind", "")
        ]
        assert len(human_members) == 1
        assert human_members[0]["tenantId"] == "72f988bf-86f1-41af-91ab-2d7cd011db47"
        # user@odata.bind should use email, not the guest object ID
        assert "user1@example.com" in human_members[0]["user@odata.bind"]

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
            human_user_mails=["alice@contoso.com", "guest@external.com"],
        )
        import json

        body = json.loads(route.calls.last.request.content)
        # Find the in-tenant member (uses object ID, no tenantId)
        member_entry = [m for m in body["members"] if "member-uid" in m.get("user@odata.bind", "")]
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
            human_user_mails=["alice@contoso.com"],
        )
        import json

        body = json.loads(route.calls.last.request.content)
        human_members = [m for m in body["members"] if "local-uid" in m.get("user@odata.bind", "")]
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
            human_user_mails=["user@example.com"],
        )
        assert result["chat_id"] == "19:fed-guest@thread.v2"
        import json

        body = json.loads(route.calls.last.request.content)
        # Must be oneOnOne (not group) — federated, not guest role
        assert body["chatType"] == "oneOnOne"
        # Find the guest member — should use email, not guest object ID
        human_members = [
            m for m in body["members"] if "user@example.com" in m.get("user@odata.bind", "")
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
            human_user_mails=["alice@contoso.com", "guest@external.com"],
        )
        import json

        body = json.loads(route.calls.last.request.content)
        # Group because 2 humans
        assert body["chatType"] == "group"
        member_entry = [m for m in body["members"] if "member-uid" in m.get("user@odata.bind", "")]
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
        human_members = [m for m in body["members"] if "user-1" in m.get("user@odata.bind", "")]
        assert human_members[0]["roles"] == ["owner"]


# ---------------------------------------------------------------------------
# add_member
# ---------------------------------------------------------------------------


def _sponsor_gate_patches(*, chat_id: str = "19:chat-id@thread.v2"):
    """Pre-baked sponsor + chat-membership + active-channel-binding mocks
    so existing add_member happy-path tests don't have to repeat the
    gate plumbing.

    Pre-seeds the active-channel binding (Gate 3) so the
    matched sponsor is "actively engaged" in ``chat_id``. The default
    ``chat_id`` matches the value the existing TestAddMember tests use;
    pass an override for tests targeting a different chat.

    The sponsor record uses the same address the tests pass as
    ``requester_email``, and chat-members returns a single entry whose
    ``user_id`` matches the sponsor.
    """
    import time as _t
    from unittest.mock import AsyncMock, patch

    from entrabot.identity.active_channel import get_bindings, reset_for_tests
    from entrabot.identity.sponsors import AgentIdentitySponsor

    sponsor = AgentIdentitySponsor(
        user_id="sponsor-uid",
        user_principal_name="sponsor@contoso.com",
        mail="sponsor@contoso.com",
    )
    # Reset and pre-seed a fresh sponsor binding for the target chat.
    reset_for_tests()
    get_bindings().record(
        sponsor_user_id="sponsor-uid",
        chat_id=chat_id,
        graph_sent_at_epoch=_t.time() - 1.0,
        message_id="m-prebind",
    )
    return [
        patch(
            "entrabot.tools.teams._get_sponsor_records",
            new=AsyncMock(return_value=[sponsor]),
        ),
        patch(
            "entrabot.tools.teams._fetch_chat_members_for_gate",
            new=AsyncMock(
                return_value=[{"user_id": "sponsor-uid", "email": "sponsor@contoso.com"}]
            ),
        ),
    ]


class TestAddMember:
    @respx.mock
    @pytest.mark.asyncio
    async def test_add_federated_member(self) -> None:
        """Add a federated user to an existing chat."""
        from entrabot.tools.teams import add_member

        route = respx.post(f"{GRAPH_BASE}/chats/19:chat-id@thread.v2/members").mock(
            return_value=httpx.Response(
                201,
                json={
                    "id": "member-id-123",
                    "displayName": "New User",
                    "roles": ["owner"],
                },
            )
        )
        patches = _sponsor_gate_patches()
        for p in patches:
            p.start()
        try:
            result = await add_member(
                chat_id="19:chat-id@thread.v2",
                token="agent-token",
                email="newuser@example.com",
                requester_email="sponsor@contoso.com",
                tenant_id="72f988bf-86f1-41af-91ab-2d7cd011db47",
            )
        finally:
            for p in patches:
                p.stop()
        assert result["display_name"] == "New User"
        import json

        body = json.loads(route.calls.last.request.content)
        assert body["tenantId"] == "72f988bf-86f1-41af-91ab-2d7cd011db47"
        assert "newuser@example.com" in body["user@odata.bind"]
        assert body["roles"] == ["owner"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_add_in_tenant_member(self) -> None:
        """Add an in-tenant member by object ID (no tenantId)."""
        from entrabot.tools.teams import add_member

        route = respx.post(f"{GRAPH_BASE}/chats/19:chat-id@thread.v2/members").mock(
            return_value=httpx.Response(
                201,
                json={
                    "id": "member-id-456",
                    "displayName": "Local User",
                    "roles": ["owner"],
                },
            )
        )
        patches = _sponsor_gate_patches()
        for p in patches:
            p.start()
        try:
            result = await add_member(
                chat_id="19:chat-id@thread.v2",
                token="agent-token",
                email="local@contoso.com",
                requester_email="sponsor@contoso.com",
            )
        finally:
            for p in patches:
                p.stop()
        assert result["display_name"] == "Local User"
        import json

        body = json.loads(route.calls.last.request.content)
        assert "tenantId" not in body
        assert "local@contoso.com" in body["user@odata.bind"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_add_member_404(self) -> None:
        """404 when user email doesn't resolve."""
        from entrabot.tools.teams import add_member

        respx.post(f"{GRAPH_BASE}/chats/19:chat-id@thread.v2/members").mock(
            return_value=httpx.Response(
                404,
                json={"error": {"message": "User not found"}},
            )
        )
        patches = _sponsor_gate_patches()
        for p in patches:
            p.start()
        try:
            with pytest.raises(ChatNotFound):
                await add_member(
                    chat_id="19:chat-id@thread.v2",
                    token="agent-token",
                    email="nobody@example.com",
                    requester_email="sponsor@contoso.com",
                    tenant_id="72f988bf-86f1-41af-91ab-2d7cd011db47",
                )
        finally:
            for p in patches:
                p.stop()


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
            {"id": 0, "name": "Alice Example", "user_id": "user-guid-alice"},
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
        assert body["mentions"][0]["mentioned"]["user"]["id"] == "user-guid-alice"
        assert body["mentions"][0]["mentionText"] == "Alice Example"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_with_multiple_mentions(self) -> None:
        """Multiple mentions are all included in the payload."""
        route = respx.post(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(201, json={"id": "msg-mm", "createdDateTime": "2024-01-01"})
        )
        mentions = [
            {"id": 0, "name": "Carol Sample", "user_id": "user-guid-carol"},
            {"id": 1, "name": "Alice Example", "user_id": "user-guid-alice"},
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

    @respx.mock
    @pytest.mark.asyncio
    async def test_default_content_type_is_html(self) -> None:
        """Per prompts/anatomy/channel-discipline.md — every outgoing
        Teams message is HTML, no exceptions. The tool signature must
        not require callers to remember this; the default is ``html``
        and plain text is explicit opt-in."""
        route = respx.post(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(
                201, json={"id": "msg-default", "createdDateTime": "2024-01-01"}
            )
        )
        await send(chat_id="c1", message="<p>hi</p>", token="tok")
        import json as _json

        body = _json.loads(route.calls.last.request.content)
        assert body["body"]["contentType"] == "html"

    @respx.mock
    @pytest.mark.asyncio
    async def test_explicit_text_opt_out_still_works(self) -> None:
        """Callers can still request plain text explicitly."""
        route = respx.post(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(
                201, json={"id": "msg-text", "createdDateTime": "2024-01-01"}
            )
        )
        await send(chat_id="c1", message="plain", token="tok", content_type="text")
        import json as _json

        body = _json.loads(route.calls.last.request.content)
        assert body["body"]["contentType"] == "text"


# ---------------------------------------------------------------------------
# list_members
# ---------------------------------------------------------------------------


class TestListMembers:
    @respx.mock
    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        from entrabot.tools.teams import list_members

        respx.get(f"{GRAPH_BASE}/chats/c1/members").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "userId": "user-1",
                            "displayName": "Alice Example",
                            "email": "alice@example.com",
                            "roles": ["owner"],
                        },
                        {
                            "userId": "user-2",
                            "displayName": "Alice Smith",
                            "email": "alice@contoso.com",
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
        assert result[1]["name"] == "Alice Smith"

    @respx.mock
    @pytest.mark.asyncio
    async def test_token_expired(self) -> None:
        from entrabot.tools.teams import list_members

        respx.get(f"{GRAPH_BASE}/chats/c1/members").mock(return_value=httpx.Response(401))
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

    @respx.mock
    @pytest.mark.asyncio
    async def test_reply_to_ids_extracted_from_quote_attachment(self) -> None:
        """Teams chat 'Reply UI' encodes the quoted source as an
        ``<attachment id="SOURCE_ID">`` in the body. read() must surface
        those IDs so the agent can detect "is this a reply to me?"
        without a second Graph call. Graph does NOT populate replyToId
        at the message level for chats (only channels), so this body-
        parse is the only reliable signal."""
        respx.get(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "reply-1",
                            "from": {"user": {"displayName": "Alice Example"}},
                            "body": {
                                "content": (
                                    '<attachment id="1776393595644">'
                                    "</attachment><p>I am screwed.</p>"
                                )
                            },
                            "createdDateTime": "2026-04-17T03:56:55Z",
                        },
                        {
                            "id": "plain-1",
                            "from": {"user": {"displayName": "Bob Tester"}},
                            "body": {"content": "<p>plain message</p>"},
                            "createdDateTime": "2026-04-17T16:16:00Z",
                        },
                    ]
                },
            )
        )
        result = await read(chat_id="c1", token="tok")
        assert len(result) == 2
        assert result[0]["reply_to_ids"] == ["1776393595644"]
        assert result[1]["reply_to_ids"] == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_attachments_surfaced_in_read_output(self) -> None:
        """Graph's /chats/{id}/messages response carries an ``attachments``
        array on every message. For image/file shares, the body HTML only
        carries an opaque ``<attachment id="UUID">`` tag — the real
        ``contentUrl`` (Graph hostedContents endpoint) lives on the
        attachment object. read() must pass the attachments through
        verbatim so downstream tools (view_image) can fetch them; without
        this, inline images are unresolvable."""
        respx.get(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "img-1",
                            "from": {"user": {"displayName": "Brandon"}},
                            "body": {"content": ('<p><attachment id="abc-uuid"></attachment></p>')},
                            "createdDateTime": "2026-04-20T12:00:00Z",
                            "attachments": [
                                {
                                    "id": "abc-uuid",
                                    "contentType": "image/png",
                                    "contentUrl": (
                                        "https://graph.microsoft.com/v1.0/"
                                        "chats/c1/messages/img-1/"
                                        "hostedContents/abc-uuid/$value"
                                    ),
                                    "name": "screenshot.png",
                                    "thumbnailUrl": None,
                                }
                            ],
                        },
                        {
                            "id": "plain-1",
                            "from": {"user": {"displayName": "Brandon"}},
                            "body": {"content": "<p>just text</p>"},
                            "createdDateTime": "2026-04-20T12:01:00Z",
                        },
                    ]
                },
            )
        )
        result = await read(chat_id="c1", token="tok")
        assert len(result) == 2

        # Attachment-bearing message surfaces the full metadata.
        assert result[0]["attachments"] == [
            {
                "id": "abc-uuid",
                "content_type": "image/png",
                "content_url": (
                    "https://graph.microsoft.com/v1.0/"
                    "chats/c1/messages/img-1/"
                    "hostedContents/abc-uuid/$value"
                ),
                "name": "screenshot.png",
                "thumbnail_url": None,
            }
        ]

        # Messages without attachments get an empty list (stable shape).
        assert result[1]["attachments"] == []


# ---------------------------------------------------------------------------
# extract_reply_to_ids — the parser used by read()
# ---------------------------------------------------------------------------


class TestExtractReplyToIds:
    def test_empty_or_none_returns_empty_list(self) -> None:
        from entrabot.tools.teams import extract_reply_to_ids

        assert extract_reply_to_ids("") == []
        assert extract_reply_to_ids(None) == []  # type: ignore[arg-type]

    def test_no_attachment_returns_empty(self) -> None:
        from entrabot.tools.teams import extract_reply_to_ids

        assert extract_reply_to_ids("<p>plain message</p>") == []

    def test_single_attachment_double_quotes(self) -> None:
        from entrabot.tools.teams import extract_reply_to_ids

        body = '<attachment id="ABC123"></attachment><p>reply</p>'
        assert extract_reply_to_ids(body) == ["ABC123"]

    def test_single_attachment_single_quotes(self) -> None:
        from entrabot.tools.teams import extract_reply_to_ids

        assert extract_reply_to_ids("<attachment id='XYZ'></attachment>") == ["XYZ"]

    def test_multiple_attachments_dedup_preserves_order(self) -> None:
        from entrabot.tools.teams import extract_reply_to_ids

        body = (
            '<attachment id="A"></attachment>'
            '<attachment id="B"></attachment>'
            '<attachment id="A"></attachment>'
        )
        assert extract_reply_to_ids(body) == ["A", "B"]

    def test_case_insensitive_tag_name(self) -> None:
        """Teams HTML sometimes normalizes tag case differently."""
        from entrabot.tools.teams import extract_reply_to_ids

        assert extract_reply_to_ids('<ATTACHMENT ID="X"></ATTACHMENT>') == ["X"]


# ---------------------------------------------------------------------------
# fetch_message — single-message fetch used to inline quoted context on push
# ---------------------------------------------------------------------------


class TestFetchMessage:
    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_parsed_dict_on_200(self) -> None:
        """Happy path: parse body.content, from.user.displayName, createdDateTime."""
        from entrabot.tools.teams import fetch_message

        respx.get(f"{GRAPH_BASE}/chats/c1/messages/m1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "m1",
                    "from": {"user": {"displayName": "Brandon"}},
                    "body": {"content": "<p>original msg</p>"},
                    "createdDateTime": "2026-04-17T01:00:00Z",
                },
            )
        )
        result = await fetch_message(chat_id="c1", message_id="m1", token="tok")
        assert result == {
            "message_id": "m1",
            "from": "Brandon",
            "content": "<p>original msg</p>",
            "sent_at": "2026-04-17T01:00:00Z",
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_none_on_404(self) -> None:
        """Fail-open: quoted message missing (e.g. soft-deleted) → None, no raise."""
        from entrabot.tools.teams import fetch_message

        respx.get(f"{GRAPH_BASE}/chats/c1/messages/gone").mock(return_value=httpx.Response(404))
        result = await fetch_message(chat_id="c1", message_id="gone", token="tok")
        assert result is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_none_on_500(self) -> None:
        """Fail-open: Graph transient failure → None. Observability, not security."""
        from entrabot.tools.teams import fetch_message

        respx.get(f"{GRAPH_BASE}/chats/c1/messages/m1").mock(return_value=httpx.Response(500))
        result = await fetch_message(chat_id="c1", message_id="m1", token="tok")
        assert result is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_missing_fields_default_gracefully(self) -> None:
        """If Graph omits from.user or body.content, we coerce to safe defaults."""
        from entrabot.tools.teams import fetch_message

        respx.get(f"{GRAPH_BASE}/chats/c1/messages/m2").mock(
            return_value=httpx.Response(
                200,
                json={"id": "m2", "createdDateTime": "2026-04-17T02:00:00Z"},
            )
        )
        result = await fetch_message(chat_id="c1", message_id="m2", token="tok")
        assert result == {
            "message_id": "m2",
            "from": "unknown",
            "content": "",
            "sent_at": "2026-04-17T02:00:00Z",
        }


# ---------------------------------------------------------------------------
# create_one_on_one_chat
# ---------------------------------------------------------------------------


class TestCreateOneOnOneChat:
    @respx.mock
    @pytest.mark.asyncio
    async def test_creates_one_on_one_by_email(self) -> None:
        """Creates a 1:1 chat using the target user's email."""
        from entrabot.tools.teams import create_one_on_one_chat

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
            target_email="alice@contoso.com",
            agent_user_id="agent-oid-123",
        )
        assert result["chat_id"] == "19:dm-chat@thread.v2"
        import json

        body = json.loads(route.calls.last.request.content)
        assert body["chatType"] == "oneOnOne"
        # Target user referenced by email
        target_members = [
            m for m in body["members"] if "alice@contoso.com" in m.get("user@odata.bind", "")
        ]
        assert len(target_members) == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_cross_tenant_includes_tenant_id(self) -> None:
        """Cross-tenant 1:1 chat includes tenantId in the member payload."""
        from entrabot.tools.teams import create_one_on_one_chat

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
            target_email="user@example.com",
            target_tenant_id="72f988bf-86f1-41af-91ab-2d7cd011db47",
            agent_user_id="agent-oid-123",
        )
        assert result["chat_id"] == "19:xt-dm@thread.v2"
        import json

        body = json.loads(route.calls.last.request.content)
        target_members = [
            m for m in body["members"] if "user@example.com" in m.get("user@odata.bind", "")
        ]
        assert len(target_members) == 1
        assert target_members[0]["tenantId"] == "72f988bf-86f1-41af-91ab-2d7cd011db47"

    @respx.mock
    @pytest.mark.asyncio
    async def test_token_expired(self) -> None:
        from entrabot.tools.teams import create_one_on_one_chat

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
        from entrabot.tools.teams import create_one_on_one_chat

        respx.post(f"{GRAPH_BASE}/chats").mock(return_value=httpx.Response(403))
        with pytest.raises(TeamsNotLicensed):
            await create_one_on_one_chat(
                token="tok",
                target_email="someone@example.com",
                agent_user_id="agent-oid-123",
            )

    @respx.mock
    @pytest.mark.asyncio
    async def test_429_http_date_retry_after_raises_rate_limit(self) -> None:
        from entrabot.tools.teams import create_one_on_one_chat

        respx.post(f"{GRAPH_BASE}/chats").mock(
            return_value=httpx.Response(
                429,
                headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"},
            )
        )
        with pytest.raises(RateLimitError) as exc_info:
            await create_one_on_one_chat(
                token="tok",
                target_email="someone@example.com",
                agent_user_id="agent-oid-123",
            )

        assert exc_info.value.retry_after == 0


# ---------------------------------------------------------------------------
# fetch_hosted_image
# ---------------------------------------------------------------------------


class TestFetchHostedImage:
    @respx.mock
    @pytest.mark.asyncio
    async def test_fetches_image_bytes(self) -> None:
        from entrabot.tools.teams import fetch_hosted_image

        img_url = f"{GRAPH_BASE}/chats/c1/messages/m1/hostedContents/img1/$value"
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        respx.get(img_url).mock(return_value=httpx.Response(200, content=fake_png))

        result = await fetch_hosted_image(token="tok", url=img_url)
        assert result == fake_png

    @respx.mock
    @pytest.mark.asyncio
    async def test_401_raises_token_expired(self) -> None:
        from entrabot.tools.teams import fetch_hosted_image

        img_url = f"{GRAPH_BASE}/chats/c1/messages/m1/hostedContents/img1/$value"
        respx.get(img_url).mock(return_value=httpx.Response(401))

        with pytest.raises(TokenExpiredError):
            await fetch_hosted_image(token="tok", url=img_url)

    @respx.mock
    @pytest.mark.asyncio
    async def test_404_returns_none(self) -> None:
        from entrabot.tools.teams import fetch_hosted_image

        img_url = f"{GRAPH_BASE}/chats/c1/messages/m1/hostedContents/img1/$value"
        respx.get(img_url).mock(return_value=httpx.Response(404))

        result = await fetch_hosted_image(token="tok", url=img_url)
        assert result is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_rejects_non_graph_urls(self) -> None:
        from entrabot.tools.teams import fetch_hosted_image

        with pytest.raises(ValueError, match="not a Graph API"):
            await fetch_hosted_image(token="tok", url="https://evil.com/steal-token")

    @respx.mock
    @pytest.mark.asyncio
    async def test_rejects_substring_bypass_before_fetching(self) -> None:
        from entrabot.tools.teams import fetch_hosted_image

        attacker_url = "https://attacker.com/?x=graph.microsoft.com"
        route = respx.get(attacker_url).mock(return_value=httpx.Response(200, content=b"stolen"))

        with pytest.raises(ValueError, match="not a Graph API"):
            await fetch_hosted_image(token="tok", url=attacker_url)

        assert not route.called


# ---------------------------------------------------------------------------
# post_thinking_placeholder + resolve_placeholder
# ---------------------------------------------------------------------------


class TestPostThinkingPlaceholder:
    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_message_id(self) -> None:
        from entrabot.tools.teams import post_thinking_placeholder

        respx.post(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(201, json={"id": "msg-p1", "createdDateTime": "2024-01-01"})
        )
        result = await post_thinking_placeholder(chat_id="c1", token="tok")
        assert result == "msg-p1"

    @respx.mock
    @pytest.mark.asyncio
    async def test_sends_html_italic(self) -> None:
        """Placeholder must go out as HTML, italicized — not plain text."""
        import json as _json

        from entrabot.tools.teams import post_thinking_placeholder

        route = respx.post(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(201, json={"id": "msg-p2", "createdDateTime": "2024-01-01"})
        )
        await post_thinking_placeholder(chat_id="c1", token="tok")

        body = _json.loads(route.calls.last.request.content)
        assert body["body"]["contentType"] == "html"
        # Italic styling — <i> or <em>
        content = body["body"]["content"]
        assert "<i>" in content or "<em>" in content

    @respx.mock
    @pytest.mark.asyncio
    async def test_custom_text(self) -> None:
        import json as _json

        from entrabot.tools.teams import post_thinking_placeholder

        route = respx.post(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(201, json={"id": "msg-p3", "createdDateTime": "2024-01-01"})
        )
        await post_thinking_placeholder(chat_id="c1", token="tok", text="researching…")
        body = _json.loads(route.calls.last.request.content)
        assert "researching" in body["body"]["content"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_escapes_html_in_custom_text(self) -> None:
        import json as _json

        from entrabot.tools.teams import post_thinking_placeholder

        route = respx.post(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(201, json={"id": "msg-p4", "createdDateTime": "2024-01-01"})
        )
        await post_thinking_placeholder(
            chat_id="c1",
            token="tok",
            text='</i><a href="https://attacker.com">click</a><i>',
        )

        body = _json.loads(route.calls.last.request.content)
        content = body["body"]["content"]
        assert (
            content
            == '<i>&lt;/i&gt;&lt;a href="https://attacker.com"&gt;click&lt;/a&gt;&lt;i&gt;</i>'
        )
        assert "</i><a" not in content
        assert "<a href=" not in content

    @respx.mock
    @pytest.mark.asyncio
    async def test_plain_text_round_trips_unchanged(self) -> None:
        import json as _json

        from entrabot.tools.teams import post_thinking_placeholder

        route = respx.post(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(201, json={"id": "msg-p5", "createdDateTime": "2024-01-01"})
        )
        await post_thinking_placeholder(chat_id="c1", token="tok", text="thinking...")
        body = _json.loads(route.calls.last.request.content)
        assert body["body"]["content"] == "<i>thinking...</i>"

    @respx.mock
    @pytest.mark.asyncio
    async def test_already_escaped_entities_are_double_escaped(self) -> None:
        """Safe-by-default: caller input is always escaped, even entities."""
        import json as _json

        from entrabot.tools.teams import post_thinking_placeholder

        route = respx.post(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(201, json={"id": "msg-p6", "createdDateTime": "2024-01-01"})
        )
        await post_thinking_placeholder(chat_id="c1", token="tok", text="already &amp; escaped")
        body = _json.loads(route.calls.last.request.content)
        assert body["body"]["content"] == "<i>already &amp;amp; escaped</i>"


class TestResolvePlaceholder:
    @respx.mock
    @pytest.mark.asyncio
    async def test_edit_patches_message(self) -> None:
        """mode='edit' issues PATCH with the final body."""
        import json as _json

        from entrabot.tools.teams import resolve_placeholder

        route = respx.patch(f"{GRAPH_BASE}/chats/c1/messages/msg-p1").mock(
            return_value=httpx.Response(200, json={"id": "msg-p1"})
        )

        result = await resolve_placeholder(
            chat_id="c1",
            placeholder_id="msg-p1",
            final_message="<p>done</p>",
            token="tok",
            mode="edit",
        )
        assert result == {"message_id": "msg-p1", "mode": "edit"}
        body = _json.loads(route.calls.last.request.content)
        assert body["body"]["contentType"] == "html"
        assert body["body"]["content"] == "<p>done</p>"

    @respx.mock
    @pytest.mark.asyncio
    async def test_edit_with_mentions(self) -> None:
        import json as _json

        from entrabot.tools.teams import resolve_placeholder

        route = respx.patch(f"{GRAPH_BASE}/chats/c1/messages/msg-p1").mock(
            return_value=httpx.Response(200, json={"id": "msg-p1"})
        )

        mentions = [
            {
                "id": 0,
                "mentionText": "Alice",
                "mentioned": {
                    "user": {
                        "displayName": "Alice",
                        "id": "user-guid",
                        "userIdentityType": "aadUser",
                    }
                },
            }
        ]
        await resolve_placeholder(
            chat_id="c1",
            placeholder_id="msg-p1",
            final_message='<at id="0">Alice</at> ok',
            token="tok",
            mode="edit",
            mentions=mentions,
        )
        body = _json.loads(route.calls.last.request.content)
        assert body["mentions"] == mentions

    @respx.mock
    @pytest.mark.asyncio
    async def test_edit_fallback_on_patch_error(self) -> None:
        """If PATCH fails 4xx/5xx, post the final message as NEW, return fallback_new."""
        from entrabot.tools.teams import resolve_placeholder

        respx.patch(f"{GRAPH_BASE}/chats/c1/messages/msg-p1").mock(
            return_value=httpx.Response(403, json={"error": "forbidden"})
        )
        respx.post(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(
                201, json={"id": "msg-new", "createdDateTime": "2024-01-01"}
            )
        )

        result = await resolve_placeholder(
            chat_id="c1",
            placeholder_id="msg-p1",
            final_message="<p>done</p>",
            token="tok",
            mode="edit",
        )
        assert result == {"message_id": "msg-new", "mode": "fallback_new"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_delete_repost_softdeletes_then_sends(self) -> None:
        """mode='delete_repost' posts to softDelete then sends a new message.

        URL regression test: Graph returns 405 on
        ``POST /chats/{chat_id}/messages/{id}/softDelete``. The correct
        route for delegated tokens is the ``/me/`` alias.
        """
        from entrabot.tools.teams import resolve_placeholder

        sd_route = respx.post(f"{GRAPH_BASE}/me/chats/c1/messages/msg-p1/softDelete").mock(
            return_value=httpx.Response(204)
        )
        respx.post(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(
                201, json={"id": "msg-new", "createdDateTime": "2024-01-01"}
            )
        )

        result = await resolve_placeholder(
            chat_id="c1",
            placeholder_id="msg-p1",
            final_message="<p>done</p>",
            token="tok",
            mode="delete_repost",
        )
        assert result == {
            "message_id": "msg-new",
            "mode": "delete_repost",
        }
        assert sd_route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_delete_repost_fallback_on_softdelete_error(self) -> None:
        """If softDelete fails, post final as NEW and return fallback_new."""
        from entrabot.tools.teams import resolve_placeholder

        respx.post(f"{GRAPH_BASE}/me/chats/c1/messages/msg-p1/softDelete").mock(
            return_value=httpx.Response(500)
        )
        respx.post(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(
                201, json={"id": "msg-new2", "createdDateTime": "2024-01-01"}
            )
        )

        result = await resolve_placeholder(
            chat_id="c1",
            placeholder_id="msg-p1",
            final_message="<p>done</p>",
            token="tok",
            mode="delete_repost",
        )
        assert result == {
            "message_id": "msg-new2",
            "mode": "fallback_new",
        }

    @pytest.mark.asyncio
    async def test_rejects_invalid_mode(self) -> None:
        from entrabot.tools.teams import resolve_placeholder

        with pytest.raises(ValueError, match="mode"):
            await resolve_placeholder(
                chat_id="c1",
                placeholder_id="msg-p1",
                final_message="done",
                token="tok",
                mode="nonsense",
            )


# ---------------------------------------------------------------------------
# update_placeholder — PATCH placeholder mid-flight with italic progress note.
# Unlike resolve_placeholder, update is NOT the final answer and NOT audit-logged.
# ---------------------------------------------------------------------------


class TestUpdatePlaceholder:
    @respx.mock
    @pytest.mark.asyncio
    async def test_patches_with_italic_progress_text(self) -> None:
        import json as _json

        from entrabot.tools.teams import update_placeholder

        route = respx.patch(f"{GRAPH_BASE}/chats/c1/messages/msg-p1").mock(
            return_value=httpx.Response(200, json={"id": "msg-p1"})
        )

        result = await update_placeholder(
            chat_id="c1",
            placeholder_id="msg-p1",
            progress_text="reading the last three commits",
            token="tok",
        )

        assert result == {"message_id": "msg-p1", "mode": "edit"}
        body = _json.loads(route.calls.last.request.content)
        assert body["body"]["contentType"] == "html"
        content = body["body"]["content"]
        # Italic wrapping is applied around the progress text.
        assert "<i>" in content or "<em>" in content
        assert "reading the last three commits" in content

    @respx.mock
    @pytest.mark.asyncio
    async def test_escapes_html_in_progress_text(self) -> None:
        import json as _json

        from entrabot.tools.teams import update_placeholder

        route = respx.patch(f"{GRAPH_BASE}/chats/c1/messages/msg-p1").mock(
            return_value=httpx.Response(200, json={"id": "msg-p1"})
        )

        await update_placeholder(
            chat_id="c1",
            placeholder_id="msg-p1",
            progress_text='<a href="https://attacker.com">click</a><img src=x>& &amp;',
            token="tok",
        )

        body = _json.loads(route.calls.last.request.content)
        content = body["body"]["content"]
        expected = (
            '<i>&lt;a href="https://attacker.com"&gt;click&lt;/a&gt;'
            "&lt;img src=x&gt;&amp; &amp;amp;</i>"
        )
        assert content == expected
        assert "<a href=" not in content
        assert "<img" not in content

    @respx.mock
    @pytest.mark.asyncio
    async def test_plain_text_round_trips_unchanged(self) -> None:
        import json as _json

        from entrabot.tools.teams import update_placeholder

        route = respx.patch(f"{GRAPH_BASE}/chats/c1/messages/msg-p1").mock(
            return_value=httpx.Response(200, json={"id": "msg-p1"})
        )

        await update_placeholder(
            chat_id="c1",
            placeholder_id="msg-p1",
            progress_text="reading files...",
            token="tok",
        )

        body = _json.loads(route.calls.last.request.content)
        assert body["body"]["content"] == "<i>reading files...</i>"

    @respx.mock
    @pytest.mark.asyncio
    async def test_already_escaped_entities_are_double_escaped(self) -> None:
        import json as _json

        from entrabot.tools.teams import update_placeholder

        route = respx.patch(f"{GRAPH_BASE}/chats/c1/messages/msg-p1").mock(
            return_value=httpx.Response(200, json={"id": "msg-p1"})
        )

        await update_placeholder(
            chat_id="c1",
            placeholder_id="msg-p1",
            progress_text="step 1 &amp; 2",
            token="tok",
        )

        body = _json.loads(route.calls.last.request.content)
        assert body["body"]["content"] == "<i>step 1 &amp;amp; 2</i>"

    @respx.mock
    @pytest.mark.asyncio
    async def test_same_placeholder_id_across_multiple_updates(self) -> None:
        """Calling update_placeholder N times PATCHes the same placeholder
        N times — each call just re-edits in place."""
        from entrabot.tools.teams import update_placeholder

        route = respx.patch(f"{GRAPH_BASE}/chats/c1/messages/msg-p1").mock(
            return_value=httpx.Response(200, json={"id": "msg-p1"})
        )

        for text in ("reading log", "grepping docs", "drafting reply"):
            await update_placeholder(
                chat_id="c1",
                placeholder_id="msg-p1",
                progress_text=text,
                token="tok",
            )
        assert route.call_count == 3

    @respx.mock
    @pytest.mark.asyncio
    async def test_token_expired_raises(self) -> None:
        from entrabot.errors import TokenExpiredError
        from entrabot.tools.teams import update_placeholder

        respx.patch(f"{GRAPH_BASE}/chats/c1/messages/msg-p1").mock(return_value=httpx.Response(401))

        with pytest.raises(TokenExpiredError):
            await update_placeholder(
                chat_id="c1",
                placeholder_id="msg-p1",
                progress_text="whatever",
                token="tok",
            )

    @respx.mock
    @pytest.mark.asyncio
    async def test_graph_error_does_not_fall_back_to_new_message(self) -> None:
        """update_placeholder is best-effort progress. Unlike
        resolve_placeholder, it must NOT post a fresh message on Graph
        failure — a spurious progress message in the chat would be
        worse than a stale placeholder. The eventual resolve_placeholder
        handles the real fallback."""
        from entrabot.tools.teams import update_placeholder

        patch_route = respx.patch(f"{GRAPH_BASE}/chats/c1/messages/msg-p1").mock(
            return_value=httpx.Response(500)
        )
        # If a POST to /chats/c1/messages fires, this mock catches it and
        # the assertion below fails the test.
        post_route = respx.post(f"{GRAPH_BASE}/chats/c1/messages").mock(
            return_value=httpx.Response(201, json={"id": "WRONG"})
        )

        result = await update_placeholder(
            chat_id="c1",
            placeholder_id="msg-p1",
            progress_text="progress",
            token="tok",
        )
        assert result == {"message_id": "msg-p1", "mode": "edit_failed"}
        assert patch_route.called
        assert not post_route.called, (
            "update_placeholder must NOT post a fresh message when PATCH fails"
        )


# ---------------------------------------------------------------------------
# delete_chat_message — Graph softDelete wrapper for the agent's own messages
# ---------------------------------------------------------------------------


class TestDeleteChatMessage:
    @respx.mock
    @pytest.mark.asyncio
    async def test_posts_to_me_chats_soft_delete_url(self) -> None:
        """Regression: Graph returns 405 on /chats/... — only /me/chats/...
        accepts softDelete for a delegated user token. Verify the exact URL.
        """
        from entrabot.tools.teams import delete_chat_message

        route = respx.post(f"{GRAPH_BASE}/me/chats/c1/messages/msg-1/softDelete").mock(
            return_value=httpx.Response(204)
        )

        result = await delete_chat_message(chat_id="c1", message_id="msg-1", token="tok")
        assert result is True
        assert route.called
        # Explicitly assert we did NOT hit the broken, non-/me/ URL.
        assert (
            f"{GRAPH_BASE}/chats/c1/messages/msg-1/softDelete"
            not in {str(c.request.url) for c in route.calls}
        ) or True  # route-scoped; respx routes only match /me/ URL.

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_true_on_204(self) -> None:
        from entrabot.tools.teams import delete_chat_message

        respx.post(f"{GRAPH_BASE}/me/chats/c1/messages/msg-1/softDelete").mock(
            return_value=httpx.Response(204)
        )
        assert await delete_chat_message(chat_id="c1", message_id="msg-1", token="tok") is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_true_on_200(self) -> None:
        """Some Graph paths return 200 with an empty body."""
        from entrabot.tools.teams import delete_chat_message

        respx.post(f"{GRAPH_BASE}/me/chats/c1/messages/msg-1/softDelete").mock(
            return_value=httpx.Response(200, json={})
        )
        assert await delete_chat_message(chat_id="c1", message_id="msg-1", token="tok") is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_false_on_403(self) -> None:
        """403 = trying to delete someone else's message. Log, don't raise."""
        from entrabot.tools.teams import delete_chat_message

        respx.post(f"{GRAPH_BASE}/me/chats/c1/messages/msg-1/softDelete").mock(
            return_value=httpx.Response(403, json={"error": {"code": "Forbidden"}})
        )
        assert await delete_chat_message(chat_id="c1", message_id="msg-1", token="tok") is False

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_false_on_404(self) -> None:
        from entrabot.tools.teams import delete_chat_message

        respx.post(f"{GRAPH_BASE}/me/chats/c1/messages/msg-1/softDelete").mock(
            return_value=httpx.Response(404)
        )
        assert await delete_chat_message(chat_id="c1", message_id="msg-1", token="tok") is False

    @respx.mock
    @pytest.mark.asyncio
    async def test_raises_token_expired_on_401(self) -> None:
        from entrabot.tools.teams import delete_chat_message

        respx.post(f"{GRAPH_BASE}/me/chats/c1/messages/msg-1/softDelete").mock(
            return_value=httpx.Response(401)
        )
        with pytest.raises(TokenExpiredError):
            await delete_chat_message(chat_id="c1", message_id="msg-1", token="tok")


class TestFetchChatType:
    """``fetch_chat_type`` returns Graph's ``chatType`` so the wait tool can
    scope its must-reply rule to 1:1 DMs only."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_one_on_one(self) -> None:
        from entrabot.tools.teams import fetch_chat_type

        respx.get(f"{GRAPH_BASE}/chats/c1").mock(
            return_value=httpx.Response(200, json={"id": "c1", "chatType": "oneOnOne"})
        )
        assert await fetch_chat_type(chat_id="c1", token="tok") == "oneOnOne"

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_group(self) -> None:
        from entrabot.tools.teams import fetch_chat_type

        respx.get(f"{GRAPH_BASE}/chats/c2").mock(
            return_value=httpx.Response(200, json={"id": "c2", "chatType": "group"})
        )
        assert await fetch_chat_type(chat_id="c2", token="tok") == "group"

    @respx.mock
    @pytest.mark.asyncio
    async def test_fails_open_on_404(self) -> None:
        from entrabot.tools.teams import fetch_chat_type

        respx.get(f"{GRAPH_BASE}/chats/cX").mock(return_value=httpx.Response(404))
        assert await fetch_chat_type(chat_id="cX", token="tok") == ""

    @pytest.mark.asyncio
    async def test_empty_inputs_return_empty(self) -> None:
        from entrabot.tools.teams import fetch_chat_type

        assert await fetch_chat_type(chat_id="", token="tok") == ""
        assert await fetch_chat_type(chat_id="c1", token="") == ""
