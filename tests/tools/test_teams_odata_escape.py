"""Verify that all Graph ``user@odata.bind`` interpolation sites in
``tools.teams`` escape single quotes in the bound value.

Without escaping, a value like ``"o'malley@example.com"`` produces an
invalid OData URL — and worst case opens a path for caller-controlled
content to break the URL grammar. ``graph_helpers.odata_escape``
doubles single quotes per the OData spec.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from entraclaw.identity.sponsors import AgentIdentitySponsor
from entraclaw.tools.teams import GRAPH_BASE


def _sponsor() -> AgentIdentitySponsor:
    return AgentIdentitySponsor(
        user_id="sponsor-uid",
        user_principal_name="sponsor@contoso.com",
        mail="sponsor@contoso.com",
    )


@pytest.mark.asyncio
@respx.mock
async def test_add_member_escapes_single_quote_in_email() -> None:
    """``add_member`` must double single quotes in ``email`` to keep
    the ``user@odata.bind`` OData path well-formed."""
    from entraclaw.tools.teams import add_member

    route = respx.post(
        f"{GRAPH_BASE}/chats/19:chat-id@thread.v2/members"
    ).mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "member-id-quote",
                "displayName": "O'Malley",
                "roles": ["owner"],
            },
        )
    )

    with (
        patch(
            "entraclaw.tools.teams._get_sponsor_records",
            new=AsyncMock(return_value=[_sponsor()]),
        ),
        patch(
            "entraclaw.tools.teams._fetch_chat_members_for_gate",
            new=AsyncMock(
                return_value=[
                    {"user_id": "sponsor-uid", "email": "sponsor@contoso.com"}
                ]
            ),
        ),
    ):
        await add_member(
            chat_id="19:chat-id@thread.v2",
            token="agent-token",
            email="o'malley@example.com",
            requester_email="sponsor@contoso.com",
        )

    body = json.loads(route.calls.last.request.content)
    bind = body["user@odata.bind"]
    # Doubled single quotes per OData spec.
    assert "o''malley@example.com" in bind
    # Raw, undoubled single quote MUST NOT appear in the payload after
    # the leading ``users('`` opening quote and before the closing one.
    assert bind.endswith("o''malley@example.com')")


@pytest.mark.asyncio
@respx.mock
async def test_create_one_on_one_chat_escapes_single_quote_in_target_email() -> None:
    """``create_one_on_one_chat`` must escape single quotes in
    ``target_email`` and in the resolved agent user id."""
    from entraclaw.tools.teams import create_one_on_one_chat

    route = respx.post(f"{GRAPH_BASE}/chats").mock(
        return_value=httpx.Response(
            201,
            json={"id": "19:new-chat@thread.v2", "createdDateTime": "2026-05-20"},
        )
    )

    await create_one_on_one_chat(
        token="agent-token",
        target_email="o'malley@example.com",
        agent_user_id="agent-oid",
    )

    body = json.loads(route.calls.last.request.content)
    members = body["members"]
    binds = [m["user@odata.bind"] for m in members]
    assert any("o''malley@example.com" in b for b in binds)
