"""Integration guard: the sponsor-gate loader must not promote the
counterparty of an unverified (e.g. auto-discovered or attacker-initiated)
watched 1:1 chat into the sponsor set.

This exercises ``load_agent_identity_sponsor_gate`` end-to-end (the wiring
that fetches per-chat members and feeds them to ``with_watched_chat_ids``),
complementing the unit tests on the gate methods themselves.
"""

from __future__ import annotations

from unittest.mock import patch

from entrabot.config import EntraBotConfig
from entrabot.identity.sponsors import (
    AgentIdentitySponsor,
    load_agent_identity_sponsor_gate,
)

AGENT_USER_ID = "aaaabbbb-cccc-dddd-eeee-111122223333"
SPONSOR_GUEST_OID = "963835fc-0b5c-4f3e-9f42-a1b906d9fbf8"
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
            "entrabot.identity.sponsors.fetch_chat_members",
            side_effect=lambda config, chat_id, **kw: members_by_chat[chat_id],
        ),
    ):
        gate = load_agent_identity_sponsor_gate(_config(tmp_path))

    # The verified sponsor's home-tenant OID is trusted...
    assert SPONSOR_HOME_OID in gate.user_ids
    assert gate.accepts({"sender_id": SPONSOR_HOME_OID, "sender": ""})
    # ...but the attacker who merely DM'd the agent (auto-discovered chat) is NOT.
    assert ATTACKER_HOME_OID not in gate.user_ids
    assert not gate.accepts({"sender_id": ATTACKER_HOME_OID, "sender": ""})
