"""Who the agent answers to: resolving sponsors, including B2B guests (setup.sh:644-734)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest

from entrabot.winsetup.azcli import AzCli, AzLocation, DirectoryUser
from entrabot.winsetup.sponsors import (
    SPONSOR_KEYS,
    Sponsor,
    SponsorError,
    home_domain_from_guest_upn,
    resolve_sponsors,
    sponsor_env,
    sponsor_from_user,
    tenant_id_from_discovery,
    validate_sponsor,
)
from tests.winsetup.fakes import FakeDirectory, FakeSpawn, make_console, words

MEMBER = {
    "id": "member-id",
    "displayName": "Pat Member",
    "userType": "Member",
    "mail": "pat@contoso.com",
    "userPrincipalName": "pat@contoso.com",
    "otherMails": [],
}
GUEST = {
    "id": "guest-id",
    "displayName": "Sam Guest",
    "userType": "Guest",
    "mail": "sam@fabrikam.com",
    "userPrincipalName": "sam_fabrikam.com#EXT#@contoso.onmicrosoft.com",
    "otherMails": [],
}
OWNER_ID = "0f3c1c7e-1111-2222-3333-444455556666"
# The tenant's owner, who signs in with a personal Microsoft account.
OWNER = {
    "id": "owner-id",
    "displayName": "Alex Wilber",
    "userType": "Member",
    "mail": None,
    "userPrincipalName": "alex_example.com#EXT#@contoso.onmicrosoft.com",
    "otherMails": ["alex@example.com"],
}


def _az_with(users: list[dict]) -> tuple[AzCli, FakeDirectory, FakeSpawn]:
    spawn = FakeSpawn()
    directory = FakeDirectory(users=[dict(user) for user in users])
    directory.attach(spawn)
    console, _ = make_console(spawn=spawn)
    az = AzCli(
        AzLocation(command=("python", "-m", "azure.cli"), bin_dir=Path("/x"), source="path"),
        console,
    )
    return az, directory, spawn


def _no_http(url: str) -> dict[str, object] | None:
    raise AssertionError(f"unexpected HTTP call to {url}")


class TestGuestHelpers:
    def test_home_domain_comes_from_the_guest_upn(self) -> None:
        upn = "sam_fabrikam.com#EXT#@contoso.onmicrosoft.com"
        assert home_domain_from_guest_upn(upn) == "fabrikam.com"

    def test_an_underscore_in_the_local_part_is_handled(self) -> None:
        upn = "sam_smith_fabrikam.com#EXT#@contoso.onmicrosoft.com"
        assert home_domain_from_guest_upn(upn) == "fabrikam.com"

    def test_a_member_upn_has_no_home_domain(self) -> None:
        assert home_domain_from_guest_upn("pat@contoso.com") == ""

    def test_tenant_id_is_the_last_segment_of_the_issuer(self) -> None:
        document = {"issuer": "https://sts.windows.net/aaaa-bbbb/"}
        assert tenant_id_from_discovery(document) == "aaaa-bbbb"
        assert tenant_id_from_discovery({}) == ""
        assert tenant_id_from_discovery(None) == ""


class TestResolve:
    def test_a_member_is_found_by_sign_in_name_and_needs_no_tenant_id(self) -> None:
        az, directory, _ = _az_with([MEMBER])
        sponsors = resolve_sponsors(["pat@contoso.com"], az, http_get=_no_http)
        assert sponsors == [
            Sponsor(
                object_id="member-id",
                upn="pat@contoso.com",
                mail="pat@contoso.com",
                user_type="Member",
                home_tenant_id="",
            )
        ]
        assert directory.asked == ["id pat@contoso.com"]  # stopped at the first hit

    def test_a_guest_gets_its_home_tenant(self) -> None:
        az, _, _ = _az_with([GUEST])
        urls: list[str] = []

        def http_get(url: str) -> dict[str, object] | None:
            urls.append(url)
            return {"issuer": "https://login.microsoftonline.com/home-tenant/v2.0"}

        sponsors = resolve_sponsors(["sam@fabrikam.com"], az, http_get=http_get)
        assert sponsors[0].user_type == "Guest"
        assert sponsors[0].home_tenant_id == "home-tenant"
        assert urls == [
            "https://login.microsoftonline.com/fabrikam.com/.well-known/openid-configuration"
        ]

    def test_a_guest_whose_home_tenant_cannot_be_found_still_resolves(self) -> None:
        az, _, _ = _az_with([GUEST])
        sponsors = resolve_sponsors(["sam@fabrikam.com"], az, http_get=lambda url: None)
        assert sponsors[0].user_type == "Guest" and sponsors[0].home_tenant_id == ""

    def test_a_guest_named_by_their_email_is_found_by_mail(self) -> None:
        # A guest's sign-in name is not their email, so the first lookup misses.
        az, directory, _ = _az_with([GUEST])
        sponsors = resolve_sponsors(["sam@fabrikam.com"], az, http_get=lambda url: None)
        assert sponsors[0].object_id == "guest-id"
        assert sponsors[0].upn == "sam@fabrikam.com"  # what was typed, as setup.sh records it
        assert directory.asked == [
            "id sam@fabrikam.com",
            "userPrincipalName eq 'sam@fabrikam.com'",
            "mail eq 'sam@fabrikam.com'",
        ]

    def test_an_unknown_user_is_an_error_saying_what_was_tried(self) -> None:
        az, _, _ = _az_with([MEMBER])
        with pytest.raises(SponsorError) as error:
            resolve_sponsors(["nobody@contoso.com"], az, http_get=_no_http)
        message = str(error.value)
        assert "nobody@contoso.com" in message
        for tried in (
            "sign-in name",
            "a search for their sign-in name",
            "primary email",
            "other email",
            "aliases",
            "guest",
        ):
            assert tried in message
        assert "object ID" in message  # the way that always works

    @pytest.mark.parametrize(
        "bad", ["not an email", "a b@c.com", 'a"b@c.com', "<a@c.com>", "a,b@c.com", "a@b", ""]
    )
    def test_something_that_is_not_an_address_or_id_is_refused_before_any_lookup(
        self, bad: str
    ) -> None:
        az, _, spawn = _az_with([MEMBER])
        with pytest.raises(SponsorError):
            resolve_sponsors([bad], az, http_get=_no_http)
        assert spawn.calls == []

    def test_a_missing_user_type_is_inferred_from_the_upn(self) -> None:
        user = DirectoryUser(object_id="g", upn=GUEST["userPrincipalName"], mail="", user_type="")
        sponsor = sponsor_from_user(user, entered="sam@fabrikam.com", http_get=lambda url: None)
        assert sponsor.user_type == "Guest"


class TestPersonalAndExternalAccounts:
    """Measured on a real tenant: its owner signs in with a personal Microsoft account.

    In the directory that person is ``alex_example.com#EXT#@<tenant>.onmicrosoft.com``, a
    Member, with no ``mail``; the address they are known by lives in ``otherMails``, the email
    aliases, or only in that sign-in name. Looking up the address as a sign-in name and as
    ``mail`` found nobody, and setup refused a sponsor who was working on another machine.
    """

    def test_the_address_is_found_among_other_mails(self) -> None:
        az, directory, _ = _az_with([OWNER])
        sponsors = resolve_sponsors(["alex@example.com"], az, http_get=_no_http)
        assert sponsors == [
            Sponsor(
                object_id="owner-id",
                upn="alex@example.com",  # what was typed, as the other machine recorded it
                mail="alex@example.com",
                user_type="Member",  # what the directory says, although the name has #EXT#
                home_tenant_id="",
            )
        ]
        assert directory.asked == [
            "id alex@example.com",
            "userPrincipalName eq 'alex@example.com'",
            "mail eq 'alex@example.com'",
            "otherMails/any(x:x eq 'alex@example.com')",
        ]

    @pytest.mark.parametrize("stored", ["SMTP:alex@example.com", "smtp:alex@example.com"])
    def test_the_address_is_found_among_email_aliases_in_either_spelling(self, stored: str) -> None:
        owner = {**OWNER, "otherMails": [], "proxyAddresses": [stored]}
        az, directory, _ = _az_with([owner])
        sponsors = resolve_sponsors(["alex@example.com"], az, http_get=_no_http)
        assert sponsors[0].object_id == "owner-id"
        assert "proxyAddresses/any(x:x eq 'SMTP:alex@example.com')" in directory.asked

    def test_the_address_is_found_from_the_external_sign_in_name_alone(self) -> None:
        owner = {**OWNER, "otherMails": []}
        az, directory, _ = _az_with([owner])
        sponsors = resolve_sponsors(["alex@example.com"], az, http_get=_no_http)
        assert sponsors[0].object_id == "owner-id" and sponsors[0].user_type == "Member"
        # "@" becomes "_" in an external sign-in name.
        assert directory.asked[-1] == "startswith(userPrincipalName,'alex_example.com')"

    def test_a_longer_name_that_merely_starts_the_same_is_not_a_match(self) -> None:
        # alex_example.com.example#EXT#... is somebody else.
        lookalike = {
            **OWNER,
            "id": "other-id",
            "otherMails": [],
            "userPrincipalName": "alex_example.com.example#EXT#@contoso.onmicrosoft.com",
        }
        az, _, _ = _az_with([lookalike])
        with pytest.raises(SponsorError, match="Could not find"):
            resolve_sponsors(["alex@example.com"], az, http_get=_no_http)

    def test_no_hash_ever_reaches_the_url_unencoded(self) -> None:
        # A raw "#" starts a URL fragment: everything after it would silently not be sent.
        az, directory, _ = _az_with([OWNER])
        resolve_sponsors([OWNER["userPrincipalName"]], az, http_get=_no_http)  # type: ignore[list-item]
        resolve_sponsors(["alex@example.com"], az, http_get=_no_http)
        assert directory.urls and not any("#" in url for url in directory.urls)
        assert directory.asked[0] == "id alex_example.com#ext#@contoso.onmicrosoft.com"

    def test_the_external_sign_in_name_itself_can_be_given(self) -> None:
        az, _, _ = _az_with([OWNER])
        typed = "alex_example.com#EXT#@contoso.onmicrosoft.com"
        assert resolve_sponsors([typed], az, http_get=_no_http)[0].object_id == "owner-id"

    def test_an_object_id_is_accepted_and_recorded_under_the_persons_address(self) -> None:
        az, directory, _ = _az_with([{**OWNER, "id": OWNER_ID}])
        sponsors = resolve_sponsors([OWNER_ID], az, http_get=_no_http)
        assert sponsors[0].object_id == OWNER_ID
        assert sponsors[0].upn == "alex@example.com"  # an address, not a GUID, in .env
        assert directory.asked == [f"id {OWNER_ID}"]

    def test_an_unknown_object_id_is_not_searched_for_as_an_email(self) -> None:
        az, directory, _ = _az_with([OWNER])
        missing = "00000000-0000-0000-0000-00000000dead"
        with pytest.raises(SponsorError, match="object ID"):
            resolve_sponsors([missing], az, http_get=_no_http)
        assert directory.asked == [f"id {missing}"]

    def test_two_people_with_the_same_address_are_listed_and_setup_asks_which(self) -> None:
        twin = {
            **OWNER,
            "id": "twin-id",
            "displayName": "Alex (old account)",
            "userPrincipalName": "alex.old@contoso.com",
        }
        az, _, _ = _az_with([OWNER, twin])
        with pytest.raises(SponsorError) as error:
            resolve_sponsors(["alex@example.com"], az, http_get=_no_http)
        message = str(error.value)
        assert "Alex Wilber" in message and "Alex (old account)" in message
        assert "alex_example.com#EXT#@contoso.onmicrosoft.com" in message
        assert "alex.old@contoso.com" in message
        assert "object ID" in message

    def test_a_lookup_graph_rejects_is_skipped_not_fatal(self) -> None:
        owner = {**OWNER, "otherMails": []}
        az, directory, _ = _az_with([owner])
        directory.fail_filters = ("proxyAddresses",)
        assert resolve_sponsors(["alex@example.com"], az, http_get=_no_http)[0].object_id == (
            "owner-id"
        )

    def test_only_the_fields_setup_uses_are_requested(self) -> None:
        # Less of the directory in the setup log.
        az, directory, _ = _az_with([OWNER])
        resolve_sponsors(["alex@example.com"], az, http_get=_no_http)
        assert all("%24select=" in url or "$select=" in url for url in directory.urls)
        assert all("proxyAddresses" not in url.split("select=")[1] for url in directory.urls)

    def test_every_lookup_asks_for_json(self) -> None:
        az, _, spawn = _az_with([OWNER])
        resolve_sponsors(["alex@example.com"], az, http_get=_no_http)
        assert spawn.calls and all(call.argv[-2:] == ["--output", "json"] for call in spawn.calls)

    def test_a_batch_file_azure_cli_still_finds_members_and_mail(self) -> None:
        # With az.cmd as the only way in, cmd.exe would reinterpret an encoded URL. The
        # lookups that need no special characters still run; the rest are named as skipped.
        spawn = FakeSpawn()
        directory = FakeDirectory(users=[dict(GUEST)])
        directory.attach_cli(spawn)
        console, _ = make_console(spawn=spawn)
        az = AzCli(
            AzLocation(command=("C:/az/az.cmd",), bin_dir=Path("/x"), source="path"), console
        )
        assert resolve_sponsors(["sam@fabrikam.com"], az, http_get=lambda url: None)[
            0
        ].object_id == ("guest-id")
        assert not any("rest" in call.argv for call in spawn.calls)
        assert directory.asked == [
            "id sam@fabrikam.com",
            "userPrincipalName eq 'sam@fabrikam.com'",
            "mail eq 'sam@fabrikam.com'",  # found by mail
        ]


_BATCH_FILE_AZ = AzLocation(command=("C:/az/az.cmd",), bin_dir=Path("/x"), source="path")
OBRIEN = {
    "id": "obrien-id",
    "displayName": "Pat O'Brien",
    "userType": "Member",
    "mail": "o'brien@contoso.com",
    "userPrincipalName": "pobrien@contoso.com",
    "otherMails": [],
}


class TestUnusualButValidAddresses:
    """An apostrophe is legal in an email address (o'brien@contoso.com), and so are "+" and
    "&". Refusing them refuses real people. What matters is that such a character can never
    change the meaning of the query: a quote is doubled inside the OData string, and the
    whole value is percent-encoded in the URL."""

    @pytest.mark.parametrize(
        "address",
        [
            "o'brien@contoso.com",
            "pat+teams@contoso.com",
            "r&d@contoso.com",
            "a.b_c-d@contoso.co.uk",
            "pat$ops@contoso.com",
            "{x}|~@contoso.com",
        ],
    )
    def test_valid_addresses_are_accepted(self, address: str) -> None:
        assert validate_sponsor(address) is None

    def test_an_apostrophe_is_found_by_mail_with_the_quote_doubled(self) -> None:
        az, directory, _ = _az_with([OBRIEN])
        sponsors = resolve_sponsors(["o'brien@contoso.com"], az, http_get=_no_http)
        assert sponsors[0].object_id == "obrien-id"
        assert sponsors[0].upn == "o'brien@contoso.com"
        # What Graph was asked, once decoded: one string literal, the quote doubled.
        assert directory.asked == [
            "id o'brien@contoso.com",
            "userPrincipalName eq 'o''brien@contoso.com'",
            "mail eq 'o''brien@contoso.com'",
        ]

    def test_two_people_with_an_apostrophe_address_are_listed_never_one_picked(self) -> None:
        twin = {
            **OBRIEN,
            "id": "twin-id",
            "displayName": "P. O'Brien (old)",
            "userPrincipalName": "pobrien.old@contoso.com",
        }
        az, _, _ = _az_with([OBRIEN, twin])
        with pytest.raises(SponsorError) as error:
            resolve_sponsors(["o'brien@contoso.com"], az, http_get=_no_http)
        message = str(error.value)
        assert "Pat O'Brien <pobrien@contoso.com>" in message
        assert "P. O'Brien (old) <pobrien.old@contoso.com>" in message
        assert "o'brien@contoso.com" in message and "object ID" in message

    def test_no_quote_reaches_the_url_unencoded(self) -> None:
        az, directory, _ = _az_with([OBRIEN])
        resolve_sponsors(["o'brien@contoso.com"], az, http_get=_no_http)
        assert directory.urls and not any("'" in url for url in directory.urls)
        assert "userPrincipalName%20eq%20%27o%27%27brien%40contoso.com%27" in directory.urls[1]
        assert "mail%20eq%20%27o%27%27brien%40contoso.com%27" in directory.urls[2]

    def test_an_apostrophe_cannot_end_the_string_and_add_a_condition(self) -> None:
        # The classic injection shape. It must be looked for as one odd address, and match
        # nobody, never be read as "... or anything".
        az, directory, _ = _az_with([OBRIEN, MEMBER])
        hostile = "x'or'1'eq'1@contoso.com"
        with pytest.raises(SponsorError, match="Could not find"):
            resolve_sponsors([hostile], az, http_get=_no_http)
        assert directory.asked[1:3] == [
            "userPrincipalName eq 'x''or''1''eq''1@contoso.com'",
            "mail eq 'x''or''1''eq''1@contoso.com'",
        ]

    @pytest.mark.parametrize("field", ["otherMails", "proxyAddresses"])
    def test_an_apostrophe_is_found_in_the_other_places_too(self, field: str) -> None:
        stored = "SMTP:o'brien@contoso.com" if field == "proxyAddresses" else "o'brien@contoso.com"
        user = {**OBRIEN, "mail": None, field: [stored]}
        az, _, _ = _az_with([user])
        assert resolve_sponsors(["o'brien@contoso.com"], az, http_get=_no_http)[0].object_id == (
            "obrien-id"
        )

    def test_an_apostrophe_is_found_in_an_external_sign_in_name(self) -> None:
        user = {
            **OBRIEN,
            "mail": None,
            "userPrincipalName": "o'brien_fabrikam.com#EXT#@contoso.onmicrosoft.com",
        }
        az, directory, _ = _az_with([user])
        sponsors = resolve_sponsors(["o'brien@fabrikam.com"], az, http_get=lambda url: None)
        assert sponsors[0].object_id == "obrien-id"
        assert directory.asked[-1] == "startswith(userPrincipalName,'o''brien_fabrikam.com')"

    def test_a_plus_sign_arrives_as_a_plus_not_as_a_space(self) -> None:
        user = {
            **MEMBER,
            "id": "plus-id",
            "mail": "pat+teams@contoso.com",
            "userPrincipalName": "pat2@contoso.com",
        }
        az, directory, _ = _az_with([user])
        assert resolve_sponsors(["pat+teams@contoso.com"], az, http_get=_no_http)[0].object_id == (
            "plus-id"
        )
        assert "mail eq 'pat+teams@contoso.com'" in directory.asked

    def test_a_batch_file_azure_cli_still_looks_an_apostrophe_up_by_mail(self) -> None:
        spawn = FakeSpawn()
        FakeDirectory(users=[dict(OBRIEN)]).attach_cli(spawn)
        console, _ = make_console(spawn=spawn)
        az = AzCli(_BATCH_FILE_AZ, console)
        sponsors = resolve_sponsors(["o'brien@contoso.com"], az, http_get=_no_http)
        assert sponsors[0].object_id == "obrien-id"
        filters = [
            call.argv[call.argv.index("--filter") + 1]
            for call in spawn.calls
            if words("ad", "user", "list")(call)
        ]
        assert filters == [
            "userPrincipalName eq 'o''brien@contoso.com'",
            "mail eq 'o''brien@contoso.com'",
        ]

    def test_a_batch_file_azure_cli_never_hands_cmd_a_character_it_would_act_on(self) -> None:
        # "&" is a valid address character and a command separator to cmd.exe. With az.cmd as
        # the only way in, the lookup is refused before anything runs, and reported.
        spawn = FakeSpawn()
        console, _ = make_console(spawn=spawn)
        az = AzCli(_BATCH_FILE_AZ, console)
        with pytest.raises(SponsorError, match="Could not find"):
            resolve_sponsors(["r&d@contoso.com"], az, http_get=_no_http)
        assert spawn.calls == []


# Signs in as o'brien@contoso.com; their mail is something else, so no email lookup finds them.
OBRIEN_SIGN_IN = {
    "id": "obrien-sign-in-id",
    "displayName": "Pat O'Brien",
    "userType": "Member",
    "mail": "pat.obrien@contoso.com",
    "userPrincipalName": "o'brien@contoso.com",
    "otherMails": [],
}
OBRIEN_TWIN = {
    **OBRIEN_SIGN_IN,
    "id": "obrien-twin-id",
    "displayName": "P. O'Brien (old)",
    "userPrincipalName": "O'Brien@contoso.com",
}
SEARCH_FOR_OBRIEN = "userPrincipalName eq 'o''brien@contoso.com'"


class TestAnApostropheInTheSignInName:
    """The defect: someone whose sign-in name has an apostrophe, and whose mail is different,
    was never found. Graph's /users/{sign-in name} lookup may not take the apostrophe, and no
    later step compared the sign-in name. A search for it (``userPrincipalName eq``, the quote
    doubled) now comes right after the direct lookup misses."""

    @staticmethod
    def _az(users: list[dict], *, path_takes_a_quote: bool = False) -> tuple[AzCli, FakeDirectory]:
        az, directory, _ = _az_with(users)
        directory.refused_in_path = "" if path_takes_a_quote else "'"
        return az, directory

    @staticmethod
    def _batch_file_az(users: list[dict]) -> tuple[AzCli, FakeDirectory, FakeSpawn]:
        spawn = FakeSpawn()
        directory = FakeDirectory(users=[dict(user) for user in users], refused_in_path="'")
        directory.attach_cli(spawn)
        console, _ = make_console(spawn=spawn)
        return AzCli(_BATCH_FILE_AZ, console), directory, spawn

    def test_they_are_found_by_a_search_for_the_sign_in_name(self) -> None:
        az, directory = self._az([OBRIEN_SIGN_IN, MEMBER])
        sponsors = resolve_sponsors(["o'brien@contoso.com"], az, http_get=_no_http)
        assert sponsors == [
            Sponsor(
                object_id="obrien-sign-in-id",
                upn="o'brien@contoso.com",
                mail="pat.obrien@contoso.com",
                user_type="Member",
                home_tenant_id="",
            )
        ]
        # Right after the direct lookup missed, and the first to find one person: it stops.
        assert directory.asked == ["id o'brien@contoso.com", SEARCH_FOR_OBRIEN]

    def test_the_url_for_that_search_doubles_the_quote_and_encodes_all_of_it(self) -> None:
        az, directory = self._az([OBRIEN_SIGN_IN])
        resolve_sponsors(["o'brien@contoso.com"], az, http_get=_no_http)
        url = directory.urls[1]
        assert url.startswith("https://graph.microsoft.com/v1.0/users?$filter=")
        value = url.split("$filter=", 1)[1].split("&", 1)[0]
        assert value == "userPrincipalName%20eq%20%27o%27%27brien%40contoso.com%27"

    def test_reserved_characters_never_reach_the_url_unencoded(self) -> None:
        # All legal in an address. Raw, "#" would end the request, "&" start a new
        # parameter, "+" arrive as a space and "%" start an escape.
        typed = "o'b#x+y&z%w@contoso.com"
        az, directory = self._az([OBRIEN_SIGN_IN])
        with pytest.raises(SponsorError, match="Could not find"):
            resolve_sponsors([typed], az, http_get=_no_http)
        search = "userPrincipalName eq 'o''b#x+y&z%w@contoso.com'"
        assert directory.asked[:2] == [f"id {typed}", search]  # arrived intact
        url = directory.urls[1]
        value = url.split("$filter=", 1)[1].split("&", 1)[0]
        assert value == quote(search, safe="")
        assert not any(character in value for character in "'#+& @")

    def test_two_people_found_by_that_search_are_listed_never_one_picked(self) -> None:
        # Sign-in names are unique, ignoring case. A directory that answered with two anyway
        # must not have one of them chosen for it.
        az, directory = self._az([OBRIEN_SIGN_IN, OBRIEN_TWIN])
        with pytest.raises(SponsorError) as error:
            resolve_sponsors(["o'brien@contoso.com"], az, http_get=_no_http)
        message = str(error.value)
        assert "Pat O'Brien <o'brien@contoso.com>" in message
        assert "P. O'Brien (old) <O'Brien@contoso.com>" in message
        assert "(by a search for their sign-in name)" in message and "object ID" in message
        assert directory.asked[-1] == SEARCH_FOR_OBRIEN  # nothing after it was tried

    def test_the_search_is_skipped_when_the_direct_lookup_finds_them(self) -> None:
        az, directory = self._az([OBRIEN_SIGN_IN], path_takes_a_quote=True)
        found = resolve_sponsors(["o'brien@contoso.com"], az, http_get=_no_http)
        assert found[0].object_id == "obrien-sign-in-id"
        assert directory.asked == ["id o'brien@contoso.com"]

    def test_an_object_id_is_never_searched_for_as_a_sign_in_name(self) -> None:
        az, directory = self._az([OBRIEN_SIGN_IN])
        missing = "00000000-0000-0000-0000-00000000beef"
        with pytest.raises(SponsorError, match="object ID"):
            resolve_sponsors([missing], az, http_get=_no_http)
        assert directory.asked == [f"id {missing}"]

    def test_nobody_found_names_the_search_among_what_was_tried(self) -> None:
        az, _ = self._az([MEMBER])
        with pytest.raises(SponsorError) as error:
            resolve_sponsors(["o'brien@contoso.com"], az, http_get=_no_http)
        assert (
            "Setup looked for a user by: their sign-in name; a search for their sign-in name; "
            "their primary email;" in str(error.value)
        )

    def test_a_batch_file_azure_cli_makes_the_same_search(self) -> None:
        az, directory, spawn = self._batch_file_az([OBRIEN_SIGN_IN])
        found = resolve_sponsors(["o'brien@contoso.com"], az, http_get=_no_http)
        assert found[0].object_id == "obrien-sign-in-id"
        assert directory.asked == ["id o'brien@contoso.com", SEARCH_FOR_OBRIEN]
        assert not any("rest" in call.argv for call in spawn.calls)

    def test_a_batch_file_azure_cli_lists_two_people_found_by_that_search(self) -> None:
        az, directory, _ = self._batch_file_az([OBRIEN_SIGN_IN, OBRIEN_TWIN])
        with pytest.raises(SponsorError) as error:
            resolve_sponsors(["o'brien@contoso.com"], az, http_get=_no_http)
        message = str(error.value)
        assert "<o'brien@contoso.com>" in message and "<O'Brien@contoso.com>" in message
        assert "(by a search for their sign-in name)" in message
        assert directory.asked[-1] == SEARCH_FOR_OBRIEN

    def test_a_batch_file_azure_cli_does_not_search_for_an_object_id(self) -> None:
        az, directory, _ = self._batch_file_az([OBRIEN_SIGN_IN])
        missing = "00000000-0000-0000-0000-00000000beef"
        with pytest.raises(SponsorError, match="object ID"):
            resolve_sponsors([missing], az, http_get=_no_http)
        assert directory.asked == [f"id {missing}"]


class TestSponsorEnv:
    def test_one_sponsor(self) -> None:
        sponsor = Sponsor("id-1", "pat@contoso.com", "pat@contoso.com", "Member", "")
        assert sponsor_env([sponsor]) == {
            "ENTRABOT_HUMAN_USER_ID": "id-1",
            "ENTRABOT_HUMAN_UPN": "pat@contoso.com",
            "ENTRABOT_HUMAN_USER_IDS": "id-1",
            "ENTRABOT_HUMAN_UPNS": "pat@contoso.com",
            "ENTRABOT_HUMAN_USER_TENANT_IDS": "",
            "ENTRABOT_HUMAN_USER_MAILS": "pat@contoso.com",
            "ENTRABOT_HUMAN_USER_TYPES": "Member",
        }

    def test_several_sponsors_are_index_aligned_and_the_first_is_primary(self) -> None:
        env = sponsor_env(
            [
                Sponsor("id-1", "pat@contoso.com", "pat@contoso.com", "Member", ""),
                Sponsor("id-2", "sam@fabrikam.com", "sam@fabrikam.com", "Guest", "home"),
            ]
        )
        assert env["ENTRABOT_HUMAN_USER_ID"] == "id-1"
        assert env["ENTRABOT_HUMAN_USER_IDS"] == "id-1,id-2"
        assert env["ENTRABOT_HUMAN_USER_TENANT_IDS"] == ",home"
        assert env["ENTRABOT_HUMAN_USER_TYPES"] == "Member,Guest"
        assert set(env) == set(SPONSOR_KEYS)
