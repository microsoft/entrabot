"""Tests for cross-tenant sponsor matching via federated identities.

When an external-tenant user is a B2B guest in the agent tenant, the guest
record's ``identities`` array contains a federated entry whose
``issuerAssignedId`` holds the user's home-tenant SMTP (e.g.
``Alice.Smith@example.com``). The chat-members API returns the
SAME SMTP as ``email``. By treating ``issuerAssignedId`` as a sponsor
email identifier we get cross-tenant alias matching for free, with no
operator override file.

This is the predictable convention from Learning #50.
"""

from __future__ import annotations

from entrabot.identity.sponsors import (
    AgentIdentitySponsor,
    SponsorGate,
)

GUEST_WITH_FEDERATED_IDENTITY = {
    "id": "963835fc-0b5c-4f3e-9f42-a1b906d9fbf8",
    "userPrincipalName": "alice_example.com#EXT#@fabrikam.onmicrosoft.com",
    "mail": "alice@example.com",
    "otherMails": ["alice@example.com"],
    "proxyAddresses": [],
    "identities": [
        {
            "signInType": "userPrincipalName",
            "issuer": "fabrikam.onmicrosoft.com",
            "issuerAssignedId": "alice_example.com#EXT#@fabrikam.onmicrosoft.com",
        },
        {
            "signInType": "federated",
            "issuer": "example.com",
            "issuerAssignedId": "Alice.Smith@example.com",
        },
    ],
}


class TestSponsorIdentitiesExtraction:
    def test_federated_issuer_assigned_id_added_to_email_identifiers(self) -> None:
        sponsor = AgentIdentitySponsor.from_graph_user(GUEST_WITH_FEDERATED_IDENTITY)
        assert sponsor is not None
        emails = sponsor.email_identifiers()
        assert "alice.smith@example.com" in emails
        assert "alice@example.com" in emails

    def test_non_email_issuer_assigned_id_skipped(self) -> None:
        user = {
            "id": "abc",
            "userPrincipalName": "alice@fabrikam.onmicrosoft.com",
            "mail": "alice@fabrikam.onmicrosoft.com",
            "identities": [
                {
                    "signInType": "federated",
                    "issuer": "google.com",
                    "issuerAssignedId": "1234567890",
                }
            ],
        }
        sponsor = AgentIdentitySponsor.from_graph_user(user)
        assert sponsor is not None
        emails = sponsor.email_identifiers()
        assert "1234567890" not in emails

    def test_missing_identities_array_no_crash(self) -> None:
        user = {
            "id": "abc",
            "userPrincipalName": "alice@fabrikam.onmicrosoft.com",
            "mail": "alice@fabrikam.onmicrosoft.com",
        }
        sponsor = AgentIdentitySponsor.from_graph_user(user)
        assert sponsor is not None
        assert "alice@fabrikam.onmicrosoft.com" in sponsor.email_identifiers()


class TestCrossTenantChatMemberMatching:
    """End-to-end: federated chat member's userId joins the gate via email match."""

    def test_chat_member_home_tenant_email_matches_federated_sponsor(self) -> None:
        sponsor = AgentIdentitySponsor.from_graph_user(GUEST_WITH_FEDERATED_IDENTITY)
        assert sponsor is not None
        gate = SponsorGate.from_agent_identity_sponsors([sponsor])

        # Federated chat-members API returns home-tenant userId + home SMTP.
        member = {
            "user_id": "00112233-4455-6677-8899-aabbccddeeff",
            "email": "Alice.Smith@example.com",
        }
        gate2 = gate.with_chat_members([member])
        assert "00112233-4455-6677-8899-aabbccddeeff" in gate2.user_ids

        # Inbound message with that home-tenant senderId is now accepted.
        assert gate2.accepts({"sender_id": "00112233-4455-6677-8899-aabbccddeeff", "sender": ""})

    def test_invitation_alias_chat_member_still_works(self) -> None:
        """Pre-existing path: chat member email matches sponsor.mail directly."""
        sponsor = AgentIdentitySponsor.from_graph_user(GUEST_WITH_FEDERATED_IDENTITY)
        assert sponsor is not None
        gate = SponsorGate.from_agent_identity_sponsors([sponsor])

        member = {"user_id": "some-other-id", "email": "alice@example.com"}
        gate2 = gate.with_chat_members([member])
        assert "some-other-id" in gate2.user_ids


class TestUnqGblSpacesChatIdEnrichment:
    """The Graph chat-members API does NOT expose email for cross-tenant
    federated B2B guests in 1:1 ``unq.gbl.spaces`` chats. The chat_id itself
    is the only reliable carrier of the sponsor's home-tenant userId.

    Format: ``19:{user_a_id}_{user_b_id}@unq.gbl.spaces`` where one half is
    the agent's user_id and the other half is the cross-tenant sponsor's
    home-tenant userId. Strip the agent half; the remainder is the sponsor.

    Promotion is gated on the chat being *verified* to contain a known
    sponsor (by member email or member userId) — so a chat the agent merely
    watches (e.g. auto-discovered) cannot widen the sponsor set.
    """

    AGENT_USER_ID = "aaaabbbb-cccc-dddd-eeee-111122223333"
    SPONSOR_HOME_USER_ID = "00112233-4455-6677-8899-aabbccddeeff"
    # The sponsor's *agent-tenant* guest OID (from GUEST_WITH_FEDERATED_IDENTITY).
    SPONSOR_GUEST_OID = "963835fc-0b5c-4f3e-9f42-a1b906d9fbf8"

    def _gate(self) -> SponsorGate:
        sponsor = AgentIdentitySponsor.from_graph_user(GUEST_WITH_FEDERATED_IDENTITY)
        assert sponsor is not None
        return SponsorGate.from_agent_identity_sponsors([sponsor])

    def _sponsor_members(self) -> list[dict]:
        """Members for a 1:1 chat that genuinely contains the sponsor."""
        return [
            {"user_id": "agent-user-oid", "email": "entrabot-agent@fabrikam.onmicrosoft.com"},
            {"user_id": self.SPONSOR_GUEST_OID, "email": "alice@example.com"},
        ]

    def test_extracts_non_agent_half_from_unq_gbl_spaces_chat_id(self) -> None:
        chat_id = f"19:{self.SPONSOR_HOME_USER_ID}_{self.AGENT_USER_ID}@unq.gbl.spaces"
        gate = self._gate().with_watched_chat_ids(
            {chat_id: self._sponsor_members()}, self.AGENT_USER_ID
        )
        assert self.SPONSOR_HOME_USER_ID in gate.user_ids
        assert gate.accepts({"sender_id": self.SPONSOR_HOME_USER_ID, "sender": ""})

    def test_handles_agent_id_in_either_position(self) -> None:
        # Sponsor first, agent second.
        chat_a = f"19:{self.SPONSOR_HOME_USER_ID}_{self.AGENT_USER_ID}@unq.gbl.spaces"
        # Agent first, sponsor second (rarer but format-legal).
        chat_b = f"19:{self.AGENT_USER_ID}_{self.SPONSOR_HOME_USER_ID}@unq.gbl.spaces"
        gate_a = self._gate().with_watched_chat_ids(
            {chat_a: self._sponsor_members()}, self.AGENT_USER_ID
        )
        gate_b = self._gate().with_watched_chat_ids(
            {chat_b: self._sponsor_members()}, self.AGENT_USER_ID
        )
        assert self.SPONSOR_HOME_USER_ID in gate_a.user_ids
        assert self.SPONSOR_HOME_USER_ID in gate_b.user_ids

    def test_verifies_via_member_userid_when_email_absent(self) -> None:
        """Opaque guest: members API returns no email, only the agent-tenant
        guest OID — which is in the relationship sponsor set. Still verifies."""
        chat_id = f"19:{self.SPONSOR_HOME_USER_ID}_{self.AGENT_USER_ID}@unq.gbl.spaces"
        members = [
            {"user_id": "agent-user-oid", "email": ""},
            {"user_id": self.SPONSOR_GUEST_OID, "email": ""},  # no email exposed
        ]
        gate = self._gate().with_watched_chat_ids({chat_id: members}, self.AGENT_USER_ID)
        assert self.SPONSOR_HOME_USER_ID in gate.user_ids

    def test_skips_group_chat_ids(self) -> None:
        """``@thread.v2`` group chats are NOT 1:1 — don't trust arbitrary halves."""
        chat_id = "19:abc123@thread.v2"
        gate = self._gate().with_watched_chat_ids(
            {chat_id: self._sponsor_members()}, self.AGENT_USER_ID
        )
        # Pre-existing user_ids stay; nothing extracted from group chat.
        assert all(uid != "abc123" for uid in gate.user_ids)

    def test_skips_chat_id_without_agent_half(self) -> None:
        """If neither half matches the agent's user_id, don't add anything."""
        unrelated = (
            "19:11111111-1111-1111-1111-111111111111"
            "_22222222-2222-2222-2222-222222222222@unq.gbl.spaces"
        )
        original = self._gate()
        enriched = original.with_watched_chat_ids(
            {unrelated: self._sponsor_members()}, self.AGENT_USER_ID
        )
        # Neither half should be added since agent is not a participant.
        assert "11111111-1111-1111-1111-111111111111" not in enriched.user_ids
        assert "22222222-2222-2222-2222-222222222222" not in enriched.user_ids

    def test_no_agent_user_id_is_no_op(self) -> None:
        chat_id = f"19:{self.SPONSOR_HOME_USER_ID}_{self.AGENT_USER_ID}@unq.gbl.spaces"
        gate = self._gate().with_watched_chat_ids(
            {chat_id: self._sponsor_members()}, ""
        )
        assert self.SPONSOR_HOME_USER_ID not in gate.user_ids

    def test_malformed_chat_ids_do_not_crash(self) -> None:
        bad = {
            "": self._sponsor_members(),
            "not-a-chat-id": self._sponsor_members(),
            "19:@unq.gbl.spaces": self._sponsor_members(),
            "19:only-one-guid@unq.gbl.spaces": self._sponsor_members(),
            # both halves agent
            f"19:{self.AGENT_USER_ID}_{self.AGENT_USER_ID}@unq.gbl.spaces": self._sponsor_members(),
        }
        gate = self._gate().with_watched_chat_ids(bad, self.AGENT_USER_ID)
        # No spurious additions.
        assert self.SPONSOR_HOME_USER_ID not in gate.user_ids


class TestWatchedChatSponsorVerification:
    """Security: a watched 1:1 chat must NOT widen the sponsor set unless the
    chat is verified to contain a known sponsor.

    Regression guard for the auto-discovery / create_chat authorization-bypass:
    any tenant user who opens a 1:1 DM with the Agent User gets that chat
    auto-registered into ``watched_chats``. Without per-chat verification, the
    attacker's home-tenant userId (encoded in the chat_id) was promoted into
    ``gate.user_ids`` and their messages were treated as trusted sponsor
    instructions.
    """

    AGENT_USER_ID = "aaaabbbb-cccc-dddd-eeee-111122223333"
    SPONSOR_GUEST_OID = "963835fc-0b5c-4f3e-9f42-a1b906d9fbf8"
    ATTACKER_HOME_OID = "deadbeef-0000-0000-0000-000000000001"

    def _gate(self) -> SponsorGate:
        sponsor = AgentIdentitySponsor.from_graph_user(GUEST_WITH_FEDERATED_IDENTITY)
        assert sponsor is not None
        return SponsorGate.from_agent_identity_sponsors([sponsor])

    def test_attacker_chat_does_not_promote_counterparty(self) -> None:
        """The core bypass: an attacker-initiated 1:1 chat (no sponsor member)
        must NOT add the attacker's userId to the sponsor set."""
        chat_id = f"19:{self.ATTACKER_HOME_OID}_{self.AGENT_USER_ID}@unq.gbl.spaces"
        members = [
            {"user_id": "agent-user-oid", "email": "entrabot-agent@fabrikam.onmicrosoft.com"},
            {"user_id": "attacker-guest-oid", "email": "attacker@evil.example"},
        ]
        gate = self._gate().with_watched_chat_ids({chat_id: members}, self.AGENT_USER_ID)
        assert self.ATTACKER_HOME_OID not in gate.user_ids
        assert not gate.accepts({"sender_id": self.ATTACKER_HOME_OID, "sender": ""})

    def test_empty_members_fail_closed(self) -> None:
        """If membership can't be fetched (transient Graph failure → empty),
        do NOT promote — fail closed."""
        chat_id = f"19:{self.ATTACKER_HOME_OID}_{self.AGENT_USER_ID}@unq.gbl.spaces"
        gate = self._gate().with_watched_chat_ids({chat_id: []}, self.AGENT_USER_ID)
        assert self.ATTACKER_HOME_OID not in gate.user_ids

    def test_mixed_chats_only_promote_verified(self) -> None:
        """A verified sponsor chat and an attacker chat watched simultaneously:
        only the sponsor's counterparty is promoted."""
        sponsor_home = "00112233-4455-6677-8899-aabbccddeeff"
        sponsor_chat = f"19:{sponsor_home}_{self.AGENT_USER_ID}@unq.gbl.spaces"
        attacker_chat = f"19:{self.ATTACKER_HOME_OID}_{self.AGENT_USER_ID}@unq.gbl.spaces"
        members_by_id = {
            sponsor_chat: [
                {"user_id": "agent-user-oid", "email": "entrabot-agent@fabrikam.onmicrosoft.com"},
                {"user_id": self.SPONSOR_GUEST_OID, "email": "alice@example.com"},
            ],
            attacker_chat: [
                {"user_id": "agent-user-oid", "email": "entrabot-agent@fabrikam.onmicrosoft.com"},
                {"user_id": "attacker-guest-oid", "email": "attacker@evil.example"},
            ],
        }
        gate = self._gate().with_watched_chat_ids(members_by_id, self.AGENT_USER_ID)
        assert sponsor_home in gate.user_ids
        assert self.ATTACKER_HOME_OID not in gate.user_ids

    def test_lookalike_spaces_suffix_not_treated_as_1to1(self) -> None:
        """A chat_id with a lookalike suffix (``...@unq.gbl.spaces.evil``) must
        NOT be treated as a 1:1 DM, even if a sponsor is a member. Matching on a
        bare substring rather than the exact suffix would misclassify it."""
        sponsor_home = "00112233-4455-6677-8899-aabbccddeeff"
        chat_id = f"19:{sponsor_home}_{self.AGENT_USER_ID}@unq.gbl.spaces.evil"
        members = [
            {"user_id": "agent-user-oid", "email": "entrabot-agent@fabrikam.onmicrosoft.com"},
            {"user_id": self.SPONSOR_GUEST_OID, "email": "alice@example.com"},
        ]
        gate = self._gate().with_watched_chat_ids({chat_id: members}, self.AGENT_USER_ID)
        assert sponsor_home not in gate.user_ids
