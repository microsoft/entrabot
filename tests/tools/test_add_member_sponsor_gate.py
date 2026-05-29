"""Sponsor-gate tests for ``add_member`` (Teams chat invitation).

Authorization model (mirrors ``share_file`` 2026-04-30 inverted gate):

- The REQUESTER (who asked the agent to invite someone) MUST be in the
  static Agent Identity sponsor allowlist.
- The REQUESTER MUST be a member of the Teams ``chat_id`` they want to
  invite into — defends against an LLM fabricating a sponsor email that
  doesn't match the chat actually under discussion.
- The INVITEE (``email``) is unrestricted; sponsors may invite anyone.
- ``audit_log`` is emitted BEFORE the underlying Graph mutation so that
  forbidden adds still produce a record.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from entrabot.errors import (
    RequesterNotInChatError,
    RequesterNotSponsorError,
)
from entrabot.identity.sponsors import AgentIdentitySponsor
from entrabot.tools.teams import GRAPH_BASE, add_member


def _sponsor(
    user_id: str = "sponsor-uid",
    upn: str = "sponsor@contoso.com",
    mail: str | None = "sponsor@contoso.com",
) -> AgentIdentitySponsor:
    return AgentIdentitySponsor(
        user_id=user_id,
        user_principal_name=upn,
        mail=mail,
    )


@pytest.mark.asyncio
class TestAddMemberRequiredArguments:
    async def test_missing_requester_email_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="requester_email"):
            await add_member(
                chat_id="19:abcd@thread.v2",
                token="agent-token",
                email="newuser@contoso.com",
                requester_email="",
            )

    async def test_missing_chat_id_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="chat_id"):
            await add_member(
                chat_id="",
                token="agent-token",
                email="newuser@contoso.com",
                requester_email="sponsor@contoso.com",
            )


@pytest.mark.asyncio
class TestAddMemberRequesterMustBeSponsor:
    async def test_non_sponsor_requester_rejected(self) -> None:
        sponsor = _sponsor()
        with (
            patch(
                "entrabot.tools.teams._get_sponsor_records",
                new=AsyncMock(return_value=[sponsor]),
            ),
            pytest.raises(RequesterNotSponsorError) as exc_info,
        ):
            await add_member(
                chat_id="19:abcd@thread.v2",
                token="agent-token",
                email="newuser@contoso.com",
                requester_email="impostor@example.com",
            )
        msg = str(exc_info.value)
        assert "impostor@example.com" in msg


@pytest.mark.asyncio
class TestAddMemberRequesterMustBeChatMember:
    async def test_sponsor_not_in_chat_rejected(self) -> None:
        sponsor = _sponsor()
        with (
            patch(
                "entrabot.tools.teams._get_sponsor_records",
                new=AsyncMock(return_value=[sponsor]),
            ),
            patch(
                "entrabot.tools.teams._fetch_chat_members_for_gate",
                new=AsyncMock(
                    return_value=[
                        {"user_id": "different-user", "email": "stranger@example.com"}
                    ]
                ),
            ),
            pytest.raises(RequesterNotInChatError) as exc_info,
        ):
            await add_member(
                chat_id="19:wrong-chat@thread.v2",
                token="agent-token",
                email="newuser@contoso.com",
                requester_email="sponsor@contoso.com",
            )
        msg = str(exc_info.value)
        assert "sponsor@contoso.com" in msg
        assert "19:wrong-chat@thread.v2" in msg


@pytest.mark.asyncio
class TestAddMemberHappyPath:
    @respx.mock
    async def test_sponsor_in_chat_can_add_anyone_and_audit_fires_first(self) -> None:
        sponsor = _sponsor()
        audit_calls: list[dict] = []

        def fake_log_event(*, action, resource, outcome, metadata=None):
            audit_calls.append(
                {
                    "action": action,
                    "resource": resource,
                    "outcome": outcome,
                    "metadata": metadata or {},
                }
            )

        route = respx.post(
            f"{GRAPH_BASE}/chats/19:abcd@thread.v2/members"
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

        with (
            patch(
                "entrabot.tools.teams._get_sponsor_records",
                new=AsyncMock(return_value=[sponsor]),
            ),
            patch(
                "entrabot.tools.teams._fetch_chat_members_for_gate",
                new=AsyncMock(
                    return_value=[
                        {"user_id": "sponsor-uid", "email": "sponsor@contoso.com"}
                    ]
                ),
            ),
            patch("entrabot.tools.teams.log_event", side_effect=fake_log_event),
        ):
            result = await add_member(
                chat_id="19:abcd@thread.v2",
                token="agent-token",
                email="newuser@contoso.com",
                requester_email="sponsor@contoso.com",
            )

        assert result["display_name"] == "New User"
        assert route.called
        # Audit was emitted at least once BEFORE outcome="success".
        actions = [c["action"] for c in audit_calls]
        outcomes = [c["outcome"] for c in audit_calls]
        assert any(a == "teams.add_member" for a in actions)
        # First teams.add_member event must be the pending one (audit before action).
        first = next(c for c in audit_calls if c["action"] == "teams.add_member")
        assert first["outcome"] == "pending"
        # And we expect a success record after the Graph call returned 201.
        assert "success" in outcomes
