"""Certificate rotation and teardown on macOS and Linux.

``test_rotation.py`` and ``test_teardown.py`` hold the behaviour; they run as Windows. Here:
the same commands against the Keychain or keyring store, where the setup (Provisioner)
certificate is its own key store item per tenant, and with every message naming this system's
commands and places.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from entrabot.winsetup import rotation, teardown
from tests.winsetup.fakes import Reply, make_console, script
from tests.winsetup.maintenance_fakes import (
    BP_OBJECT,
    ENV_LINES,
    NEW_SHA1,
    NEW_X5T,
    OLD_SHA1,
    PROV_SHA1,
    PROVE_CERTIFICATE,
    REMOVE_BLUEPRINT_CREDENTIAL,
    REMOVE_PROVISIONER_CREDENTIAL,
    TENANT,
    FakeStore,
    Project,
    StoreCert,
    blueprint_cert,
    make_project,
    provisioner_cert,
    result,
    sleeper,
)
from tests.winsetup.test_wizard_posix import WINDOWS_WORDS

REGISTER = script("register_windows_blueprint_cert.py")
OTHER_AGENT_CERT = StoreCert(
    sha1_hex="D" * 40,
    x5t_s256="d" * 43,
    friendly_name="Entrabot Blueprint 99999999-2222-3333-4444-555555555555",
    ksp="keychain",
)


@dataclass
class FakeKeyStore(FakeStore):
    """``entrabot.platform.keycert`` in memory: Blueprint certificates are listed, and the
    setup certificate is a separate item found by tenant."""

    setup: dict[str, StoreCert] = field(default_factory=dict)  # by tenant ID
    asked_for_tenants: list[str] = field(default_factory=list)
    # The account's older single slot holds a key that is none of this agent's certificates'.
    someone_elses_older_key: bool = False
    older_key_error: str | None = None
    older_key_asked_about: list[list[str]] = field(default_factory=list)

    older_key: str | None = None  # what the account's older single slot holds

    def legacy_private_key_pem(self) -> str | None:
        return self.older_key

    def older_key_is_someone_elses(self, sha1_hexes: list[str]) -> bool:
        self.older_key_asked_about.append(list(sha1_hexes))
        if self.older_key_error:
            raise self.WinCertError(self.older_key_error)
        return self.someone_elses_older_key

    def setup_certs(self, tenant_id: str) -> list[StoreCert]:
        self.asked_for_tenants.append(tenant_id)
        cert = self.setup.get(tenant_id)
        if cert is None:
            return []
        self.certs[cert.sha1_hex] = cert  # reachable by thumbprint from here on
        return [cert]

    def list_certs(self, *, friendly_name: str | None = None) -> list[StoreCert]:
        return [
            cert
            for cert in super().list_certs(friendly_name=friendly_name)
            if cert.sha1_hex not in {item.sha1_hex for item in self.setup.values()}
        ]


@pytest.fixture(params=["darwin", "linux"])
def project(request: pytest.FixtureRequest, tmp_path: Path) -> Project:
    ksp = "keychain" if request.param == "darwin" else "keyring"
    store = FakeKeyStore(created_ksp=ksp, setup={TENANT: provisioner_cert()})
    project = make_project(
        tmp_path, platform=request.param, store=store, certs=[blueprint_cert(ksp=ksp)]
    )
    project.spawn.on(REGISTER, Reply(0))
    project.spawn.on(PROVE_CERTIFICATE, result(0, ok=True))
    project.spawn.on(REMOVE_BLUEPRINT_CREDENTIAL, result(0, removed=1, kept=1))
    project.spawn.on(REMOVE_PROVISIONER_CREDENTIAL, result(0, removed=1, kept=1))
    return project


def _key_store(project: Project) -> str:
    return "macOS Keychain" if project.world.platform == "darwin" else "Linux keyring"


def rotate(project: Project, *, yes: bool = True) -> tuple[int, str]:
    console, out = make_console(spawn=project.spawn, assume_yes=yes)
    sleep, _ = sleeper()
    code = rotation.run_rotate_cert(console, project.paths, project.world, sleep=sleep)
    console.close()
    return code, out.getvalue()


def tear_down(project: Project, *, yes: bool = True, **options: object) -> tuple[int, str]:
    console, out = make_console(spawn=project.spawn, assume_yes=yes)
    code = teardown.run_teardown(console, project.paths, project.world, **options)
    console.close()
    return code, out.getvalue()


class TestRotation:
    def test_rotates_and_names_this_systems_key_store(self, project: Project) -> None:
        code, out = rotate(project)

        assert code == rotation.EXIT_OK, out
        values = project.env_values()
        assert values["ENTRABOT_BLUEPRINT_CERT_SHA1"] == NEW_SHA1
        assert values["ENTRABOT_BLUEPRINT_CERT_THUMBPRINT"] == NEW_X5T
        assert project.store.deleted == [(OLD_SHA1, True)]
        assert f"key is kept in the {_key_store(project)}" in out
        assert f"Sign-in key:      {_key_store(project)}" in out
        assert WINDOWS_WORDS.findall(out) == []

    def test_the_new_certificate_is_registered_from_the_key_store_by_thumbprint(
        self, project: Project
    ) -> None:
        rotate(project)

        (register,) = [call for call in project.spawn.calls if REGISTER(call)]
        assert register.argv[-4:] == ["--blueprint-object-id", BP_OBJECT, "--sha1", NEW_SHA1]

    def test_without_a_terminal_it_asks_for_the_posix_flag(self, project: Project) -> None:
        code, out = rotate(project, yes=False)

        assert code == rotation.EXIT_USAGE
        assert "add --yes." in out and "-Yes" not in out

    def test_a_computer_set_up_before_the_wizard_is_sent_to_setup(self, tmp_path: Path) -> None:
        # No SHA-1 in .env: setup gives the agent's key its own item first.
        lines = [line for line in ENV_LINES if "CERT_SHA1" not in line]
        project = make_project(tmp_path, platform="darwin", store=FakeKeyStore(), env_lines=lines)

        code, out = rotate(project)

        assert code == rotation.EXIT_FAILED
        assert "Run ./scripts/setup.sh" in out and project.store.created == []

    def test_cleanup_left_over_names_the_command_that_finishes_it(self, project: Project) -> None:
        project.spawn.on(REMOVE_BLUEPRINT_CREDENTIAL, result(1, problem="Graph answered 503"))

        code, out = rotate(project)

        assert code == rotation.EXIT_LEFTOVERS
        assert "./scripts/setup.sh --rotate-cert" in out
        assert WINDOWS_WORDS.findall(out) == []


class TestTeardown:
    def test_disconnecting_retires_the_setup_certificate_found_by_tenant(
        self, project: Project
    ) -> None:
        code, out = tear_down(project)

        assert code == teardown.EXIT_OK, out
        assert project.store.asked_for_tenants == [TENANT]
        (removal,) = project.ran(REMOVE_PROVISIONER_CREDENTIAL)
        assert removal.argv[-1] == PROV_SHA1
        assert (OLD_SHA1, True) in project.store.deleted
        assert (PROV_SHA1, True) in project.store.deleted
        assert "Retired this computer's setup certificate" in out
        assert WINDOWS_WORDS.findall(out) == []

    def test_another_agent_on_this_account_keeps_the_setup_certificate(
        self, project: Project
    ) -> None:
        project.store.certs[OTHER_AGENT_CERT.sha1_hex] = OTHER_AGENT_CERT

        code, out = tear_down(project)

        assert code == teardown.EXIT_OK, out
        assert project.ran(REMOVE_PROVISIONER_CREDENTIAL) == []
        assert (PROV_SHA1, True) not in project.store.deleted
        assert project.store.find_cert(OTHER_AGENT_CERT.sha1_hex) is not None
        assert "another Entrabot agent on this computer" in out

    def test_an_agent_from_before_the_wizard_on_this_account_keeps_it_too(
        self, project: Project
    ) -> None:
        # That agent has no certificate in the store: only its key, in the older single slot.
        project.store.someone_elses_older_key = True

        code, out = tear_down(project)

        assert code == teardown.EXIT_OK, out
        assert project.store.older_key_asked_about == [[OLD_SHA1]]
        assert project.ran(REMOVE_PROVISIONER_CREDENTIAL) == []
        assert (PROV_SHA1, True) not in project.store.deleted
        assert (OLD_SHA1, True) in project.store.deleted  # this agent's own still goes
        assert "another Entrabot agent on this computer" in out

    def test_when_that_cannot_be_checked_the_setup_certificate_is_not_retired(
        self, project: Project
    ) -> None:
        project.store.older_key_error = "the keyring is locked"

        code, out = tear_down(project)

        assert code == teardown.EXIT_INCOMPLETE
        assert project.ran(REMOVE_PROVISIONER_CREDENTIAL) == []
        assert (PROV_SHA1, True) not in project.store.deleted
        assert "could not be looked up" in out

    def test_a_computer_set_up_before_the_wizard_is_sent_to_setup_first(
        self, tmp_path: Path
    ) -> None:
        # No SHA-1 in .env and a key in the account's older slot: teardown can't know whose
        # it is, and must not call the computer disconnected while that key can still sign.
        lines = [line for line in ENV_LINES if "CERT_SHA1" not in line]
        store = FakeKeyStore(older_key="-----BEGIN PRIVATE KEY-----")
        project = make_project(tmp_path, platform="darwin", store=store, env_lines=lines, certs=[])

        code, out = tear_down(project)

        assert code == teardown.EXIT_INCOMPLETE
        assert "nothing was changed" in out and "Run ./scripts/setup.sh once" in out
        assert project.paths.env_file.exists() and project.paths.state_file.exists()
        assert project.spawn.calls == [] and project.store.deleted == []

    def test_settings_without_a_certificate_and_no_older_key_are_just_removed(
        self, tmp_path: Path
    ) -> None:
        lines = [line for line in ENV_LINES if "CERT_SHA1" not in line]
        project = make_project(
            tmp_path, platform="linux", store=FakeKeyStore(), env_lines=lines, certs=[]
        )

        code, out = tear_down(project)

        assert code == teardown.EXIT_OK, out
        assert not project.paths.env_file.exists()

    def test_keeping_it_is_said_with_the_posix_flag(self, project: Project) -> None:
        code, out = tear_down(project, keep_setup_credential=True)

        assert code == teardown.EXIT_OK, out
        assert "--keep-setup-credential" in out and "-KeepSetupCredential" not in out
        assert "Keychain Access" in out or "keyring manager" in out
        assert "certmgr" not in out

    def test_the_agent_still_exists_and_setup_is_named(self, project: Project) -> None:
        _, out = tear_down(project)

        assert "run ./scripts/setup.sh." in out

    def test_a_certificate_that_cannot_be_deleted_says_where_to_delete_it(
        self, project: Project
    ) -> None:
        project.store.delete_error = "the keyring is locked"

        code, out = tear_down(project)

        assert code == teardown.EXIT_INCOMPLETE
        assert _key_store(project) in out
        assert "entrabot-blueprint-cert" in out and "certmgr" not in out

    def test_without_a_terminal_it_asks_for_the_posix_flag(self, project: Project) -> None:
        code, out = tear_down(project, yes=False)

        assert code == teardown.EXIT_USAGE
        assert "add --yes." in out

    def test_deleting_the_agent_without_a_sign_in_names_the_posix_command(
        self, tmp_path: Path
    ) -> None:
        store = FakeKeyStore(created_ksp="keychain")
        project = make_project(tmp_path, platform="darwin", store=store, signed_in=False)

        code, out = tear_down(project, delete_agent=True)

        assert code == teardown.EXIT_INCOMPLETE
        assert "--delete-agent" in out and "-DeleteAgent" not in out
        assert "Run ./scripts/setup.sh" in out
