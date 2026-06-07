"""Gate 3 tests for share_file — Chain A regression + edge coverage.

Mirrors tests/tools/test_add_member_channel_binding.py but exercises
the share_file mutation path. Same authorization fix, same active-channel
binding store, same fail-closed contract.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from entrabot.errors import (
    NoActiveSponsorChannelError,
    SponsorChannelMismatchError,
)
from entrabot.identity.active_channel import get_bindings, reset_for_tests
from entrabot.identity.sponsors import AgentIdentitySponsor
from entrabot.tools.files import FileRef, share_file

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
    """Stubs for Gate 1 (sponsor list) and Gate 2 (chat membership) — both pass."""

    async def _sponsors():
        return [sponsor]

    with (
        patch("entrabot.tools.files._get_sponsor_records", _sponsors),
        patch(
            "entrabot.identity.sponsors.fetch_chat_members",
            MagicMock(return_value=[{"user_id": SPONSOR_UID, "email": SPONSOR_EMAIL}]),
        ),
    ):
        yield


def _ref() -> FileRef:
    return FileRef(
        drive_id="d-1",
        item_id="i-1",
        name="x.txt",
        mime_type="text/plain",
        kind="onedrive_business",
    )


def _bind(chat_id: str, *, age_seconds: float = 1.0) -> None:
    get_bindings().record(
        sponsor_user_id=SPONSOR_UID,
        chat_id=chat_id,
        graph_sent_at_epoch=time.time() - age_seconds,
        message_id="m-prebind",
    )


@pytest.mark.asyncio
class TestChainARegression:
    """Exact security report pattern but exercised against share_file."""

    async def test_no_binding_rejects(self, patched_gate1_gate2):
        with pytest.raises(NoActiveSponsorChannelError):
            await share_file(
                file_ref=_ref(),
                recipient_email="attacker@evil.com",
                requester_email=SPONSOR_EMAIL,
                chat_id="19:confidential-chat@thread.v2",
                role="read",
                token="t",
            )

    async def test_sponsor_active_in_other_chat_rejects(self, patched_gate1_gate2):
        _bind("19:low-priv-chat@thread.v2")
        with pytest.raises(SponsorChannelMismatchError) as exc_info:
            await share_file(
                file_ref=_ref(),
                recipient_email="attacker@evil.com",
                requester_email=SPONSOR_EMAIL,
                chat_id="19:confidential-chat@thread.v2",
                role="read",
                token="t",
            )
        err = exc_info.value
        assert err.supplied_chat_id == "19:confidential-chat@thread.v2"
        assert err.bound_chat_id == "19:low-priv-chat@thread.v2"


@pytest.mark.asyncio
class TestTTLExpiry:
    async def test_stale_binding_rejected(self, patched_gate1_gate2):
        # Past-TTL record refused at store time.
        get_bindings().record(
            sponsor_user_id=SPONSOR_UID,
            chat_id="19:target-chat@thread.v2",
            graph_sent_at_epoch=time.time() - 1000.0,
            message_id="m-old",
        )
        assert get_bindings().lookup(SPONSOR_UID) is None

        with pytest.raises(NoActiveSponsorChannelError):
            await share_file(
                file_ref=_ref(),
                recipient_email="attacker@evil.com",
                requester_email=SPONSOR_EMAIL,
                chat_id="19:target-chat@thread.v2",
                role="read",
                token="t",
            )


@pytest.mark.asyncio
class TestHappyPath:
    async def test_active_binding_matches_supplied_chat(self, patched_gate1_gate2):
        chat = "19:target-chat@thread.v2"
        _bind(chat)

        # Mock the Graph /invite call.
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json = MagicMock(
            return_value={
                "value": [
                    {
                        "id": "perm-1",
                        "webUrl": "https://example.com/x",
                    }
                ]
            }
        )
        fake_client = AsyncMock()
        fake_client.post = AsyncMock(return_value=fake_resp)

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _fake_client_ctx(*args, **kwargs):
            yield fake_client

        with patch("entrabot.tools.files._client", _fake_client_ctx):
            result = await share_file(
                file_ref=_ref(),
                recipient_email="bob@example.com",
                requester_email=SPONSOR_EMAIL,
                chat_id=chat,
                role="read",
                token="t",
            )

        assert result.permission_id == "perm-1"
