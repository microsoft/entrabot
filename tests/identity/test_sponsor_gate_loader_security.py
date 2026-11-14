"""Integration guard: the sponsor-gate loader must not promote the
counterparty of an unverified (e.g. auto-discovered or attacker-initiated)
watched 1:1 chat into the sponsor set.

This exercises ``load_agent_identity_sponsor_gate`` end-to-end (the wiring
that fetches per-chat members and feeds them to ``with_watched_chat_ids``),
complementing the unit tests on the gate methods themselves.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from entrabot.config import EntraBotConfig
from entrabot.identity import sponsors as sponsors_module
from entrabot.identity.sponsors import (
    AgentIdentitySponsor,
    acquire_agent_user_token,
    load_agent_identity_sponsor_gate,
)
from entrabot.storage.backend import BlobBackend
from entrabot.tools.poll_state import WATCHED_CHATS_KEY

AGENT_USER_ID = "aaaabbbb-cccc-dddd-eeee-111122223333"
SPONSOR_GUEST_OID = "ffeeddcc-bbaa-4998-8776-554433221100"
SPONSOR_HOME_OID = "00112233-4455-6677-8899-aabbccddeeff"
ATTACKER_HOME_OID = "deadbeef-0000-0000-0000-000000000001"

SPONSOR_CHAT = f"19:{SPONSOR_HOME_OID}_{AGENT_USER_ID}@unq.gbl.spaces"
ATTACKER_CHAT = f"19:{ATTACKER_HOME_OID}_{AGENT_USER_ID}@unq.gbl.spaces"


def _config(tmp_path) -> EntraBotConfig:
    return EntraBotConfig(agent_user_id=AGENT_USER_ID, data_dir=tmp_path)


def test_loader_promotes_only_verified_sponsor_chat(tmp_path) -> None:
    sponsor = AgentIdentitySponsor(
        user_id=SPONSOR_GUEST_OID,
        user_principal_name="alice@example.com",
        mail="alice@example.com",
    )

    members_by_chat = {
        SPONSOR_CHAT: [
            {"user_id": "agent-user-oid", "email": "entrabot-agent@fabrikam.onmicrosoft.com"},
            {"user_id": SPONSOR_GUEST_OID, "email": "alice@example.com"},
        ],
        ATTACKER_CHAT: [
            {"user_id": "agent-user-oid", "email": "entrabot-agent@fabrikam.onmicrosoft.com"},
            {"user_id": "attacker-guest-oid", "email": "attacker@evil.example"},
        ],
    }

    with (
        patch(
            "entrabot.identity.sponsors.fetch_agent_identity_sponsors",
            return_value=[sponsor],
        ),
        patch(
            "entrabot.identity.sponsors._watched_chat_ids",
            return_value=[SPONSOR_CHAT, ATTACKER_CHAT],
        ),
        patch(
            "entrabot.identity.sponsors._fetch_chat_members_grouped",
            side_effect=lambda config, chat_ids, **kw: {
                cid: members_by_chat[cid] for cid in chat_ids
            },
        ),
    ):
        gate = load_agent_identity_sponsor_gate(_config(tmp_path))

    # The verified sponsor's home-tenant OID is trusted...
    assert SPONSOR_HOME_OID in gate.user_ids
    assert gate.accepts({"sender_id": SPONSOR_HOME_OID, "sender": ""})
    # ...but the attacker who merely DM'd the agent (auto-discovered chat) is NOT.
    assert ATTACKER_HOME_OID not in gate.user_ids
    assert not gate.accepts({"sender_id": ATTACKER_HOME_OID, "sender": ""})


def test_loader_reads_sponsor_emails_with_the_agent_user_token(tmp_path) -> None:
    # The Agent Identity token can't read /users/{id}. Without emails the gate can't match a
    # federated sponsor's chat ID in a group chat, so their messages would stay wrapped.
    sponsor = AgentIdentitySponsor(
        user_id=SPONSOR_GUEST_OID,
        user_principal_name="alice@example.com",
        mail="alice@example.com",
    )
    with (
        patch(
            "entrabot.identity.sponsors.fetch_agent_identity_sponsors",
            return_value=[sponsor],
        ) as fetch,
        patch("entrabot.identity.sponsors._watched_chat_ids", return_value=[]),
        patch(
            "entrabot.identity.sponsors._fetch_chat_members_grouped",
            side_effect=lambda config, chat_ids, **kw: {},
        ),
    ):
        load_agent_identity_sponsor_gate(_config(tmp_path))

    assert fetch.call_args.kwargs["user_token_provider"] is acquire_agent_user_token


_SPONSOR = AgentIdentitySponsor(
    user_id=SPONSOR_GUEST_OID,
    user_principal_name="alice@example.com",
    mail="alice@example.com",
)
_SPONSOR_MEMBERS = [
    {"userId": AGENT_USER_ID, "email": "entrabot-agent@fabrikam.onmicrosoft.com"},
    {"userId": SPONSOR_GUEST_OID, "email": "alice@example.com"},
]
_ATTACKER_MEMBERS = [
    {"userId": AGENT_USER_ID, "email": "entrabot-agent@fabrikam.onmicrosoft.com"},
    {"userId": ATTACKER_HOME_OID, "email": "attacker@evil.example"},
]


def _graph_chat_members(responses: dict[str, httpx.Response]) -> httpx.MockTransport:
    """Answer ``GET /chats/{id}/members`` the way Graph would for each chat."""

    def handler(request: httpx.Request) -> httpx.Response:
        for chat_id, response in responses.items():
            if chat_id in request.url.path:
                return response
        raise AssertionError(f"unexpected Graph call: {request.url}")

    return httpx.MockTransport(handler)


def _load_gate_with_real_member_fetch(tmp_path, responses: dict[str, httpx.Response]):
    """Run the real loader against the shared list, with Graph as the only fake."""
    real_grouped = sponsors_module._fetch_chat_members_grouped

    def grouped(config, chat_ids, **_kwargs):
        return real_grouped(
            config,
            chat_ids,
            token_provider=lambda _config: "fake-token",
            transport=_graph_chat_members(responses),
        )

    with (
        patch(
            "entrabot.identity.sponsors.fetch_agent_identity_sponsors",
            return_value=[_SPONSOR],
        ),
        patch("entrabot.identity.sponsors._fetch_chat_members_grouped", grouped),
    ):
        return load_agent_identity_sponsor_gate(_config(tmp_path))


class TestPlantedChatInTheSharedList:
    """The list now lives in shared storage, so being on it can't mean anything.

    A chat ID planted there must not grant sponsor status. Only Graph confirming a
    sponsor is a member of that chat does.
    """

    def test_planted_chat_without_a_sponsor_member_grants_nothing(
        self, tmp_path, fake_cloud_store
    ) -> None:
        BlobBackend(fake_cloud_store).write_text(
            WATCHED_CHATS_KEY, f"{SPONSOR_CHAT}\n{ATTACKER_CHAT}\n"
        )

        gate = _load_gate_with_real_member_fetch(
            tmp_path,
            {
                SPONSOR_CHAT: httpx.Response(200, json={"value": _SPONSOR_MEMBERS}),
                ATTACKER_CHAT: httpx.Response(200, json={"value": _ATTACKER_MEMBERS}),
            },
        )

        assert ATTACKER_HOME_OID not in gate.user_ids
        assert not gate.accepts({"sender_id": ATTACKER_HOME_OID, "sender": ""})
        assert not gate.accepts({"sender_id": "", "sender": "attacker@evil.example"})
        # The real sponsor chat from the same list is still verified and promoted,
        # so this isn't passing just because nothing was read from the list.
        assert SPONSOR_HOME_OID in gate.user_ids

    def test_planted_chat_the_agent_cannot_read_grants_nothing(
        self, tmp_path, fake_cloud_store
    ) -> None:
        """A chat the Agent User isn't in returns 403. Membership stays unconfirmed."""
        BlobBackend(fake_cloud_store).write_text(WATCHED_CHATS_KEY, f"{ATTACKER_CHAT}\n")

        gate = _load_gate_with_real_member_fetch(
            tmp_path, {ATTACKER_CHAT: httpx.Response(403, text="Forbidden")}
        )

        assert ATTACKER_HOME_OID not in gate.user_ids
        assert not gate.accepts({"sender_id": ATTACKER_HOME_OID, "sender": ""})

    def test_planted_path_traversal_entry_never_reaches_graph(
        self, tmp_path, fake_cloud_store
    ) -> None:
        BlobBackend(fake_cloud_store).write_text(WATCHED_CHATS_KEY, "../../users/someone\n")
        # The transport fails the test on any Graph call.
        gate = _load_gate_with_real_member_fetch(tmp_path, {})
        assert SPONSOR_GUEST_OID.lower() in gate.user_ids


class TestGateOnReadFailure:
    def test_read_failure_narrows_and_never_widens(self, tmp_path, fake_cloud_store) -> None:
        BlobBackend(fake_cloud_store).write_text(WATCHED_CHATS_KEY, f"{SPONSOR_CHAT}\n")
        with patch.object(fake_cloud_store, "get_with_etag", side_effect=OSError("outage")):
            gate = _load_gate_with_real_member_fetch(tmp_path, {})

        # The sponsor relationship itself still works...
        assert gate.accepts({"sender_id": SPONSOR_GUEST_OID, "sender": ""})
        # ...but nothing is promoted from a list that couldn't be read.
        assert SPONSOR_HOME_OID not in gate.user_ids
        assert not gate.accepts({"sender_id": ATTACKER_HOME_OID, "sender": ""})

    def test_gate_from_an_unreadable_list_is_provisional(self, tmp_path, fake_cloud_store) -> None:
        with patch.object(fake_cloud_store, "get_with_etag", side_effect=OSError("outage")):
            gate = _load_gate_with_real_member_fetch(tmp_path, {})
        assert gate.provisional is True

    def test_gate_from_an_empty_list_is_provisional(self, tmp_path, fake_cloud_store) -> None:
        """No chats means no enrichment data, so there is nothing worth caching."""
        gate = _load_gate_with_real_member_fetch(tmp_path, {})
        assert gate.provisional is True

    def test_gate_from_a_readable_list_is_not_provisional(self, tmp_path, fake_cloud_store) -> None:
        BlobBackend(fake_cloud_store).write_text(WATCHED_CHATS_KEY, f"{SPONSOR_CHAT}\n")
        gate = _load_gate_with_real_member_fetch(
            tmp_path, {SPONSOR_CHAT: httpx.Response(200, json={"value": _SPONSOR_MEMBERS})}
        )
        assert gate.provisional is False

    def test_gate_is_not_provisional_when_every_chat_returned_members(
        self, tmp_path, fake_cloud_store
    ) -> None:
        BlobBackend(fake_cloud_store).write_text(
            WATCHED_CHATS_KEY, f"{SPONSOR_CHAT}\n{ATTACKER_CHAT}\n"
        )
        gate = _load_gate_with_real_member_fetch(
            tmp_path,
            {
                SPONSOR_CHAT: httpx.Response(200, json={"value": _SPONSOR_MEMBERS}),
                ATTACKER_CHAT: httpx.Response(200, json={"value": _ATTACKER_MEMBERS}),
            },
        )
        assert gate.provisional is False

    @pytest.mark.parametrize(
        "failed_chat_response",
        [
            pytest.param(httpx.Response(500, text="Graph is down"), id="server-error"),
            pytest.param(httpx.Response(403, text="Forbidden"), id="forbidden"),
            pytest.param(httpx.Response(200, text="<html>proxy page</html>"), id="not-json"),
            pytest.param(httpx.Response(200, json={"value": []}), id="no-members"),
        ],
    )
    def test_gate_is_provisional_when_one_chats_members_could_not_be_read(
        self, tmp_path, fake_cloud_store, failed_chat_response
    ) -> None:
        """The member fetch records a failed chat with an empty list, which looks
        complete to a gate that only checks whether any chat data came back. A real
        chat always has at least the agent in it, so an empty list means "couldn't
        read". Caching that gate would stop a federated sponsor in the failed chat from
        matching for the rest of the session."""
        BlobBackend(fake_cloud_store).write_text(
            WATCHED_CHATS_KEY, f"{SPONSOR_CHAT}\n{ATTACKER_CHAT}\n"
        )
        gate = _load_gate_with_real_member_fetch(
            tmp_path,
            {
                SPONSOR_CHAT: httpx.Response(200, json={"value": _SPONSOR_MEMBERS}),
                ATTACKER_CHAT: failed_chat_response,
            },
        )

        assert gate.provisional is True
        # Still safe to use: the chat that did load is verified and promoted as before.
        assert SPONSOR_HOME_OID in gate.user_ids
        assert ATTACKER_HOME_OID not in gate.user_ids
