"""The same wizard on macOS and Linux, end to end, with the outside world faked.

``test_wizard.py`` holds the wizard's behaviour; it runs as Windows. This file is about what
differs elsewhere: the commands and flags messages name, where the agent's key is kept (the
account's Keychain or keyring, checked before anything changes), a computer that was set up
before the wizard existed, and how the agent is started when setup is done.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from entrabot.winsetup.console import RunRequest
from entrabot.winsetup.envfile import read_env
from entrabot.winsetup.helpers import RESULT_MARKER, Paths
from tests.winsetup.fakes import Reply, script, snippet, words
from tests.winsetup.test_certstep import NEW_SHA1, NEW_X5T, FakeWinCert, StoreCert, WinCertError
from tests.winsetup.test_wizard import BP_APP, HOOK_OUTPUT, REPO_ROOT, TENANT, Scenario

OLDER_KEY = "-----BEGIN PRIVATE KEY-----\nolder\n-----END PRIVATE KEY-----\n"
OLDER_PUBLIC = b"older-public-key"
OLDER_DER = b"\x30\x82older-certificate"
OLDER_SHA1 = "C" * 40
OLDER_X5T = "c" * 43

# Nothing a person on macOS or Linux is told may name a Windows command, flag or place.
WINDOWS_WORDS = re.compile(
    r"setup-windows|deploy-windows|teardown-windows|prereqs-windows|\.cmd\b|\.ps1\b|PowerShell"
    r"|cmd\.exe|\bthis PC\b|\bTPM\b|Credential Manager|Git for Windows|Double-click"
    r"|-(?:Diagnose|UseBlueprint|NewChain|UpnSuffix|Sponsor|Tenant|Yes|SkipSmoke)\b"
)


@dataclass
class FakeKeyStore(FakeWinCert):
    """The macOS and Linux certificate store (``entrabot.platform.keycert``), in memory."""

    ksp: str = "keychain"
    store_error: str | None = None  # the account's key store can't be used at all
    older_key: str | None = None  # an older setup's key, in the account's single slot
    imported: list[dict[str, object]] = field(default_factory=list)

    def list_certs(self, *, friendly_name: str | None = None) -> list[StoreCert]:
        if self.store_error:
            raise WinCertError(self.store_error)
        return list(self.store.values())

    def create_signing_cert(
        self, *, subject: str, friendly_name: str, days: int = 365, prefer_tpm: bool = True
    ) -> tuple[StoreCert, str | None]:
        if self.create_error:
            raise WinCertError(self.create_error)
        self.created.append({"subject": subject, "friendly_name": friendly_name, "days": days})
        cert = StoreCert(
            sha1_hex=NEW_SHA1,
            x5t_s256=NEW_X5T,
            der=b"\x01\x02\x03",
            subject=subject,
            friendly_name=friendly_name,
            ksp=self.ksp,
        )
        self.store[NEW_SHA1] = cert
        return cert, None

    def legacy_private_key_pem(self) -> str | None:
        return self.older_key

    def public_key_der(self, private_key_pem: str) -> bytes:
        assert private_key_pem == OLDER_KEY
        return OLDER_PUBLIC

    def import_cert(self, *, private_key_pem: str, der: bytes, friendly_name: str) -> StoreCert:
        self.imported.append({"key": private_key_pem, "der": der, "friendly_name": friendly_name})
        cert = StoreCert(
            sha1_hex=OLDER_SHA1,
            x5t_s256=OLDER_X5T,
            der=der,
            friendly_name=friendly_name,
            ksp=self.ksp,
        )
        self.store[OLDER_SHA1] = cert
        return cert


def _program(request: RunRequest) -> str:
    """The program a request starts, by its file name. These tests also run on Windows, where
    the folders they make have backslash paths: matching on "/bash" found nothing there, the
    hooks and the Claude Code version came back empty, and setup did not offer to start the
    agent."""
    return re.split(r"[\\/]", request.argv[0])[-1]


@dataclass
class PosixScenario(Scenario):
    platform: str = "darwin"
    os_text: str = "Darwin 25.1.0 (Darwin Kernel Version 25.1.0), arm64"
    wincert: FakeKeyStore = field(default_factory=FakeKeyStore)
    # What the check for an older key's certificate answers: "found", "not-this-blueprint",
    # or "could-not-check".
    older_key_answer: str = "found"
    older_key_checks: list[RunRequest] = field(default_factory=list)

    def paths(self) -> Paths:
        return Paths(
            self.root, self.root / ".venv" / "bin" / "python", self.tmp_path / "home" / ".entrabot"
        )

    def __post_init__(self) -> None:
        root = self.root
        (root / "scripts").mkdir(parents=True)
        shutil.copy(REPO_ROOT / "scripts" / "mcp_config.py", root / "scripts" / "mcp_config.py")
        (root / "scripts" / "entra_provisioning.py").write_text("", encoding="utf-8")
        launcher = root / "start-entrabot.sh"
        launcher.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        launcher.chmod(0o755)
        venv = root / ".venv" / "bin"
        venv.mkdir(parents=True)
        for name in ("python", "entrabot-mcp"):
            (venv / name).write_bytes(b"")
        (root / ".claude").mkdir()
        hook = {"type": "command", "command": "run-the-hook", "shell": "bash"}
        (root / ".claude" / "settings.json").write_text(
            json.dumps({"hooks": {"SessionStart": [{"hooks": [hook]}]}}), encoding="utf-8"
        )
        tools = self.tmp_path / "tools"
        tools.mkdir()
        for name in ("bash", "claude"):
            (tools / name).write_bytes(b"")
        self.env.update(
            {
                "USER": "alex",
                "HOME": str(self.tmp_path / "home"),
                "COPILOT_HOME": str(self.tmp_path / "copilot"),
                "PATH": "/usr/bin:/bin",
            }
        )
        if self.platform != "darwin":
            self.env["DISPLAY"] = ":0"
        self._rules()
        on = self.spawn.on
        on(lambda r: _program(r) == "bash", Reply(stdout=HOOK_OUTPUT))
        on(
            lambda r: _program(r) == "claude" and r.argv[1:] == ["--version"],
            lambda _r: Reply(stdout=self.claude_version + "\n"),
        )
        on(snippet("find_credential_for_public_key"), self._older_key_check)

    def _older_key_check(self, request: RunRequest) -> Reply:
        self.older_key_checks.append(request)
        if self.older_key_answer == "found":
            body = {"der": base64.b64encode(OLDER_DER).decode()}
            return Reply(stdout=RESULT_MARKER + json.dumps(body))
        if self.older_key_answer == "not-this-blueprint":
            return Reply(returncode=1)
        return Reply(returncode=2, stderr="could not read the Blueprint's certificates (503)")

    def which(self, name: str) -> str | None:
        if name == "az" and self.az_on_path:
            return "/usr/local/bin/az"
        tool = self.tmp_path / "tools" / name
        if name in ("claude", "bash") and tool.exists():
            return str(tool)
        return None

    def older_setup(self) -> None:
        """This computer as ``setup.sh`` left it before the wizard: the agent's IDs in the
        state file and ``.env``, the SHA-256 thumbprint but no SHA-1, and the key on its own
        in the account's single slot."""
        from tests.winsetup.test_wizard import CREATED_STATE

        self.state_file.write_text(json.dumps(CREATED_STATE), encoding="utf-8")
        self.env_file.write_text(
            "\n".join(
                [
                    f"ENTRABOT_TENANT_ID={TENANT}",
                    f"ENTRABOT_BLUEPRINT_APP_ID={BP_APP}",
                    "ENTRABOT_BLUEPRINT_OBJECT_ID=bp-object",
                    f"ENTRABOT_BLUEPRINT_CERT_THUMBPRINT={OLDER_X5T}",
                    "ENTRABOT_AGENT_ID=agent-app",
                    "ENTRABOT_AGENT_OBJECT_ID=agent-object",
                    "ENTRABOT_AGENT_USER_ID=agent-user",
                    "ENTRABOT_AGENT_USER_UPN=entrabot-agent-sati@contoso.com",
                    "ENTRABOT_HUMAN_USER_ID=admin-object",
                    "ENTRABOT_HUMAN_UPN=admin@contoso.com",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.wincert.older_key = OLDER_KEY

    @property
    def dotenv(self) -> dict[str, str]:
        return read_env(self.env_file)


@pytest.fixture(params=["darwin", "linux"])
def world(request: pytest.FixtureRequest, tmp_path: Path) -> PosixScenario:
    platform = request.param
    return PosixScenario(
        tmp_path,
        platform=platform,
        os_text=(
            "Darwin 25.1.0 (Darwin Kernel Version 25.1.0), arm64"
            if platform == "darwin"
            else "Linux 6.8.0-1017-azure (#20-Ubuntu SMP), x86_64"
        ),
        wincert=FakeKeyStore(ksp="keychain" if platform == "darwin" else "keyring"),
    )


def _key_store(world: PosixScenario) -> str:
    return "macOS Keychain" if world.platform == "darwin" else "Linux keyring"


class TestANewAgent:
    def test_a_fresh_machine_is_taken_all_the_way(self, world: PosixScenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out

        env = world.dotenv
        assert env["ENTRABOT_BLUEPRINT_APP_ID"] == BP_APP
        assert env["ENTRABOT_BLUEPRINT_CERT_SHA1"] == NEW_SHA1
        assert env["ENTRABOT_BLUEPRINT_CERT_THUMBPRINT"] == NEW_X5T
        assert world.wincert.created == [
            {
                "subject": f"CN=Entrabot Blueprint {BP_APP[:8]}",
                "friendly_name": f"Entrabot Blueprint {BP_APP}",
                "days": 365,
            }
        ]
        assert len(world.ran("register_windows_blueprint_cert.py")) == 1
        assert "Entrabot is set up" in world.out

    def test_nothing_on_screen_or_in_the_log_names_a_windows_command(
        self, world: PosixScenario
    ) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out

        assert WINDOWS_WORDS.findall(world.out) == []
        assert "./scripts/setup.sh --diagnose" in world.out
        assert "run ./scripts/setup.sh again" in world.out

    def test_the_key_store_is_named_where_the_key_is_mentioned(self, world: PosixScenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out

        store = _key_store(world)
        assert f"The agent's sign-in key will be kept in the {store}" in world.out
        assert f"Created a certificate whose key is kept in the {store}" in world.out
        assert f"Sign-in key:  {store}" in world.out

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_the_settings_file_is_only_the_accounts(self, world: PosixScenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out

        assert world.env_file.stat().st_mode & 0o077 == 0
        assert "only your account can read or change it" in world.out

    def test_the_last_screen_says_how_to_start_the_agent_from_a_terminal(
        self, world: PosixScenario
    ) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out

        assert "To start your agent:" in world.out
        assert "In a terminal, run:" in world.out
        assert str(world.root / "start-entrabot.sh") in world.out
        assert "claude --dangerously-load-development-channels server:entrabot" in world.out
        assert "Then say:  Who are you?" in world.out
        assert world.launched == []  # never started unattended


class TestTheKeyStoreIsCheckedFirst:
    def test_no_usable_key_store_stops_before_anything_is_asked_or_changed(
        self, world: PosixScenario
    ) -> None:
        world.wincert.store_error = (
            "This account has no secure key store Entrabot can use. Entrabot needs a Secret "
            "Service keyring that is running and unlocked."
        )

        assert world.run(new_chain=True, upn_suffix="sati") == 1

        assert world.spawn.calls == []  # no sign-in, no helper, nothing in the tenant
        assert not world.env_file.exists() and not world.state_file.exists()
        assert "Nothing was changed" in world.out
        assert "Secret Service keyring" in world.out
        assert _key_store(world) in world.out

    def test_windows_is_not_asked_for_one(self, tmp_path: Path) -> None:
        # The Windows key is created in the certificate step, as it always was.
        from tests.winsetup.test_wizard import Scenario as WindowsScenario

        windows = WindowsScenario(tmp_path)
        windows.acl.install(pytest.MonkeyPatch())
        assert windows.run(new_chain=True, upn_suffix="sati") == 0, windows.out
        assert "sign-in key will be kept" not in windows.out


class TestAComputerSetUpBeforeTheWizard:
    """``setup.sh`` used to keep one Blueprint key for the whole account and no certificate
    on the computer. The wizard keeps that agent signing in with the key it has."""

    def test_its_key_is_kept_and_stored_as_this_agents_own(self, world: PosixScenario) -> None:
        world.older_setup()

        assert world.run() == 0, world.out

        assert world.wincert.created == []  # no new key
        assert world.wincert.imported == [
            {"key": OLDER_KEY, "der": OLDER_DER, "friendly_name": f"Entrabot Blueprint {BP_APP}"}
        ]
        assert world.ran("register_windows_blueprint_cert.py") == []  # already on the Blueprint
        assert world.dotenv["ENTRABOT_BLUEPRINT_CERT_SHA1"] == OLDER_SHA1
        assert world.dotenv["ENTRABOT_BLUEPRINT_CERT_THUMBPRINT"] == OLDER_X5T
        assert "Kept this computer's existing sign-in key" in world.out

    def test_only_the_public_half_of_the_key_leaves_the_wizard(self, world: PosixScenario) -> None:
        world.older_setup()

        assert world.run() == 0, world.out

        (check,) = world.older_key_checks
        assert check.argv[-1] == OLDER_PUBLIC.hex()
        assert "bp-object" in check.argv
        assert OLDER_KEY not in " ".join(check.argv)
        assert OLDER_KEY not in world.out and OLDER_KEY not in world.log

    def test_a_key_that_is_another_agents_is_left_alone(self, world: PosixScenario) -> None:
        world.older_setup()
        world.older_key_answer = "not-this-blueprint"

        assert world.run() == 0, world.out

        assert world.wincert.imported == []
        assert len(world.wincert.created) == 1  # this agent gets its own
        assert world.wincert.older_key == OLDER_KEY
        assert world.wincert.deleted == []
        assert world.dotenv["ENTRABOT_BLUEPRINT_CERT_SHA1"] == NEW_SHA1

    def test_when_it_cannot_be_checked_nothing_is_generated(self, world: PosixScenario) -> None:
        world.older_setup()
        world.older_key_answer = "could-not-check"

        assert world.run() == 1

        assert world.wincert.created == [] and world.wincert.imported == []
        assert world.ran("register_windows_blueprint_cert.py") == []
        assert "Nothing was generated or changed" in world.out

    def test_settings_that_name_a_certificate_this_computer_lacks_also_keep_the_older_key(
        self, world: PosixScenario
    ) -> None:
        # Settings copied from another computer (or a key store item deleted by hand): .env
        # names a certificate, the store has none, and the account's older slot has the key.
        world.older_setup()
        with world.env_file.open("a", encoding="utf-8") as handle:
            handle.write(f"ENTRABOT_BLUEPRINT_CERT_SHA1={'9' * 40}\n")

        assert world.run() == 0, world.out

        assert world.wincert.created == [] and len(world.wincert.imported) == 1
        assert world.dotenv["ENTRABOT_BLUEPRINT_CERT_SHA1"] == OLDER_SHA1

    def test_a_computer_the_wizard_set_up_is_not_asked_about_an_older_key(
        self, world: PosixScenario
    ) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out
        world.wincert.older_key = OLDER_KEY  # some other agent's, in the account's slot
        world.older_key_checks.clear()

        assert world.run() == 0, world.out

        assert world.older_key_checks == [] and world.wincert.imported == []


class TestFlagsAreNamedAsSetupShTakesThem:
    def test_two_identity_modes_at_once(self, world: PosixScenario) -> None:
        assert world.run(new_chain=True, use_blueprint=BP_APP) == 2
        assert "(--new)" in world.out and "(--use-blueprint)" in world.out

    def test_a_missing_answer_names_the_flags_that_give_it(self, world: PosixScenario) -> None:
        assert world.run() == 2
        assert "--new --with-upn-suffix=NAME   or   --use-blueprint=APP_ID" in world.out
        assert "(--yes)" in world.out
        assert WINDOWS_WORDS.findall(world.out) == []

    def test_a_bad_value_names_its_flag(self, world: PosixScenario) -> None:
        assert world.run(use_blueprint="not-an-id") == 2
        assert "--use-blueprint 'not-an-id'" in world.out

    def test_storage_flags_without_cloud_memory(self, world: PosixScenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati", storage_account="acct") == 2
        assert "--use-cloud-memory" in world.out

    def test_a_blueprint_that_does_not_exist(self, world: PosixScenario) -> None:
        world.spawn.on(
            script("create_entra_agent_ids.py"),
            Reply(returncode=1, stdout=RESULT_MARKER + "blueprint-not-found\n"),
        )
        assert world.run(use_blueprint=BP_APP) == 1
        assert "(--use-blueprint=APP_ID)" in world.out
        assert "(--new --with-upn-suffix=NAME)" in world.out
        assert WINDOWS_WORDS.findall(world.out) == []


class TestSwitchUser:
    def test_a_working_sign_in_is_not_offered(self, world: PosixScenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati", switch_user=True) == 0, world.out

        assert any(words("login")(call) for call in world.spawn.calls)
        assert "asked to sign in as someone else" in world.out

    def test_without_it_the_sign_in_is_used(self, world: PosixScenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out
        assert not any(words("login")(call) for call in world.spawn.calls)


class TestReplacingEveryCertificate:
    def test_it_cannot_be_combined_with_a_new_agent(self, world: PosixScenario) -> None:
        code = world.run(new_chain=True, upn_suffix="sati", remove_all_existing_certs=True)
        assert code == 2
        assert "--new" in world.out and world.spawn.calls == []

    def test_unattended_the_flag_is_the_consent(self, world: PosixScenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out
        # This computer's own certificate, under another thumbprint than the fake creates next.
        mine = world.wincert.store.pop(NEW_SHA1)
        world.wincert.store[OLDER_SHA1] = replace(mine, sha1_hex=OLDER_SHA1, x5t_s256=OLDER_X5T)
        env = world.env_file.read_text("utf-8").replace(NEW_SHA1, OLDER_SHA1)
        world.env_file.write_text(env, encoding="utf-8")

        assert world.run(remove_all_existing_certs=True) == 0, world.out

        register = world.ran("register_windows_blueprint_cert.py")[-1]
        assert "--remove-all-existing-certs" in register.argv
        assert len(world.wincert.created) == 2  # a new one, although the old one still worked
        assert (OLDER_SHA1, True) in world.wincert.deleted
        assert world.dotenv["ENTRABOT_BLUEPRINT_CERT_SHA1"] == NEW_SHA1
        assert "It is now the only one there" in world.out
        assert "EVERY certificate" in world.out

    def test_at_a_keyboard_it_is_asked_and_the_default_is_no(self, world: PosixScenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out
        registered = len(world.ran("register_windows_blueprint_cert.py"))

        code = world.run(typed="\n" * 12, remove_all_existing_certs=True)

        assert code == 130
        assert "Replace them all? [y/N]" in world.out
        assert len(world.ran("register_windows_blueprint_cert.py")) == registered
        assert len(world.wincert.created) == 1

    def test_an_ordinary_run_never_passes_the_flag(self, world: PosixScenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out
        for call in world.ran("register_windows_blueprint_cert.py"):
            assert "--remove-all-existing-certs" not in call.argv


class TestStartingTheAgent:
    def test_interactively_the_terminal_is_handed_to_the_agent(self, world: PosixScenario) -> None:
        code = world.run(typed="\n" * 20, new_chain=True, upn_suffix="sati")

        assert code == 0, world.out
        assert "Start your agent now, in this terminal? [Y/n]" in world.out
        assert world.launched == [([str(world.root / "start-entrabot.sh")], world.root)]

    def test_saying_no_starts_nothing(self, world: PosixScenario) -> None:
        code = world.run(
            typed="\n" * 20, answers={"start_agent": "n"}, new_chain=True, upn_suffix="sati"
        )
        assert code == 0, world.out
        assert world.launched == []

    def test_a_launcher_that_is_not_executable_is_not_offered(self, world: PosixScenario) -> None:
        if sys.platform == "win32":
            pytest.skip("POSIX file modes")
        (world.root / "start-entrabot.sh").chmod(0o644)
        code = world.run(typed="\n" * 20, new_chain=True, upn_suffix="sati")
        assert code == 0, world.out
        assert "Start your agent now" not in world.out and world.launched == []

    def test_a_folder_with_a_space_in_its_name_is_quoted(self, tmp_path: Path) -> None:
        spaced = tmp_path / "My Projects"
        spaced.mkdir()
        world = PosixScenario(spaced)
        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out
        assert f"'{world.root / 'start-entrabot.sh'}'" in world.out


class TestSigningIn:
    def test_linux_with_no_display_signs_in_with_a_code(self, tmp_path: Path) -> None:
        world = PosixScenario(tmp_path, platform="linux", wincert=FakeKeyStore(ksp="keyring"))
        world.env.pop("DISPLAY", None)
        world.signed_in = False

        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out

        (login,) = [call for call in world.spawn.calls if words("login")(call)]
        assert "--use-device-code" in login.argv
        assert "You will get a short code" in world.out

    def test_a_mac_signs_in_through_the_browser(self, tmp_path: Path) -> None:
        world = PosixScenario(tmp_path)
        world.signed_in = False

        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out

        (login,) = [call for call in world.spawn.calls if words("login")(call)]
        assert "--use-device-code" not in login.argv
        assert "Your browser is opening" in world.out

    def test_over_ssh_a_mac_signs_in_with_a_code(self, tmp_path: Path) -> None:
        world = PosixScenario(tmp_path)
        world.env["SSH_CONNECTION"] = "10.0.0.1 50000 10.0.0.2 22"
        world.signed_in = False

        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out

        (login,) = [call for call in world.spawn.calls if words("login")(call)]
        assert "--use-device-code" in login.argv


class TestTheKeyCannotBeReadWhenTesting:
    def test_the_last_screen_says_it_is_the_session_and_what_to_run(
        self, world: PosixScenario
    ) -> None:
        from entrabot.winsetup.helpers import CHECKS_MARKER

        failing = [
            {
                "name": "Token mint (three-hop)",
                "status": "fail",
                "detail": "Could not read this account's key store, the keyring (locked)",
                "remediation": None,
            }
        ]
        world.spawn.on(
            snippet("run_smoke_checks"),
            Reply(stdout=CHECKS_MARKER + json.dumps(failing)),
        )

        assert world.run(new_chain=True, upn_suffix="sati") == 3, world.out

        assert f"could not read the agent's key from the {_key_store(world)}" in world.out
        assert "./scripts/setup.sh --diagnose" in world.out
        assert "Credential Manager" not in world.out


def test_launching_in_this_terminal_replaces_the_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from entrabot.winsetup import wizard

    seen: dict[str, object] = {}
    monkeypatch.setattr(os, "chdir", lambda path: seen.__setitem__("cwd", path))
    monkeypatch.setattr(os, "execv", lambda path, argv: seen.__setitem__("exec", (path, argv)))

    wizard.launch_in_this_terminal(["/x/start-entrabot.sh"], tmp_path)

    assert seen == {"cwd": tmp_path, "exec": ("/x/start-entrabot.sh", ["/x/start-entrabot.sh"])}


class TestTheTenantIsNamed:
    def test_a_sign_in_the_azure_cli_does_not_name_is_named_from_the_directory(
        self, tmp_path: Path
    ) -> None:
        # Measured on Ubuntu after "az login --use-device-code --tenant <domain>": az account
        # show carried neither the tenant's name nor its domain, and setup showed its ID.
        world = PosixScenario(tmp_path, platform="linux", wincert=FakeKeyStore(ksp="keyring"))
        nameless = {
            key: value
            for key, value in world._account().items()
            if key not in ("tenantDisplayName", "tenantDefaultDomain")
        }
        world.spawn.on(words("account", "show"), Reply(stdout=json.dumps(nameless)))
        organization = {
            "value": [
                {
                    "id": TENANT,
                    "displayName": "Contoso Showcase",
                    "technicalNotificationMails": ["it-contact@contoso.com"],
                }
            ]
        }
        world.spawn.on(
            lambda r: "rest" in r.argv and any(a.endswith("/organization") for a in r.argv),
            Reply(stdout=json.dumps(organization)),
        )

        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out

        assert "Signed in as admin@contoso.com (Contoso Showcase)" in world.out
        assert f"({TENANT})" not in world.out
        # The answer holds the organization's contact details: none of it goes to the log.
        assert (
            "it-contact@contoso.com" not in world.log and "it-contact@contoso.com" not in world.out
        )

    def test_when_the_directory_cannot_say_either_the_id_is_shown(self, tmp_path: Path) -> None:
        world = PosixScenario(tmp_path)
        nameless = {
            key: value
            for key, value in world._account().items()
            if key not in ("tenantDisplayName", "tenantDefaultDomain")
        }
        world.spawn.on(words("account", "show"), Reply(stdout=json.dumps(nameless)))

        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out

        assert f"Signed in as admin@contoso.com ({TENANT})" in world.out

    def test_a_named_sign_in_asks_the_directory_nothing_more(self, world: PosixScenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out
        assert not [
            call
            for call in world.spawn.calls
            if any(argument.endswith("/organization") for argument in call.argv)
        ]


class TestThisComputerIsNamedAsItsOwnerWould:
    def test_linux_says_the_distribution_not_the_kernel_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from entrabot.winsetup import wizard

        monkeypatch.setattr(wizard.platform_module, "system", lambda: "Linux")
        monkeypatch.setattr(wizard.platform_module, "machine", lambda: "x86_64")
        monkeypatch.setattr(wizard.platform_module, "release", lambda: "6.8.0-1017-azure")
        monkeypatch.setattr(
            wizard.platform_module,
            "freedesktop_os_release",
            lambda: {"PRETTY_NAME": "Ubuntu 24.04.5 LTS"},
        )

        text = wizard._os_description()

        assert text == "Ubuntu 24.04.5 LTS (Linux 6.8.0-1017-azure), x86_64"
        assert "SMP" not in text

    def test_linux_without_an_os_release_file_falls_back_to_the_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from entrabot.winsetup import wizard

        def missing() -> dict[str, str]:
            raise OSError("no os-release")

        monkeypatch.setattr(wizard.platform_module, "system", lambda: "Linux")
        monkeypatch.setattr(wizard.platform_module, "machine", lambda: "aarch64")
        monkeypatch.setattr(wizard.platform_module, "release", lambda: "6.8.0")
        monkeypatch.setattr(wizard.platform_module, "freedesktop_os_release", missing)

        assert wizard._os_description() == "Linux 6.8.0, aarch64"

    def test_a_mac_says_macos_and_its_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from entrabot.winsetup import wizard

        monkeypatch.setattr(wizard.platform_module, "system", lambda: "Darwin")
        monkeypatch.setattr(wizard.platform_module, "machine", lambda: "arm64")
        monkeypatch.setattr(wizard.platform_module, "mac_ver", lambda: ("15.6", ("", "", ""), ""))

        assert wizard._os_description() == "macOS 15.6, arm64"

    def test_windows_is_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from entrabot.winsetup import wizard

        monkeypatch.setattr(wizard.platform_module, "system", lambda: "Windows")
        monkeypatch.setattr(wizard.platform_module, "release", lambda: "11")
        monkeypatch.setattr(wizard.platform_module, "version", lambda: "10.0.26200")
        monkeypatch.setattr(wizard.platform_module, "machine", lambda: "AMD64")

        assert wizard._os_description() == "Windows 11 (10.0.26200), AMD64"
