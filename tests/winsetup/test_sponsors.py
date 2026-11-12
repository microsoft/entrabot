"""Who the agent answers to: resolving sponsors, including B2B guests (setup.sh:644-734)."""

from __future__ import annotations

import json
from pathlib import Path

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
from tests.winsetup.fakes import FakeDirectory, FakeSpawn, Reply, make_console, words

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
    "displayName": "Brandon Werner",
    "userType": "Member",
    "mail": None,
    "userPrincipalName": "brandwe_outlook.com#EXT#@werner.onmicrosoft.com",
    "otherMails": ["brandwe@outlook.com"],
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
        assert directory.asked == ["id sam@fabrikam.com", "mail eq 'sam@fabrikam.com'"]

    def test_an_unknown_user_is_an_error_saying_what_was_tried(self) -> None:
        az, _, _ = _az_with([MEMBER])
        with pytest.raises(SponsorError) as error:
            resolve_sponsors(["nobody@contoso.com"], az, http_get=_no_http)
        message = str(error.value)
        assert "nobody@contoso.com" in message
        for tried in ("sign-in name", "primary email", "other email", "aliases", "guest"):
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

    In the directory that person is ``brandwe_outlook.com#EXT#@<tenant>.onmicrosoft.com``, a
    Member, with no ``mail``; the address they are known by lives in ``otherMails``, the email
    aliases, or only in that sign-in name. Looking up the address as a sign-in name and as
    ``mail`` found nobody, and setup refused a sponsor who was working on another machine.
    """

    def test_the_address_is_found_among_other_mails(self) -> None:
        az, directory, _ = _az_with([OWNER])
        sponsors = resolve_sponsors(["brandwe@outlook.com"], az, http_get=_no_http)
        assert sponsors == [
            Sponsor(
                object_id="owner-id",
                upn="brandwe@outlook.com",  # what was typed, as the other machine recorded it
                mail="brandwe@outlook.com",
                user_type="Member",  # what the directory says, although the name has #EXT#
                home_tenant_id="",
            )
        ]
        assert directory.asked == [
            "id brandwe@outlook.com",
            "mail eq 'brandwe@outlook.com'",
            "otherMails/any(x:x eq 'brandwe@outlook.com')",
        ]

    @pytest.mark.parametrize("stored", ["SMTP:brandwe@outlook.com", "smtp:brandwe@outlook.com"])
    def test_the_address_is_found_among_email_aliases_in_either_spelling(self, stored: str) -> None:
        owner = {**OWNER, "otherMails": [], "proxyAddresses": [stored]}
        az, directory, _ = _az_with([owner])
        sponsors = resolve_sponsors(["brandwe@outlook.com"], az, http_get=_no_http)
        assert sponsors[0].object_id == "owner-id"
        assert "proxyAddresses/any(x:x eq 'SMTP:brandwe@outlook.com')" in directory.asked

    def test_the_address_is_found_from_the_external_sign_in_name_alone(self) -> None:
        owner = {**OWNER, "otherMails": []}
        az, directory, _ = _az_with([owner])
        sponsors = resolve_sponsors(["brandwe@outlook.com"], az, http_get=_no_http)
        assert sponsors[0].object_id == "owner-id" and sponsors[0].user_type == "Member"
        # "@" becomes "_" in an external sign-in name.
        assert directory.asked[-1] == "startswith(userPrincipalName,'brandwe_outlook.com')"

    def test_a_longer_name_that_merely_starts_the_same_is_not_a_match(self) -> None:
        # brandwe_outlook.com.au#EXT#... is somebody else.
        lookalike = {
            **OWNER,
            "id": "other-id",
            "otherMails": [],
            "userPrincipalName": "brandwe_outlook.com.au#EXT#@werner.onmicrosoft.com",
        }
        az, _, _ = _az_with([lookalike])
        with pytest.raises(SponsorError, match="Could not find"):
            resolve_sponsors(["brandwe@outlook.com"], az, http_get=_no_http)

    def test_no_hash_ever_reaches_the_url_unencoded(self) -> None:
        # A raw "#" starts a URL fragment: everything after it would silently not be sent.
        az, directory, _ = _az_with([OWNER])
        resolve_sponsors([OWNER["userPrincipalName"]], az, http_get=_no_http)  # type: ignore[list-item]
        resolve_sponsors(["brandwe@outlook.com"], az, http_get=_no_http)
        assert directory.urls and not any("#" in url for url in directory.urls)
        assert directory.asked[0] == "id brandwe_outlook.com#ext#@werner.onmicrosoft.com"

    def test_the_external_sign_in_name_itself_can_be_given(self) -> None:
        az, _, _ = _az_with([OWNER])
        typed = "brandwe_outlook.com#EXT#@werner.onmicrosoft.com"
        assert resolve_sponsors([typed], az, http_get=_no_http)[0].object_id == "owner-id"

    def test_an_object_id_is_accepted_and_recorded_under_the_persons_address(self) -> None:
        az, directory, _ = _az_with([{**OWNER, "id": OWNER_ID}])
        sponsors = resolve_sponsors([OWNER_ID], az, http_get=_no_http)
        assert sponsors[0].object_id == OWNER_ID
        assert sponsors[0].upn == "brandwe@outlook.com"  # an address, not a GUID, in .env
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
            "displayName": "Brandon (old account)",
            "userPrincipalName": "brandon.old@werner.ac",
        }
        az, _, _ = _az_with([OWNER, twin])
        with pytest.raises(SponsorError) as error:
            resolve_sponsors(["brandwe@outlook.com"], az, http_get=_no_http)
        message = str(error.value)
        assert "Brandon Werner" in message and "Brandon (old account)" in message
        assert "brandwe_outlook.com#EXT#@werner.onmicrosoft.com" in message
        assert "brandon.old@werner.ac" in message
        assert "object ID" in message

    def test_a_lookup_graph_rejects_is_skipped_not_fatal(self) -> None:
        owner = {**OWNER, "otherMails": []}
        az, directory, _ = _az_with([owner])
        directory.fail_filters = ("proxyAddresses",)
        assert resolve_sponsors(["brandwe@outlook.com"], az, http_get=_no_http)[0].object_id == (
            "owner-id"
        )

    def test_only_the_fields_setup_uses_are_requested(self) -> None:
        # Less of the directory in the setup log.
        az, directory, _ = _az_with([OWNER])
        resolve_sponsors(["brandwe@outlook.com"], az, http_get=_no_http)
        assert all("%24select=" in url or "$select=" in url for url in directory.urls)
        assert all("proxyAddresses" not in url.split("select=")[1] for url in directory.urls)

    def test_every_lookup_asks_for_json(self) -> None:
        az, _, spawn = _az_with([OWNER])
        resolve_sponsors(["brandwe@outlook.com"], az, http_get=_no_http)
        assert spawn.calls and all(call.argv[-2:] == ["--output", "json"] for call in spawn.calls)

    def test_a_batch_file_azure_cli_still_finds_members_and_mail(self) -> None:
        # With az.cmd as the only way in, cmd.exe would reinterpret an encoded URL. The two
        # lookups that need no special characters still run; the rest are named as skipped.
        spawn = FakeSpawn()
        spawn.on(words("ad", "user", "show"), Reply(returncode=3, stderr="does not exist"))
        spawn.on(words("ad", "user", "list"), Reply(stdout=json.dumps([GUEST])))
        console, _ = make_console(spawn=spawn)
        az = AzCli(
            AzLocation(command=("C:/az/az.cmd",), bin_dir=Path("/x"), source="path"), console
        )
        assert resolve_sponsors(["sam@fabrikam.com"], az, http_get=lambda url: None)[
            0
        ].object_id == ("guest-id")
        assert not any("rest" in call.argv for call in spawn.calls)


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
        assert directory.asked == ["id o'brien@contoso.com", "mail eq 'o''brien@contoso.com'"]

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
        assert "mail%20eq%20%27o%27%27brien%40contoso.com%27" in directory.urls[1]

    def test_an_apostrophe_cannot_end_the_string_and_add_a_condition(self) -> None:
        # The classic injection shape. It must be looked for as one odd address, and match
        # nobody, never be read as "... or anything".
        az, directory, _ = _az_with([OBRIEN, MEMBER])
        hostile = "x'or'1'eq'1@contoso.com"
        with pytest.raises(SponsorError, match="Could not find"):
            resolve_sponsors([hostile], az, http_get=_no_http)
        assert directory.asked[1] == "mail eq 'x''or''1''eq''1@contoso.com'"

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
        spawn.on(words("ad", "user", "show"), Reply(returncode=3, stderr="does not exist"))
        spawn.on(words("ad", "user", "list"), Reply(stdout=json.dumps([OBRIEN])))
        console, _ = make_console(spawn=spawn)
        az = AzCli(_BATCH_FILE_AZ, console)
        resolve_sponsors(["o'brien@contoso.com"], az, http_get=_no_http)
        listed = next(call.argv for call in spawn.calls if words("ad", "user", "list")(call))
        assert listed[listed.index("--filter") + 1] == "mail eq 'o''brien@contoso.com'"

    def test_a_batch_file_azure_cli_never_hands_cmd_a_character_it_would_act_on(self) -> None:
        # "&" is a valid address character and a command separator to cmd.exe. With az.cmd as
        # the only way in, the lookup is refused before anything runs, and reported.
        spawn = FakeSpawn()
        console, _ = make_console(spawn=spawn)
        az = AzCli(_BATCH_FILE_AZ, console)
        with pytest.raises(SponsorError, match="Could not find"):
            resolve_sponsors(["r&d@contoso.com"], az, http_get=_no_http)
        assert spawn.calls == []


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
