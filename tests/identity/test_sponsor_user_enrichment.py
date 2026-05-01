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

from entraclaw.identity.sponsors import (
    AgentIdentitySponsor,
    fetch_agent_identity_sponsors,
)


def _make_config(agent_object_id: str = "spn-1") -> Any:
    """Tiny stand-in for EntraClawConfig — only ``agent_object_id`` is read."""

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
        "userPrincipalName": "charlie_smith.ac#EXT#@sara.onmicrosoft.com",
        "mail": "brandon@werner.ac",
        "otherMails": ["brandon@werner.ac"],
        "proxyAddresses": ["SMTP:brandon@werner.ac"],
        "identities": [
            {
                "signInType": "federated",
                "issuer": "werner.ac",
                "issuerAssignedId": "brandon@werner.ac",
            }
        ],
    }


class TestUserEnrichmentTokenSeparation:
    """The user-details hop must accept an independent token provider."""

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
        assert sponsor.mail == "brandon@werner.ac"
        assert "brandon@werner.ac" in sponsor.email_identifiers()
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
        assert sponsors[0].mail == "brandon@werner.ac"
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

        from entraclaw.tools.files import _get_sponsor_allowlist
        from entraclaw.tools.teams import acquire_agent_user_token

        sponsor = AgentIdentitySponsor(
            user_id="u1",
            user_principal_name="brandon@werner.ac",
            mail="brandon@werner.ac",
        )

        with (
            patch("entraclaw.config.get_config") as mock_get_config,
            patch("entraclaw.identity.sponsors.fetch_agent_identity_sponsors") as mock_fetch,
        ):
            mock_get_config.return_value = object()
            mock_fetch.return_value = [sponsor]

            allowlist = await _get_sponsor_allowlist()

            # The fix: must pass the Agent User token provider so the
            # /users/{id} hop has User.Read.All to read sponsor emails.
            _, kwargs = mock_fetch.call_args
            assert kwargs.get("user_token_provider") is acquire_agent_user_token
            assert allowlist == {"brandon@werner.ac"}
