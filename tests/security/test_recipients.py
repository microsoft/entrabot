"""Who the agent may reach: the server-side recipient policy.

Default: the agent's sponsors (from the directory), the members of the chat the request
came from, and anyone in the agent's home tenant. Anyone else needs an explicit allowlist
entry (``ENTRABOT_OUTBOUND_ALLOW``). It is enforced in code, before Graph is called; the
prompt has no say.
"""

from __future__ import annotations

import pytest

from entrabot.security.recipients import (
    RecipientContext,
    RecipientPolicy,
    decide,
    parse_allowlist,
)

HOME_TENANT = "11111111-1111-4111-8111-111111111111"
OTHER_TENANT = "22222222-2222-4222-8222-222222222222"


def _policy(**overrides: object) -> RecipientPolicy:
    values: dict[str, object] = {
        "home_tenant_id": HOME_TENANT,
        "home_domains": frozenset({"contoso.com"}),
        "allow_addresses": frozenset(),
        "allow_domains": frozenset(),
    }
    values.update(overrides)
    return RecipientPolicy(**values)  # type: ignore[arg-type]


def _context(**overrides: object) -> RecipientContext:
    values: dict[str, object] = {
        "sponsors": frozenset(),
        "chat_members": frozenset(),
        "tenant_of_domain": {},
    }
    values.update(overrides)
    return RecipientContext(**values)  # type: ignore[arg-type]


class TestAllowed:
    def test_someone_in_a_home_domain(self) -> None:
        assert decide("Pat@Contoso.com", _policy(), _context()).allowed

    def test_a_domain_that_resolves_to_the_home_tenant(self) -> None:
        context = _context(tenant_of_domain={"contoso.onmicrosoft.com": HOME_TENANT})
        assert decide("pat@contoso.onmicrosoft.com", _policy(), context).allowed

    def test_a_sponsor_in_another_tenant(self) -> None:
        # Cross-tenant sponsor chats are a shipped feature.
        context = _context(
            sponsors=frozenset({"sponsor@fabrikam.com"}),
            tenant_of_domain={"fabrikam.com": OTHER_TENANT},
        )
        decision = decide("Sponsor@Fabrikam.com", _policy(), context)
        assert decision.allowed and decision.reason == "sponsor"

    def test_a_member_of_the_chat_the_request_came_from(self) -> None:
        context = _context(chat_members=frozenset({"guest@fabrikam.com"}))
        decision = decide("guest@fabrikam.com", _policy(), context)
        assert decision.allowed and decision.reason == "member of the requesting chat"

    def test_an_allowlisted_address(self) -> None:
        policy = _policy(allow_addresses=frozenset({"partner@fabrikam.com"}))
        assert decide("partner@fabrikam.com", policy, _context()).allowed

    def test_an_allowlisted_domain(self) -> None:
        policy = _policy(allow_domains=frozenset({"fabrikam.com"}))
        assert decide("anyone@fabrikam.com", policy, _context()).allowed


class TestRefused:
    def test_an_external_address_nobody_vouched_for(self) -> None:
        context = _context(tenant_of_domain={"gmail.com": None})
        decision = decide("attacker@gmail.com", _policy(), context)
        assert not decision.allowed
        assert "ENTRABOT_OUTBOUND_ALLOW" in decision.reason

    def test_a_domain_whose_tenant_could_not_be_resolved_is_external(self) -> None:
        assert not decide("x@unknown.example", _policy(), _context()).allowed

    def test_a_lookalike_of_a_home_domain(self) -> None:
        for address in ("pat@contoso.com.evil.example", "pat@evilcontoso.com"):
            assert not decide(address, _policy(), _context()).allowed

    def test_an_allowlisted_domain_does_not_cover_its_parent_or_a_lookalike(self) -> None:
        policy = _policy(allow_domains=frozenset({"fabrikam.com"}))
        assert not decide("x@notfabrikam.com", policy, _context()).allowed

    @pytest.mark.parametrize(
        "address", ["", "no-at-sign", "a@", "@b.com", "a@b@c.com", "a b@c.com"]
    )
    def test_something_that_is_not_an_address(self, address: str) -> None:
        assert not decide(address, _policy(), _context()).allowed


class TestAllowlist:
    def test_addresses_and_domains_are_told_apart_and_lowercased(self) -> None:
        addresses, domains = parse_allowlist(" Partner@Fabrikam.com, @Tailspin.com ,adatum.com,")
        assert addresses == frozenset({"partner@fabrikam.com"})
        assert domains == frozenset({"tailspin.com", "adatum.com"})

    def test_empty_is_empty(self) -> None:
        assert parse_allowlist(None) == (frozenset(), frozenset())
