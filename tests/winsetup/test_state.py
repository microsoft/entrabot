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
    certificate_recorded_on,
    default_suffix,
    identity_environment,
    not_reusing_env_agent_user,
    read_state,
    undo_join,
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


OTHER_BP = "33333333-3333-3333-3333-333333333333"
BACKED_UP = (
    "Switching agents. Backed up the previous record to .entrabot-state.json.bak.20261111-093015."
)
NOT_REVOKED = (
    "This machine's certificate on the previous Blueprint is NOT revoked. Ask an administrator "
    "to remove it if this machine should lose access to that agent."
)
# What .env holds once setup has registered this computer's certificate on OLD_BP.
CERT_ENV = {
    "ENTRABOT_BLUEPRINT_APP_ID": OLD_BP,
    "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT": "n" * 43,
    "ENTRABOT_BLUEPRINT_CERT_SHA1": "A" * 40,
}


class TestTheSwitchWarnsOnlyAboutACertificateThatIsRecorded:
    """Seen on a real machine: a run had recorded a Blueprint App ID that did not exist, and
    stopped, as it should. The next run, with the right App ID, was a switch, and said this
    machine's certificate on the previous Blueprint was not revoked. No certificate was ever
    made for that Blueprint."""

    @staticmethod
    def _switch(
        tmp_path: Path,
        env_values: dict[str, str] | None,
        state: dict[str, str] = FULL_STATE,
    ) -> list[str]:
        return apply_identity_choice(
            _state_file(tmp_path, state),
            IdentityChoice(mode="join", blueprint_app_id=NEW_BP),
            now=NOW,
            env_values=env_values,
        )

    def test_a_certificate_env_records_for_the_previous_blueprint_is_warned_about(
        self, tmp_path: Path
    ) -> None:
        assert self._switch(tmp_path, CERT_ENV) == [BACKED_UP, NOT_REVOKED]

    @pytest.mark.parametrize(
        "only", ["ENTRABOT_BLUEPRINT_CERT_SHA1", "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT"]
    )
    def test_either_thumbprint_is_a_certificate(self, tmp_path: Path, only: str) -> None:
        env = {"ENTRABOT_BLUEPRINT_APP_ID": OLD_BP, only: CERT_ENV[only]}
        assert self._switch(tmp_path, env) == [BACKED_UP, NOT_REVOKED]

    def test_the_blueprint_in_other_letter_case_is_the_same_blueprint(self, tmp_path: Path) -> None:
        state = {**FULL_STATE, "BLUEPRINT_APP_ID": "abcdef01-2345-4678-9abc-def012345678"}
        env = {**CERT_ENV, "ENTRABOT_BLUEPRINT_APP_ID": "ABCDEF01-2345-4678-9ABC-DEF012345678"}
        assert self._switch(tmp_path, env, state) == [BACKED_UP, NOT_REVOKED]

    def test_a_certificate_env_names_no_blueprint_for_could_be_on_this_one(
        self, tmp_path: Path
    ) -> None:
        # Nothing ties it to another Blueprint, so it is not ruled out.
        env = {"ENTRABOT_BLUEPRINT_CERT_SHA1": "A" * 40}
        assert self._switch(tmp_path, env) == [BACKED_UP, NOT_REVOKED]

    def test_a_certificate_the_state_file_records_is_warned_about(self, tmp_path: Path) -> None:
        # scripts/register_blueprint_cert.py (setup.sh) records it there once it is registered.
        state = {**FULL_STATE, "BLUEPRINT_CERT_THUMBPRINT": "C" * 40}
        assert self._switch(tmp_path, {}, state) == [BACKED_UP, NOT_REVOKED]

    @pytest.mark.parametrize(
        "env",
        [
            {},
            {"ENTRABOT_BLUEPRINT_APP_ID": OLD_BP},
            {"ENTRABOT_BLUEPRINT_APP_ID": OLD_BP, "ENTRABOT_BLUEPRINT_CERT_SHA1": "   "},
            {**CERT_ENV, "ENTRABOT_BLUEPRINT_APP_ID": OTHER_BP},
        ],
        ids=["nothing-saved", "no-certificate", "blank", "another-blueprints-certificate"],
    )
    def test_without_one_it_only_says_the_record_was_backed_up(
        self, tmp_path: Path, env: dict[str, str]
    ) -> None:
        assert self._switch(tmp_path, env) == [BACKED_UP]
        assert (tmp_path / ".entrabot-state.json.bak.20261111-093015").is_file()

    def test_a_caller_that_cannot_say_what_env_records_still_gets_the_warning(
        self, tmp_path: Path
    ) -> None:
        assert self._switch(tmp_path, None) == [BACKED_UP, NOT_REVOKED]

    def test_the_rule_on_its_own(self) -> None:
        assert certificate_recorded_on(OLD_BP, state=FULL_STATE, env_values=CERT_ENV)
        assert not certificate_recorded_on(NEW_BP, state=FULL_STATE, env_values=CERT_ENV)
        assert not certificate_recorded_on(OLD_BP, state=FULL_STATE, env_values={})
        # A state file's certificate belongs to the Blueprint that record names.
        recorded = {**FULL_STATE, "BLUEPRINT_CERT_THUMBPRINT": "C" * 40}
        assert certificate_recorded_on(OLD_BP, state=recorded, env_values={})
        assert not certificate_recorded_on(NEW_BP, state=recorded, env_values={})


class TestUndoJoin:
    """A run that joins a Blueprint App ID records it before the agent step looks it up. When
    that Blueprint does not exist, the record goes back to what it was, so the next run is not
    a switch away from an agent that was never there. What the provisioning step recorded in
    the same run (the Provisioner app, this computer's setup key, an old key still to switch
    off, the tenant) is kept as it is now."""

    @staticmethod
    def _join(path: Path, app_id: str = NEW_BP) -> tuple[dict[str, str], dict[str, str]]:
        """The state before the join, and as the join left it."""
        before = read_state(path)
        apply_identity_choice(
            path, IdentityChoice(mode="join", blueprint_app_id=app_id), now=NOW, env_values={}
        )
        return before, read_state(path)

    @staticmethod
    def _provisioning_writes(path: Path, **values: str) -> dict[str, str]:
        written = {**read_state(path), **values}
        path.write_text(json.dumps(written), encoding="utf-8")
        return written

    def test_a_fresh_machine_keeps_only_what_the_provisioning_step_recorded(
        self, tmp_path: Path
    ) -> None:
        path = _state_file(tmp_path, None)
        before, applied = self._join(path)
        self._provisioning_writes(path, PROVISIONER_CLIENT_ID="prov-app", TENANT_ID="tenant")

        assert undo_join(path, before=before, applied=applied) is True

        assert read_state(path) == {"PROVISIONER_CLIENT_ID": "prov-app", "TENANT_ID": "tenant"}

    def test_a_switch_puts_the_previous_agent_back_beside_the_provisioner_as_it_is_now(
        self, tmp_path: Path
    ) -> None:
        path = _state_file(tmp_path)
        before, applied = self._join(path)
        # In the same run the provisioning step moved this computer to a new setup key and
        # recorded an old one it could not switch off yet.
        self._provisioning_writes(
            path,
            PROVISIONER_CERT_THUMBPRINT="new-thumb",
            PROVISIONER_CERT_REVOKE_PENDING="old-thumb",
        )

        assert undo_join(path, before=before, applied=applied) is True

        assert read_state(path) == {
            **FULL_STATE,
            "PROVISIONER_CERT_THUMBPRINT": "new-thumb",
            "PROVISIONER_CERT_REVOKE_PENDING": "old-thumb",
        }
        # The backup the switch made stays where it is.
        assert (tmp_path / ".entrabot-state.json.bak.20261111-093015").is_file()

    def test_an_agent_record_something_else_wrote_to_since_is_left_alone(
        self, tmp_path: Path
    ) -> None:
        path = _state_file(tmp_path, None)
        before, applied = self._join(path)
        changed = self._provisioning_writes(path, BLUEPRINT_OBJECT_ID="bp-object")

        assert undo_join(path, before=before, applied=applied) is False

        assert read_state(path) == changed

    def test_the_blueprint_this_computer_already_had_is_not_undone(self, tmp_path: Path) -> None:
        path = _state_file(tmp_path)
        before, applied = self._join(path, OLD_BP)
        unchanged = path.read_bytes()

        assert undo_join(path, before=before, applied=applied) is False

        assert path.read_bytes() == unchanged


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


OLD_UPN = "entrabot-agent-old@contoso.com"


def _env_for(blueprint: str, upn: str = OLD_UPN) -> dict[str, str]:
    """The .env setup writes for one agent: its Blueprint and its Agent User, together."""
    return {"ENTRABOT_BLUEPRINT_APP_ID": blueprint, "ENTRABOT_AGENT_USER_UPN": upn}


class TestAnAgentUserNameEnvHoldsForAnotherBlueprintIsNotCarried:
    """The helper loads .env for any variable that is not set, so whatever ENTRABOT_AGENT_USER_UPN
    .env holds is used unless the helper is handed one. The wizard handed it an empty one only
    when the state file showed a switch. A switch that was interrupted (the state file already
    names the new Blueprint, .env still the previous agent's) carried the previous agent's name
    in, and the helper's "belongs to a different Blueprint" check stopped setup. scripts/setup.sh
    avoids it up front ("Not reusing the Agent User named in .env"); so does the wizard now."""

    def test_joining_again_after_an_interrupted_switch(self) -> None:
        choice = IdentityChoice(mode="join", blueprint_app_id=NEW_BP)  # the state file agrees
        env = identity_environment(choice, base={}, env_values=_env_for(OLD_BP))
        assert env["ENTRABOT_AGENT_USER_UPN"] == ""

    @pytest.mark.parametrize(
        "env_values",
        [{}, {"ENTRABOT_AGENT_USER_UPN": OLD_UPN}, _env_for("")],
        ids=["no-env", "names-no-blueprint", "blank-blueprint"],
    )
    def test_joining_with_an_env_that_names_no_blueprint(self, env_values: dict[str, str]) -> None:
        # As setup.sh does: nothing shows that .env's Agent User is this Blueprint's.
        choice = IdentityChoice(mode="join", blueprint_app_id=NEW_BP)
        assert identity_environment(choice, base={}, env_values=env_values) == {
            "ENTRABOT_AGENT_USER_UPN": ""
        }

    @pytest.mark.parametrize("recorded", [NEW_BP, NEW_BP.upper()], ids=["same", "capitals"])
    def test_the_blueprint_env_was_written_for_keeps_its_agent_user(self, recorded: str) -> None:
        # The repair: join the agent this computer is set up for. The helper reads .env's name.
        choice = IdentityChoice(mode="join", blueprint_app_id=NEW_BP)
        assert identity_environment(choice, base={}, env_values=_env_for(recorded)) == {}

    def test_a_switch_the_state_file_shows_still_counts(self) -> None:
        choice = IdentityChoice(mode="join", blueprint_app_id=NEW_BP, switching=True)
        env = identity_environment(choice, base={}, env_values=_env_for(NEW_BP))
        assert env["ENTRABOT_AGENT_USER_UPN"] == ""

    def test_an_agent_user_or_suffix_the_operator_named_wins(self) -> None:
        upn = IdentityChoice(mode="join", blueprint_app_id=NEW_BP, agent_user_upn="mine@c.com")
        suffix = IdentityChoice(mode="join", blueprint_app_id=NEW_BP, upn_suffix="sati")
        stale = _env_for(OLD_BP)
        assert identity_environment(upn, base={}, env_values=stale) == {
            "ENTRABOT_AGENT_USER_UPN": "mine@c.com"
        }
        assert identity_environment(suffix, base={}, env_values=stale) == {
            "_ENTRABOT_UPN_SUFFIX": "sati"
        }

    def test_continuing_after_an_interrupted_switch(self) -> None:
        # Run again with no flags: setup continues with the agent the state file names.
        env = identity_environment(
            IdentityChoice(mode="continue"),
            base={"PATH": "/bin"},
            env_values=_env_for(OLD_BP),
            blueprint_app_id=NEW_BP,
        )
        assert env == {"PATH": "/bin", "ENTRABOT_AGENT_USER_UPN": ""}

    @pytest.mark.parametrize(
        "env_values",
        [_env_for(NEW_BP), _env_for(NEW_BP.upper()), {"ENTRABOT_AGENT_USER_UPN": OLD_UPN}, {}],
        ids=["same", "capitals", "names-no-blueprint", "no-env"],
    )
    def test_continuing_with_this_computers_agent_changes_nothing(
        self, env_values: dict[str, str]
    ) -> None:
        # Continuing needs .env to name another Blueprint: one that names none was written for
        # this computer's agent by an earlier setup, and its name is what the repair needs.
        env = identity_environment(
            IdentityChoice(mode="continue"),
            base={"PATH": "/bin"},
            env_values=env_values,
            blueprint_app_id=NEW_BP,
        )
        assert env == {"PATH": "/bin"}

    def test_a_new_agent_is_unaffected(self) -> None:
        choice = IdentityChoice(mode="new", upn_suffix="bob")
        assert identity_environment(choice, base={}, env_values=_env_for(OLD_BP)) == {
            "ENTRABOT_NEW_CHAIN": "1",
            "ENTRABOT_AGENT_USER_UPN": "",
            "_ENTRABOT_UPN_SUFFIX": "bob",
        }

    def test_a_caller_that_does_not_say_what_env_holds_gets_the_old_rule(self) -> None:
        choice = IdentityChoice(mode="join", blueprint_app_id=NEW_BP)
        assert identity_environment(choice, base={}) == {}
        assert identity_environment(IdentityChoice(mode="continue"), base={}) == {}


class TestWhatIsSaidWhenEnvsAgentUserIsNotCarried:
    """The words of setup.sh, with the wizard's parameters."""

    WANTED = (
        f"To use one of {NEW_BP}'s Agent Users, run setup with -UseBlueprint {NEW_BP} "
        "and -AgentUserUpn UPN or -UpnSuffix NAME."
    )

    def test_joining(self) -> None:
        choice = IdentityChoice(mode="join", blueprint_app_id=NEW_BP)
        assert not_reusing_env_agent_user(
            choice, blueprint_app_id=NEW_BP, env_values=_env_for(OLD_BP)
        ) == [
            f"Not reusing the Agent User named in .env ({OLD_UPN}):",
            f".env was written for Blueprint {OLD_BP}, not {NEW_BP}.",
            self.WANTED,
        ]

    def test_continuing(self) -> None:
        lines = not_reusing_env_agent_user(
            IdentityChoice(mode="continue"), blueprint_app_id=NEW_BP, env_values=_env_for(OLD_BP)
        )
        assert lines[1:] == [f".env was written for Blueprint {OLD_BP}, not {NEW_BP}.", self.WANTED]

    def test_an_env_that_names_no_blueprint(self) -> None:
        choice = IdentityChoice(mode="join", blueprint_app_id=NEW_BP)
        lines = not_reusing_env_agent_user(
            choice, blueprint_app_id=NEW_BP, env_values={"ENTRABOT_AGENT_USER_UPN": OLD_UPN}
        )
        assert lines[1] == f".env was written for Blueprint (none recorded), not {NEW_BP}."

    @pytest.mark.parametrize(
        ("choice", "env_values"),
        [
            (IdentityChoice(mode="join", blueprint_app_id=NEW_BP), _env_for(OLD_BP, upn="")),
            (IdentityChoice(mode="join", blueprint_app_id=NEW_BP), {}),
            (IdentityChoice(mode="join", blueprint_app_id=NEW_BP), _env_for(NEW_BP.upper())),
            (
                IdentityChoice(mode="join", blueprint_app_id=NEW_BP, agent_user_upn="m@c.com"),
                _env_for(OLD_BP),
            ),
            (
                IdentityChoice(mode="join", blueprint_app_id=NEW_BP, upn_suffix="sati"),
                _env_for(OLD_BP),
            ),
            # The state file shows a switch, but .env is this Blueprint's: nothing to explain.
            (
                IdentityChoice(mode="join", blueprint_app_id=NEW_BP, switching=True),
                _env_for(NEW_BP),
            ),
            (IdentityChoice(mode="continue"), _env_for(NEW_BP)),
            (IdentityChoice(mode="new", upn_suffix="bob"), _env_for(OLD_BP)),
        ],
        ids=[
            "env-names-no-agent-user",
            "no-env",
            "same-blueprint",
            "agent-user-named",
            "suffix-named",
            "switch-to-the-blueprint-env-names",
            "continue-same",
            "new",
        ],
    )
    def test_nothing_is_said_otherwise(
        self, choice: IdentityChoice, env_values: dict[str, str]
    ) -> None:
        assert (
            not_reusing_env_agent_user(choice, blueprint_app_id=NEW_BP, env_values=env_values) == []
        )


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
