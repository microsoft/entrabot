"""Identity mode: what happens to the state file, and what the provisioning helper is told.

setup.sh:757-882 is the reference. The Windows script used to pass these choices as command
line arguments that ``create_entra_agent_ids.py`` never reads, so ``-NewChain`` silently
attached to whichever agent already existed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from entrabot.winsetup.state import (
    IdentityChoice,
    IdentityError,
    apply_identity_choice,
    default_suffix,
    identity_environment,
    read_state,
    validate_app_id,
    validate_suffix,
    validate_upn,
)

NOW = datetime(2026, 11, 11, 9, 30, 15, tzinfo=UTC)
OLD_BP = "11111111-1111-1111-1111-111111111111"
NEW_BP = "22222222-2222-2222-2222-222222222222"
FULL_STATE = {
    "TENANT_ID": "tenant",
    "PROVISIONER_CLIENT_ID": "prov-app",
    "PROVISIONER_CERT_THUMBPRINT": "prov-thumb",
    "BLUEPRINT_APP_ID": OLD_BP,
    "BLUEPRINT_OBJECT_ID": "bp-object",
    "AGENT_ID": "agent",
    "AGENT_USER_ID": "agent-user",
    "AGENT_USER_UPN": "entrabot-agent@contoso.com",
}


def _state_file(tmp_path: Path, data: dict[str, str] | None = FULL_STATE) -> Path:
    path = tmp_path / ".entrabot-state.json"
    if data is not None:
        path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestReadState:
    def test_a_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert read_state(tmp_path / "nope.json") == {}

    def test_a_byte_order_mark_is_tolerated(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"AGENT_ID": "a"}).encode())
        assert read_state(path) == {"AGENT_ID": "a"}

    def test_a_corrupt_file_is_an_error_not_an_empty_state(self, tmp_path: Path) -> None:
        # Treating it as empty would send a repair down the "brand new agent" path.
        path = tmp_path / "state.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(IdentityError):
            read_state(path)


class TestNewChain:
    def test_state_is_backed_up_and_only_the_provisioner_is_kept(self, tmp_path: Path) -> None:
        path = _state_file(tmp_path)
        notes = apply_identity_choice(path, IdentityChoice(mode="new", upn_suffix="bob"), now=NOW)
        assert read_state(path) == {
            "PROVISIONER_CLIENT_ID": "prov-app",
            "PROVISIONER_CERT_THUMBPRINT": "prov-thumb",
        }
        backup = tmp_path / ".entrabot-state.json.bak.20261111-093015"
        assert json.loads(backup.read_text(encoding="utf-8")) == FULL_STATE
        assert any("backed up" in note.lower() for note in notes)

    def test_no_state_file_means_nothing_to_back_up(self, tmp_path: Path) -> None:
        path = _state_file(tmp_path, None)
        apply_identity_choice(path, IdentityChoice(mode="new", upn_suffix="bob"), now=NOW)
        assert not path.exists()
        assert list(tmp_path.iterdir()) == []

    def test_the_helper_is_told_to_create_a_fresh_chain_with_the_suffix(self) -> None:
        env = identity_environment(IdentityChoice(mode="new", upn_suffix="bob"), base={})
        assert env["ENTRABOT_NEW_CHAIN"] == "1"
        assert env["_ENTRABOT_UPN_SUFFIX"] == "bob"
        # An empty value still counts as "set", so the helper's own .env load can't bring back
        # the previous agent's UPN.
        assert env["ENTRABOT_AGENT_USER_UPN"] == ""

    def test_an_explicit_upn_beats_the_suffix(self) -> None:
        choice = IdentityChoice(mode="new", upn_suffix="bob", agent_user_upn="bot@contoso.com")
        env = identity_environment(choice, base={})
        assert env["ENTRABOT_NEW_CHAIN"] == "1"
        assert env["ENTRABOT_AGENT_USER_UPN"] == "bot@contoso.com"
        assert "_ENTRABOT_UPN_SUFFIX" not in env

    def test_a_new_chain_needs_a_suffix_or_a_upn(self) -> None:
        with pytest.raises(IdentityError):
            identity_environment(IdentityChoice(mode="new"), base={})


class TestJoinBlueprint:
    def test_a_fresh_machine_just_records_the_blueprint(self, tmp_path: Path) -> None:
        path = _state_file(tmp_path, None)
        apply_identity_choice(path, IdentityChoice(mode="join", blueprint_app_id=NEW_BP), now=NOW)
        assert read_state(path) == {"BLUEPRINT_APP_ID": NEW_BP}

    def test_the_same_blueprint_keeps_everything(self, tmp_path: Path) -> None:
        path = _state_file(tmp_path)
        apply_identity_choice(path, IdentityChoice(mode="join", blueprint_app_id=OLD_BP), now=NOW)
        assert read_state(path) == FULL_STATE
        assert [item.name for item in tmp_path.iterdir()] == [".entrabot-state.json"]

    def test_a_different_blueprint_is_a_switch(self, tmp_path: Path) -> None:
        # The helper prefers the cached BLUEPRINT_OBJECT_ID, so leaving it would silently pin
        # setup back to the old Blueprint.
        path = _state_file(tmp_path)
        notes = apply_identity_choice(
            path, IdentityChoice(mode="join", blueprint_app_id=NEW_BP), now=NOW
        )
        assert read_state(path) == {
            "TENANT_ID": "tenant",
            "PROVISIONER_CLIENT_ID": "prov-app",
            "PROVISIONER_CERT_THUMBPRINT": "prov-thumb",
            "BLUEPRINT_APP_ID": NEW_BP,
        }
        assert (tmp_path / ".entrabot-state.json.bak.20261111-093015").is_file()
        assert any("not revoked" in note.lower() for note in notes)

    def test_the_helper_is_not_told_to_create_a_new_chain(self) -> None:
        choice = IdentityChoice(mode="join", blueprint_app_id=NEW_BP)
        env = identity_environment(choice, base={"ENTRABOT_NEW_CHAIN": "1"})
        assert "ENTRABOT_NEW_CHAIN" not in env

    def test_an_agent_user_upn_is_passed_through(self) -> None:
        choice = IdentityChoice(mode="join", blueprint_app_id=NEW_BP, agent_user_upn="a@b.com")
        env = identity_environment(choice, base={})
        assert env["ENTRABOT_AGENT_USER_UPN"] == "a@b.com"
        assert "_ENTRABOT_UPN_SUFFIX" not in env

    def test_a_suffix_selects_an_existing_suffixed_agent_user(self) -> None:
        choice = IdentityChoice(mode="join", blueprint_app_id=NEW_BP, upn_suffix="sati")
        env = identity_environment(choice, base={})
        assert env["_ENTRABOT_UPN_SUFFIX"] == "sati"

    def test_a_switch_does_not_inherit_the_old_agents_upn(self) -> None:
        choice = IdentityChoice(mode="join", blueprint_app_id=NEW_BP, switching=True)
        assert identity_environment(choice, base={})["ENTRABOT_AGENT_USER_UPN"] == ""

    def test_join_needs_an_app_id(self, tmp_path: Path) -> None:
        with pytest.raises(IdentityError):
            apply_identity_choice(_state_file(tmp_path), IdentityChoice(mode="join"), now=NOW)


class TestContinue:
    def test_nothing_in_the_state_file_changes(self, tmp_path: Path) -> None:
        path = _state_file(tmp_path)
        before = path.read_bytes()
        apply_identity_choice(path, IdentityChoice(mode="continue"), now=NOW)
        assert path.read_bytes() == before

    def test_the_helper_gets_no_identity_overrides(self) -> None:
        # .env still names the agent, and the helper reads it for itself.
        env = identity_environment(IdentityChoice(mode="continue"), base={"PATH": "/bin"})
        assert env == {"PATH": "/bin"}

    def test_continue_without_an_existing_agent_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(IdentityError):
            apply_identity_choice(
                _state_file(tmp_path, None), IdentityChoice(mode="continue"), now=NOW
            )


def test_choices_left_in_the_calling_shell_never_leak_into_this_run() -> None:
    # Run directly, an earlier setup leaves these in the session; a stale suffix would beat
    # this run's explicit UPN, because the helper lets a suffix outrank a UPN.
    stale = {
        "ENTRABOT_NEW_CHAIN": "1",
        "_ENTRABOT_UPN_SUFFIX": "old",
        "ENTRABOT_AGENT_USER_UPN": "old@contoso.com",
        "PATH": "/bin",
    }
    choice = IdentityChoice(mode="join", blueprint_app_id=NEW_BP, agent_user_upn="new@contoso.com")
    assert identity_environment(choice, base=stale) == {
        "PATH": "/bin",
        "ENTRABOT_AGENT_USER_UPN": "new@contoso.com",
    }
    assert identity_environment(IdentityChoice(mode="continue"), base=stale) == {"PATH": "/bin"}


class TestValidation:
    @pytest.mark.parametrize("value", ["bob", "sati-agent", "dev2", "a"])
    def test_good_suffixes(self, value: str) -> None:
        assert validate_suffix(value) is None

    @pytest.mark.parametrize("value", ["", "Bob", "has space", "-lead", "trail-", "a@b", "x" * 40])
    def test_bad_suffixes(self, value: str) -> None:
        assert validate_suffix(value)

    def test_app_id_must_be_a_guid(self) -> None:
        assert validate_app_id(NEW_BP) is None
        assert validate_app_id("not-a-guid")
        assert validate_app_id(NEW_BP + "&calc")

    def test_upn_shape(self) -> None:
        assert validate_upn("entrabot-agent@contoso.onmicrosoft.com") is None
        assert validate_upn("nope")
        assert validate_upn("a b@c.com")

    @pytest.mark.parametrize(
        ("username", "expected"),
        [("Brandon", "brandon"), ("josé.garcía", "jos-garc-a"), ("  ", "agent"), ("A_B", "a-b")],
    )
    def test_a_default_suffix_is_derived_from_the_username(
        self, username: str, expected: str
    ) -> None:
        suffix = default_suffix(username)
        assert suffix == expected
        assert validate_suffix(suffix) is None
