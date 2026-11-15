"""The one address to write to for a sponsor, from their directory record."""

from __future__ import annotations

from entrabot.identity.sponsors import AgentIdentitySponsor, sponsor_addresses


def _sponsor(**fields: object) -> AgentIdentitySponsor:
    values: dict[str, object] = {"user_id": "u-1", "user_principal_name": None, "mail": None}
    values.update(fields)
    return AgentIdentitySponsor(**values)  # type: ignore[arg-type]


class TestAddress:
    def test_mail_first(self) -> None:
        sponsor = _sponsor(user_principal_name="pat@contoso.com", mail="Pat.Lee@Contoso.com")
        assert sponsor.address() == "pat.lee@contoso.com"

    def test_a_guests_home_address_rather_than_its_ext_upn(self) -> None:
        sponsor = _sponsor(user_principal_name="alice_example.com#EXT#@fabrikam.onmicrosoft.com")
        assert sponsor.address() == "alice@example.com"

    def test_the_upn_when_there_is_no_mail(self) -> None:
        assert _sponsor(user_principal_name="pat@contoso.com").address() == "pat@contoso.com"

    def test_none_when_the_record_could_not_be_read(self) -> None:
        # Graph's sponsors relationship gives only the ID; the user record holds the rest.
        assert _sponsor().address() is None


def test_sponsor_addresses_skips_unreadable_records_and_duplicates() -> None:
    sponsors = [
        _sponsor(user_id="u-1", mail="pat@contoso.com"),
        _sponsor(user_id="u-2"),
        _sponsor(user_id="u-3", user_principal_name="PAT@contoso.com"),
    ]
    assert sponsor_addresses(sponsors) == ["pat@contoso.com"]
