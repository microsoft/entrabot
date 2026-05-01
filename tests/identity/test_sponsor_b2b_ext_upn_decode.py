"""Tests for B2B-guest EXT-UPN decoding into the sponsor email allowlist.

When a Microsoft Account (or any B2B guest with sparse user-object
fields) is a sponsor on the Agent Identity, Graph populates
``userPrincipalName`` with the encoded EXT form
(``brandwe_outlook.com#EXT#@brandwedir.onmicrosoft.com``) but often
leaves ``mail``, ``otherMails``, ``proxyAddresses``, and federated
``identities[].issuerAssignedId`` null or with non-email values (the
guest's OID).

Humans never type the EXT form. They type the home address. So the
sponsor allowlist must accept BOTH the EXT UPN and its decoded home
address.

Real-world test case: 2026-04-30 production. Brandon's MSA guest
record had only ``userPrincipalName`` populated. Stage 1 (User.ReadBasic.All)
returned a populated UPN but null mail/otherMails/identities. Stage 2
(chat-members) returned the same EXT UPN as ``email``. The user typed
``brandwe@outlook.com`` in chat — neither stage's data matched without
the EXT decoder.
"""

from __future__ import annotations

from entraclaw.identity.sponsors import (
    AgentIdentitySponsor,
    _decode_b2b_ext_upn,
)


class TestDecodeB2bExtUpn:
    def test_msa_guest_simple_form(self) -> None:
        upn = "brandwe_outlook.com#EXT#@brandwedir.onmicrosoft.com"
        assert _decode_b2b_ext_upn(upn) == "brandwe@outlook.com"

    def test_federated_guest_form(self) -> None:
        upn = "charlie_smith.ac#EXT#@sara.onmicrosoft.com"
        assert _decode_b2b_ext_upn(upn) == "brandon@werner.ac"

    def test_local_part_with_dots_and_underscores(self) -> None:
        # Local-part can legitimately contain underscores; the LAST `_`
        # before `#EXT#@` separates local-part from domain.
        upn = "charlie_smith_microsoft.com#EXT#@tenant.onmicrosoft.com"
        assert _decode_b2b_ext_upn(upn) == "user@example.com"

    def test_case_insensitive_separator(self) -> None:
        # Graph emits ``#EXT#@`` but we accept any case for robustness.
        assert (
            _decode_b2b_ext_upn("foo_bar.com#ext#@tenant.onmicrosoft.com")
            == "foo@bar.com"
        )
        assert (
            _decode_b2b_ext_upn("foo_bar.com#EXT#@tenant.onmicrosoft.com")
            == "foo@bar.com"
        )

    def test_native_user_upn_returns_none(self) -> None:
        # A regular tenant UPN (no #EXT#@) is not a B2B EXT form.
        assert _decode_b2b_ext_upn("brandon@werner.ac") is None
        assert _decode_b2b_ext_upn("agent@example.onmicrosoft.com") is None

    def test_malformed_inputs_return_none(self) -> None:
        assert _decode_b2b_ext_upn(None) is None
        assert _decode_b2b_ext_upn("") is None
        assert _decode_b2b_ext_upn("   ") is None
        # Encoded local-part with no underscore separator → unparseable.
        assert _decode_b2b_ext_upn("garbage#EXT#@tenant.onmicrosoft.com") is None
        # Domain without dot → unparseable.
        assert _decode_b2b_ext_upn("foo_bar#EXT#@tenant.onmicrosoft.com") is None
        # Empty local-part (leading underscore) → unparseable.
        assert _decode_b2b_ext_upn("_bar.com#EXT#@tenant.onmicrosoft.com") is None


class TestSponsorEmailIdentifiersIncludesDecodedExtUpn:
    """``email_identifiers()`` must return BOTH the EXT UPN and its
    decoded home address so the allowlist matches what humans type."""

    def test_msa_guest_with_only_upn_populated(self) -> None:
        # Real-world shape: MSA guest, all fields except UPN are null.
        sponsor = AgentIdentitySponsor.from_graph_user(
            {
                "id": "33333333-3333-3333-3333-333333333333",
                "userPrincipalName": "brandwe_outlook.com#EXT#@brandwedir.onmicrosoft.com",
                "mail": None,
                "otherMails": [],
                "proxyAddresses": [],
                "identities": [],
            }
        )
        assert sponsor is not None
        identifiers = sponsor.email_identifiers()
        # The decoded home address — what the user actually typed.
        assert "brandwe@outlook.com" in identifiers
        # The EXT UPN form — kept for paths that already match on UPN.
        assert (
            "brandwe_outlook.com#ext#@brandwedir.onmicrosoft.com" in identifiers
        )

    def test_federated_guest_decoded_alongside_other_identifiers(self) -> None:
        sponsor = AgentIdentitySponsor.from_graph_user(
            {
                "id": "ABC",
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
        )
        assert sponsor is not None
        identifiers = sponsor.email_identifiers()
        assert "brandon@werner.ac" in identifiers
        # Decoded EXT UPN duplicates the home address — that's fine,
        # frozenset dedups.
        assert (
            "charlie_smith.ac#ext#@sara.onmicrosoft.com" in identifiers
        )

    def test_native_tenant_user_no_decoded_form(self) -> None:
        # Native (non-guest) UPN should not produce a spurious decoded form.
        sponsor = AgentIdentitySponsor.from_graph_user(
            {
                "id": "ABC",
                "userPrincipalName": "alice@werner.ac",
                "mail": "alice@werner.ac",
            }
        )
        assert sponsor is not None
        identifiers = sponsor.email_identifiers()
        assert identifiers == frozenset({"alice@werner.ac"})
