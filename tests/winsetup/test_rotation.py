"""Certificate rotation: the old certificate stays in use until the new one is proven.

The rotation this replaces could not succeed (it removed the old certificate in the same
write that added the new one, tested the wrong certificate, and used a token that may not
write the Blueprint). These tests pin the order that makes rotation safe, and what is left
behind, exactly, when a step fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from entrabot.winsetup import rotation
from entrabot.winsetup.console import RunRequest
from tests.winsetup.fakes import FakeWindowsAcl, Reply, make_console, script
from tests.winsetup.maintenance_fakes import (
    BP_APP,
    BP_OBJECT,
    ENV_LINES,
    NEW_SHA1,
    NEW_X5T,
    NOW,
    OLD_SHA1,
    OLD_X5T,
    PROVE_CERTIFICATE,
    REMOVE_BLUEPRINT_CREDENTIAL,
    Project,
    blueprint_cert,
    make_project,
    result,
    sleeper,
)

REGISTER = script("register_windows_blueprint_cert.py")
ACCEPTED = result(0, ok=True)
NOT_YET = result(
    1,
    ok=False,
    retry=True,
    hop="hop1:blueprint",
    error="invalid_client",
    description="AADSTS700027: The certificate with identifier used to sign the client "
    f"assertion is not registered on application. Thumbprint of key used by client: '{NEW_SHA1}'",
)


def rotate(project: Project, *, typed: str | None = None, yes: bool = True, **options: object):
    console, out = make_console(
        spawn=project.spawn,
        typed=typed,
        assume_yes=yes,
        log_file=project.paths.data_root / "logs" / "rotate.log",
    )
    sleep, waits = sleeper()
    code = rotation.run_rotate_cert(console, project.paths, project.world, sleep=sleep, **options)
    console.close()
    return code, out.getvalue(), waits


def healthy(project: Project) -> None:
    project.spawn.on(REGISTER, Reply(0))
    project.spawn.on(PROVE_CERTIFICATE, ACCEPTED)
    project.spawn.on(REMOVE_BLUEPRINT_CREDENTIAL, result(0, removed=1, kept=1))


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return make_project(tmp_path)


class TestHappyPath:
    def test_rotates_to_a_new_certificate_and_retires_the_old_one(self, project: Project) -> None:
        healthy(project)

        code, out, _ = rotate(project)

        assert code == rotation.EXIT_OK
        values = project.env_values()
        assert values["ENTRABOT_BLUEPRINT_CERT_SHA1"] == NEW_SHA1
        assert values["ENTRABOT_BLUEPRINT_CERT_THUMBPRINT"] == NEW_X5T
        assert values["ENTRABOT_BLUEPRINT_KSP"] == "tpm"
        assert rotation.RETIRING_KEY not in values
        assert project.store.find_cert(NEW_SHA1) is not None
        assert project.store.find_cert(OLD_SHA1) is None
        assert (OLD_SHA1, True) in project.store.deleted  # deleted WITH its private key

    def test_the_new_certificate_follows_setups_naming(self, project: Project) -> None:
        healthy(project)

        rotate(project)

        assert project.store.created == [
            {
                "subject": f"CN=Entrabot Blueprint {BP_APP[:8]}",
                "friendly_name": f"Entrabot Blueprint {BP_APP}",
                "days": 365,
            }
        ]

    def test_it_is_registered_additively_from_the_certificate_store(self, project: Project) -> None:
        healthy(project)

        rotate(project)

        (register,) = [call for call in project.spawn.calls if REGISTER(call)]
        assert register.argv[2:] == ["--blueprint-object-id", BP_OBJECT, "--sha1", NEW_SHA1]

    def test_only_the_old_certificate_is_removed_from_the_blueprint(self, project: Project) -> None:
        healthy(project)

        rotate(project)

        (removal,) = project.ran(REMOVE_BLUEPRINT_CREDENTIAL)
        assert removal.argv[-3:-1] == [BP_OBJECT, OLD_SHA1]

    def test_the_removal_is_told_the_new_certificate_must_stay(self, project: Project) -> None:
        # Removal writes back the list it read. Told which certificate was just added, it
        # refuses to write from a read that does not show it yet (blueprint_certs.must_keep).
        healthy(project)

        rotate(project)

        (removal,) = project.ran(REMOVE_BLUEPRINT_CREDENTIAL)
        assert removal.argv[-3:] == [BP_OBJECT, OLD_SHA1, NEW_SHA1]

    def test_other_settings_are_kept(self, project: Project) -> None:
        healthy(project)

        rotate(project)

        kept = project.env_values()
        for line in ENV_LINES:
            key, value = line.split("=", 1)
            if "CERT" not in key and "KSP" not in key:
                assert kept[key] == value

    def test_tells_the_person_to_restart_and_reports_expiry(self, project: Project) -> None:
        healthy(project)

        _, out, _ = rotate(project)

        assert "65 days left" in out  # the certificate being replaced
        assert "11 Nov 2027" in out  # the new one: a year from now
        assert "hardware TPM" in out
        assert "Restart" in out and "Claude Code" in out

    def test_a_software_key_is_reported_with_the_reason(self, project: Project) -> None:
        healthy(project)
        project.store.tpm_reason = "this computer has no usable TPM"

        _, out, _ = rotate(project)

        assert project.env_values()["ENTRABOT_BLUEPRINT_KSP"] == "software"
        assert "software key store" in out
        assert "no usable TPM" in out

    def test_no_full_thumbprint_on_screen_or_in_the_log(self, project: Project) -> None:
        healthy(project)

        _, out, _ = rotate(project)

        log = (project.paths.data_root / "logs" / "rotate.log").read_text(encoding="utf-8")
        for text in (out, log):
            assert OLD_SHA1 not in text
            assert NEW_SHA1 not in text

    def test_the_settings_file_stays_private_when_it_is_rewritten(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Every write of .env replaces the file, so every write has to lock the new one
        # before it lands (envfile.update_env). Rotation writes it twice: the switch, and
        # clearing the note about the certificate being retired.
        healthy(project)
        acl = FakeWindowsAcl()
        acl.install(monkeypatch)

        rotate(project)

        locks = [i for i, event in enumerate(acl.events) if event.startswith("lock ")]
        replaces = [i for i, event in enumerate(acl.events) if event == "replace .env"]
        assert len(replaces) == 2
        assert len(locks) == 2
        assert all(lock < put for lock, put in zip(locks, replaces, strict=True))
        assert acl.acls[str(project.env_file)] == f"D:P(A;;0x1301bf;;;{acl.sid})"

    def test_a_settings_file_that_cant_be_locked_is_reported_not_fatal(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The file holds IDs, not secrets. Stopping at the switch would strand a
        # certificate that has just been proven.
        healthy(project)
        acl = FakeWindowsAcl(fail_set="Access is denied.")
        acl.install(monkeypatch)

        code, out, _ = rotate(project)

        assert code == rotation.EXIT_OK
        assert project.env_values()["ENTRABOT_BLUEPRINT_CERT_SHA1"] == NEW_SHA1
        assert "Access is denied" in out


class TestOldCertificateStaysInUseUntilTheNewOneIsProven:
    def test_settings_and_old_certificate_are_untouched_while_proving(
        self, project: Project
    ) -> None:
        healthy(project)
        seen: dict[str, object] = {}

        def prove(request: RunRequest) -> Reply:
            seen["sha1_in_env_file"] = project.env_values()["ENTRABOT_BLUEPRINT_CERT_SHA1"]
            seen["old_cert_present"] = project.store.find_cert(OLD_SHA1) is not None
            seen["removals"] = len(project.ran(REMOVE_BLUEPRINT_CREDENTIAL))
            seen["child_env"] = dict(request.env or {})
            return ACCEPTED

        project.spawn.on(PROVE_CERTIFICATE, prove)

        rotate(project)

        assert seen["sha1_in_env_file"] == OLD_SHA1
        assert seen["old_cert_present"] is True
        assert seen["removals"] == 0

    def test_the_proof_signs_with_the_new_certificate(self, project: Project) -> None:
        healthy(project)
        project.env["ENTRABOT_BLUEPRINT_CERT_SHA1"] = "stale-value-in-the-shell"
        envs: list[dict[str, str]] = []

        def prove(request: RunRequest) -> Reply:
            envs.append(dict(request.env or {}))
            return ACCEPTED

        project.spawn.on(PROVE_CERTIFICATE, prove)

        rotate(project)

        (env,) = envs
        assert env["ENTRABOT_BLUEPRINT_CERT_SHA1"] == NEW_SHA1
        assert env["ENTRABOT_BLUEPRINT_CERT_THUMBPRINT"] == NEW_X5T
        assert env["ENTRABOT_BLUEPRINT_KSP"] == "tpm"
        # Everything else comes from .env, not from whatever the calling shell had set.
        assert "ENTRABOT_BLUEPRINT_APP_ID" not in env

    def test_waits_for_entra_to_accept_the_new_certificate(self, project: Project) -> None:
        healthy(project)
        replies = [NOT_YET, NOT_YET, ACCEPTED]
        project.spawn.on(PROVE_CERTIFICATE, lambda request: replies.pop(0))

        code, out, waits = rotate(project, proof_delay_seconds=10.0)

        assert code == rotation.EXIT_OK
        assert waits == [10.0, 10.0]
        assert "AADSTS700027" in out  # the person sees why it is waiting
        assert "attempt 2 of" in out
        assert project.env_values()["ENTRABOT_BLUEPRINT_CERT_SHA1"] == NEW_SHA1

    def test_a_later_sign_in_step_failing_does_not_fail_the_certificate(
        self, project: Project
    ) -> None:
        # Hop 1 is the only step the Blueprint certificate signs. If it passed, the
        # certificate is proven; hops 2 and 3 fail the same way with the old one.
        healthy(project)
        project.spawn.on(
            PROVE_CERTIFICATE,
            result(
                1,
                ok=False,
                retry=True,
                hop="hop3:agent_user",
                error="invalid_grant",
                description="AADSTS50076: something about the agent user",
            ),
        )

        code, out, waits = rotate(project)

        assert code == rotation.EXIT_OK
        assert waits == []
        assert project.env_values()["ENTRABOT_BLUEPRINT_CERT_SHA1"] == NEW_SHA1
        assert "-Diagnose" in out


class TestFailureBeforeTheSwitchChangesNothing:
    def _assert_untouched(self, project: Project, before: bytes) -> None:
        assert project.env_file.read_bytes() == before
        assert project.store.find_cert(OLD_SHA1) is not None
        assert project.store.find_cert(NEW_SHA1) is None

    def test_a_certificate_entra_never_accepts_is_undone(self, project: Project) -> None:
        healthy(project)
        project.spawn.on(PROVE_CERTIFICATE, NOT_YET)
        before = project.env_file.read_bytes()

        code, out, waits = rotate(project, proof_attempts=4, proof_delay_seconds=5.0)

        assert code == rotation.EXIT_FAILED
        assert len(project.ran(PROVE_CERTIFICATE)) == 4  # bounded
        assert waits == [5.0, 5.0, 5.0]
        self._assert_untouched(project, before)
        (removal,) = project.ran(REMOVE_BLUEPRINT_CREDENTIAL)
        assert removal.argv[-2:] == [BP_OBJECT, NEW_SHA1]  # the NEW one comes back off
        assert (NEW_SHA1, True) in project.store.deleted
        assert "still in use" in out

    def test_a_refused_registration_leaves_nothing_behind(self, project: Project) -> None:
        healthy(project)
        project.spawn.on(REGISTER, Reply(1, stderr="ERROR: Graph refused the update with 403"))
        before = project.env_file.read_bytes()

        code, out, _ = rotate(project)

        assert code == rotation.EXIT_FAILED
        self._assert_untouched(project, before)
        assert project.ran(REMOVE_BLUEPRINT_CREDENTIAL) == []  # nothing was registered
        assert project.ran(PROVE_CERTIFICATE) == []
        assert "403" in out  # the helper's own error is on screen

    def test_an_unconfirmed_registration_is_taken_back_off_the_blueprint(
        self, project: Project
    ) -> None:
        healthy(project)
        project.spawn.on(REGISTER, Reply(3, stderr="ERROR: could not confirm"))
        before = project.env_file.read_bytes()

        code, _, _ = rotate(project)

        assert code == rotation.EXIT_FAILED
        self._assert_untouched(project, before)
        (removal,) = project.ran(REMOVE_BLUEPRINT_CREDENTIAL)
        assert removal.argv[-1] == NEW_SHA1

    def test_when_the_new_certificate_cant_be_taken_off_the_blueprint_it_says_so(
        self, project: Project
    ) -> None:
        healthy(project)
        project.spawn.on(PROVE_CERTIFICATE, NOT_YET)
        project.spawn.on(
            REMOVE_BLUEPRINT_CREDENTIAL, result(1, problem="Graph refused the update with 403")
        )

        code, out, _ = rotate(project, proof_attempts=1)

        assert code == rotation.EXIT_FAILED
        # Its private key is gone, so it can't be used; it is only still listed.
        assert (NEW_SHA1, True) in project.store.deleted
        assert "still listed on the Blueprint" in out
        assert "2E3F" in out  # enough to find it in the Entra admin center, not the whole thing

    def test_a_certificate_that_cant_be_created_changes_nothing(self, project: Project) -> None:
        healthy(project)
        project.store.create_error = "the key store refused"
        before = project.env_file.read_bytes()

        code, out, _ = rotate(project)

        assert code == rotation.EXIT_FAILED
        assert project.env_file.read_bytes() == before
        assert "the key store refused" in out
        assert [call for call in project.spawn.calls if REGISTER(call)] == []

    def test_ctrl_c_while_waiting_undoes_the_new_certificate(self, project: Project) -> None:
        healthy(project)

        def interrupted(request: RunRequest) -> Reply:
            raise KeyboardInterrupt

        project.spawn.on(PROVE_CERTIFICATE, interrupted)
        before = project.env_file.read_bytes()

        code, out, _ = rotate(project)

        assert code == rotation.EXIT_CANCELLED
        self._assert_untouched(project, before)
        assert len(project.ran(REMOVE_BLUEPRINT_CREDENTIAL)) == 1


class TestFailureAfterTheSwitchIsFinishedByRunningAgain:
    def test_an_old_certificate_still_on_the_blueprint_is_reported_exactly(
        self, project: Project
    ) -> None:
        healthy(project)
        project.spawn.on(
            REMOVE_BLUEPRINT_CREDENTIAL, result(1, problem="Graph refused the update with 403")
        )

        code, out, _ = rotate(project)

        assert code == rotation.EXIT_LEFTOVERS
        values = project.env_values()
        assert values["ENTRABOT_BLUEPRINT_CERT_SHA1"] == NEW_SHA1  # the agent is on the new one
        assert values[rotation.RETIRING_KEY] == OLD_SHA1  # remembered for the next run
        # Its private key is deleted regardless, so the listed certificate is unusable.
        assert (OLD_SHA1, True) in project.store.deleted
        assert "working on the new certificate" in out
        assert "old certificate is still listed on the Blueprint" in out
        assert "again" in out

    def test_running_again_finishes_the_cleanup_without_a_new_certificate(
        self, tmp_path: Path
    ) -> None:
        lines = [line for line in ENV_LINES if "CERT" not in line] + [
            f"ENTRABOT_BLUEPRINT_CERT_THUMBPRINT={NEW_X5T}",
            f"ENTRABOT_BLUEPRINT_CERT_SHA1={NEW_SHA1}",
            f"{rotation.RETIRING_KEY}={OLD_SHA1}",
        ]
        project = make_project(
            tmp_path,
            env_lines=lines,
            certs=[blueprint_cert(NEW_SHA1, NEW_X5T), blueprint_cert(OLD_SHA1, OLD_X5T)],
        )
        healthy(project)

        code, out, _ = rotate(project)

        assert code == rotation.EXIT_OK
        assert project.store.created == []
        (removal,) = project.ran(REMOVE_BLUEPRINT_CREDENTIAL)
        # The certificate the agent is on now is the one that must stay.
        assert removal.argv[-3:] == [BP_OBJECT, OLD_SHA1, NEW_SHA1]
        assert project.store.find_cert(OLD_SHA1) is None
        assert project.store.find_cert(NEW_SHA1) is not None
        assert rotation.RETIRING_KEY not in project.env_values()
        assert "cleanup" in out.lower()

    def test_a_list_that_does_not_show_the_new_certificate_is_never_written_back(
        self, project: Project
    ) -> None:
        # What the helper reports when Microsoft Entra's read has not caught up.
        healthy(project)
        project.spawn.on(
            REMOVE_BLUEPRINT_CREDENTIAL,
            result(
                1,
                problem="1 certificate(s) that must stay on the Blueprint were not on the list "
                "Microsoft Graph returned after 18 read(s). Nothing was changed.",
            ),
        )

        code, out, _ = rotate(project)

        assert code == rotation.EXIT_LEFTOVERS
        assert project.env_values()["ENTRABOT_BLUEPRINT_CERT_SHA1"] == NEW_SHA1
        assert project.env_values()[rotation.RETIRING_KEY] == OLD_SHA1
        assert "Nothing was changed" in out
        assert "Run rotation again" in out

    def test_a_private_key_that_could_not_be_deleted_is_never_reported_as_deleted(
        self, project: Project
    ) -> None:
        healthy(project)
        project.store.key_stuck = {OLD_SHA1}

        code, out, _ = rotate(project)

        assert code == rotation.EXIT_LEFTOVERS
        assert "Deleted the old certificate and its private key" not in out
        assert "private key could not be deleted" in out
        assert "certutil" in out  # how to remove it by hand
        assert "Microsoft Platform Crypto Provider" in out  # the old key was in the TPM
        # The certificate is gone, so running again has nothing to find: don't suggest it,
        # and don't leave a note behind that a second run would quietly clear.
        assert "Run rotation again" not in out
        assert rotation.RETIRING_KEY not in project.env_values()

    def test_a_certificate_kept_because_its_key_could_not_go_is_left_for_a_retry(
        self, project: Project
    ) -> None:
        # What entrabot.platform.wincert does today: the certificate stays with its key, so
        # running again has something to find, and the note in .env must stay for it.
        healthy(project)
        project.store.key_stuck = {OLD_SHA1}
        project.store.key_stuck_keeps_cert = True

        code, out, _ = rotate(project)

        assert code == rotation.EXIT_LEFTOVERS
        assert "Deleted the old certificate and its private key" not in out
        assert project.store.find_cert(OLD_SHA1) is not None
        assert project.env_values()[rotation.RETIRING_KEY] == OLD_SHA1
        assert "Run rotation again" in out

    def test_an_unused_new_key_that_could_not_be_deleted_is_said_too(
        self, project: Project
    ) -> None:
        healthy(project)
        project.spawn.on(PROVE_CERTIFICATE, NOT_YET)
        project.store.key_stuck = {NEW_SHA1}

        code, out, _ = rotate(project, proof_attempts=1)

        assert code == rotation.EXIT_FAILED
        assert "private key could not be deleted" in out
        assert "certutil" in out

    def test_an_old_certificate_that_cant_be_deleted_locally_is_remembered(
        self, project: Project
    ) -> None:
        healthy(project)
        project.store.delete_error = "access denied"

        code, out, _ = rotate(project)

        assert code == rotation.EXIT_LEFTOVERS
        assert project.env_values()[rotation.RETIRING_KEY] == OLD_SHA1
        assert "access denied" in out


class TestWhenRotationIsNotTheRightTool:
    def test_a_computer_that_was_never_set_up(self, tmp_path: Path) -> None:
        project = make_project(tmp_path, env_lines=[])

        code, out, _ = rotate(project)

        assert code == rotation.EXIT_FAILED
        assert "setup-windows.cmd" in out
        assert project.store.created == []

    def test_no_certificate_configured(self, tmp_path: Path) -> None:
        project = make_project(tmp_path, env_lines=[x for x in ENV_LINES if "CERT" not in x])

        code, out, _ = rotate(project)

        assert code == rotation.EXIT_FAILED
        assert "setup-windows.cmd" in out
        assert project.store.created == []

    def test_configured_certificate_missing_from_the_store_points_to_setup(
        self, tmp_path: Path
    ) -> None:
        project = make_project(tmp_path, certs=[])

        code, out, _ = rotate(project)

        assert code == rotation.EXIT_FAILED
        assert "not in this computer's certificate store" in out
        assert "setup-windows.cmd" in out
        assert project.store.created == []

    def test_not_signed_in(self, tmp_path: Path) -> None:
        project = make_project(tmp_path, signed_in=False)

        code, out, _ = rotate(project)

        assert code == rotation.EXIT_FAILED
        assert "signed in" in out
        assert project.store.created == []

    def test_no_azure_cli(self, tmp_path: Path) -> None:
        project = make_project(tmp_path, with_az=False)

        code, out, _ = rotate(project)

        assert code == rotation.EXIT_FAILED
        assert "Azure CLI" in out
        assert project.store.created == []

    def test_an_expired_certificate_can_still_be_rotated(self, tmp_path: Path) -> None:
        from datetime import timedelta

        expired = blueprint_cert(not_after=NOW - timedelta(days=3))
        project = make_project(tmp_path, certs=[expired])
        healthy(project)

        code, out, _ = rotate(project)

        assert code == rotation.EXIT_OK
        assert "expired" in out
        assert project.env_values()["ENTRABOT_BLUEPRINT_CERT_SHA1"] == NEW_SHA1

    def test_unattended_without_yes_is_refused(self, project: Project) -> None:
        # No terminal and no -Yes: nobody agreed to this. Teardown refuses in the same
        # state; rotation used to take the question's default and go ahead.
        healthy(project)
        before = project.env_file.read_bytes()

        code, out, _ = rotate(project, yes=False)

        assert code == rotation.EXIT_USAGE == 2
        assert "-Yes" in out and "--yes" in out
        assert project.env_file.read_bytes() == before
        assert project.store.created == []
        assert project.spawn.ran("register_windows_blueprint_cert.py") == []

    def test_finishing_an_earlier_rotations_cleanup_needs_no_new_yes(self, tmp_path: Path) -> None:
        # The rotation itself was agreed to when it ran; this only finishes it.
        lines = [line for line in ENV_LINES if "CERT" not in line] + [
            f"ENTRABOT_BLUEPRINT_CERT_THUMBPRINT={NEW_X5T}",
            f"ENTRABOT_BLUEPRINT_CERT_SHA1={NEW_SHA1}",
            f"{rotation.RETIRING_KEY}={OLD_SHA1}",
        ]
        project = make_project(
            tmp_path,
            env_lines=lines,
            certs=[blueprint_cert(NEW_SHA1, NEW_X5T), blueprint_cert(OLD_SHA1, OLD_X5T)],
        )
        healthy(project)

        code, _, _ = rotate(project, yes=False)

        assert code == rotation.EXIT_OK

    def test_declining_changes_nothing(self, project: Project) -> None:
        healthy(project)
        before = project.env_file.read_bytes()

        code, out, _ = rotate(project, typed="n\n", yes=False)

        assert code == rotation.EXIT_CANCELLED
        assert project.env_file.read_bytes() == before
        assert project.store.created == []
        assert "Nothing was changed" in out
