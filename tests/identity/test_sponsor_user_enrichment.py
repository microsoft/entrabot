"""Tests for sponsor user-detail enrichment via a separate token provider.

The Agent Identity FIC token has ``AgentIdentity.ReadWrite.All``, which lets
it read ``/servicePrincipals/{id}/microsoft.graph.agentIdentity/sponsors``
but does NOT grant ``User.Read.All``. Graph's nav-property collection at
``/sponsors`` projects only ``id`` for each member, regardless of
``$select``. So the email fields (``userPrincipalName``, ``mail``,
``otherMails``, ``identities``) must come from a separate
``/users/{id}`` enrichment hop — and that hop fails (403) when the Agent
Identity token is reused.

The Agent User token (third-hop user token) DOES have ``User.Read.All``
delegated by default, so it can read ``/users/{id}`` for any user in the
tenant, including B2B guests.

These tests assert that ``fetch_agent_identity_sponsors`` accepts an
optional ``user_token_provider`` kwarg and passes its result to the
``/users/{id}`` enrichment call.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from entrabot.errors import GraphApiError
from entrabot.identity.sponsors import (
    AGENT_IDENTITY_GRAPH_BASE,
    AgentIdentitySponsor,
    fetch_agent_identity_sponsors,
)


def _make_config(agent_object_id: str = "spn-1") -> Any:
    """Tiny stand-in for EntraBotConfig — only ``agent_object_id`` is read."""

    class _Cfg:
        def __init__(self) -> None:
            self.agent_object_id = agent_object_id

    return _Cfg()


def _sponsors_response_with_id_only(sponsor_id: str) -> dict[str, Any]:
    """Real-world Graph response for the sponsors nav collection.

    Graph projects only ``id`` for nav-collection members regardless of
    ``$select`` — this matches what Sara's tenant returned in production
    on 2026-04-30 (sponsor allowlist empty bug).
    """
    return {"value": [{"id": sponsor_id}]}


def _full_user_response(sponsor_id: str) -> dict[str, Any]:
    """Real ``/users/{id}`` response with all email-shaped fields populated."""
    return {
        "id": sponsor_id,
        "userPrincipalName": "alice_contoso.com#EXT#@fabrikam.onmicrosoft.com",
        "mail": "alice@contoso.com",
        "otherMails": ["alice@contoso.com"],
        "proxyAddresses": ["SMTP:alice@contoso.com"],
        "identities": [
            {
                "signInType": "federated",
                "issuer": "contoso.com",
                "issuerAssignedId": "alice@contoso.com",
            }
        ],
    }


class TestUserEnrichmentTokenSeparation:
    """The user-details hop must accept an independent token provider."""

    def test_sponsors_collection_raises_graph_error_for_200_non_json_response(self) -> None:
        config = _make_config()
        url = (
            f"{AGENT_IDENTITY_GRAPH_BASE}/servicePrincipals/{config.agent_object_id}"
            "/microsoft.graph.agentIdentity/sponsors?"
            "$select=id,userPrincipalName,mail,otherMails,proxyAddresses,identities"
        )

        with respx.mock(assert_all_called=True) as router:
            router.get(url).mock(
                return_value=httpx.Response(
                    200,
                    text="<html>edge proxy error</html>",
                    headers={"content-type": "text/html"},
                )
            )

            with pytest.raises(GraphApiError) as exc_info:
                fetch_agent_identity_sponsors(
                    config,
                    token_provider=lambda _cfg: "agent-identity-token",
                )

        assert exc_info.value.status_code == 200
        assert "invalid JSON" in exc_info.value.message
        assert "<html>edge proxy error</html>" in exc_info.value.message

    def test_sponsors_collection_valid_json_still_works_with_respx(self) -> None:
        sponsor_id = "33333333-3333-3333-3333-333333333333"
        config = _make_config()
        sponsors_url = (
            f"{AGENT_IDENTITY_GRAPH_BASE}/servicePrincipals/{config.agent_object_id}"
            "/microsoft.graph.agentIdentity/sponsors?"
            "$select=id,userPrincipalName,mail,otherMails,proxyAddresses,identities"
        )
        user_url = (
            f"{AGENT_IDENTITY_GRAPH_BASE}/users/{sponsor_id}"
            "?$select=id,userPrincipalName,mail,otherMails,proxyAddresses,identities"
        )

        with respx.mock(assert_all_called=True) as router:
            router.get(sponsors_url).mock(
                return_value=httpx.Response(200, json=_sponsors_response_with_id_only(sponsor_id))
            )
            router.get(user_url).mock(
                return_value=httpx.Response(200, json=_full_user_response(sponsor_id))
            )

            sponsors = fetch_agent_identity_sponsors(
                config,
                token_provider=lambda _cfg: "agent-identity-token",
                user_token_provider=lambda _cfg: "agent-user-token",
            )

        assert len(sponsors) == 1
        assert sponsors[0].mail == "alice@contoso.com"

    def test_user_enrichment_uses_separate_user_token_when_provided(self) -> None:
        sponsor_id = "33333333-3333-3333-3333-333333333333"
        captured_authorization_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            auth = request.headers.get("Authorization", "")
            if "/sponsors" in str(request.url):
                captured_authorization_headers["sponsors"] = auth
                return httpx.Response(200, json=_sponsors_response_with_id_only(sponsor_id))
            if f"/users/{sponsor_id}" in str(request.url):
                captured_authorization_headers["users"] = auth
                # Agent Identity FIC token gets 403 (no User.Read.All).
                # Agent User token gets 200 with full user payload.
                if auth == "Bearer agent-identity-token":
                    return httpx.Response(403, json={"error": "Forbidden"})
                return httpx.Response(200, json=_full_user_response(sponsor_id))
            raise AssertionError(f"unexpected request: {request.url}")

        transport = httpx.MockTransport(handler)
        config = _make_config()

        sponsors = fetch_agent_identity_sponsors(
            config,
            token_provider=lambda _cfg: "agent-identity-token",
            user_token_provider=lambda _cfg: "agent-user-token",
            transport=transport,
        )

        assert len(sponsors) == 1
        sponsor = sponsors[0]
        assert sponsor.user_id == sponsor_id
        # The enrichment ran via the Agent User token and populated emails.
        assert sponsor.mail == "alice@contoso.com"
        assert "alice@contoso.com" in sponsor.email_identifiers()
        # Both endpoints saw the right tokens.
        assert captured_authorization_headers["sponsors"] == "Bearer agent-identity-token"
        assert captured_authorization_headers["users"] == "Bearer agent-user-token"

    def test_user_enrichment_falls_back_to_token_provider_when_user_token_provider_omitted(
        self,
    ) -> None:
        """Back-compat: when ``user_token_provider`` is None, both calls reuse the
        same Agent Identity token (preserves existing wait-tool / supervisor path)."""
        sponsor_id = "33333333-3333-3333-3333-333333333333"
        captured_authorization_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            auth = request.headers.get("Authorization", "")
            if "/sponsors" in str(request.url):
                captured_authorization_headers["sponsors"] = auth
                return httpx.Response(200, json=_sponsors_response_with_id_only(sponsor_id))
            if f"/users/{sponsor_id}" in str(request.url):
                captured_authorization_headers["users"] = auth
                return httpx.Response(200, json=_full_user_response(sponsor_id))
            raise AssertionError(f"unexpected request: {request.url}")

        transport = httpx.MockTransport(handler)
        config = _make_config()

        sponsors = fetch_agent_identity_sponsors(
            config,
            token_provider=lambda _cfg: "agent-identity-token",
            transport=transport,
        )

        assert len(sponsors) == 1
        assert sponsors[0].mail == "alice@contoso.com"
        # Same token used for both endpoints when no user provider is given.
        assert captured_authorization_headers["sponsors"] == "Bearer agent-identity-token"
        assert captured_authorization_headers["users"] == "Bearer agent-identity-token"

    def test_unenriched_sponsor_with_only_id_returned_when_user_hop_fails(self) -> None:
        """Regression: 2026-04-30 sponsor-allowlist-empty bug.

        When ``/sponsors`` projects only ``id`` (Graph nav-collection quirk)
        AND the ``/users/{id}`` hop returns 403 because the Agent Identity
        token lacks ``User.Read.All``, the unenriched sponsor must still
        be returned. ``email_identifiers()`` will be empty, but the caller
        gets a chance to see this and fail loudly rather than silently
        producing an empty allowlist.
        """
        sponsor_id = "33333333-3333-3333-3333-333333333333"

        def handler(request: httpx.Request) -> httpx.Response:
            if "/sponsors" in str(request.url):
                return httpx.Response(200, json=_sponsors_response_with_id_only(sponsor_id))
            if f"/users/{sponsor_id}" in str(request.url):
                return httpx.Response(403, json={"error": "Forbidden"})
            raise AssertionError(f"unexpected request: {request.url}")

        transport = httpx.MockTransport(handler)
        config = _make_config()

        sponsors = fetch_agent_identity_sponsors(
            config,
            token_provider=lambda _cfg: "agent-identity-token",
            transport=transport,
        )

        assert len(sponsors) == 1
        sponsor = sponsors[0]
        assert sponsor.user_id == sponsor_id
        assert sponsor.user_principal_name is None
        assert sponsor.mail is None
        assert sponsor.email_identifiers() == frozenset()


@pytest.mark.asyncio
class TestGetSponsorAllowlistPassesUserTokenProvider:
    """Integration: ``files._get_sponsor_allowlist`` must use the Agent User
    token for the ``/users/{id}`` enrichment hop, otherwise the email
    allowlist is silently empty (production bug, 2026-04-30)."""

    async def test_get_sponsor_allowlist_routes_user_enrichment_via_agent_user_token(self):
        from unittest.mock import patch

        from entrabot.tools.files import _get_sponsor_allowlist
        from entrabot.tools.teams import acquire_agent_user_token

        sponsor = AgentIdentitySponsor(
            user_id="u1",
            user_principal_name="alice@contoso.com",
            mail="alice@contoso.com",
        )

        with (
            patch("entrabot.config.get_config") as mock_get_config,
            patch("entrabot.identity.sponsors.fetch_agent_identity_sponsors") as mock_fetch,
        ):
            mock_get_config.return_value = object()
            mock_fetch.return_value = [sponsor]

            allowlist = await _get_sponsor_allowlist()

            # The fix: must pass the Agent User token provider so the
            # /users/{id} hop has User.Read.All to read sponsor emails.
            _, kwargs = mock_fetch.call_args
            assert kwargs.get("user_token_provider") is acquire_agent_user_token
            assert allowlist == {"alice@contoso.com"}


@pytest.mark.asyncio
class TestGetSponsorAllowlistChatMembersFallback:
    """When ``/users/{id}`` enrichment returns 403 (Agent User lacks
    ``User.ReadBasic.All`` — happens on tenants whose setup predates the
    2026-04-30 grant fix), the sponsor allowlist falls back to scanning
    chat members of watched chats."""

    async def test_chat_members_fallback_recovers_sponsor_email(self):
        from unittest.mock import patch

        from entrabot.tools.files import _get_sponsor_allowlist

        sponsor_id = "33333333-3333-3333-3333-333333333333"
        unenriched_sponsor = AgentIdentitySponsor(
            user_id=sponsor_id, user_principal_name=None, mail=None
        )

        with (
            patch("entrabot.config.get_config") as mock_get_config,
            patch("entrabot.identity.sponsors.fetch_agent_identity_sponsors") as mock_fetch,
            patch("entrabot.identity.sponsors.fetch_watched_chat_members") as mock_chat_members,
        ):
            mock_get_config.return_value = object()
            mock_fetch.return_value = [unenriched_sponsor]
            mock_chat_members.return_value = [
                # The Agent User itself — must NOT pollute the allowlist.
                {
                    "user_id": "agent-user-oid",
                    "email": "entrabot-agent@fabrikam.onmicrosoft.com",
                    "name": "Entrabot Agent",
                },
                # The sponsor — its email is recovered from chat metadata.
                {
                    "user_id": sponsor_id,
                    "email": "alice@contoso.com",
                    "name": "Alice Smith",
                },
                # An unrelated chat member — must NOT enter the allowlist.
                {
                    "user_id": "stranger-oid",
                    "email": "stranger@example.com",
                    "name": "Stranger Danger",
                },
            ]

            allowlist = await _get_sponsor_allowlist()

            assert allowlist == {"alice@contoso.com"}
            mock_chat_members.assert_called_once()

    async def test_chat_members_fallback_skipped_when_enrichment_succeeds(self):
        """If ``fetch_agent_identity_sponsors`` already returned populated
        emails, we must NOT spend a Graph round-trip enumerating chats."""
        from unittest.mock import patch

        from entrabot.tools.files import _get_sponsor_allowlist

        enriched_sponsor = AgentIdentitySponsor(
            user_id="u1",
            mail="alice@contoso.com",
            user_principal_name="alice@contoso.com",
        )

        with (
            patch("entrabot.config.get_config") as mock_get_config,
            patch("entrabot.identity.sponsors.fetch_agent_identity_sponsors") as mock_fetch,
            patch("entrabot.identity.sponsors.fetch_watched_chat_members") as mock_chat_members,
        ):
            mock_get_config.return_value = object()
            mock_fetch.return_value = [enriched_sponsor]

            allowlist = await _get_sponsor_allowlist()

            assert allowlist == {"alice@contoso.com"}
            mock_chat_members.assert_not_called()

    async def test_chat_members_fallback_swallows_errors(self):
        """If the chat-members hop itself blows up (e.g., 401, transport
        error), share_file should still get a clean empty allowlist and
        fail loudly with NotASponsorError rather than crashing on a
        Graph exception."""
        from unittest.mock import patch

        from entrabot.tools.files import _get_sponsor_allowlist

        unenriched_sponsor = AgentIdentitySponsor(user_id="u1", user_principal_name=None, mail=None)

        with (
            patch("entrabot.config.get_config") as mock_get_config,
            patch("entrabot.identity.sponsors.fetch_agent_identity_sponsors") as mock_fetch,
            patch("entrabot.identity.sponsors.fetch_watched_chat_members") as mock_chat_members,
        ):
            mock_get_config.return_value = object()
            mock_fetch.return_value = [unenriched_sponsor]
            mock_chat_members.side_effect = RuntimeError("Graph exploded")

            allowlist = await _get_sponsor_allowlist()

            assert allowlist == set()

    async def test_chat_members_fallback_only_for_unenriched_sponsors(self):
        """Mixed case: one sponsor enriched via /users/{id}, another not.
        Allowlist must contain emails from BOTH sources."""
        from unittest.mock import patch

        from entrabot.tools.files import _get_sponsor_allowlist

        enriched = AgentIdentitySponsor(
            user_id="u1", user_principal_name="alice@contoso.com", mail="alice@contoso.com"
        )
        unenriched = AgentIdentitySponsor(user_id="u2", user_principal_name=None, mail=None)

        with (
            patch("entrabot.config.get_config") as mock_get_config,
            patch("entrabot.identity.sponsors.fetch_agent_identity_sponsors") as mock_fetch,
            patch("entrabot.identity.sponsors.fetch_watched_chat_members") as mock_chat_members,
        ):
            mock_get_config.return_value = object()
            mock_fetch.return_value = [enriched, unenriched]
            mock_chat_members.return_value = [
                {"user_id": "u2", "email": "bob@contoso.com", "name": "Bob"},
            ]

            allowlist = await _get_sponsor_allowlist()

            assert allowlist == {"alice@contoso.com", "bob@contoso.com"}


class TestSiblingJsonParseHardening:
    """Two sibling .json() call sites in sponsors.py share the same exposure
    as the previously-hardened sponsors-collection parse: an edge proxy / WAF
    returning HTTP 200 with HTML body raises json.JSONDecodeError before any
    typed Graph error can surface. The agent's docstring and the existing
    test above only covered the sponsors-collection site at L369; the sibling
    sites at L402 (_fetch_sponsor_user_details) and L480 (fetch_chat_members)
    are covered here.

    Semantics: both sibling sites already degrade gracefully on non-200
    responses (return None / continue with warning). A bad-JSON 200 must
    follow the same degradation path, not raise — otherwise a single
    misbehaving Graph response would crash unrelated per-chat / per-sponsor
    iteration.
    """

    def test_sponsor_user_details_returns_none_and_warns_on_non_json_200(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from entrabot.identity.sponsors import _fetch_sponsor_user_details

        sponsor_id = "33333333-3333-3333-3333-333333333333"
        user_url = (
            f"{AGENT_IDENTITY_GRAPH_BASE}/users/{sponsor_id}"
            "?$select=id,userPrincipalName,mail,otherMails,proxyAddresses,identities"
        )

        with respx.mock(assert_all_called=True) as router:
            router.get(user_url).mock(
                return_value=httpx.Response(
                    200,
                    text="<html>captive portal</html>",
                    headers={"content-type": "text/html"},
                )
            )

            with httpx.Client() as client, caplog.at_level("WARNING"):
                result = _fetch_sponsor_user_details(
                    token="user-token",
                    user_id=sponsor_id,
                    client=client,
                )

        assert result is None, "expected None on bad-JSON 200, not a raised exception"
        assert any(sponsor_id in r.message for r in caplog.records), (
            "expected a WARNING naming the sponsor user_id so operators can correlate"
        )

    def test_fetch_chat_members_skips_bad_json_chat_and_continues(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One chat returns 200 + HTML, another returns valid JSON. The
        first chat's members are skipped (with WARNING), the second chat's
        members are returned. This mirrors the existing non-200 behavior
        at sponsors.py L472-478.
        """
        from entrabot.identity.sponsors import fetch_chat_members

        chat_a = "19:badjson@thread.v2"
        chat_b = "19:goodjson@thread.v2"
        members_url_template = (
            "https://graph.microsoft.com/v1.0/chats/{chat_id}/members"
        )

        with respx.mock(assert_all_called=True) as router:
            router.get(members_url_template.format(chat_id=chat_a)).mock(
                return_value=httpx.Response(
                    200,
                    text="<html>WAF returned an error page</html>",
                    headers={"content-type": "text/html"},
                )
            )
            router.get(members_url_template.format(chat_id=chat_b)).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "userId": "user-b",
                                "displayName": "Bob",
                                "email": "bob@contoso.com",
                                "roles": ["member"],
                            }
                        ]
                    },
                )
            )

            with caplog.at_level("WARNING"):
                members = fetch_chat_members(
                    _make_config(),
                    [chat_a, chat_b],
                    token_provider=lambda _cfg: "agent-user-token",
                )

        # Chat A skipped — its members are NOT in the result
        assert all(m["user_id"] != "user-a" for m in members)
        # Chat B's members ARE in the result
        assert any(m["user_id"] == "user-b" for m in members)
        # Warning logged for chat A
        assert any(chat_a in r.message for r in caplog.records), (
            "expected a WARNING naming the bad-JSON chat_id so operators can correlate"
        )
