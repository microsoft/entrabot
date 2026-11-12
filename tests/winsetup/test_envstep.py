"""Writing identity and the sponsor to ``.env``.

These rules come from the tests of the PowerShell ``.env`` step this replaced: the same
rules, now exercised on every OS instead of only where PowerShell 7 is installed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from entrabot.winsetup.certstep import CertOutcome
from entrabot.winsetup.envfile import read_env
from entrabot.winsetup.envstep import (
    EnvResult,
    Identity,
    identity_from_state,
    write_identity_env,
)
from entrabot.winsetup.sponsors import Sponsor

NEW_SHA1 = "A" * 40
NEW_X5T = "n" * 43
RUNNER = Sponsor("runner-object", "runner@example.invalid", "runner@example.invalid", "Member", "")
CERT = CertOutcome(
    sha1_hex=NEW_SHA1,
    x5t_s256=NEW_X5T,
    ksp="tpm",
    not_after=datetime(2027, 11, 11, tzinfo=UTC),
    days_left=365,
    generated=True,
    unconfirmed=False,
    tpm_fallback_reason=None,
)


def _identity(agent_user: str = "agent-user") -> Identity:
    return Identity(
        tenant_id="tenant",
        blueprint_app_id="blueprint-app",
        blueprint_object_id="blueprint-object",
        agent_id="agent-app",
        agent_object_id="agent-object",
        agent_user_id=agent_user,
        agent_user_upn="agent@example.invalid",
        provisioner_app_id="provisioner-app",
    )


class Run:
    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / ".env"
        self.lookups = 0

    def __call__(
        self,
        env: list[str] | None = None,
        *,
        agent_user: str = "agent-user",
        explicit: list[Sponsor] | None = None,
        lookup_fails: bool = False,
    ) -> EnvResult:
        if env is not None:
            self.path.write_text("\n".join(env) + "\n", encoding="utf-8")

        def default_sponsor() -> Sponsor | None:
            self.lookups += 1
            return None if lookup_fails else RUNNER

        return write_identity_env(
            self.path,
            identity=_identity(agent_user),
            cert=CERT,
            explicit_sponsors=explicit,
            default_sponsor=default_sponsor,
        )

    @property
    def lines(self) -> list[str]:
        return self.path.read_text(encoding="utf-8").splitlines()

    def values(self, key: str) -> list[str]:
        return [line.split("=", 1)[1] for line in self.lines if line.startswith(f"{key}=")]

    def effective(self, key: str) -> str | None:
        return read_env(self.path).get(key)


@pytest.fixture
def run(tmp_path: Path) -> Run:
    return Run(tmp_path)


class TestIdentity:
    def test_identity_is_written_and_repeated_keys_are_replaced_in_place(self, run: Run) -> None:
        run(["ENTRABOT_AGENT_ID=stale", "ENTRABOT_AGENT_ID=staler", "OTHER=kept"])
        assert run.values("ENTRABOT_AGENT_ID") == ["agent-app"]
        assert run.values("ENTRABOT_AGENT_USER_ID") == ["agent-user"]
        assert run.values("ENTRABOT_AGENT_OBJECT_ID") == ["agent-object"]
        assert run.values("ENTRABOT_BLUEPRINT_APP_ID") == ["blueprint-app"]
        assert run.values("ENTRABOT_BLUEPRINT_OBJECT_ID") == ["blueprint-object"]
        assert run.values("ENTRABOT_TENANT_ID") == ["tenant"]
        assert run.values("ENTRABOT_PROVISIONER_APP_ID") == ["provisioner-app"]
        assert "OTHER=kept" in run.lines

    def test_the_certificate_is_recorded_with_both_thumbprints(self, run: Run) -> None:
        run()
        assert run.values("ENTRABOT_BLUEPRINT_CERT_SHA1") == [NEW_SHA1]
        assert run.values("ENTRABOT_BLUEPRINT_CERT_THUMBPRINT") == [NEW_X5T]
        assert run.values("ENTRABOT_BLUEPRINT_KSP") == ["tpm"]

    def test_a_value_that_is_missing_keeps_what_env_already_has(self, run: Run) -> None:
        run(["ENTRABOT_AGENT_USER_ID=already-here"], agent_user="")
        assert run.values("ENTRABOT_AGENT_USER_ID") == ["already-here"]

    def test_both_upn_keys_name_the_selected_agent(self, run: Run) -> None:
        # config.py prefers ENTRABOT_AGENT_UPN, so a stale one would outrank the alias and
        # the email poll's self-sender filter would stop recognizing the agent's own mail.
        run(["ENTRABOT_AGENT_UPN=old-agent@example.invalid"])
        assert run.values("ENTRABOT_AGENT_UPN") == ["agent@example.invalid"]
        assert run.values("ENTRABOT_AGENT_USER_UPN") == ["agent@example.invalid"]

    def test_padded_keys_are_replaced_and_comments_are_left_alone(self, run: Run) -> None:
        # config.py trims keys, so a padded stale line above the new one would win.
        run(["ENTRABOT_AGENT_ID = stale", "  ENTRABOT_TENANT_ID=old", "# ENTRABOT_AGENT_ID=a note"])
        assert run.effective("ENTRABOT_AGENT_ID") == "agent-app"
        assert run.effective("ENTRABOT_TENANT_ID") == "tenant"
        assert "# ENTRABOT_AGENT_ID=a note" in run.lines

    def test_every_key_setup_sh_writes_for_the_runtime_is_written(self, run: Run) -> None:
        # config.py reads identity only from the environment and .env, never from the state
        # file, so a key missing here is an identity the agent can't sign in with.
        import re

        script = (Path(__file__).resolve().parents[2] / "scripts" / "setup.sh").read_text(
            encoding="utf-8"
        )
        heredoc = script[script.index("cat > .env << EOF") :]
        heredoc = heredoc[: heredoc.index("\nEOF\n")]
        keys = set(re.findall(r"^(ENTRABOT_[A-Z_]+)=", heredoc, re.M)) - {"ENTRABOT_LOG_LEVEL"}
        assert len(keys) > 10
        run()
        written = set(read_env(run.path))
        assert sorted(keys - written) == []

    def test_identity_values_come_from_the_state_file(self) -> None:
        state = {
            "BLUEPRINT_APP_ID": "bp",
            "BLUEPRINT_OBJECT_ID": "bpo",
            "AGENT_ID": "a",
            "AGENT_OBJECT_ID": "ao",
            "AGENT_USER_ID": "au",
            "AGENT_USER_UPN": "u@x",
            "PROVISIONER_CLIENT_ID": "p",
        }
        assert identity_from_state(state, tenant_id="t") == Identity(
            "t", "bp", "bpo", "a", "ao", "au", "u@x", "p"
        )
        assert identity_from_state({}, tenant_id="t").agent_user_id == ""


class TestSponsor:
    def test_the_signed_in_account_becomes_the_sponsor_when_env_names_none(self, run: Run) -> None:
        result = run()
        assert run.values("ENTRABOT_HUMAN_USER_ID") == ["runner-object"]
        assert run.values("ENTRABOT_HUMAN_UPN") == ["runner@example.invalid"]
        assert run.values("ENTRABOT_HUMAN_USER_TYPES") == ["Member"]
        assert result.sponsor == "set"

    def test_a_re_run_does_not_change_the_sponsor(self, run: Run) -> None:
        result = run(["ENTRABOT_HUMAN_USER_ID=the-sponsor"])
        assert run.values("ENTRABOT_HUMAN_USER_ID") == ["the-sponsor"]
        assert run.values("ENTRABOT_HUMAN_UPN") == []
        assert run.lookups == 0  # nobody is looked up when nothing will change
        assert result.sponsor == "kept" and result.sponsor_label == "the-sponsor"

    def test_a_failed_sponsor_lookup_warns_and_setup_continues(self, run: Run) -> None:
        result = run(lookup_fails=True)
        assert run.values("ENTRABOT_HUMAN_USER_ID") == []
        assert run.values("ENTRABOT_AGENT_ID") == ["agent-app"]
        assert result.sponsor == "none"

    @pytest.mark.parametrize(
        "line",
        [
            "ENTRABOT_HUMAN_USER_IDS=sponsor-a,sponsor-b",
            "ENTRABOT_HUMAN_UPN=sponsor@contoso.com",
            "ENTRABOT_HUMAN_UPNS=sponsor@contoso.com,other@contoso.com",
            " ENTRABOT_HUMAN_USER_ID = the-sponsor ",
        ],
    )
    def test_any_sponsor_key_counts_as_naming_a_sponsor(self, run: Run, line: str) -> None:
        run(["ENTRABOT_AGENT_USER_ID=agent-user", line])
        assert line in run.lines
        assert run.lookups == 0
        assert run.values("ENTRABOT_HUMAN_USER_TYPES") == []

    def test_a_commented_out_sponsor_does_not_count(self, run: Run) -> None:
        run(["# ENTRABOT_HUMAN_USER_ID=old-sponsor"])
        assert run.values("ENTRABOT_HUMAN_USER_ID") == ["runner-object"]

    def test_a_run_for_a_different_agent_does_not_keep_the_old_sponsor(self, run: Run) -> None:
        result = run(["ENTRABOT_AGENT_USER_ID=another-agent", "ENTRABOT_HUMAN_USER_ID=old-sponsor"])
        assert run.values("ENTRABOT_HUMAN_USER_ID") == ["runner-object"]
        assert run.values("ENTRABOT_AGENT_USER_ID") == ["agent-user"]
        assert result.same_agent is False

    def test_a_different_agent_loses_the_old_sponsor_even_if_the_lookup_fails(
        self, run: Run
    ) -> None:
        # Otherwise daily summaries for the new agent could reach the previous sponsor.
        run(
            [
                "ENTRABOT_AGENT_USER_ID=another-agent",
                "ENTRABOT_HUMAN_USER_ID=old-sponsor",
                "ENTRABOT_HUMAN_UPNS=old@contoso.com",
            ],
            lookup_fails=True,
        )
        assert run.effective("ENTRABOT_HUMAN_USER_ID") is None
        assert run.effective("ENTRABOT_HUMAN_UPNS") is None

    def test_a_different_agent_loses_the_old_blob_store_and_local_flag(self, run: Run) -> None:
        # The container is scoped to one Agent User. This happens here, not in the memory
        # step, so it holds even when setup stops early (an unconfirmed certificate): the
        # next run sees this agent's ID in .env and would take the old blob lines for its own.
        run(
            [
                "ENTRABOT_AGENT_USER_ID=another-agent",
                "ENTRABOT_BLOB_ENDPOINT=https://old.example.invalid",
                "ENTRABOT_BLOB_CONTAINER=old-container",
                "ENTRABOT_KEEP_MEMORY_LOCAL=false",
            ]
        )
        assert run.effective("ENTRABOT_BLOB_ENDPOINT") is None
        assert run.effective("ENTRABOT_BLOB_CONTAINER") is None
        assert run.effective("ENTRABOT_KEEP_MEMORY_LOCAL") is None

    def test_the_same_agent_keeps_its_blob_store(self, run: Run) -> None:
        run(["ENTRABOT_AGENT_USER_ID=agent-user", "ENTRABOT_BLOB_ENDPOINT=https://old"])
        assert run.effective("ENTRABOT_BLOB_ENDPOINT") == "https://old"

    def test_no_id_on_either_side_counts_as_the_same_agent(self, run: Run) -> None:
        # A machine set up before setup wrote IDs has none in .env; there's no telling.
        assert run(["ENTRABOT_HUMAN_USER_ID=the-sponsor"]).same_agent is True
        assert run(["ENTRABOT_AGENT_USER_ID=x"], agent_user="").same_agent is True

    def test_a_new_sponsor_does_not_inherit_the_old_sponsors_tenant_ids(self, run: Run) -> None:
        run(["ENTRABOT_AGENT_USER_ID=another-agent", "ENTRABOT_HUMAN_USER_TENANT_IDS=old-tenant"])
        assert run.values("ENTRABOT_HUMAN_USER_TENANT_IDS") == [""]

    def test_a_sponsor_chosen_in_this_run_replaces_the_one_in_env(self, run: Run) -> None:
        chosen = [
            Sponsor("id-1", "pat@contoso.com", "pat@contoso.com", "Member", ""),
            Sponsor("id-2", "sam@fabrikam.com", "sam@fabrikam.com", "Guest", "home"),
        ]
        result = run(
            ["ENTRABOT_AGENT_USER_ID=agent-user", "ENTRABOT_HUMAN_USER_ID=the-sponsor"],
            explicit=chosen,
        )
        assert run.values("ENTRABOT_HUMAN_USER_ID") == ["id-1"]
        assert run.values("ENTRABOT_HUMAN_USER_IDS") == ["id-1,id-2"]
        assert run.values("ENTRABOT_HUMAN_USER_TENANT_IDS") == [",home"]
        assert run.lookups == 0 and result.sponsor == "set"
