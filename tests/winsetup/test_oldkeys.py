"""The record of older setup keys, read the same way by the wizard, the health check
(``entrabot.preflight``) and the status script (``scripts/show_agent_status.py``).

An older setup kept the Provisioner's private key in a plain file. When setup could not take
that key's certificate off the Provisioner app, it recorded the certificate in
``.entrabot-state.json``: PROVISIONER_CERT_REVOKE_PENDING (lowercase SHA-1 thumbprints,
comma-separated) and PROVISIONER_CERT_REVOKE_PENDING_EXPIRES (their expiry dates, in the same
order). How the wizard words it is covered in tests/winsetup/test_wizard.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from entrabot.winsetup.oldkeys import (
    CHECK_NAME,
    ENTRA_ADMIN_CENTER,
    PROVISIONER_APP_NAME,
    REVOKE_PENDING,
    REVOKE_PENDING_EXPIRES,
    OldSetupKey,
    OldSetupKeys,
    listed,
    old_setup_key_check,
    old_setup_key_waiting,
    old_setup_keys,
    read_old_setup_keys,
)

OLD_KEY_SHA1 = "c3f1a2b4d5e6f708192a3b4c5d6e7f8091a2b3c4"
SECOND_OLD_KEY_SHA1 = "0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b9e8d"
THIS_COMPUTERS_KEY_SHA1 = "77aa88bb99cc00dd11ee22ff33aa44bb55cc66dd"
WHERE = (
    "App registrations > 'EntraBot Agent ID Provisioner' (application ID prov-app) > "
    "Certificates & secrets > Certificates"
)


def _state(tmp_path: Path, values: object) -> Path:
    path = tmp_path / ".entrabot-state.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


def _one_key(**extra: str) -> OldSetupKeys:
    return old_setup_keys(
        {
            REVOKE_PENDING: OLD_KEY_SHA1,
            REVOKE_PENDING_EXPIRES: "2027-02-01",
            "PROVISIONER_CLIENT_ID": "prov-app",
            "PROVISIONER_CERT_THUMBPRINT": THIS_COMPUTERS_KEY_SHA1,
            **extra,
        }
    )


def test_the_record_keys_are_the_ones_the_provisioning_script_writes() -> None:
    assert REVOKE_PENDING == "PROVISIONER_CERT_REVOKE_PENDING"
    assert REVOKE_PENDING_EXPIRES == "PROVISIONER_CERT_REVOKE_PENDING_EXPIRES"
    assert PROVISIONER_APP_NAME == "EntraBot Agent ID Provisioner"
    assert ENTRA_ADMIN_CENTER == "https://entra.microsoft.com"
    assert CHECK_NAME == "Older setup keys"


@pytest.mark.parametrize(
    ("items", "expected"),
    [(["A"], "A"), (["A", "B"], "A and B"), (["A", "B", "C"], "A, B and C")],
)
def test_listed(items: list[str], expected: str) -> None:
    assert listed(items) == expected


class TestReadingTheStateFile:
    """The state file as each tool finds it, including the ways it can be wrong."""

    def test_no_state_file_is_nothing_pending(self, tmp_path: Path) -> None:
        note = read_old_setup_keys(tmp_path / "missing.json")
        assert note == OldSetupKeys() and not note.pending

    def test_a_pending_key_is_read_from_the_file(self, tmp_path: Path) -> None:
        path = _state(
            tmp_path,
            {
                REVOKE_PENDING: OLD_KEY_SHA1,
                REVOKE_PENDING_EXPIRES: "2027-02-01",
                "PROVISIONER_CLIENT_ID": "prov-app",
                "PROVISIONER_CERT_THUMBPRINT": THIS_COMPUTERS_KEY_SHA1,
            },
        )
        note = read_old_setup_keys(path)
        assert note.keys == (OldSetupKey(ending="B3C4", expires="2027-02-01"),)
        assert note.app_id == "prov-app" and note.current == "66DD"
        assert not note.unreadable

    def test_a_byte_order_mark_from_a_windows_editor_is_fine(self, tmp_path: Path) -> None:
        path = tmp_path / ".entrabot-state.json"
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps({REVOKE_PENDING: OLD_KEY_SHA1}).encode())
        assert read_old_setup_keys(path).keys == (OldSetupKey("B3C4", ""),)

    @pytest.mark.parametrize("content", ["{half written", "[1, 2]", '"text"', ""])
    def test_a_file_that_cannot_be_read_is_an_unreadable_record(
        self, tmp_path: Path, content: str
    ) -> None:
        # Whatever it said can't be known, so it may still name a key Entra trusts.
        path = tmp_path / ".entrabot-state.json"
        path.write_text(content, encoding="utf-8")
        note = read_old_setup_keys(path)
        assert note.pending and note.unreadable and note.keys == ()

    def test_a_value_that_is_not_text_is_read_as_text(self, tmp_path: Path) -> None:
        # A hand-edited file can hold a list where setup writes text: not a thumbprint.
        note = read_old_setup_keys(_state(tmp_path, {REVOKE_PENDING: [OLD_KEY_SHA1]}))
        assert note.unreadable and note.keys == ()

    def test_values_that_are_missing_as_a_key_lookup_returns_them_are_nothing(self) -> None:
        # scripts/show_agent_status.py reads each key with get_state, which returns None.
        note = old_setup_keys(
            {
                REVOKE_PENDING: None,
                REVOKE_PENDING_EXPIRES: None,
                "PROVISIONER_CLIENT_ID": None,
                "PROVISIONER_CERT_THUMBPRINT": None,
            }
        )
        assert note == OldSetupKeys() and not note.pending


class TestHealthCheckWording:
    """One line for what is wrong and one for what to do, for a report that prints each
    check on a line (diagnose) or two (status)."""

    def test_nothing_pending_is_nothing_to_report(self) -> None:
        assert old_setup_key_check(OldSetupKeys()) is None
        assert old_setup_key_check(old_setup_keys({REVOKE_PENDING: " , "})) is None

    def test_one_key_is_named_with_its_expiry_and_where_to_remove_it(self) -> None:
        finding = old_setup_key_check(_one_key())
        assert finding is not None
        assert finding.detail == (
            "An older setup key for this computer could not be switched off in Microsoft "
            "Entra: the certificate whose thumbprint ends in B3C4 (expires 2027-02-01). An "
            "earlier version of setup kept its key in an unprotected file."
        )
        assert finding.fix == (
            f"Remove it by hand in the Microsoft Entra admin center ({ENTRA_ADMIN_CENTER}): "
            f"{WHERE}. Setup will try again the next time it runs."
        )

    def test_two_keys_are_both_named(self) -> None:
        finding = old_setup_key_check(
            old_setup_keys(
                {
                    REVOKE_PENDING: f"{OLD_KEY_SHA1},{SECOND_OLD_KEY_SHA1}",
                    REVOKE_PENDING_EXPIRES: "2027-02-01,",
                }
            )
        )
        assert finding is not None
        assert finding.detail.startswith(
            "2 older setup keys for this computer could not be switched off in Microsoft Entra"
        )
        assert (
            "thumbprints end in B3C4 (expires 2027-02-01) and 9E8D (expiry date unknown)."
            in finding.detail
        )
        assert finding.fix.startswith("Remove them by hand in the Microsoft Entra admin center")
        assert "App registrations > 'EntraBot Agent ID Provisioner' >" in finding.fix
        assert finding.fix.endswith("Setup will try again the next time it runs.")

    def test_an_unreadable_record_says_it_could_not_be_read(self) -> None:
        note = old_setup_keys(
            {
                REVOKE_PENDING: "not-a-thumbprint",
                "PROVISIONER_CLIENT_ID": "prov-app",
                "PROVISIONER_CERT_THUMBPRINT": THIS_COMPUTERS_KEY_SHA1,
            }
        )
        finding = old_setup_key_check(note)
        assert finding is not None
        assert finding.detail.startswith(
            "Setup could not read its record of older setup keys for this computer."
        )
        assert f"({ENTRA_ADMIN_CENTER}): {WHERE}" in finding.fix
        assert "remove any certificate there that you can't account for" in finding.fix
        assert finding.fix.endswith(
            "Keep the one whose thumbprint ends in 66DD: this computer uses it now."
        )
        assert "try again" not in finding.fix  # it can't: it does not know which certificate

    def test_an_unreadable_file_names_no_app_it_does_not_know(self) -> None:
        finding = old_setup_key_check(OldSetupKeys(unreadable=True))
        assert finding is not None
        assert "App registrations > 'EntraBot Agent ID Provisioner' > Certificates" in finding.fix
        assert "application ID" not in finding.fix and "Keep the one" not in finding.fix

    def test_a_readable_key_beside_an_unreadable_entry_is_still_named(self) -> None:
        finding = old_setup_key_check(_one_key(**{REVOKE_PENDING: f"junk,{OLD_KEY_SHA1}"}))
        assert finding is not None
        assert "ends in B3C4 (expires" not in finding.detail  # the expiry is in position 0
        assert "ends in B3C4 (expiry date unknown)" in finding.detail
        assert "Setup's record also has an entry it could not read." in finding.detail
        assert "Also remove any other certificate there that you can't account for." in finding.fix
        assert "Keep the one whose thumbprint ends in 66DD" in finding.fix
        assert finding.fix.endswith("Setup will try again the next time it runs.")

    def test_each_part_is_one_line(self) -> None:
        for note in (_one_key(), OldSetupKeys(unreadable=True)):
            finding = old_setup_key_check(note)
            assert finding is not None and "\n" not in finding.detail + finding.fix


class TestWaitingLine:
    """What ``status`` says about a folder that only holds setup's note about an older key."""

    def test_nothing_pending_says_nothing(self) -> None:
        assert old_setup_key_waiting(OldSetupKeys()) == ""

    def test_one_key(self) -> None:
        assert old_setup_key_waiting(_one_key()) == (
            "An older setup key is still waiting to be switched off in Microsoft Entra: the "
            "certificate whose thumbprint ends in B3C4 (expires 2027-02-01). Setup will try "
            "again the next time it runs here."
        )

    def test_two_keys(self) -> None:
        line = old_setup_key_waiting(
            old_setup_keys({REVOKE_PENDING: f"{OLD_KEY_SHA1},{SECOND_OLD_KEY_SHA1}"})
        )
        assert line.startswith("2 older setup keys are still waiting to be switched off")
        assert "end in B3C4 (expiry date unknown) and 9E8D (expiry date unknown)" in line
        assert line.endswith("Setup will try again the next time it runs here.")

    def test_a_record_that_cannot_be_read_says_where_to_look(self) -> None:
        line = old_setup_key_waiting(old_setup_keys({REVOKE_PENDING: "junk"}))
        assert line.startswith("Setup could not read its record of an older setup key here")
        assert ENTRA_ADMIN_CENTER in line and "try again" not in line
        assert "\n" not in line


def test_no_wording_ever_holds_a_full_thumbprint() -> None:
    notes = [
        _one_key(),
        _one_key(**{REVOKE_PENDING: f"junk,{OLD_KEY_SHA1},{SECOND_OLD_KEY_SHA1}"}),
        old_setup_keys(
            {REVOKE_PENDING: "junk", "PROVISIONER_CERT_THUMBPRINT": THIS_COMPUTERS_KEY_SHA1}
        ),
    ]
    for note in notes:
        finding = old_setup_key_check(note)
        assert finding is not None
        text = (repr(note) + finding.detail + finding.fix + old_setup_key_waiting(note)).lower()
        for full in (OLD_KEY_SHA1, SECOND_OLD_KEY_SHA1, THIS_COMPUTERS_KEY_SHA1):
            assert full not in text
            assert full[:36] not in text  # nor most of one
