"""Teardown: safe by default, explicit about its effect, and precise about what is left.

The default disconnects THIS computer and nothing else. The old script matched certificates
by subject (deleting other agents' certificates), left this computer's certificate trusted
on the Blueprint until it expired, left its private key behind, and deleted the data folder
without being asked. Each of those is pinned here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from entrabot.winsetup import teardown
from tests.winsetup.fakes import Reply, make_console, script
from tests.winsetup.maintenance_fakes import (
    BP_APP,
    BP_OBJECT,
    ENV_LINES,
    NEW_SHA1,
    NEW_X5T,
    OLD_SHA1,
    OLD_X5T,
    PROV_SHA1,
    REMOVE_BLUEPRINT_CREDENTIAL,
    REMOVE_PROVISIONER_CREDENTIAL,
    STATE,
    UPN,
    Project,
    StoreCert,
    blueprint_cert,
    make_project,
    provisioner_cert,
    result,
)

DEPROVISION = script("deprovision_entra_agent_identity.py")
OTHER_AGENT_CERT = StoreCert(
    sha1_hex="D" * 40,
    x5t_s256="d" * 43,
    subject="CN=Entrabot Blueprint 99999999",
    friendly_name="Entrabot Blueprint 99999999-2222-3333-4444-555555555555",
)
LEGACY_NAMED_CERT = StoreCert(
    sha1_hex="E" * 40, x5t_s256="e" * 43, subject="CN=entrabot-blueprint", friendly_name=""
)
ENTRABOT_ENTRY = {"command": "C:/x/entrabot-mcp.exe", "args": []}


def write_mcp(path: Path, extra: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "mcpServers": {"entrabot": ENTRABOT_ENTRY, "persona-sati": {"command": "pérsona"}},
        "theme": "dark",
    }
    data.update(extra or {})
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def tear_down(project: Project, *, typed: str | None = None, yes: bool = True, **options: object):
    console, out = make_console(
        spawn=project.spawn,
        typed=typed,
        assume_yes=yes,
        log_file=project.paths.data_root / "logs" / "teardown.log",
    )
    code = teardown.run_teardown(console, project.paths, project.world, **options)
    console.close()
    return code, out.getvalue()


def backups(project: Project, name: str) -> list[Path]:
    return sorted(project.root.glob(f"{name}.bak.*"))


@pytest.fixture
def project(tmp_path: Path) -> Project:
    project = make_project(
        tmp_path, certs=[blueprint_cert(), provisioner_cert(), OTHER_AGENT_CERT, LEGACY_NAMED_CERT]
    )
    write_mcp(project.paths.claude_mcp)
    write_mcp(Path(project.env["COPILOT_HOME"]) / "mcp-config.json")
    project.spawn.on(REMOVE_BLUEPRINT_CREDENTIAL, result(0, removed=1, kept=2))
    project.spawn.on(REMOVE_PROVISIONER_CREDENTIAL, result(0, removed=1, kept=1))
    project.spawn.on(DEPROVISION, Reply(0, stdout="  [done] Deleted Agent User\n"))
    (project.paths.data_root / "interactions.jsonl").write_text("{}\n", encoding="utf-8")
    return project


class TestDefaultDisconnectsThisComputerOnly:
    def test_this_computers_certificate_comes_off_the_blueprint(self, project: Project) -> None:
        code, _ = tear_down(project)

        assert code == teardown.EXIT_OK
        (removal,) = project.ran(REMOVE_BLUEPRINT_CREDENTIAL)
        assert removal.argv[-2:] == [BP_OBJECT, OLD_SHA1]

    def test_only_this_agents_certificate_is_deleted_and_with_its_key(
        self, project: Project
    ) -> None:
        tear_down(project)

        assert project.store.deleted == [(OLD_SHA1, True)]
        # Chosen by the thumbprint in .env. Another agent's certificate, and one that merely
        # has the old shared subject, are not this agent's to delete.
        assert project.store.find_cert(OTHER_AGENT_CERT.sha1_hex) is not None
        assert project.store.find_cert(LEGACY_NAMED_CERT.sha1_hex) is not None
        assert project.store.find_cert(PROV_SHA1) is not None

    def test_only_the_entrabot_entry_leaves_each_assistant_config(self, project: Project) -> None:
        tear_down(project)

        for path in (
            project.paths.claude_mcp,
            Path(project.env["COPILOT_HOME"]) / "mcp-config.json",
        ):
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data == {"mcpServers": {"persona-sati": {"command": "pérsona"}}, "theme": "dark"}
            assert "pérsona" in path.read_text(encoding="utf-8")  # written as UTF-8, unescaped

    def test_the_real_home_folder_is_never_touched(self, project: Project) -> None:
        tear_down(project)

        assert not (Path.home() / ".copilot").exists()

    def test_settings_are_backed_up_then_removed(self, project: Project) -> None:
        env_before = project.env_file.read_bytes()

        tear_down(project)

        assert not project.env_file.exists()
        assert not project.paths.state_file.exists()
        (env_backup,) = backups(project, ".env")
        assert env_backup.read_bytes() == env_before
        (state_backup,) = backups(project, ".entrabot-state.json")
        assert json.loads(state_backup.read_text(encoding="utf-8")) == STATE

    def test_the_agent_and_its_data_are_left_alone(self, project: Project) -> None:
        _, out = tear_down(project)

        assert [call for call in project.spawn.calls if DEPROVISION(call)] == []
        assert (project.paths.data_root / "interactions.jsonl").exists()
        assert UPN in out and "still exists" in out

    @pytest.mark.parametrize("options", [{}, {"delete_agent": True}, {"purge_data": True}])
    def test_the_project_folders_launcher_is_not_teardowns_to_remove(
        self, project: Project, options: dict[str, bool]
    ) -> None:
        # Setup leaves a start-entrabot.cmd in the project folder (and puts Claude Code on
        # the user's PATH). Those belong to the folder and the person, not to this
        # computer's connection to the agent, so no scope of teardown removes them.
        launcher = project.root / "start-entrabot.cmd"
        launcher.write_bytes(b"@echo off\r\n")

        tear_down(project, running_from=[], **options)

        assert launcher.read_bytes() == b"@echo off\r\n"

    def test_it_never_edits_the_users_path_or_starts_anything_but_its_helpers(
        self, project: Project
    ) -> None:
        tear_down(project, delete_agent=True, purge_data=True, running_from=[])

        programs = {Path(call.argv[0]).name.lower() for call in project.spawn.calls}
        # The Azure CLI (who is signed in) and the venv interpreter (the helper scripts).
        # No setx, reg, or PowerShell: nothing that could edit PATH or a profile.
        assert programs <= {"az", "python.exe"}

    def test_it_says_what_it_will_do_before_doing_it(self, project: Project) -> None:
        _, out = tear_down(project)

        plan = out.index("This will")
        first_action = out.index("Removed this computer's certificate from the agent's Blueprint")
        assert plan < first_action
        assert "other computers" in out

    def test_no_full_thumbprint_on_screen_or_in_the_log(self, project: Project) -> None:
        _, out = tear_down(project, delete_agent=True)

        log = (project.paths.data_root / "logs" / "teardown.log").read_text(encoding="utf-8")
        for text in (out, log):
            assert OLD_SHA1 not in text
            assert PROV_SHA1 not in text


class TestSetupCredentialIsNotLeftBehind:
    """Setup signs in to Microsoft Entra with its own certificate (the Provisioner's), which
    can manage applications across the tenant. Disconnecting a computer someone is done with
    must not leave that key in its certificate store and trusted on the Provisioner app."""

    @pytest.fixture
    def alone(self, tmp_path: Path) -> Project:
        """This agent is the only Entrabot agent on the computer."""
        project = make_project(tmp_path, certs=[blueprint_cert(), provisioner_cert()])
        project.spawn.on(REMOVE_BLUEPRINT_CREDENTIAL, result(0, removed=1, kept=1))
        project.spawn.on(REMOVE_PROVISIONER_CREDENTIAL, result(0, removed=1, kept=2))
        return project

    def test_disconnecting_retires_it_in_entra_and_on_this_computer(self, alone: Project) -> None:
        code, out = tear_down(alone)

        assert code == teardown.EXIT_OK
        (removal,) = alone.ran(REMOVE_PROVISIONER_CREDENTIAL)
        assert removal.argv[-1] == PROV_SHA1
        assert (PROV_SHA1, True) in alone.store.deleted  # with its private key
        assert alone.store.find_cert(PROV_SHA1) is None
        assert "Retired this computer's setup certificate" in out

    def test_the_blueprint_certificate_comes_off_first(self, alone: Project) -> None:
        # Taking this computer's certificate off the Blueprint needs the setup certificate
        # to sign in with, so the setup certificate is retired last.
        tear_down(alone)

        (blueprint,) = alone.ran(REMOVE_BLUEPRINT_CREDENTIAL)
        (setup,) = alone.ran(REMOVE_PROVISIONER_CREDENTIAL)
        calls = alone.spawn.calls
        assert calls.index(blueprint) < calls.index(setup)
        assert alone.store.events == ["delete:01D1", "delete:9A9A"]

    def test_the_plan_says_so_before_the_question(self, alone: Project) -> None:
        code, out = tear_down(alone, typed="n\n", yes=False)

        assert code == teardown.EXIT_CANCELLED
        plan = out[: out.index("Disconnect this computer?")]
        assert "setup (Provisioner) certificate is retired" in plan
        assert "-KeepSetupCredential" in plan
        assert alone.ran(REMOVE_PROVISIONER_CREDENTIAL) == []
        assert alone.store.find_cert(PROV_SHA1) is not None

    def test_the_report_says_it_was_removed(self, alone: Project) -> None:
        _, out = tear_down(alone)

        report = out[out.index("This computer is disconnected") :]
        assert "setup (Provisioner) certificate and its private key" in report

    def test_it_is_kept_while_another_agent_on_this_computer_needs_it(
        self, project: Project
    ) -> None:
        # The fixture's store also holds another Entrabot agent's Blueprint certificate.
        code, out = tear_down(project)

        assert code == teardown.EXIT_OK
        assert project.ran(REMOVE_PROVISIONER_CREDENTIAL) == []
        assert project.store.find_cert(PROV_SHA1) is not None
        plan = out[: out.index("Removed this computer's certificate from the agent's Blueprint")]
        assert "another Entrabot agent on this computer" in plan  # said beforehand ...
        report = out[out.index("This computer is disconnected") :]
        assert "another Entrabot agent on this computer" in report  # ... and afterwards

    def test_it_is_kept_when_asked(self, alone: Project) -> None:
        code, out = tear_down(alone, keep_setup_credential=True)

        assert code == teardown.EXIT_OK
        assert alone.ran(REMOVE_PROVISIONER_CREDENTIAL) == []
        assert alone.store.find_cert(PROV_SHA1) is not None
        assert alone.store.deleted == [(OLD_SHA1, True)]
        plan = out[: out.index("Removed this computer's certificate from the agent's Blueprint")]
        assert "kept, as asked" in plan
        report = out[out.index("This computer is disconnected") :]
        assert "-KeepSetupCredential" in report
        assert "can manage" in report  # what was kept, in words that say why it matters

    def test_keeping_it_applies_when_the_agent_is_deleted_too(self, alone: Project) -> None:
        alone.spawn.on(DEPROVISION, Reply(0))

        code, _ = tear_down(alone, delete_agent=True, keep_setup_credential=True)

        assert code == teardown.EXIT_OK
        assert alone.ran(REMOVE_PROVISIONER_CREDENTIAL) == []
        assert alone.store.find_cert(PROV_SHA1) is not None

    def test_a_failed_retirement_is_reported_with_the_way_to_finish_by_hand(
        self, alone: Project
    ) -> None:
        alone.spawn.on(
            REMOVE_PROVISIONER_CREDENTIAL, result(1, problem="Graph refused the update with 403")
        )

        code, out = tear_down(alone)

        assert code == teardown.EXIT_INCOMPLETE
        # The key is gone from this computer either way, so what stays listed is unusable.
        assert (PROV_SHA1, True) in alone.store.deleted
        todo = out[out.index("Still to do:") :]
        assert "still listed on the Provisioner app" in todo
        assert "9A9A" in todo and "403" in todo
        assert "EntraBot Agent ID Provisioner" in todo
        assert "Certificates & secrets" in todo
        assert "cannot be used" in todo
        # What did happen is still said: the key is off this computer.
        assert "Deleted the setup certificate and its private key here" in out

    def test_without_access_to_entra_the_local_key_still_goes(self, tmp_path: Path) -> None:
        project = make_project(
            tmp_path, signed_in=False, certs=[blueprint_cert(), provisioner_cert()]
        )

        code, out = tear_down(project)

        assert code == teardown.EXIT_INCOMPLETE
        assert (PROV_SHA1, True) in project.store.deleted
        assert project.ran(REMOVE_PROVISIONER_CREDENTIAL) == []
        assert "still listed on the Provisioner app" in out

    def test_a_computer_with_no_setup_certificate_says_nothing_about_one(
        self, tmp_path: Path
    ) -> None:
        project = make_project(tmp_path)
        project.spawn.on(REMOVE_BLUEPRINT_CREDENTIAL, result(0, removed=1, kept=0))

        code, out = tear_down(project)

        assert code == teardown.EXIT_OK
        assert "Provisioner" not in out

    def test_a_failed_agent_deletion_still_keeps_it_for_the_retry(self, alone: Project) -> None:
        alone.spawn.on(DEPROVISION, Reply(1, stdout="ERROR: refusing\n"))

        code, _ = tear_down(alone, delete_agent=True)

        assert code == teardown.EXIT_INCOMPLETE
        assert alone.ran(REMOVE_PROVISIONER_CREDENTIAL) == []
        assert alone.store.find_cert(PROV_SHA1) is not None


class TestOnlyThisComputersCertificateComesOffTheBlueprint:
    """The thumbprint in .env says which certificate this folder was set up with, not that
    the certificate is on THIS computer. A copied or shared .env names another computer's
    certificate; removing that from the Blueprint would cut the other computer off."""

    @pytest.fixture
    def copied(self, tmp_path: Path) -> Project:
        """A .env naming a certificate that is not in this computer's store."""
        project = make_project(tmp_path, certs=[])
        project.spawn.on(REMOVE_BLUEPRINT_CREDENTIAL, result(0, removed=1, kept=1))
        return project

    def test_unattended_it_is_left_on_the_blueprint_and_said(self, copied: Project) -> None:
        code, out = tear_down(copied)

        assert code == teardown.EXIT_OK
        assert copied.ran(REMOVE_BLUEPRINT_CREDENTIAL) == []
        plan = out[: out.index("Removed .env")]
        assert "not in this computer's certificate store" in plan
        report = out[out.index("This computer is disconnected") :]
        assert "left on the agent's Blueprint" in report
        assert "01D1" in report and "another computer" in report
        assert "Certificates & secrets" in report  # where to remove it if it really is stale

    def test_interactively_it_is_asked_about_and_the_default_is_no(self, copied: Project) -> None:
        code, out = tear_down(copied, typed="y\n\n", yes=False)

        assert code == teardown.EXIT_OK
        assert "Remove it from the agent's Blueprint anyway?" in out
        assert copied.ran(REMOVE_BLUEPRINT_CREDENTIAL) == []

    def test_an_explicit_yes_to_that_question_removes_it(self, copied: Project) -> None:
        code, _ = tear_down(copied, typed="y\ny\n", yes=False)

        assert code == teardown.EXIT_OK
        (removal,) = copied.ran(REMOVE_BLUEPRINT_CREDENTIAL)
        assert removal.argv[-2:] == [BP_OBJECT, OLD_SHA1]

    def test_a_store_that_cant_be_read_is_not_proof_either(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        project.store.unreadable = True

        code, out = tear_down(project)

        assert project.ran(REMOVE_BLUEPRINT_CREDENTIAL) == []
        assert code == teardown.EXIT_INCOMPLETE  # the local certificate could not be checked
        assert "certificate store could not be opened" in out

    def test_an_unreadable_store_is_asked_about_too(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        project.spawn.on(REMOVE_BLUEPRINT_CREDENTIAL, result(0, removed=1, kept=0))
        project.store.unreadable = True

        code, out = tear_down(project, typed="y\n\n", yes=False)

        assert "Remove it from the agent's Blueprint anyway?" in out
        assert project.ran(REMOVE_BLUEPRINT_CREDENTIAL) == []
        assert code == teardown.EXIT_INCOMPLETE

    def test_it_is_not_asked_about_when_entra_cant_be_reached(self, tmp_path: Path) -> None:
        # Nothing could be removed anyway, so the question would only mislead.
        project = make_project(tmp_path, signed_in=False, certs=[])

        code, out = tear_down(project, typed="y\n", yes=False)

        assert code == teardown.EXIT_OK
        assert "anyway?" not in out
        assert "left on the agent's Blueprint" in out

    def test_a_certificate_that_is_here_is_removed_without_a_second_question(
        self, project: Project
    ) -> None:
        code, out = tear_down(project, typed="y\n", yes=False)

        assert code == teardown.EXIT_OK
        assert len(project.ran(REMOVE_BLUEPRINT_CREDENTIAL)) == 1
        assert "anyway?" not in out

    def test_nothing_extra_is_asked_to_stay(self, project: Project) -> None:
        # Teardown has not just added a certificate, so it names none that must stay.
        tear_down(project)

        (removal,) = project.ran(REMOVE_BLUEPRINT_CREDENTIAL)
        assert removal.argv[-2:] == [BP_OBJECT, OLD_SHA1]


class TestACertificateARotationLeftBehind:
    """A rotation that could not finish retiring its old certificate records it in .env
    (ENTRABOT_BLUEPRINT_CERT_RETIRING_SHA1). Disconnecting the computer has to deal with
    that one too, not report "disconnected" with it still trusted."""

    def _half_rotated(self, tmp_path: Path, *, old_in_store: bool) -> Project:
        lines = [line for line in ENV_LINES if "CERT" not in line] + [
            f"ENTRABOT_BLUEPRINT_CERT_THUMBPRINT={NEW_X5T}",
            f"ENTRABOT_BLUEPRINT_CERT_SHA1={NEW_SHA1}",
            f"ENTRABOT_BLUEPRINT_CERT_RETIRING_SHA1={OLD_SHA1}",
        ]
        certs = [blueprint_cert(NEW_SHA1, NEW_X5T)]
        if old_in_store:
            certs.append(blueprint_cert(OLD_SHA1, OLD_X5T))
        project = make_project(tmp_path, env_lines=lines, certs=certs)
        project.spawn.on(REMOVE_BLUEPRINT_CREDENTIAL, result(0, removed=1, kept=0))
        return project

    def test_both_certificates_come_off_the_blueprint_and_this_computer(
        self, tmp_path: Path
    ) -> None:
        project = self._half_rotated(tmp_path, old_in_store=True)

        code, out = tear_down(project)

        assert code == teardown.EXIT_OK
        removed = sorted(call.argv[-1] for call in project.ran(REMOVE_BLUEPRINT_CREDENTIAL))
        assert removed == sorted([NEW_SHA1, OLD_SHA1])
        assert sorted(project.store.deleted) == sorted([(NEW_SHA1, True), (OLD_SHA1, True)])
        assert "earlier certificate rotation" in out
        plan = out[: out.index("Removed this computer's certificate from the agent's Blueprint")]
        assert "earlier certificate rotation" in plan

    def test_one_that_is_no_longer_in_this_store_is_reported_not_removed(
        self, tmp_path: Path
    ) -> None:
        # Rotation deletes the old key locally even when the Blueprint could not be
        # changed, so this is the usual shape of the leftover.
        project = self._half_rotated(tmp_path, old_in_store=False)

        code, out = tear_down(project)

        assert code == teardown.EXIT_OK
        (removal,) = project.ran(REMOVE_BLUEPRINT_CREDENTIAL)
        assert removal.argv[-1] == NEW_SHA1
        report = out[out.index("This computer is disconnected") :]
        assert "earlier certificate rotation" in report
        assert "01D1" in report
        assert "Certificates & secrets" in report


class TestAPrivateKeyThatCouldNotBeDeleted:
    """wincert.delete_cert raises when the certificate went but its key did not. That is not
    "deleted the certificate and its private key", and the report must not say so."""

    def test_the_blueprint_certificates_key(self, project: Project) -> None:
        project.store.key_stuck = {OLD_SHA1}

        code, out = tear_down(project)

        assert code == teardown.EXIT_INCOMPLETE
        assert "Deleted this computer's certificate and its private key" not in out
        todo = out[out.index("Still to do:") :]
        assert "private key could not be deleted" in todo
        assert "certutil" in todo and "Microsoft Platform Crypto Provider" in todo

    def test_the_setup_certificates_key(self, tmp_path: Path) -> None:
        project = make_project(tmp_path, certs=[blueprint_cert(), provisioner_cert()])
        project.spawn.on(REMOVE_BLUEPRINT_CREDENTIAL, result(0, removed=1, kept=0))
        project.spawn.on(REMOVE_PROVISIONER_CREDENTIAL, result(0, removed=1, kept=0))
        project.store.key_stuck = {PROV_SHA1}

        code, out = tear_down(project)

        assert code == teardown.EXIT_INCOMPLETE
        assert "Retired this computer's setup certificate" not in out
        report = out[out.index("Teardown finished") :]
        assert "setup (Provisioner) certificate and its private key" not in report
        todo = out[out.index("Still to do:") :]
        assert "private key could not be deleted" in todo
        assert "certutil" in todo
        # It is off the Provisioner app, which is what stops it being usable.
        assert "removed from the Provisioner app" in todo

    def test_a_key_that_stays_is_never_called_unusable(self, tmp_path: Path) -> None:
        # Microsoft Entra can't be reached, so the certificate stays listed on the
        # Blueprint. With its key still here it CAN sign the agent in: saying "it cannot
        # be used" would be false.
        project = make_project(tmp_path, signed_in=False)
        project.store.key_stuck = {OLD_SHA1}

        code, out = tear_down(project)

        assert code == teardown.EXIT_INCOMPLETE
        todo = out[out.index("Still to do:") :]
        assert "still listed on the agent's Blueprint" in todo
        assert "cannot be used" not in todo
        assert "can still sign the agent in" in todo

    def test_a_certificate_kept_because_its_key_could_not_go(self, project: Project) -> None:
        # What entrabot.platform.wincert does today: the certificate is left in the store
        # with its key, as the handle for a retry.
        project.store.key_stuck = {OLD_SHA1}
        project.store.key_stuck_keeps_cert = True

        code, out = tear_down(project)

        assert code == teardown.EXIT_INCOMPLETE
        assert "Deleted this computer's certificate and its private key" not in out
        todo = out[out.index("Still to do:") :]
        assert "could not be deleted" in todo and "01D1" in todo
        assert "certmgr.msc" in todo
        assert OLD_SHA1 not in out

    def test_a_rotation_leftover_whose_key_could_not_go(self, tmp_path: Path) -> None:
        lines = [line for line in ENV_LINES if "CERT" not in line] + [
            f"ENTRABOT_BLUEPRINT_CERT_THUMBPRINT={NEW_X5T}",
            f"ENTRABOT_BLUEPRINT_CERT_SHA1={NEW_SHA1}",
            f"ENTRABOT_BLUEPRINT_CERT_RETIRING_SHA1={OLD_SHA1}",
        ]
        project = make_project(
            tmp_path,
            env_lines=lines,
            certs=[blueprint_cert(NEW_SHA1, NEW_X5T), blueprint_cert(OLD_SHA1, OLD_X5T)],
        )
        project.spawn.on(REMOVE_BLUEPRINT_CREDENTIAL, result(0, removed=1, kept=0))
        project.store.key_stuck = {OLD_SHA1}

        code, out = tear_down(project)

        assert code == teardown.EXIT_INCOMPLETE
        assert (NEW_SHA1, True) in project.store.deleted
        todo = out[out.index("Still to do:") :]
        assert "earlier certificate rotation" in todo
        assert "private key could not be deleted" in todo and "certutil" in todo


class TestConfirmation:
    def test_unattended_without_yes_is_refused(self, project: Project) -> None:
        before = project.env_file.read_bytes()

        code, out = tear_down(project, yes=False)

        assert code == teardown.EXIT_USAGE
        assert "-Yes" in out
        assert project.env_file.read_bytes() == before
        assert project.store.deleted == []

    def test_declining_changes_nothing(self, project: Project) -> None:
        code, out = tear_down(project, typed="\n", yes=False)  # Enter = the default = no

        assert code == teardown.EXIT_CANCELLED
        assert project.env_file.exists()
        assert project.store.deleted == []
        assert project.ran(REMOVE_BLUEPRINT_CREDENTIAL) == []
        assert "Nothing was changed" in out

    def test_saying_yes_goes_ahead(self, project: Project) -> None:
        code, _ = tear_down(project, typed="y\n", yes=False)

        assert code == teardown.EXIT_OK
        assert not project.env_file.exists()

    def test_deleting_the_agent_needs_its_address_typed(self, project: Project) -> None:
        code, out = tear_down(project, typed="y\nyes please\n", yes=False, delete_agent=True)

        assert code == teardown.EXIT_CANCELLED
        assert [call for call in project.spawn.calls if DEPROVISION(call)] == []
        assert project.env_file.exists()
        assert "Nothing was changed" in out

    def test_typing_the_address_deletes_the_agent(self, project: Project) -> None:
        code, _ = tear_down(project, typed=f"y\n{UPN}\n", yes=False, delete_agent=True)

        assert code == teardown.EXIT_OK
        assert [call for call in project.spawn.calls if DEPROVISION(call)]


class TestWhenMicrosoftEntraCantBeReached:
    def test_local_cleanup_still_completes_and_the_report_says_what_is_still_trusted(
        self, tmp_path: Path
    ) -> None:
        project = make_project(tmp_path, signed_in=False)
        write_mcp(project.paths.claude_mcp)

        code, out = tear_down(project)

        assert code == teardown.EXIT_INCOMPLETE
        assert project.store.deleted == [(OLD_SHA1, True)]
        assert not project.env_file.exists()
        assert "entrabot" not in json.loads(project.paths.claude_mcp.read_text())["mcpServers"]
        assert "still listed on the agent's Blueprint" in out
        assert "01D1" in out  # which one, without printing the whole thumbprint
        assert BP_APP in out  # where to find it
        assert "Certificates & secrets" in out  # how to remove it
        assert "cannot be used" in out  # and that it is harmless meanwhile

    def test_the_person_is_told_before_being_asked(self, tmp_path: Path) -> None:
        project = make_project(tmp_path, signed_in=False)

        code, out = tear_down(project, typed="n\n", yes=False)

        assert code == teardown.EXIT_CANCELLED
        assert out.index("not signed in to Microsoft") < out.index("Disconnect this computer?")
        assert "only this computer would be cleaned up" in out
        assert "setup-windows.cmd" in out
        assert project.store.deleted == []

    def test_a_refused_removal_is_reported_the_same_way(self, project: Project) -> None:
        project.spawn.on(REMOVE_BLUEPRINT_CREDENTIAL, result(1, problem="Graph refused it (403)"))

        code, out = tear_down(project)

        assert code == teardown.EXIT_INCOMPLETE
        assert project.store.deleted == [(OLD_SHA1, True)]
        assert "403" in out
        assert "still listed on the agent's Blueprint" in out


class TestIdempotent:
    def test_a_second_run_has_nothing_to_do(self, project: Project) -> None:
        tear_down(project)
        removals = len(project.ran(REMOVE_BLUEPRINT_CREDENTIAL))
        deleted = list(project.store.deleted)

        code, out = tear_down(project)

        assert code == teardown.EXIT_OK
        assert "Nothing to remove" in out
        assert len(backups(project, ".env")) == 1
        assert len(project.ran(REMOVE_BLUEPRINT_CREDENTIAL)) == removals
        assert project.store.deleted == deleted

    def test_a_folder_with_only_an_assistant_entry_left_is_still_cleaned(
        self, tmp_path: Path
    ) -> None:
        project = make_project(tmp_path, env_lines=[], certs=[], state={})
        project.paths.state_file.unlink()
        write_mcp(project.paths.claude_mcp)

        code, _ = tear_down(project)

        assert code == teardown.EXIT_OK
        assert "entrabot" not in json.loads(project.paths.claude_mcp.read_text())["mcpServers"]

    def test_an_assistant_config_that_isnt_json_is_left_alone_and_reported(
        self, project: Project
    ) -> None:
        project.paths.claude_mcp.write_text("{not json", encoding="utf-8")

        code, out = tear_down(project)

        assert code == teardown.EXIT_INCOMPLETE
        assert project.paths.claude_mcp.read_text(encoding="utf-8") == "{not json"
        assert ".mcp.json" in out


class TestDeleteAgent:
    def test_deletes_the_directory_objects_through_the_deprovisioning_helper(
        self, project: Project
    ) -> None:
        code, out = tear_down(project, delete_agent=True)

        assert code == teardown.EXIT_OK
        (call,) = [call for call in project.spawn.calls if DEPROVISION(call)]
        assert call.argv[2:] == ["--agent-user-upn", UPN]
        # The Blueprint is gone, and its certificate list with it.
        assert project.ran(REMOVE_BLUEPRINT_CREDENTIAL) == []
        assert "every computer" in out  # said before, not after

    def test_then_retires_this_computers_setup_certificate(self, tmp_path: Path) -> None:
        project = make_project(tmp_path, certs=[blueprint_cert(), provisioner_cert()])
        project.spawn.on(DEPROVISION, Reply(0))
        project.spawn.on(REMOVE_PROVISIONER_CREDENTIAL, result(0, removed=1, kept=0))

        code, _ = tear_down(project, delete_agent=True)

        assert code == teardown.EXIT_OK
        (removal,) = project.ran(REMOVE_PROVISIONER_CREDENTIAL)
        assert removal.argv[-1] == PROV_SHA1
        assert (PROV_SHA1, True) in project.store.deleted
        # It needs the setup certificate to delete the agent, so the agent goes first.
        order = [call for call in project.spawn.calls if DEPROVISION(call) or removal is call]
        assert DEPROVISION(order[0]) and order[-1] is removal

    def test_the_setup_certificate_is_kept_while_another_agent_here_needs_it(
        self, project: Project
    ) -> None:
        code, out = tear_down(project, delete_agent=True)  # OTHER_AGENT_CERT is in the store

        assert code == teardown.EXIT_OK
        assert project.ran(REMOVE_PROVISIONER_CREDENTIAL) == []
        assert project.store.find_cert(PROV_SHA1) is not None
        assert "another Entrabot agent" in out

    def test_an_agent_that_cant_be_deleted_keeps_what_a_retry_needs(self, project: Project) -> None:
        project.spawn.on(
            DEPROVISION,
            Reply(1, stdout="ERROR: Blueprint has 1 other Agent Identity object(s); refusing\n"),
        )

        code, out = tear_down(project, delete_agent=True)

        assert code == teardown.EXIT_INCOMPLETE
        assert "refusing" in out  # the helper's reason reaches the screen
        # This computer is still disconnected ...
        assert len(project.ran(REMOVE_BLUEPRINT_CREDENTIAL)) == 1
        assert (OLD_SHA1, True) in project.store.deleted
        # ... but the settings and setup certificate a second attempt needs are kept.
        assert project.env_file.exists() and project.paths.state_file.exists()
        assert project.store.find_cert(PROV_SHA1) is not None
        assert "was NOT deleted" in out

    def test_cloud_memory_is_never_deleted_and_the_command_is_given(self, tmp_path: Path) -> None:
        lines = [line for line in ENV_LINES if "KEEP_MEMORY_LOCAL" not in line] + [
            "ENTRABOT_BLOB_ENDPOINT=https://entclawabc.blob.core.windows.net",
            "ENTRABOT_BLOB_CONTAINER=agent-123",
        ]
        project = make_project(tmp_path, env_lines=lines)
        project.spawn.on(DEPROVISION, Reply(0))
        project.spawn.on(REMOVE_PROVISIONER_CREDENTIAL, result(0, removed=1, kept=0))

        _, out = tear_down(project, delete_agent=True)

        assert [c for c in project.spawn.calls if script("deprovision_blob_storage.py")(c)] == []
        command = "deprovision_blob_storage.py --storage-account entclawabc --container agent-123"
        assert command in out

    def test_an_unknown_agent_cant_be_deleted(self, tmp_path: Path) -> None:
        lines = [line for line in ENV_LINES if "AGENT_UPN" not in line]
        project = make_project(tmp_path, env_lines=lines, state={})

        code, out = tear_down(project, delete_agent=True)

        assert code == teardown.EXIT_USAGE
        assert project.env_file.exists()
        assert project.store.deleted == []


class TestPurgeData:
    def test_the_data_folder_is_only_deleted_when_asked(self, project: Project) -> None:
        tear_down(project)

        assert (project.paths.data_root / "interactions.jsonl").exists()

    def test_purge_deletes_the_data_and_keeps_the_python_it_is_running_from(
        self, project: Project
    ) -> None:
        data = project.paths.data_root
        running = data / "python" / "3.12.10" / "tools"
        running.mkdir(parents=True)
        (running / "python.exe").write_text("", encoding="utf-8")
        (data / "tools" / "azure-cli").mkdir(parents=True)
        (data / "tools" / "azure-cli" / "pyvenv.cfg").write_text("", encoding="utf-8")

        code, out = tear_down(project, purge_data=True, running_from=[running])

        assert code == teardown.EXIT_OK
        assert not (data / "interactions.jsonl").exists()
        assert not (data / "tools").exists()
        assert (running / "python.exe").exists()
        assert "private copy of Python" in out
        assert ".venv" in out  # what the person has to know about the project's environment

    def test_purge_is_announced_in_the_plan(self, project: Project) -> None:
        _, out = tear_down(project, purge_data=True, running_from=[])

        assert out.index("data folder") < out.index("Removed this computer's certificate")
