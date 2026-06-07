"""Gate 3 tests for add_member — Chain A regression + edge coverage.

confused-deputy authorization fix. These tests verify the active-sponsor-channel
binding gate that runs between Gate 1 (sponsor allowlist) and Gate 2
(Graph chat membership) in ``entrabot.tools.teams.add_member``.

the Chain A pattern from the security report: attacker in any chat the agent
participates in can ask the agent to call ``add_member`` with a
``chat_id`` of a confidential chat and a ``requester_email`` matching
a real sponsor — both pre-security report gates pass because the attacker only
needs to fabricate two values that are individually plausible.

Gate 3 closes Chain A by requiring the matched sponsor to have a
live binding (most recent successfully-pushed inbound message) whose
``chat_id`` equals the LLM-supplied ``chat_id``.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from entrabot.errors import (
    NoActiveSponsorChannelError,
    SponsorChannelMismatchError,
)
from entrabot.identity.active_channel import get_bindings, reset_for_tests
from entrabot.identity.sponsors import AgentIdentitySponsor
from entrabot.tools.teams import GRAPH_BASE, add_member

SPONSOR_UID = "sponsor-uid-1"
SPONSOR_EMAIL = "sarah@example.com"


@pytest.fixture(autouse=True)
def _clean_bindings():
    reset_for_tests()
    yield
    reset_for_tests()


@pytest.fixture
def sponsor():
    return AgentIdentitySponsor(
        user_id=SPONSOR_UID,
        user_principal_name=SPONSOR_EMAIL,
        mail=SPONSOR_EMAIL,
    )


@pytest.fixture
def patched_gate1_gate2(sponsor):
    """Stubs for Gate 1 (sponsor list) and Gate 2 (chat membership).

    Both ALWAYS pass — so when a test still fails, it must be Gate 3.
    """
    with (
        patch(
            "entrabot.tools.teams._get_sponsor_records",
            new=AsyncMock(return_value=[sponsor]),
        ),
        patch(
            "entrabot.tools.teams._fetch_chat_members_for_gate",
            new=AsyncMock(
                return_value=[{"user_id": SPONSOR_UID, "email": SPONSOR_EMAIL}]
            ),
        ),
    ):
        yield


def _bind(chat_id: str, *, age_seconds: float = 1.0, message_id: str = "m-1") -> None:
    """Pre-seed the binding store with a fresh sponsor binding."""
    get_bindings().record(
        sponsor_user_id=SPONSOR_UID,
        chat_id=chat_id,
        graph_sent_at_epoch=time.time() - age_seconds,
        message_id=message_id,
    )


@pytest.mark.asyncio
class TestChainARegression:
    """The exact attack pattern from the security report."""

    async def test_no_binding_rejects(self, patched_gate1_gate2):
        """Sponsor genuinely a member of confidential chat (Gate 1+2 pass).

        Attacker in another chat manipulates the agent. Gate 3 must
        reject because the sponsor has no live binding for any chat.
        """
        with pytest.raises(NoActiveSponsorChannelError):
            await add_member(
                chat_id="19:confidential-chat@thread.v2",
                token="agent-token",
                email="attacker@evil.com",
                requester_email=SPONSOR_EMAIL,
            )

    async def test_sponsor_active_in_other_chat_rejects(self, patched_gate1_gate2):
        """Sponsor's binding is chat A (low priv) but LLM passes chat B (high priv)."""
        _bind("19:low-priv-chat@thread.v2")
        with pytest.raises(SponsorChannelMismatchError) as exc_info:
            await add_member(
                chat_id="19:confidential-chat@thread.v2",
                token="agent-token",
                email="attacker@evil.com",
                requester_email=SPONSOR_EMAIL,
            )
        err = exc_info.value
        assert err.supplied_chat_id == "19:confidential-chat@thread.v2"
        assert err.bound_chat_id == "19:low-priv-chat@thread.v2"


@pytest.mark.asyncio
class TestTTLExpiry:
    async def test_stale_binding_rejected(self, patched_gate1_gate2):
        """Binding store refuses to accept a past-TTL record so the
        gate sees 'no binding' and raises NoActiveSponsorChannelError.
        """
        # Try to bind with a 1000s-old message — record() returns False,
        # nothing stored.
        get_bindings().record(
            sponsor_user_id=SPONSOR_UID,
            chat_id="19:target-chat@thread.v2",
            graph_sent_at_epoch=time.time() - 1000.0,
            message_id="m-old",
        )
        assert get_bindings().lookup(SPONSOR_UID) is None

        with pytest.raises(NoActiveSponsorChannelError):
            await add_member(
                chat_id="19:target-chat@thread.v2",
                token="agent-token",
                email="x@example.com",
                requester_email=SPONSOR_EMAIL,
            )


@pytest.mark.asyncio
class TestHappyPath:
    @respx.mock
    async def test_active_binding_to_target_chat_passes_gate3(
        self, patched_gate1_gate2
    ):
        """Sponsor's bound chat equals supplied chat_id — Graph mutation proceeds."""
        chat = "19:target-chat@thread.v2"
        _bind(chat)
        respx.post(f"{GRAPH_BASE}/chats/{chat}/members").mock(
            return_value=httpx.Response(
                201,
                json={
                    "id": "member-1",
                    "displayName": "Attacker",
                    "roles": ["owner"],
                },
            )
        )
        result = await add_member(
            chat_id=chat,
            token="agent-token",
            email="anyone@example.com",
            requester_email=SPONSOR_EMAIL,
        )
        assert result["member_id"] == "member-1"


@pytest.mark.asyncio
class TestAuditLogEnrichment:
    async def test_gate3_mismatch_failure_audits_bound_and_supplied(
        self, patched_gate1_gate2, monkeypatch
    ):
        events: list[dict] = []

        def fake_log_event(*, action, resource, outcome, metadata):
            events.append({"outcome": outcome, "metadata": dict(metadata)})

        monkeypatch.setattr("entrabot.tools.teams.log_event", fake_log_event)

        _bind("19:low-priv-chat@thread.v2")
        with pytest.raises(SponsorChannelMismatchError):
            await add_member(
                chat_id="19:high-priv-chat@thread.v2",
                token="agent-token",
                email="x@x.com",
                requester_email=SPONSOR_EMAIL,
            )

        fail = [e for e in events if e["outcome"] == "failure"]
        assert fail, "Gate 3 failure must emit a failure audit event"
        md = fail[-1]["metadata"]
        assert md.get("supplied_chat_id") == "19:high-priv-chat@thread.v2"
        assert md.get("bound_chat_id") == "19:low-priv-chat@thread.v2"
        assert "SponsorChannelMismatchError" in str(md.get("error", ""))

    async def test_gate3_no_binding_failure_audits_empty_bound(
        self, patched_gate1_gate2, monkeypatch
    ):
        events: list[dict] = []

        def fake_log_event(*, action, resource, outcome, metadata):
            events.append({"outcome": outcome, "metadata": dict(metadata)})

        monkeypatch.setattr("entrabot.tools.teams.log_event", fake_log_event)

        with pytest.raises(NoActiveSponsorChannelError):
            await add_member(
                chat_id="19:target@thread.v2",
                token="agent-token",
                email="x@x.com",
                requester_email=SPONSOR_EMAIL,
            )

        fail = [e for e in events if e["outcome"] == "failure"]
        assert fail
        md = fail[-1]["metadata"]
        assert md.get("supplied_chat_id") == "19:target@thread.v2"
        assert md.get("bound_chat_id") == ""
        assert "NoActiveSponsorChannelError" in str(md.get("error", ""))
