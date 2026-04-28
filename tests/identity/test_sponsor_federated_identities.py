"""Tests for cross-tenant sponsor matching via federated identities.

When a microsoft.com user is a B2B guest in the agent tenant, the guest
record's ``identities`` array contains a federated entry whose
``issuerAssignedId`` holds the user's home-tenant SMTP (e.g.
``Brandon.Werner@microsoft.com``). The chat-members API returns the
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
    "userPrincipalName": "brandwe_microsoft.com#EXT#@werner.ac",
    "mail": "brandwe@microsoft.com",
    "otherMails": ["brandwe@microsoft.com"],
    "proxyAddresses": [],
    "identities": [
        {
            "signInType": "userPrincipalName",
            "issuer": "werner.ac",
            "issuerAssignedId": "brandwe_microsoft.com#EXT#@werner.ac",
        },
        {
            "signInType": "federated",
            "issuer": "microsoft.com",
            "issuerAssignedId": "Brandon.Werner@microsoft.com",
        },
    ],
}


class TestSponsorIdentitiesExtraction:
    def test_federated_issuer_assigned_id_added_to_email_identifiers(self) -> None:
        sponsor = AgentIdentitySponsor.from_graph_user(GUEST_WITH_FEDERATED_IDENTITY)
        assert sponsor is not None
        emails = sponsor.email_identifiers()
        assert "user@example.com" in emails
        assert "brandwe@microsoft.com" in emails

    def test_non_email_issuer_assigned_id_skipped(self) -> None:
        user = {
            "id": "abc",
            "userPrincipalName": "alice@werner.ac",
            "mail": "alice@werner.ac",
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
            "userPrincipalName": "alice@werner.ac",
            "mail": "alice@werner.ac",
        }
        sponsor = AgentIdentitySponsor.from_graph_user(user)
        assert sponsor is not None
        assert "alice@werner.ac" in sponsor.email_identifiers()


class TestCrossTenantChatMemberMatching:
    """End-to-end: federated chat member's userId joins the gate via email match."""

    def test_chat_member_home_tenant_email_matches_federated_sponsor(self) -> None:
        sponsor = AgentIdentitySponsor.from_graph_user(GUEST_WITH_FEDERATED_IDENTITY)
        assert sponsor is not None
        gate = SponsorGate.from_agent_identity_sponsors([sponsor])

        # Federated chat-members API returns home-tenant userId + home SMTP.
        member = {
            "user_id": "4d4a65ef-e9b3-4ec2-a1e2-b430a5855118",
            "email": "Brandon.Werner@microsoft.com",
        }
        gate2 = gate.with_chat_members([member])
        assert "4d4a65ef-e9b3-4ec2-a1e2-b430a5855118" in gate2.user_ids

        # Inbound message with that home-tenant senderId is now accepted.
        assert gate2.accepts(
            {"sender_id": "4d4a65ef-e9b3-4ec2-a1e2-b430a5855118", "sender": ""}
        )

    def test_invitation_alias_chat_member_still_works(self) -> None:
        """Pre-existing path: chat member email matches sponsor.mail directly."""
        sponsor = AgentIdentitySponsor.from_graph_user(GUEST_WITH_FEDERATED_IDENTITY)
        assert sponsor is not None
        gate = SponsorGate.from_agent_identity_sponsors([sponsor])

        member = {"user_id": "some-other-id", "email": "brandwe@microsoft.com"}
        gate2 = gate.with_chat_members([member])
        assert "some-other-id" in gate2.user_ids
