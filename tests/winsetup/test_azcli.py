"""Finding, installing and driving the Azure CLI."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from entrabot.winsetup import azcli
from entrabot.winsetup.azcli import (
    APPLICATION_ADMIN,
    AZURE_CLI_REQUIREMENT,
    GLOBAL_ADMIN,
    PRIVILEGED_ROLE_ADMIN,
    AzCli,
    AzError,
    AzLocation,
    assess_privileges,
    find_az,
    has_interactive_desktop,
    install_az,
    validate_tenant,
)
from entrabot.winsetup.console import RunRequest
from tests.winsetup.fakes import FakeDirectory, FakeSpawn, Reply, make_console

ACCOUNT = {
    "id": "sub-1",
    "name": "Visual Studio Enterprise",
    "tenantId": "tenant-1",
    "tenantDisplayName": "Contoso",
    "user": {"name": "admin@contoso.com", "type": "user"},
}


def _az(spawn: FakeSpawn, command: tuple[str, ...] = ("az",)) -> AzCli:
    console, _ = make_console(spawn=spawn)
    return AzCli(AzLocation(command=command, bin_dir=Path("/opt/az/bin"), source="path"), console)


class TestFindAz:
    def test_az_on_path_is_used(self, tmp_path: Path) -> None:
        location = find_az(
            env={}, data_dir=tmp_path, platform="linux", which=lambda name: "/usr/bin/az"
        )
        assert location is not None
        # The launcher is kept as a path, so it reads back in this OS's own spelling.
        assert location.command == (str(Path("/usr/bin/az")),)
        assert location.bin_dir == Path("/usr/bin")

    def test_nothing_found_is_none(self, tmp_path: Path) -> None:
        assert find_az(env={}, data_dir=tmp_path, platform="linux", which=lambda name: None) is None

    def test_windows_msi_install_is_found_without_path(self, tmp_path: Path) -> None:
        # A fresh install isn't on this process's PATH yet.
        wbin = tmp_path / "Program Files" / "Microsoft SDKs" / "Azure" / "CLI2" / "wbin"
        wbin.mkdir(parents=True)
        (wbin / "az.cmd").write_text("@echo off\n", encoding="utf-8")
        location = find_az(
            env={"ProgramFiles": str(tmp_path / "Program Files")},
            data_dir=tmp_path / "data",
            platform="win32",
            which=lambda name: None,
        )
        assert location is not None and location.bin_dir == wbin
        assert location.command == (str(wbin / "az.cmd"),)

    def test_windows_calls_the_bundled_python_directly_when_it_is_there(
        self, tmp_path: Path
    ) -> None:
        # az.cmd hands its arguments to cmd.exe, where & | ( ) ^ % mean something. Calling the
        # interpreter az.cmd would have called keeps cmd.exe out of it.
        root = tmp_path / "CLI2"
        (root / "wbin").mkdir(parents=True)
        (root / "wbin" / "az.cmd").write_text("@echo off\n", encoding="utf-8")
        (root / "python.exe").write_bytes(b"")
        location = find_az(
            env={},
            data_dir=tmp_path / "data",
            platform="win32",
            which=lambda name: str(root / "wbin" / "az.cmd"),
        )
        assert location is not None
        assert location.command == (str(root / "python.exe"), "-IBm", "azure.cli")
        assert location.bin_dir == root / "wbin"  # helpers still find az.cmd through PATH

    def test_the_private_tools_venv_is_found(self, tmp_path: Path) -> None:
        scripts = tmp_path / "tools" / "azure-cli" / "Scripts"
        scripts.mkdir(parents=True)
        (scripts / "python.exe").write_bytes(b"")
        (scripts / "az.bat").write_text("@echo off\n", encoding="utf-8")
        location = find_az(env={}, data_dir=tmp_path, platform="win32", which=lambda name: None)
        assert location is not None and location.source == "tools-venv"
        assert location.command == (str(scripts / "python.exe"), "-m", "azure.cli")
        assert location.bin_dir == scripts


def _calls(spawn: FakeSpawn, *words: str) -> list[RunRequest]:
    """Calls whose arguments after the program are exactly *words* to start with."""
    return [call for call in spawn.calls if call.argv[1 : 1 + len(words)] == list(words)]


class TestInstall:
    def _spawn(self, tmp_path: Path, *, winget_rc: int = 0, pip_rc: int = 0) -> FakeSpawn:
        spawn = FakeSpawn()
        scripts = tmp_path / "tools" / "azure-cli" / "Scripts"

        def make_venv(request: RunRequest) -> Reply:
            scripts.mkdir(parents=True, exist_ok=True)
            (scripts / "python.exe").write_bytes(b"")
            return Reply()

        def pip(request: RunRequest) -> Reply:
            if pip_rc == 0:
                (scripts / "az.bat").write_text("@echo off\n", encoding="utf-8")
            return Reply(
                returncode=pip_rc, stderr="" if pip_rc == 0 else "No matching distribution"
            )

        # Predicates on argv positions: the temp path holds the test's name, words and all.
        spawn.on(lambda request: request.argv[1:3] == ["-m", "venv"], make_venv)
        spawn.on(lambda request: request.argv[1:4] == ["-m", "pip", "install"], pip)
        spawn.on(lambda request: request.argv[1:2] == ["install"], Reply(returncode=winget_rc))
        spawn.on(
            lambda request: request.argv[1:4] == ["-m", "azure.cli", "version"],
            Reply(stdout=json.dumps({"azure-cli": "2.91.0"})),
        )
        return spawn

    def test_without_winget_azure_cli_goes_into_a_private_venv(self, tmp_path: Path) -> None:
        spawn = self._spawn(tmp_path)
        console, out = make_console(spawn=spawn)
        location = install_az(
            console,
            data_dir=tmp_path,
            python=Path("C:/py/python.exe"),
            env={},
            platform="win32",
            which=lambda name: None,
        )
        assert location.source == "tools-venv"
        assert [call for call in spawn.calls if call.argv[1:2] == ["install"]] == []
        venv = _calls(spawn, "-m", "venv")[0].argv
        assert venv[0] == str(Path("C:/py/python.exe"))
        assert venv[-1] == str(tmp_path / "tools" / "azure-cli")
        pip = _calls(spawn, "-m", "pip", "install")[0]
        assert pip.argv[-1] == AZURE_CLI_REQUIREMENT and pip.heartbeat_seconds  # a heartbeat
        assert _calls(spawn, "-m", "azure.cli", "version")  # verified before it is trusted
        assert "2.91.0" in out.getvalue()

    def test_the_pip_install_is_constrained(self, tmp_path: Path) -> None:
        # Unpinned, "pip install azure-cli" takes whatever is newest, including a future major
        # version with whatever that changes.
        spawn = self._spawn(tmp_path)
        console, _ = make_console(spawn=spawn)
        install_az(
            console,
            data_dir=tmp_path,
            python=Path("python"),
            env={},
            platform="win32",
            which=lambda name: None,
        )
        (pip,) = _calls(spawn, "-m", "pip", "install")  # one install, never a second attempt
        assert pip.argv[-1] == AZURE_CLI_REQUIREMENT
        assert re.fullmatch(r"azure-cli>=2\.\d+\.\d+,<3", AZURE_CLI_REQUIREMENT)
        lower = AZURE_CLI_REQUIREMENT.removeprefix("azure-cli>=").split(",")[0]
        # Never below the oldest the project documents as supported.
        assert tuple(int(part) for part in lower.split(".")) >= (2, 55, 0)

    def test_prebuilt_packages_are_preferred_but_not_required(self, tmp_path: Path) -> None:
        # "--only-binary :all:" would fail the whole install on any platform where one of the
        # Azure CLI's many dependencies has no prebuilt package, leaving no Azure CLI at all.
        # Nothing here can show that every dependency has one for every Python and Windows
        # architecture setup runs on, so prebuilt packages are preferred, not required.
        spawn = self._spawn(tmp_path)
        console, _ = make_console(spawn=spawn)
        install_az(
            console,
            data_dir=tmp_path,
            python=Path("python"),
            env={},
            platform="win32",
            which=lambda name: None,
        )
        (pip,) = _calls(spawn, "-m", "pip", "install")
        assert "--prefer-binary" in pip.argv
        assert "--only-binary" not in pip.argv and ":all:" not in pip.argv

    def test_a_failed_winget_install_falls_back_to_the_private_venv(self, tmp_path: Path) -> None:
        spawn = self._spawn(tmp_path, winget_rc=1)
        console, _ = make_console(spawn=spawn)
        location = install_az(
            console,
            data_dir=tmp_path,
            python=Path("python"),
            env={},
            platform="win32",
            which=lambda name: "C:/winget.exe" if name == "winget" else None,
        )
        winget = [call for call in spawn.calls if call.argv[0] == "C:/winget.exe"]
        assert winget[0].interactive  # UAC and winget's own progress need the console
        assert location.source == "tools-venv"

    def test_an_existing_tools_venv_is_reused_not_reinstalled(self, tmp_path: Path) -> None:
        spawn = self._spawn(tmp_path)
        console, _ = make_console(spawn=spawn)
        kwargs = {"data_dir": tmp_path, "python": Path("python"), "env": {}, "platform": "win32"}
        install_az(console, which=lambda name: None, **kwargs)
        spawn.calls.clear()
        again = install_az(console, which=lambda name: None, **kwargs)
        assert again.source == "tools-venv"
        assert _calls(spawn, "-m", "pip", "install") == [] and _calls(spawn, "-m", "venv") == []

    def test_a_half_installed_venv_is_finished(self, tmp_path: Path) -> None:
        # Ctrl+C during pip leaves a venv with no az in it.
        spawn = self._spawn(tmp_path)
        scripts = tmp_path / "tools" / "azure-cli" / "Scripts"
        scripts.mkdir(parents=True)
        (scripts / "python.exe").write_bytes(b"")
        console, _ = make_console(spawn=spawn)
        install_az(
            console,
            data_dir=tmp_path,
            python=Path("python"),
            env={},
            platform="win32",
            which=lambda name: None,
        )
        assert _calls(spawn, "-m", "venv") == []
        assert len(_calls(spawn, "-m", "pip", "install")) == 1

    def test_a_failed_pip_install_is_an_error_with_the_reason(self, tmp_path: Path) -> None:
        spawn = self._spawn(tmp_path, pip_rc=1)
        console, _ = make_console(spawn=spawn)
        with pytest.raises(AzError, match="No matching distribution"):
            install_az(
                console,
                data_dir=tmp_path,
                python=Path("python"),
                env={},
                platform="win32",
                which=lambda name: None,
            )

    def test_an_install_that_does_not_answer_az_version_is_not_trusted(
        self, tmp_path: Path
    ) -> None:
        spawn = self._spawn(tmp_path)
        spawn.on(
            lambda request: request.argv[1:4] == ["-m", "azure.cli", "version"],
            Reply(returncode=1, stderr="ImportError: boom"),
        )
        console, _ = make_console(spawn=spawn)
        with pytest.raises(AzError, match="ImportError"):
            install_az(
                console,
                data_dir=tmp_path,
                python=Path("python"),
                env={},
                platform="win32",
                which=lambda name: None,
            )


class TestAccount:
    def test_no_session_is_none_and_not_an_error(self) -> None:
        spawn = FakeSpawn()
        spawn.on("account show", Reply(returncode=1, stderr="Please run 'az login'"))
        assert _az(spawn).account() is None

    def test_the_account_is_parsed_from_json(self) -> None:
        spawn = FakeSpawn()
        spawn.on("account show", Reply(stdout=json.dumps(ACCOUNT)))
        account = _az(spawn).account()
        assert account is not None
        assert (account.user, account.tenant_id, account.tenant_name) == (
            "admin@contoso.com",
            "tenant-1",
            "Contoso",
        )
        assert spawn.calls[0].argv[-2:] == ["--output", "json"]  # JSON, never TSV

    def test_warnings_around_the_json_do_not_break_parsing(self) -> None:
        spawn = FakeSpawn()
        spawn.on(
            "account show",
            Reply(
                stdout=json.dumps(ACCOUNT, indent=2), stderr="WARNING: a new version is available"
            ),
        )
        assert _az(spawn).account() is not None

    def test_subscriptions_skip_tenant_level_placeholders_and_other_tenants(self) -> None:
        spawn = FakeSpawn()
        spawn.on(
            "account list",
            Reply(
                stdout=json.dumps(
                    [
                        {
                            "id": "tenant-1",
                            "name": "N/A(tenant level account)",
                            "tenantId": "tenant-1",
                            "state": "Enabled",
                            "isDefault": False,
                        },
                        {
                            "id": "sub-1",
                            "name": "Dev & Test (R&D)",
                            "tenantId": "tenant-1",
                            "state": "Enabled",
                            "isDefault": True,
                        },
                        {
                            "id": "sub-2",
                            "name": "Off",
                            "tenantId": "tenant-1",
                            "state": "Disabled",
                            "isDefault": False,
                        },
                        {
                            "id": "sub-3",
                            "name": "Elsewhere",
                            "tenantId": "tenant-2",
                            "state": "Enabled",
                            "isDefault": False,
                        },
                    ]
                )
            ),
        )
        subscriptions = _az(spawn).subscriptions("tenant-1")
        assert [(item.id, item.is_default) for item in subscriptions] == [("sub-1", True)]

    def test_the_graph_token_is_never_logged(self, tmp_path: Path) -> None:
        spawn = FakeSpawn()
        spawn.on("get-access-token", Reply(stdout=json.dumps({"accessToken": "opaque-secret"})))
        log = tmp_path / "setup.log"
        console, out = make_console(spawn=spawn, log_file=log)
        az = AzCli(AzLocation(command=("az",), bin_dir=Path("/x"), source="path"), console)
        assert az.graph_token() == "opaque-secret"
        console.close()
        assert "opaque-secret" not in out.getvalue() + log.read_text(encoding="utf-8")


class TestLogin:
    # How az login ends, seen verbatim on a real machine, when nobody used the device code
    # before it ran out: MSAL stops polling at the code's expiry and az reports the last answer.
    UNUSED_CODE = (
        "ERROR: AADSTS70016: OAuth 2.0 device flow error. Authorization is pending. Continue "
        "polling. Trace ID: 0b1c Correlation ID: 2d3e Timestamp: 2026-11-12 09:15:00Z"
    )

    def test_login_never_requires_a_subscription(self) -> None:
        spawn = FakeSpawn()
        assert _az(spawn).login(tenant=None, device_code=False) == "signed-in"
        request = spawn.calls[0]
        assert request.argv[:3] == ["az", "login", "--allow-no-subscriptions"]
        assert "--use-device-code" not in request.argv and "--tenant" not in request.argv
        assert request.interactive  # the sign-in talks to the person directly

    def test_tenant_and_device_code_are_passed(self) -> None:
        spawn = FakeSpawn()
        _az(spawn).login(tenant="contoso.com", device_code=True)
        argv = spawn.calls[0].argv
        assert argv[argv.index("--tenant") + 1] == "contoso.com" and "--use-device-code" in argv

    def test_a_failed_login_is_false(self) -> None:
        spawn = FakeSpawn()
        spawn.on("login", Reply(returncode=1))
        assert _az(spawn).login(tenant=None, device_code=False) == "failed"

    def test_a_device_code_sign_in_is_read_as_well_as_shown(self) -> None:
        # Started as an interactive child, az writes straight to the terminal and nothing it
        # says can be read back, so how the sign-in ended could not be told apart.
        spawn = FakeSpawn()
        prompt = (
            "To sign in, use a web browser to open the page https://microsoft.com/devicelogin "
            "and enter the code F7KQXBRZ3 to authenticate."
        )
        spawn.on("login", Reply(stderr=prompt))
        console, out = make_console(spawn=spawn)
        az = AzCli(AzLocation(command=("az",), bin_dir=Path("/x"), source="path"), console)
        assert az.login(tenant=None, device_code=True) == "signed-in"
        request = spawn.calls[0]
        assert not request.interactive and request.input_text is None
        assert prompt in out.getvalue()  # the person still sees the page and the code

    @pytest.mark.parametrize("code", ["AADSTS70016", "AADSTS70019", "AADSTS70020"])
    def test_a_device_code_nobody_used_in_time_is_told_apart(self, code: str) -> None:
        spawn = FakeSpawn()
        spawn.on("login", Reply(returncode=1, stderr=self.UNUSED_CODE.replace("AADSTS70016", code)))
        assert _az(spawn).login(tenant=None, device_code=True) == "code-expired"

    @pytest.mark.parametrize(
        "stderr",
        [
            "ERROR: AADSTS50097: Device authentication is required.",
            # Another code that merely starts with the same digits is another answer.
            "ERROR: AADSTS700160: something else entirely.",
            "",
        ],
        ids=["managed-devices-only", "longer-code", "no-reason"],
    )
    def test_any_other_failed_device_code_sign_in_is_a_failure(self, stderr: str) -> None:
        spawn = FakeSpawn()
        spawn.on("login", Reply(returncode=1, stderr=stderr))
        assert _az(spawn).login(tenant=None, device_code=True) == "failed"

    def test_the_words_alone_are_not_a_code(self) -> None:
        spawn = FakeSpawn()
        spawn.on("login", Reply(returncode=1, stderr="ERROR: Authorization is pending."))
        assert _az(spawn).login(tenant=None, device_code=True) == "failed"

    def test_a_browser_sign_in_is_never_read_as_an_unused_code(self) -> None:
        spawn = FakeSpawn()
        spawn.on("login", Reply(returncode=1, stderr=self.UNUSED_CODE))
        assert _az(spawn).login(tenant=None, device_code=False) == "failed"

    @pytest.mark.parametrize(
        "value", ["contoso.com", "contoso.onmicrosoft.com", "0f3c1c7e-1111-2222-3333-444455556666"]
    )
    def test_good_tenants(self, value: str) -> None:
        assert validate_tenant(value) is None

    @pytest.mark.parametrize("value", ["", "a b", "x&calc", "t;rm"])
    def test_bad_tenants(self, value: str) -> None:
        assert validate_tenant(value)


class TestCmdSafety:
    def test_an_argument_cmd_would_reinterpret_is_refused_for_a_batch_launcher(self) -> None:
        spawn = FakeSpawn()
        az = _az(spawn, command=("C:/az/wbin/az.cmd",))
        with pytest.raises(AzError, match="can't be passed"):
            az.run(["ad", "user", "show", "--id", "a&b@contoso.com"])
        assert spawn.calls == []

    def test_the_same_argument_is_fine_when_python_is_called_directly(self) -> None:
        spawn = FakeSpawn()
        az = _az(spawn, command=("C:/az/python.exe", "-IBm", "azure.cli"))
        az.run(["account", "set", "--subscription", "Dev & Test (R&D)"])
        assert spawn.calls[0].argv[:3] == ["C:/az/python.exe", "-IBm", "azure.cli"]


class TestSearchUserOrder:
    """The order of the lookups, as search_user reports them. Who is found by each one is in
    tests/winsetup/test_sponsors.py."""

    MISSING_ID = "00000000-0000-0000-0000-00000000beef"

    def _search(self, identifier: str, *, batch_file: bool = False) -> tuple[str, ...]:
        spawn = FakeSpawn()
        directory = FakeDirectory(users=[])
        if batch_file:
            directory.attach_cli(spawn)
        else:
            directory.attach(spawn)
        az = _az(spawn, command=("C:/az/az.cmd",) if batch_file else ("az",))
        found = az.search_user(identifier)
        assert found.user is None
        return found.tried

    def test_a_search_for_the_sign_in_name_comes_right_after_the_direct_lookup(self) -> None:
        assert self._search("o'brien@contoso.com") == (
            "their sign-in name",
            "a search for their sign-in name",
            "their primary email",
            "their other email addresses",
            "their email aliases",
            "a guest or personal account made from that address",
        )

    def test_a_batch_file_azure_cli_makes_the_same_search_in_the_same_place(self) -> None:
        assert self._search("o'brien@contoso.com", batch_file=True) == (
            "their sign-in name or object ID",
            "a search for their sign-in name",
            "their primary email",
            "(other lookups are not possible with this Azure CLI install)",
        )

    def test_an_object_id_gets_the_direct_lookup_only(self) -> None:
        assert self._search(self.MISSING_ID) == ("that object ID",)
        assert self._search(self.MISSING_ID, batch_file=True) == (
            "their sign-in name or object ID",
            "(other lookups are not possible with this Azure CLI install)",
        )


class TestBrowserSignIn:
    """From a desktop run on a PC its organization does not manage: the Windows account broker
    asked at every sign-in whether to remember the account and manage the device. The Azure
    CLI is told to use the browser instead."""

    def test_windows_gets_the_browser(self) -> None:
        env = azcli.sign_in_env({"PATH": "x"}, platform="win32")
        assert env == {"PATH": "x", "AZURE_CORE_ENABLE_BROKER_ON_WINDOWS": "false"}

    def test_a_choice_the_person_made_is_kept_whatever_its_case(self) -> None:
        for name in ("AZURE_CORE_ENABLE_BROKER_ON_WINDOWS", "azure_core_enable_broker_on_windows"):
            env = azcli.sign_in_env({name: "true"}, platform="win32")
            assert env == {name: "true"}

    def test_other_systems_are_left_alone(self) -> None:
        assert azcli.sign_in_env({"PATH": "x"}, platform="darwin") == {"PATH": "x"}

    def test_every_az_the_wrapper_starts_is_given_the_setting(self) -> None:
        # From the first desktop run of browser sign-in: the setting was put in the wizard's
        # own environment mapping, but az was started with no environment of its own, so it
        # inherited the real one and opened the Windows account sign-in window again.
        spawn = FakeSpawn()
        console, _ = make_console(spawn=spawn)
        location = AzLocation(command=("az",), bin_dir=Path("/x"), source="path")
        az = AzCli(location, console, env={"PATH": "/usr/bin"}, platform="win32")
        az.account()
        az.login(tenant=None, device_code=False)
        az.login(tenant=None, device_code=True)
        assert len(spawn.calls) == 3
        for call in spawn.calls:
            assert call.env is not None, call.argv
            assert call.env["AZURE_CORE_ENABLE_BROKER_ON_WINDOWS"] == "false", call.argv
            assert call.env["PATH"] == "/usr/bin"

    def test_helpers_started_through_the_wrapper_get_it_too(self) -> None:
        console, _ = make_console(spawn=FakeSpawn())
        location = AzLocation(command=("az",), bin_dir=Path("/x"), source="path")
        az = AzCli(location, console, platform="win32")
        assert az.child_env({"PATH": "p"})["AZURE_CORE_ENABLE_BROKER_ON_WINDOWS"] == "false"

    def test_the_given_mapping_is_not_changed(self) -> None:
        given = {"PATH": "x"}
        azcli.sign_in_env(given, platform="win32")
        assert given == {"PATH": "x"}


class TestInteractiveDesktop:
    def test_an_ssh_session_has_no_desktop(self) -> None:
        # Measured on Windows 11: SESSIONNAME is empty and SSH_CONNECTION is set.
        env = {"SSH_CONNECTION": "10.0.0.1 5000 10.0.0.2 22", "SESSIONNAME": ""}
        assert has_interactive_desktop(env, platform="win32") is False

    @pytest.mark.parametrize("session", ["Console", "RDP-Tcp#0"])
    def test_console_and_rdp_sessions_have_one(self, session: str) -> None:
        assert has_interactive_desktop({"SESSIONNAME": session}, platform="win32") is True

    def test_windows_without_a_session_name_is_treated_as_no_desktop(self) -> None:
        # Device code works everywhere; a hidden sign-in window that nobody can see doesn't.
        assert has_interactive_desktop({}, platform="win32") is False


class TestPrivileges:
    """Reading the signed-in user's directory roles.

    The first version fetched ``/me/transitiveMemberOf`` whole: every group the person is in,
    dozens of fields each, all of it written to the setup log. Now only directory roles are
    asked for, and only three fields of each.
    """

    ROLE = "/me/transitiveMemberOf/microsoft.graph.directoryRole"

    def _url(self, request: RunRequest) -> str:
        return request.argv[request.argv.index("--url") + 1]

    def _roles(self, spawn: FakeSpawn, ids: list[str]) -> None:
        value = [
            {"id": f"role-{n}", "displayName": "A role", "roleTemplateId": item}
            for n, item in enumerate(ids)
        ]
        spawn.on(
            lambda request: "rest" in request.argv and self.ROLE in self._url(request),
            Reply(stdout=json.dumps({"@odata.count": len(value), "value": value})),
        )

    def test_only_directory_roles_and_three_fields_are_requested(self) -> None:
        spawn = FakeSpawn()
        self._roles(spawn, [GLOBAL_ADMIN])
        assert _az(spawn).directory_role_template_ids() == {GLOBAL_ADMIN}
        request = spawn.calls[0]
        url = self._url(request)
        assert self.ROLE in url
        assert "$select=id%2CdisplayName%2CroleTemplateId" in url
        # A cast on transitiveMemberOf is an "advanced query": it needs both of these.
        assert "$count=true" in url
        headers = request.argv[request.argv.index("--headers") + 1]
        assert headers == "ConsistencyLevel=eventual"
        assert len(spawn.calls) == 1

    def test_the_log_holds_roles_only_not_every_group(self, tmp_path: Path) -> None:
        spawn = FakeSpawn()
        self._roles(spawn, [GLOBAL_ADMIN])
        log = tmp_path / "setup.log"
        console, _ = make_console(spawn=spawn, log_file=log)
        az = AzCli(AzLocation(command=("az",), bin_dir=Path("/x"), source="path"), console)
        az.directory_role_template_ids()
        console.close()
        logged = log.read_text(encoding="utf-8")
        assert GLOBAL_ADMIN in logged and "securityEnabled" not in logged

    def test_a_tenant_that_refuses_the_cast_gets_a_filtered_fallback(self) -> None:
        # Same endpoint as before, but az prints (and so the log keeps) only the role fields.
        spawn = FakeSpawn()
        everything = {
            "value": [
                {
                    "@odata.type": "#microsoft.graph.directoryRole",
                    "id": "r",
                    "displayName": "Global Administrator",
                    "roleTemplateId": GLOBAL_ADMIN,
                },
                {"@odata.type": "#microsoft.graph.group", "id": "g", "securityEnabled": True},
            ]
        }

        def answer(request: RunRequest) -> Reply:
            if self.ROLE in self._url(request):
                return Reply(returncode=1, stderr="ERROR: Request_UnsupportedQuery")
            assert "--query" in request.argv  # az itself drops everything that is not a role
            query = request.argv[request.argv.index("--query") + 1]
            assert "roleTemplateId" in query and "nextLink" in query
            roles = [item for item in everything["value"] if item.get("roleTemplateId")]
            return Reply(stdout=json.dumps({"value": roles, "nextLink": None}))

        spawn.on(lambda request: "rest" in request.argv, answer)
        assert _az(spawn).directory_role_template_ids() == {GLOBAL_ADMIN}
        assert len(spawn.calls) == 2

    def test_more_pages_are_followed(self) -> None:
        spawn = FakeSpawn()
        next_link = "https://graph.microsoft.com/v1.0/me/next?$skiptoken=abc"

        def answer(request: RunRequest) -> Reply:
            if self._url(request) == next_link:
                return Reply(stdout=json.dumps({"value": [{"roleTemplateId": APPLICATION_ADMIN}]}))
            return Reply(
                stdout=json.dumps(
                    {"value": [{"roleTemplateId": GLOBAL_ADMIN}], "@odata.nextLink": next_link}
                )
            )

        spawn.on(lambda request: "rest" in request.argv, answer)
        assert _az(spawn).directory_role_template_ids() == {GLOBAL_ADMIN, APPLICATION_ADMIN}

    def test_no_roles_is_an_empty_set_not_unknown(self) -> None:
        spawn = FakeSpawn()
        self._roles(spawn, [])
        assert _az(spawn).directory_role_template_ids() == set()

    def test_roles_that_cannot_be_read_are_unknown_not_empty(self) -> None:
        spawn = FakeSpawn()
        spawn.on(lambda request: "rest" in request.argv, Reply(returncode=1, stderr="Forbidden"))
        assert _az(spawn).directory_role_template_ids() is None

    def test_a_batch_file_azure_cli_reads_roles_without_logging_the_response(
        self, tmp_path: Path
    ) -> None:
        # cmd.exe would reinterpret "&" and quotes, so the plain URL is used; the response
        # (every group) is then kept out of the log instead.
        spawn = FakeSpawn()
        body = {"value": [{"roleTemplateId": GLOBAL_ADMIN}, {"id": "g", "securityEnabled": True}]}
        spawn.on(lambda request: "rest" in request.argv, Reply(stdout=json.dumps(body)))
        log = tmp_path / "setup.log"
        console, _ = make_console(spawn=spawn, log_file=log)
        az = AzCli(
            AzLocation(command=("C:/az/az.cmd",), bin_dir=Path("/x"), source="path"), console
        )
        assert az.directory_role_template_ids() == {GLOBAL_ADMIN}
        console.close()
        assert "securityEnabled" not in log.read_text(encoding="utf-8")
        assert not any("&" in arg or '"' in arg for call in spawn.calls for arg in call.argv)

    def test_a_global_admin_has_what_setup_needs(self) -> None:
        assert assess_privileges({GLOBAL_ADMIN}, mode="new").level == "enough"

    def test_role_admin_plus_app_admin_is_enough(self) -> None:
        roles = {PRIVILEGED_ROLE_ADMIN, APPLICATION_ADMIN}
        assert assess_privileges(roles, mode="new").level == "enough"

    def test_an_app_admin_alone_cannot_grant_graph_consent(self) -> None:
        verdict = assess_privileges({APPLICATION_ADMIN}, mode="new")
        assert verdict.level == "partial"
        assert "consent" in verdict.message.lower()

    def test_no_admin_role_is_flagged_with_what_to_ask_for(self) -> None:
        verdict = assess_privileges(set(), mode="new")
        assert verdict.level == "none"
        assert "Global Administrator" in verdict.message

    def test_joining_an_existing_agent_needs_less(self) -> None:
        assert assess_privileges({APPLICATION_ADMIN}, mode="join").level == "enough"
        verdict = assess_privileges(set(), mode="join")
        assert verdict.level == "none" and "owner" in verdict.message.lower()

    def test_unreadable_roles_are_reported_as_unknown(self) -> None:
        assert assess_privileges(None, mode="new").level == "unknown"


def test_az_error_carries_the_cli_message() -> None:
    spawn = FakeSpawn()
    spawn.on("ad signed-in-user", Reply(returncode=1, stderr="ERROR: AADSTS50076"))
    with pytest.raises(AzError, match="AADSTS50076"):
        _az(spawn).json(["ad", "signed-in-user", "show"])


def test_module_exposes_role_ids_as_guids() -> None:
    for value in (
        azcli.GLOBAL_ADMIN,
        azcli.PRIVILEGED_ROLE_ADMIN,
        azcli.APPLICATION_ADMIN,
        azcli.CLOUD_APPLICATION_ADMIN,
    ):
        assert len(value) == 36
