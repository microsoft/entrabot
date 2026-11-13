"""Teardown: safe by default, explicit about its effect, and precise about what is left.

The default disconnects THIS computer and nothing else. The old script matched certificates
by subject (deleting other agents' certificates), left this computer's certificate trusted
on the Blueprint until it expired, left its private key behind, and deleted the data folder
without being asked. Each of those is pinned here.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from entrabot.winsetup import teardown
from entrabot.winsetup.console import RunRequest
from tests.winsetup.fakes import Reply, make_console, script
from tests.winsetup.maintenance_fakes import (
    BP_APP,
    BP_OBJECT,
    ENV_LINES,
    NEW_SHA1,
    NEW_X5T,
    OLD_PROV_EXPIRES,
    OLD_PROV_SHA1,
    OLD_SHA1,
    OLD_X5T,
    PROV_SHA1,
    REMOVE_BLUEPRINT_CREDENTIAL,
    REMOVE_BLUEPRINT_CREDENTIALS_IN_ONE_WRITE,
    REMOVE_PROVISIONER_CREDENTIAL,
    REVOKE_OLD_SETUP_CERTIFICATE,
    REVOKE_PENDING,
    STATE,
    UPN,
    Project,
    StoreCert,
    blueprint_cert,
    make_project,
    provisioner_cert,
    result,
)

SETUP_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "entra_provisioning.py"

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


def revoked(*outcomes: str, problem: str = "") -> Reply:
    """The revocation child's answer: one outcome per certificate, in the order given."""
    return result(
        0,
        results=[
            {"outcome": outcome, **({"problem": problem} if outcome == "failed" else {})}
            for outcome in outcomes
        ],
    )


def setup_script(monkeypatch: pytest.MonkeyPatch, state_file: Path) -> Any:
    """scripts/entra_provisioning.py, loaded the way setup runs it, reading *state_file*."""
    spec = importlib.util.spec_from_file_location("entra_provisioning", SETUP_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "entra_provisioning", module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_STATE_FILE", state_file)
    return module


INSUFFICIENT = "Entra did not remove certificate ...C0DE: Insufficient privileges to complete"


class TestAnOldSetupCertificateStillToBeRevoked:
    """Setup moves an older install off its plaintext Provisioner key file: it takes that key's
    certificate off the Provisioner app and deletes the file. When the first part fails it
    notes the certificate in the state file (PROVISIONER_CERT_REVOKE_PENDING) and tries again
    on its next run. Teardown removes the state file, so without handling it the note was
    simply lost: the old certificate, whose key may survive in a backup, stayed trusted on an
    app that can manage applications in the tenant until it expired, and nothing would retry.
    """

    @pytest.fixture
    def pending(self, tmp_path: Path) -> Project:
        project = make_project(
            tmp_path,
            certs=[blueprint_cert(), provisioner_cert()],
            state={**STATE, **REVOKE_PENDING},
        )
        project.spawn.on(REMOVE_BLUEPRINT_CREDENTIAL, result(0, removed=1, kept=1))
        project.spawn.on(REMOVE_PROVISIONER_CREDENTIAL, result(0, removed=1, kept=1))
        project.spawn.on(REVOKE_OLD_SETUP_CERTIFICATE, revoked("removed"))
        return project

    def test_it_is_said_in_the_plan_before_the_question(self, pending: Project) -> None:
        code, out = tear_down(pending, typed="n\n", yes=False)

        assert code == teardown.EXIT_CANCELLED
        plan = out[: out.index("Disconnect this computer?")]
        assert "old setup (Provisioner) certificate" in plan
        assert "C0DE" in plan and OLD_PROV_EXPIRES in plan  # exactly the one the note holds
        assert "plaintext" in plan  # and why it matters
        assert pending.ran(REVOKE_OLD_SETUP_CERTIFICATE) == []

    def test_it_is_removed_from_the_provisioner_app_and_reported(self, pending: Project) -> None:
        code, out = tear_down(pending)

        assert code == teardown.EXIT_OK
        (call,) = pending.ran(REVOKE_OLD_SETUP_CERTIFICATE)
        assert call.argv[-2:] == ["prov-app", OLD_PROV_SHA1]  # the app, then the certificate
        report = out[out.index("This computer is disconnected") :]
        removed = report[report.index("Removed:") :]
        assert "old setup (Provisioner) certificate" in removed
        assert "C0DE" in removed and OLD_PROV_EXPIRES in removed
        assert not pending.paths.state_file.exists()

    def test_it_runs_with_setups_own_retry_through_the_sign_in(self, pending: Project) -> None:
        tear_down(pending)

        (call,) = pending.ran(REVOKE_OLD_SETUP_CERTIFICATE)
        assert call.argv[1] == "-c" and "_revoke_app_certificate" in call.argv[2]
        assert call.argv[3] == str(pending.paths.scripts)
        assert OLD_PROV_SHA1 not in call.argv[2]  # a value, never program text
        assert call.env is not None and call.env.get("PYTHONUTF8") == "1"

    def test_it_comes_before_the_setup_certificate_and_the_note_go(self, pending: Project) -> None:
        seen: dict[str, bool] = {}

        def revoke(request: RunRequest) -> Reply:
            seen["note"] = pending.paths.state_file.exists()
            seen["setup_certificate"] = pending.store.find_cert(PROV_SHA1) is not None
            seen["setup_certificate_on_app"] = not pending.ran(REMOVE_PROVISIONER_CREDENTIAL)
            return revoked("removed")

        pending.spawn.on(REVOKE_OLD_SETUP_CERTIFICATE, revoke)

        tear_down(pending)

        assert seen == {"note": True, "setup_certificate": True, "setup_certificate_on_app": True}
        assert len(pending.ran(REMOVE_PROVISIONER_CREDENTIAL)) == 1  # then it is retired

    def test_one_already_gone_from_the_app_is_settled_too(self, pending: Project) -> None:
        pending.spawn.on(REVOKE_OLD_SETUP_CERTIFICATE, revoked("not_registered"))

        code, out = tear_down(pending)

        assert code == teardown.EXIT_OK
        assert "was no longer on the Provisioner app" in out
        assert "Still to do" not in out

    def test_one_that_cannot_be_removed_is_left_to_do_exactly(self, pending: Project) -> None:
        pending.spawn.on(REVOKE_OLD_SETUP_CERTIFICATE, revoked("failed", problem=INSUFFICIENT))

        code, out = tear_down(pending)

        assert code == teardown.EXIT_INCOMPLETE
        todo = out[out.index("Still to do:") :]
        assert "C0DE" in todo and f"expires {OLD_PROV_EXPIRES}" in todo
        assert "Insufficient privileges" in todo  # why
        # Where, as the provisioning script itself says it.
        assert "App registrations > 'EntraBot Agent ID Provisioner' (application ID prov-app)" in (
            todo
        )
        assert "Certificates & secrets > Certificates" in todo
        assert "plaintext" in todo
        report = out[out.index("Teardown finished") :]
        assert "C0DE" not in report[report.index("Removed:") : report.index("Still to do:")]
        # The local cleanup still completes, the setup certificate included.
        assert not pending.env_file.exists() and not pending.paths.state_file.exists()
        assert (OLD_SHA1, True) in pending.store.deleted
        assert (PROV_SHA1, True) in pending.store.deleted

    def test_without_a_sign_in_it_is_left_to_do_and_nothing_is_run(self, tmp_path: Path) -> None:
        project = make_project(
            tmp_path,
            signed_in=False,
            certs=[blueprint_cert(), provisioner_cert()],
            state={**STATE, **REVOKE_PENDING},
        )

        code, out = tear_down(project)

        assert code == teardown.EXIT_INCOMPLETE
        assert project.ran(REVOKE_OLD_SETUP_CERTIFICATE) == []
        todo = out[out.index("Still to do:") :]
        assert "C0DE" in todo and "not signed in" in todo
        assert "Certificates & secrets > Certificates" in todo

    def test_a_child_that_dies_claims_nothing(self, pending: Project) -> None:
        pending.spawn.on(
            REVOKE_OLD_SETUP_CERTIFICATE,
            Reply(1, stderr="Traceback (most recent call last):\nRuntimeError: boom"),
        )

        code, out = tear_down(pending)

        assert code == teardown.EXIT_INCOMPLETE
        todo = out[out.index("Still to do:") :]
        assert "C0DE" in todo and "boom" in todo

    def test_a_folder_with_no_record_of_the_provisioner_app_says_so(self, tmp_path: Path) -> None:
        state = {**STATE, **REVOKE_PENDING}
        del state["PROVISIONER_CLIENT_ID"]
        project = make_project(tmp_path, certs=[blueprint_cert()], state=state)
        project.spawn.on(REMOVE_BLUEPRINT_CREDENTIAL, result(0, removed=1, kept=0))

        code, out = tear_down(project)

        assert code == teardown.EXIT_INCOMPLETE
        assert project.ran(REVOKE_OLD_SETUP_CERTIFICATE) == []
        todo = out[out.index("Still to do:") :]
        assert "no record of the Provisioner app" in todo
        assert "App registrations > 'EntraBot Agent ID Provisioner' >" in todo

    def test_several_are_tried_in_one_child_and_each_is_reported(self, tmp_path: Path) -> None:
        other = "9" * 36 + "BEEF"
        state = {
            **STATE,
            "PROVISIONER_CERT_REVOKE_PENDING": f"{OLD_PROV_SHA1.lower()},{other.lower()}",
            "PROVISIONER_CERT_REVOKE_PENDING_EXPIRES": f"{OLD_PROV_EXPIRES},",
        }
        project = make_project(tmp_path, certs=[blueprint_cert(), provisioner_cert()], state=state)
        project.spawn.on(REMOVE_BLUEPRINT_CREDENTIAL, result(0, removed=1, kept=1))
        project.spawn.on(REMOVE_PROVISIONER_CREDENTIAL, result(0, removed=1, kept=1))
        project.spawn.on(
            REVOKE_OLD_SETUP_CERTIFICATE,
            result(0, results=[{"outcome": "removed"}, {"outcome": "failed", "problem": "403"}]),
        )

        code, out = tear_down(project)

        assert code == teardown.EXIT_INCOMPLETE
        (call,) = project.ran(REVOKE_OLD_SETUP_CERTIFICATE)
        assert call.argv[-2:] == [OLD_PROV_SHA1, other]
        report = out[out.index("Teardown finished") :]
        assert "C0DE" in report[report.index("Removed:") : report.index("Still to do:")]
        todo = report[report.index("Still to do:") :]
        assert "BEEF" in todo and "expiry date unknown" in todo and "C0DE" not in todo

    def test_no_note_means_no_extra_call(self, project: Project) -> None:
        code, out = tear_down(project)

        assert code == teardown.EXIT_OK
        assert project.ran(REVOKE_OLD_SETUP_CERTIFICATE) == []
        assert "old setup" not in out

    def test_no_full_thumbprint_on_screen_or_in_the_log(self, pending: Project) -> None:
        pending.spawn.on(REVOKE_OLD_SETUP_CERTIFICATE, revoked("failed", problem=INSUFFICIENT))

        _, out = tear_down(pending)

        log = (pending.paths.data_root / "logs" / "teardown.log").read_text(encoding="utf-8")
        for text in (out, log):
            assert OLD_PROV_SHA1 not in text and OLD_PROV_SHA1.lower() not in text

    def test_this_computers_own_setup_certificate_is_never_taken_for_an_old_one(
        self, tmp_path: Path
    ) -> None:
        # A note naming the certificate this computer signs in with is not setup's: setup
        # never removes the certificate it uses, and teardown doesn't either.
        state = {
            **STATE,
            "PROVISIONER_CERT_REVOKE_PENDING": f"{PROV_SHA1.lower()},{OLD_PROV_SHA1.lower()}",
            "PROVISIONER_CERT_REVOKE_PENDING_EXPIRES": f"2027-06-01,{OLD_PROV_EXPIRES}",
        }
        project = make_project(tmp_path, certs=[blueprint_cert(), provisioner_cert()], state=state)
        project.spawn.on(REMOVE_BLUEPRINT_CREDENTIAL, result(0, removed=1, kept=1))
        project.spawn.on(REVOKE_OLD_SETUP_CERTIFICATE, revoked("removed"))

        code, _ = tear_down(project, keep_setup_credential=True)

        assert code == teardown.EXIT_OK
        (call,) = project.ran(REVOKE_OLD_SETUP_CERTIFICATE)
        assert call.argv[-2:] == ["prov-app", OLD_PROV_SHA1]
        assert project.store.find_cert(PROV_SHA1) is not None

    def test_a_failed_agent_deletion_keeps_the_note_with_the_settings(
        self, pending: Project
    ) -> None:
        pending.spawn.on(DEPROVISION, Reply(1, stdout="ERROR: refusing\n"))

        code, out = tear_down(pending, delete_agent=True)

        assert code == teardown.EXIT_INCOMPLETE
        assert pending.ran(REVOKE_OLD_SETUP_CERTIFICATE) == []  # tried with the deletion
        state = json.loads(pending.paths.state_file.read_text(encoding="utf-8"))
        assert state["PROVISIONER_CERT_REVOKE_PENDING"] == OLD_PROV_SHA1.lower()
        kept = out[out.index("Kept:") :]
        assert "C0DE" in kept

    def test_the_child_program_is_valid_python_that_matches_setups_function(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Nothing else runs this program before it reaches a Windows machine.
        import ast
        import inspect

        from entrabot.winsetup import directory

        program = directory.REVOKE_OLD_SETUP_CERTIFICATES_SNIPPET
        assert REVOKE_OLD_SETUP_CERTIFICATE in program
        assert REMOVE_PROVISIONER_CREDENTIAL not in program  # told apart from that one
        tree = ast.parse(program)
        call = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "_revoke_app_certificate"
        )
        script_module = setup_script(monkeypatch, tmp_path / ".entrabot-state.json")
        inspect.signature(script_module._revoke_app_certificate).bind(*call.args)
        assert call.keywords == []  # no keep_sha1: see Directory.revoke_old_setup_certificates
        assert issubclass(
            script_module.ProvisionerCertificateLostError, script_module.ProvisionerBootstrapError
        )
        assert "ProvisionerBootstrapError" in program


class TestKeepingTheSetupCredentialKeepsTheNote:
    """-KeepSetupCredential is for someone about to set up again, and setup is what retries
    an old certificate's removal. It finds the note in the state file, which teardown removes,
    so when the removal does not work now the state file is left holding only that note."""

    @pytest.fixture
    def kept(self, tmp_path: Path) -> Project:
        project = make_project(
            tmp_path,
            certs=[blueprint_cert(), provisioner_cert()],
            state={**STATE, **REVOKE_PENDING},
        )
        project.spawn.on(REMOVE_BLUEPRINT_CREDENTIAL, result(0, removed=1, kept=1))
        project.spawn.on(REVOKE_OLD_SETUP_CERTIFICATE, revoked("failed", problem=INSUFFICIENT))
        return project

    def test_the_state_file_is_left_with_only_what_setups_retry_needs(self, kept: Project) -> None:
        code, out = tear_down(kept, keep_setup_credential=True)

        assert code == teardown.EXIT_INCOMPLETE
        # The note, and which tenant's Provisioner app it is about.
        assert json.loads(kept.paths.state_file.read_text(encoding="utf-8")) == {
            "TENANT_ID": STATE["TENANT_ID"],
            "PROVISIONER_CLIENT_ID": "prov-app",
            "PROVISIONER_CERT_REVOKE_PENDING": OLD_PROV_SHA1.lower(),
            "PROVISIONER_CERT_REVOKE_PENDING_EXPIRES": OLD_PROV_EXPIRES,
        }
        # Everything else went, and a copy of the whole file is kept as usual.
        (backup,) = backups(kept, ".entrabot-state.json")
        assert json.loads(backup.read_text(encoding="utf-8")) == {**STATE, **REVOKE_PENDING}
        assert not kept.env_file.exists()
        report = out[out.index("Teardown finished") :]
        kept_items = report[report.index("Kept:") : report.index("Still to do:")]
        assert ".entrabot-state.json" in kept_items and "C0DE" in kept_items
        assert "next run" in kept_items
        todo = report[report.index("Still to do:") :]
        assert "Certificates & secrets > Certificates" in todo  # and how to do it by hand

    def test_setup_reads_that_note_back_as_its_own(
        self, kept: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tear_down(kept, keep_setup_credential=True)
        setup = setup_script(monkeypatch, kept.paths.state_file)

        assert setup._pending_revocations() == [(OLD_PROV_SHA1, OLD_PROV_EXPIRES)]
        assert setup.get_state("PROVISIONER_CLIENT_ID") == "prov-app"  # the app it retries on
        # The setup certificate is not recorded, so setup uploads the kept one again rather
        # than trusting that it is still on the app; and no agent is named, so the next
        # setup is not taken for a repair of the one this computer was disconnected from.
        assert setup.get_state("PROVISIONER_CERT_THUMBPRINT") is None
        assert setup.get_state("BLUEPRINT_APP_ID") is None
        assert setup.get_state("AGENT_USER_UPN") is None

    def test_teardown_reads_the_note_under_setups_own_names(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        setup = setup_script(monkeypatch, tmp_path / ".entrabot-state.json")

        assert teardown.REVOKE_PENDING_KEY == setup._REVOKE_PENDING
        assert teardown.REVOKE_PENDING_EXPIRES_KEY == setup._REVOKE_PENDING_EXPIRES

    def test_a_removal_that_works_leaves_no_state_file(self, kept: Project) -> None:
        kept.spawn.on(REVOKE_OLD_SETUP_CERTIFICATE, revoked("removed"))

        code, _ = tear_down(kept, keep_setup_credential=True)

        assert code == teardown.EXIT_OK
        assert not kept.paths.state_file.exists()

    def test_only_what_is_still_pending_is_kept(self, tmp_path: Path) -> None:
        other = "9" * 36 + "BEEF"
        state = {
            **STATE,
            "PROVISIONER_CERT_REVOKE_PENDING": f"{OLD_PROV_SHA1.lower()},{other.lower()}",
            "PROVISIONER_CERT_REVOKE_PENDING_EXPIRES": f"{OLD_PROV_EXPIRES},2027-05-05",
        }
        project = make_project(tmp_path, certs=[blueprint_cert(), provisioner_cert()], state=state)
        project.spawn.on(REMOVE_BLUEPRINT_CREDENTIAL, result(0, removed=1, kept=1))
        project.spawn.on(
            REVOKE_OLD_SETUP_CERTIFICATE,
            result(0, results=[{"outcome": "removed"}, {"outcome": "failed", "problem": "403"}]),
        )

        tear_down(project, keep_setup_credential=True)

        note = json.loads(project.paths.state_file.read_text(encoding="utf-8"))
        assert note["PROVISIONER_CERT_REVOKE_PENDING"] == other.lower()
        assert note["PROVISIONER_CERT_REVOKE_PENDING_EXPIRES"] == "2027-05-05"

    def test_without_a_sign_in_the_note_is_kept_too(self, tmp_path: Path) -> None:
        project = make_project(
            tmp_path,
            signed_in=False,
            certs=[blueprint_cert(), provisioner_cert()],
            state={**STATE, **REVOKE_PENDING},
        )

        code, _ = tear_down(project, keep_setup_credential=True)

        assert code == teardown.EXIT_INCOMPLETE
        note = json.loads(project.paths.state_file.read_text(encoding="utf-8"))
        assert note["PROVISIONER_CERT_REVOKE_PENDING"] == OLD_PROV_SHA1.lower()

    def test_the_plan_says_the_note_would_stay(self, kept: Project) -> None:
        _, out = tear_down(kept, keep_setup_credential=True, typed="n\n", yes=False)

        plan = out[: out.index("Disconnect this computer?")]
        assert "tries again on its next run" in plan

    def test_retiring_the_setup_credential_does_not_keep_it(self, kept: Project) -> None:
        # Nobody is about to set up again here: the report is the record, and the backup.
        kept.spawn.on(REMOVE_PROVISIONER_CREDENTIAL, result(0, removed=1, kept=1))

        code, _ = tear_down(kept)

        assert code == teardown.EXIT_INCOMPLETE
        assert not kept.paths.state_file.exists()

    def test_teardown_run_again_retries_from_the_note_and_then_retires_the_credential(
        self, kept: Project
    ) -> None:
        tear_down(kept, keep_setup_credential=True)  # the removal fails: the note stays
        kept.spawn.on(REVOKE_OLD_SETUP_CERTIFICATE, revoked("removed"))
        kept.spawn.on(REMOVE_PROVISIONER_CREDENTIAL, result(0, removed=1, kept=1))

        code, out = tear_down(kept)  # later, without -KeepSetupCredential

        assert code == teardown.EXIT_OK
        assert len(kept.ran(REVOKE_OLD_SETUP_CERTIFICATE)) == 2
        assert kept.ran(REVOKE_OLD_SETUP_CERTIFICATE)[-1].argv[-2:] == ["prov-app", OLD_PROV_SHA1]
        # The note says which tenant, so this computer's setup certificate is found and retired.
        (retired,) = kept.ran(REMOVE_PROVISIONER_CREDENTIAL)
        assert retired.argv[-1] == PROV_SHA1
        assert kept.store.find_cert(PROV_SHA1) is None
        assert not kept.paths.state_file.exists()
        assert "Retired this computer's setup certificate" in out


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
        # Both in ONE write (see TestBothCertificatesGoInOneWrite for why).
        (removal,) = project.ran(REMOVE_BLUEPRINT_CREDENTIAL)
        assert removal.argv[-3:] == [BP_OBJECT, NEW_SHA1, OLD_SHA1]
        assert sorted(project.store.deleted) == sorted([(NEW_SHA1, True), (OLD_SHA1, True)])
        assert "earlier certificate rotation" in out
        plan = out[: out.index("Removed this computer's certificate from the agent's Blueprint")]
        assert "earlier certificate rotation" in plan
        report = out[out.index("This computer is disconnected") :]
        assert "This computer's certificate, from the agent's Blueprint" in report
        assert "rotation left behind (ending 01D1), from the agent's Blueprint" in report

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


def _half_rotated_project(tmp_path: Path) -> Project:
    lines = [line for line in ENV_LINES if "CERT" not in line] + [
        f"ENTRABOT_BLUEPRINT_CERT_THUMBPRINT={NEW_X5T}",
        f"ENTRABOT_BLUEPRINT_CERT_SHA1={NEW_SHA1}",
        f"ENTRABOT_BLUEPRINT_CERT_RETIRING_SHA1={OLD_SHA1}",
    ]
    return make_project(
        tmp_path,
        env_lines=lines,
        certs=[blueprint_cert(NEW_SHA1, NEW_X5T), blueprint_cert(OLD_SHA1, OLD_X5T)],
    )


class TestBothCertificatesGoInOneWrite:
    """Microsoft Graph replaces the whole certificate list on every write, and a read can
    land on a replica that has not caught up. Two removals in a row are two read-then-write
    cycles: the second read could still show the certificate the first removed, and its
    write would put that certificate back while both report success. So teardown removes
    this computer's certificate and a rotation's leftover in ONE write, and reports only
    what that write's read-back confirmed."""

    def test_one_child_one_write_for_both(self, tmp_path: Path) -> None:
        project = _half_rotated_project(tmp_path)
        project.spawn.on(REMOVE_BLUEPRINT_CREDENTIAL, result(0, removed=2, kept=1))

        code, _ = tear_down(project)

        assert code == teardown.EXIT_OK
        (removal,) = project.ran(REMOVE_BLUEPRINT_CREDENTIALS_IN_ONE_WRITE)
        assert project.ran(REMOVE_BLUEPRINT_CREDENTIAL) == [removal]
        assert removal.argv[-3:] == [BP_OBJECT, NEW_SHA1, OLD_SHA1]

    def test_a_write_that_could_not_be_confirmed_is_never_reported_as_done(
        self, tmp_path: Path
    ) -> None:
        project = _half_rotated_project(tmp_path)
        project.spawn.on(
            REMOVE_BLUEPRINT_CREDENTIAL,
            result(
                3,
                problem="Could not confirm the certificate removal after 18 read(s). 1 of the "
                "2 certificates being removed is still listed on the Blueprint.",
            ),
        )

        code, out = tear_down(project)

        assert code == teardown.EXIT_INCOMPLETE
        assert "Removed this computer's certificate from the agent's Blueprint" not in out
        assert "Removed the old certificate an earlier certificate rotation left behind" not in out
        assert "no longer listed" not in out
        report = out[out.index("Teardown finished") :]
        removed = report[report.index("Removed:") : report.index("Still to do:")]
        assert "Blueprint" not in removed  # neither is claimed
        todo = report[report.index("Still to do:") :]
        assert todo.count("still listed on the agent's Blueprint") == 2
        assert "2E3F" in todo and "01D1" in todo
        assert "still listed on the Blueprint" in todo  # the helper's own reason
        # The keys are gone either way, so what is still listed can't be used.
        assert sorted(project.store.deleted) == sorted([(NEW_SHA1, True), (OLD_SHA1, True)])
        assert todo.count("cannot be used") == 2

    def test_one_that_was_already_gone_is_said_so(self, tmp_path: Path) -> None:
        project = _half_rotated_project(tmp_path)
        project.spawn.on(REMOVE_BLUEPRINT_CREDENTIAL, result(0, removed=1, kept=1, not_listed=[1]))

        code, out = tear_down(project)

        assert code == teardown.EXIT_OK
        assert "Removed this computer's certificate from the agent's Blueprint" in out
        assert "was no longer listed on the agent's Blueprint" in out
        assert "Removed the old certificate an earlier certificate rotation left behind" not in out

    def test_the_child_program_is_valid_python_that_matches_the_helper(self) -> None:
        # Nothing else runs this program before it reaches a Windows machine.
        import ast
        import inspect

        from entrabot.winsetup import directory
        from tests.scripts._blueprint_cert_helpers import blueprint_certs

        program = directory.REMOVE_BLUEPRINT_CREDENTIALS_SNIPPET
        assert REMOVE_BLUEPRINT_CREDENTIAL in program  # it is a Blueprint removal ...
        assert REMOVE_BLUEPRINT_CREDENTIALS_IN_ONE_WRITE in program  # ... of several at once
        tree = ast.parse(program)
        call = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and ast.unparse(node.func) == "blueprint_certs.remove_key_credentials"
        )
        keywords = {keyword.arg: None for keyword in call.keywords}
        # Teardown has added no certificate, so it names none that must stay.
        assert set(keywords) == {"object_id", "token", "thumbprints"}
        inspect.signature(blueprint_certs.remove_key_credentials).bind(**keywords)
        assert "result.not_listed" in program
        assert "not_listed" in blueprint_certs.RemovalResult.__dataclass_fields__
        for name in ("KeyCredentialsReadError", "RegistrationRejectedError"):
            assert f"blueprint_certs.{name}" in program
            assert hasattr(blueprint_certs, name)


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

    def test_purge_takes_the_tools_an_earlier_setup_installed(self, project: Project) -> None:
        # An earlier version of setup installed these for Word, Excel and OneDrive files, under
        # the data folder and with nothing on PATH: deleting the folders is all there is.
        data = project.paths.data_root
        for folder in ("dotnet", "a365"):
            (data / "tools" / folder).mkdir(parents=True)
            (data / "tools" / folder / f"{folder}.exe").write_text("", encoding="utf-8")

        code, out = tear_down(project, purge_data=True, running_from=[])

        assert code == teardown.EXIT_OK
        assert not (data / "tools").exists()
        plan = out[: out.index("Removed this computer's certificate")]
        assert (
            "the .NET SDK and the Microsoft Agent 365 CLI if an earlier version of setup "
            "installed them" in " ".join(plan.split())
        )
