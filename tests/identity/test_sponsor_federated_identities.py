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

from entraclaw.identity.sponsors import (
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
    """

    AGENT_USER_ID = "aaaabbbb-cccc-dddd-eeee-111122223333"
    SPONSOR_HOME_USER_ID = "00112233-4455-6677-8899-aabbccddeeff"

    def _gate(self) -> SponsorGate:
        sponsor = AgentIdentitySponsor.from_graph_user(GUEST_WITH_FEDERATED_IDENTITY)
        assert sponsor is not None
        return SponsorGate.from_agent_identity_sponsors([sponsor])

    def test_extracts_non_agent_half_from_unq_gbl_spaces_chat_id(self) -> None:
        chat_id = f"19:{self.SPONSOR_HOME_USER_ID}_{self.AGENT_USER_ID}@unq.gbl.spaces"
        gate = self._gate().with_watched_chat_ids([chat_id], self.AGENT_USER_ID)
        assert self.SPONSOR_HOME_USER_ID in gate.user_ids
        assert gate.accepts({"sender_id": self.SPONSOR_HOME_USER_ID, "sender": ""})

    def test_handles_agent_id_in_either_position(self) -> None:
        # Sponsor first, agent second.
        chat_a = f"19:{self.SPONSOR_HOME_USER_ID}_{self.AGENT_USER_ID}@unq.gbl.spaces"
        # Agent first, sponsor second (rarer but format-legal).
        chat_b = f"19:{self.AGENT_USER_ID}_{self.SPONSOR_HOME_USER_ID}@unq.gbl.spaces"
        gate_a = self._gate().with_watched_chat_ids([chat_a], self.AGENT_USER_ID)
        gate_b = self._gate().with_watched_chat_ids([chat_b], self.AGENT_USER_ID)
        assert self.SPONSOR_HOME_USER_ID in gate_a.user_ids
        assert self.SPONSOR_HOME_USER_ID in gate_b.user_ids

    def test_skips_group_chat_ids(self) -> None:
        """``@thread.v2`` group chats are NOT 1:1 — don't trust arbitrary halves."""
        chat_id = "19:abc123@thread.v2"
        gate = self._gate().with_watched_chat_ids([chat_id], self.AGENT_USER_ID)
        # Pre-existing user_ids stay; nothing extracted from group chat.
        assert all(uid != "abc123" for uid in gate.user_ids)

    def test_skips_chat_id_without_agent_half(self) -> None:
        """If neither half matches the agent's user_id, don't add anything."""
        unrelated = (
            "19:11111111-1111-1111-1111-111111111111"
            "_22222222-2222-2222-2222-222222222222@unq.gbl.spaces"
        )
        original = self._gate()
        enriched = original.with_watched_chat_ids([unrelated], self.AGENT_USER_ID)
        # Neither half should be added since agent is not a participant.
        assert "11111111-1111-1111-1111-111111111111" not in enriched.user_ids
        assert "22222222-2222-2222-2222-222222222222" not in enriched.user_ids

    def test_no_agent_user_id_is_no_op(self) -> None:
        chat_id = f"19:{self.SPONSOR_HOME_USER_ID}_{self.AGENT_USER_ID}@unq.gbl.spaces"
        gate = self._gate().with_watched_chat_ids([chat_id], "")
        assert self.SPONSOR_HOME_USER_ID not in gate.user_ids

    def test_malformed_chat_ids_do_not_crash(self) -> None:
        bad_ids = [
            "",
            "not-a-chat-id",
            "19:@unq.gbl.spaces",
            "19:only-one-guid@unq.gbl.spaces",
            f"19:{self.AGENT_USER_ID}_{self.AGENT_USER_ID}@unq.gbl.spaces",  # both halves agent
        ]
        gate = self._gate().with_watched_chat_ids(bad_ids, self.AGENT_USER_ID)
        # No spurious additions.
        assert self.SPONSOR_HOME_USER_ID not in gate.user_ids
