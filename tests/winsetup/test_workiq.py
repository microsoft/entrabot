"""The optional Microsoft Agent 365 Work IQ step (port of Configure-A365WorkIq)."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from entrabot.winsetup.azcli import AzCli, AzLocation
from entrabot.winsetup.helpers import Helpers, Paths
from entrabot.winsetup.workiq import (
    A365_INSTALL_HINT,
    INSTALL_COMMAND,
    WorkIqError,
    WorkIqTools,
    configure_work_iq,
    find_a365,
    find_pwsh,
    find_tools,
    use_tools,
    word_tools_configured,
)
from tests.winsetup.fakes import FakeSpawn, Reply, make_console, script, words

A365 = Path("C:/Users/x/.dotnet/tools/a365.exe")
VENV_PYTHON = Path("/venv/Scripts/python.exe")


def _setup(tmp_path: Path) -> tuple[FakeSpawn, dict]:
    spawn, kwargs, _ = _setup_with_output(tmp_path)
    return spawn, kwargs


def _setup_with_output(tmp_path: Path) -> tuple[FakeSpawn, dict, io.StringIO]:
    spawn = FakeSpawn()
    spawn.on(words("ad", "app", "list"), Reply(stdout=json.dumps([{"appId": "cli-app"}])))
    spawn.on(words("ad", "sp", "show"), Reply(stdout=json.dumps({"displayName": "Sati Agent"})))
    console, out = make_console(spawn=spawn)
    paths = Paths(tmp_path, VENV_PYTHON, tmp_path / "data")
    az = AzCli(AzLocation(command=("az",), bin_dir=Path("/az"), source="path"), console)
    kwargs = {
        "paths": paths,
        "helpers": Helpers(console, paths, {}),
        "console": console,
        "az": az,
        "a365": A365,
        "tenant_id": "tenant",
        "blueprint_app_id": "bp-app",
        "blueprint_object_id": "bp-object",
        "agent_id": "agent-app",
    }
    return spawn, kwargs, out


def script_name(argv: list[str]) -> str:
    """The helper script a command runs, or '' when it isn't ``python scripts/<name>.py``."""
    return Path(argv[1]).name if len(argv) > 1 and argv[1].endswith(".py") else ""


def test_the_steps_run_in_order_with_the_right_programs(tmp_path: Path) -> None:
    spawn, kwargs = _setup(tmp_path)
    configure_work_iq(**kwargs)
    ran = [call.argv for call in spawn.calls if call.argv[0] != "az"]
    assert ran[0] == [str(A365), "setup", "requirements"]
    assert ran[1][:3] == [str(A365), "develop", "add-mcp-servers"]
    assert ran[1][-2:] == ["--project-path", str(tmp_path)]
    assert {"mcp_WordServer", "mcp_ODSPRemoteServer", "mcp_ExcelServer"} <= set(ran[1])
    assert ran[2][1].endswith("ensure_a365_work_iq_permissions.py")
    assert ran[2][-2:] == ["--blueprint-app-id", "bp-app"]
    assert ran[3] == [str(A365), "setup", "permissions", "mcp"]
    # The manifest check runs with the virtual-environment Python, never whatever "python" is.
    assert ran[4][0] == str(VENV_PYTHON) and ran[4][1].endswith("spike_a365_work_iq.py")


def test_the_config_and_manifest_exist_before_the_first_a365_command(tmp_path: Path) -> None:
    # "a365 setup requirements" reads a365.config.json, so writing it afterwards would point
    # the tooling at nothing (or at a previous run's Blueprint).
    spawn, kwargs = _setup(tmp_path)
    seen: dict[str, object] = {}

    def requirements(_request: object) -> Reply:
        seen["config"] = json.loads((tmp_path / "a365.config.json").read_text("utf-8"))
        seen["manifest"] = (tmp_path / "ToolingManifest.json").is_file()
        return Reply()

    spawn.on(words("setup", "requirements"), requirements)
    configure_work_iq(**kwargs)
    assert seen["manifest"] is True
    assert seen["config"]["agentBlueprintId"] == "bp-app"  # type: ignore[index]


def test_the_permission_grant_comes_before_a365_applies_permissions(tmp_path: Path) -> None:
    spawn, kwargs = _setup(tmp_path)
    configure_work_iq(**kwargs)
    order = [call.argv for call in spawn.calls]
    ensure = next(
        i
        for i, argv in enumerate(order)
        if script_name(argv) == "ensure_a365_work_iq_permissions.py"
    )
    requirements = order.index([str(A365), "setup", "requirements"])
    permissions = order.index([str(A365), "setup", "permissions", "mcp"])
    spike = next(i for i, argv in enumerate(order) if script_name(argv) == "spike_a365_work_iq.py")
    assert requirements < ensure < permissions < spike


def test_no_a365_command_names_an_agent_or_creates_a_blueprint(tmp_path: Path) -> None:
    # Setup reuses the Entrabot Blueprint through a365.config.json. Letting a365 create its
    # own ("setup blueprint", "--agent-name") would give the agent a second, unrelated one.
    spawn, kwargs = _setup(tmp_path)
    configure_work_iq(**kwargs)
    a365_calls = [call.argv for call in spawn.calls if call.argv[0] == str(A365)]
    assert len(a365_calls) == 3
    for argv in a365_calls:
        assert "--agent-name" not in argv and "blueprint" not in argv


def test_tenant_lookups_are_read_as_json(tmp_path: Path) -> None:
    spawn, kwargs = _setup(tmp_path)
    configure_work_iq(**kwargs)
    az_calls = [call.argv for call in spawn.calls if call.argv[0] == "az"]
    assert len(az_calls) == 2
    assert all(argv[-2:] == ["--output", "json"] for argv in az_calls)  # never TSV
    show = next(argv for argv in az_calls if argv[1:4] == ["ad", "sp", "show"])
    assert show[show.index("--id") + 1] == "agent-app"


def test_success_is_reported_once_everything_validated(tmp_path: Path) -> None:
    spawn, kwargs, out = _setup_with_output(tmp_path)
    configure_work_iq(**kwargs)
    assert "Work IQ configured" in out.getvalue()


def test_every_step_runs_from_the_project_folder(tmp_path: Path) -> None:
    # a365 and the manifest check both read files relative to the current directory.
    spawn, kwargs = _setup(tmp_path)
    configure_work_iq(**kwargs)
    assert all(call.cwd == tmp_path for call in spawn.calls if call.argv[0] != "az")


def test_the_config_points_at_the_existing_blueprint_and_keeps_other_keys(tmp_path: Path) -> None:
    spawn, kwargs = _setup(tmp_path)
    (tmp_path / "a365.config.json").write_text(json.dumps({"custom": 1}), encoding="utf-8")
    configure_work_iq(**kwargs)
    config = json.loads((tmp_path / "a365.config.json").read_text(encoding="utf-8"))
    assert config == {
        "custom": 1,
        "tenantId": "tenant",
        "clientAppId": "cli-app",
        "agentBlueprintId": "bp-app",
        "agentBlueprintDisplayName": "EntraBot Code Agent",
        "agentIdentityDisplayName": "Sati Agent",
        "deploymentProjectPath": str(tmp_path),
        "agentBlueprintObjectId": "bp-object",
        "agentIdentityId": "agent-app",
    }
    assert json.loads((tmp_path / "ToolingManifest.json").read_text("utf-8")) == {"mcpServers": []}


def test_an_existing_tooling_manifest_is_left_alone(tmp_path: Path) -> None:
    spawn, kwargs = _setup(tmp_path)
    (tmp_path / "ToolingManifest.json").write_text('{"mcpServers":[{"x":1}]}', encoding="utf-8")
    configure_work_iq(**kwargs)
    assert json.loads((tmp_path / "ToolingManifest.json").read_text("utf-8"))["mcpServers"]


def test_a_missing_agent_365_cli_app_is_explained(tmp_path: Path) -> None:
    spawn, kwargs = _setup(tmp_path)
    spawn.on(words("ad", "app", "list"), Reply(stdout="[]"))
    with pytest.raises(WorkIqError, match="Agent 365 CLI"):
        configure_work_iq(**kwargs)


@pytest.mark.parametrize(
    "failing", [("setup", "requirements"), ("develop", "add-mcp-servers"), ("setup", "permissions")]
)
def test_a_failed_a365_step_stops_the_step(tmp_path: Path, failing: tuple[str, str]) -> None:
    spawn, kwargs = _setup(tmp_path)
    spawn.on(words(*failing), Reply(returncode=1))
    with pytest.raises(WorkIqError):
        configure_work_iq(**kwargs)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_grants_that_failed_quietly_are_treated_as_a_failure(tmp_path: Path, stream: str) -> None:
    # a365 exits 0 while printing this, on either stream.
    spawn, kwargs, out = _setup_with_output(tmp_path)
    reply = Reply(**{stream: "WARN OAuth2 grants failed for mcp_WordServer\n"})
    spawn.on(words("setup", "permissions"), reply)
    with pytest.raises(WorkIqError, match="OAuth2 grants failed"):
        configure_work_iq(**kwargs)
    assert not any(script("spike_a365_work_iq.py")(call) for call in spawn.calls)
    assert "Work IQ configured" not in out.getvalue()


def test_a_clean_permissions_run_that_merely_mentions_grants_passes(tmp_path: Path) -> None:
    spawn, kwargs = _setup(tmp_path)
    spawn.on(words("setup", "permissions"), Reply(stdout="OAuth2 grants succeeded\n"))
    configure_work_iq(**kwargs)
    assert any(script("spike_a365_work_iq.py")(call) for call in spawn.calls)


def test_a_failed_helper_stops_the_step(tmp_path: Path) -> None:
    spawn, kwargs = _setup(tmp_path)
    spawn.on(script("ensure_a365_work_iq_permissions.py"), Reply(returncode=1))
    with pytest.raises(WorkIqError):
        configure_work_iq(**kwargs)


# From a real run (Windows 11, over SSH): the sign-in window could not open, and a365 said so
# in MSAL's words. The last screen gets plain ones; the log keeps a365's.
MSAL_FAILURE = (
    "ERROR: MSAL authentication failed: Unknown Status: Unexpected Error: 0xffffffff800703f0\n"
    "ERROR: Interactive authentication failed: Failed to acquire token\n"
    "ERROR: Exception type: Microsoft.Agents.A365.DevTools.Cli.Services."
    "MsalAuthenticationFailedException\n"
)


@pytest.mark.parametrize("failing", [("develop", "add-mcp-servers"), ("setup", "permissions")])
def test_a_sign_in_failure_has_a_plain_reason(tmp_path: Path, failing: tuple[str, str]) -> None:
    spawn, kwargs = _setup(tmp_path)
    spawn.on(words(*failing), Reply(returncode=1, stdout=MSAL_FAILURE))
    with pytest.raises(WorkIqError) as raised:
        configure_work_iq(**kwargs)
    assert raised.value.signed_in is False
    assert raised.value.reason == "the Agent 365 tool could not sign in to Microsoft"
    assert "MSAL" not in raised.value.reason


def test_another_failure_has_a_short_reason(tmp_path: Path) -> None:
    spawn, kwargs = _setup(tmp_path)
    spawn.on(words("setup", "permissions"), Reply(returncode=1, stdout="ERROR: 500\n"))
    with pytest.raises(WorkIqError) as raised:
        configure_work_iq(**kwargs)
    assert raised.value.signed_in is True
    assert raised.value.reason == "one of the steps that switch them on did not finish"


def test_the_first_step_talks_to_the_person_so_its_failure_points_at_the_screen(
    tmp_path: Path,
) -> None:
    # 'a365 setup requirements' runs in the person's terminal (it may ask before changing the
    # app registration), so setup cannot read what it printed.
    spawn, kwargs = _setup(tmp_path)
    spawn.on(words("setup", "requirements"), Reply(returncode=1))
    with pytest.raises(WorkIqError) as raised:
        configure_work_iq(**kwargs)
    assert "its messages are on the screen above" in raised.value.reason


@pytest.mark.parametrize("before", [None, '{"mcpServers":[{"mcpServerName":"mcp_WordServer"}]}'])
def test_a_failed_attempt_leaves_the_projects_work_iq_files_as_they_were(
    tmp_path: Path, before: str | None
) -> None:
    # Otherwise a manifest that a365 filled in before a later step failed would make the next
    # run (and the agent) think Word, Excel and OneDrive files were switched on.
    spawn, kwargs = _setup(tmp_path)
    manifest, config = tmp_path / "ToolingManifest.json", tmp_path / "a365.config.json"
    if before is not None:
        manifest.write_text(before, encoding="utf-8")
        config.write_text('{"tenantId": "old"}', encoding="utf-8")

    def add_servers(_request: object) -> Reply:
        manifest.write_text('{"mcpServers":[{"mcpServerName":"mcp_WordServer","new":1}]}')
        return Reply()

    spawn.on(words("develop", "add-mcp-servers"), add_servers)
    spawn.on(words("setup", "permissions"), Reply(returncode=1))
    with pytest.raises(WorkIqError):
        configure_work_iq(**kwargs)
    if before is None:
        assert not manifest.exists() and not config.exists()
        assert not word_tools_configured(tmp_path)
    else:
        assert manifest.read_text(encoding="utf-8") == before
        assert config.read_text(encoding="utf-8") == '{"tenantId": "old"}'


class TestFindA365:
    def test_on_path(self) -> None:
        assert find_a365({}, which=lambda name: "C:/tools/a365.exe") == Path("C:/tools/a365.exe")

    def test_the_dotnet_tools_folder_is_checked_without_path(self, tmp_path: Path) -> None:
        # A just-installed .NET global tool isn't on this process's PATH yet.
        exe = tmp_path / ".dotnet" / "tools" / "a365.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        assert find_a365({"USERPROFILE": str(tmp_path)}, which=lambda name: None) == exe

    def test_missing_is_none(self) -> None:
        assert find_a365({}, which=lambda name: None) is None

    def test_entrabots_own_copy_is_found(self, tmp_path: Path) -> None:
        # Where scripts\prereqs-windows.ps1 -WithWorkIq installs it without admin rights.
        exe = _file(tmp_path / "data" / "tools" / "a365" / "a365.exe")
        found = find_a365({}, which=lambda name: None, data_root=tmp_path / "data")
        assert found == exe

    def test_one_the_person_installed_comes_first(self, tmp_path: Path) -> None:
        _file(tmp_path / "data" / "tools" / "a365" / "a365.exe")
        global_tool = _file(tmp_path / ".dotnet" / "tools" / "a365.exe")
        found = find_a365(
            {"USERPROFILE": str(tmp_path)}, which=lambda name: None, data_root=tmp_path / "data"
        )
        assert found == global_tool


def _file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


class TestTheToolsWorkIqNeeds:
    """The a365 CLI, and PowerShell 7 for its permissions step (docs: setup-windows-ps1.md)."""

    def test_powershell_7_on_path(self) -> None:
        tools = find_tools({}, which=lambda name: f"C:/bin/{name}.exe")
        assert tools.pwsh == Path("C:/bin/pwsh.exe") and tools.missing == []

    @pytest.mark.parametrize(
        "where",
        [
            ("ProgramFiles", "PowerShell/7/pwsh.exe"),
            ("LOCALAPPDATA", "Microsoft/WindowsApps/pwsh.exe"),
        ],
        ids=["msi", "msix-alias"],
    )
    def test_a_just_installed_powershell_7_is_found_without_path(
        self, tmp_path: Path, where: tuple[str, str]
    ) -> None:
        variable, relative = where
        pwsh = _file(tmp_path / relative)
        assert find_pwsh({variable: str(tmp_path)}, which=lambda name: None) == pwsh

    def test_what_is_missing_is_named(self) -> None:
        assert find_tools({}, which=lambda name: None).missing == [
            "the Microsoft Agent 365 CLI",
            "PowerShell 7",
        ]
        only_a365 = find_tools({}, which=lambda name: "C:/a365.exe" if name == "a365" else None)
        assert only_a365.missing == ["PowerShell 7"]

    def test_tools_found_off_path_are_put_on_the_path_the_a365_steps_get(
        self, tmp_path: Path
    ) -> None:
        a365 = _file(tmp_path / "profile" / ".dotnet" / "tools" / "a365.exe")
        pwsh = _file(tmp_path / "pf" / "PowerShell" / "7" / "pwsh.exe")
        system = str(tmp_path / "Windows")
        env = {"PATH": system}
        use_tools(env, WorkIqTools(a365=a365, pwsh=pwsh), which=lambda name: None)
        assert env["PATH"].split(os.pathsep) == [str(pwsh.parent), str(a365.parent), system]
        use_tools(env, WorkIqTools(a365=a365, pwsh=pwsh), which=lambda name: None)
        assert env["PATH"].split(os.pathsep) == [str(pwsh.parent), str(a365.parent), system]
        assert "DOTNET_ROOT" not in env  # a365 the person installed finds its own .NET

    def test_tools_already_on_path_leave_it_alone(self, tmp_path: Path) -> None:
        env = {"PATH": str(tmp_path / "Windows")}
        tools = WorkIqTools(a365=tmp_path / "bin" / "a365.exe", pwsh=tmp_path / "bin" / "pwsh.exe")
        use_tools(env, tools, which=lambda name: str(tmp_path / "bin" / name))
        assert env["PATH"] == str(tmp_path / "Windows")


class TestEntrabotsOwnDotnet:
    """The helper installs the .NET SDK and a365 under %LOCALAPPDATA%\\entrabot\\tools when
    the PC has no SDK, without admin rights. a365.exe is a .NET apphost: started by its own
    .exe it finds a runtime outside Program Files only through DOTNET_ROOT (Microsoft Learn,
    ".NET environment variables")."""

    def _private(self, tmp_path: Path) -> tuple[Path, Path]:
        data = tmp_path / "data"
        return (
            _file(data / "tools" / "a365" / "a365.exe"),
            _file(data / "tools" / "dotnet" / "dotnet.exe"),
        )

    def test_its_own_a365_runs_on_its_own_dotnet(self, tmp_path: Path) -> None:
        a365, dotnet = self._private(tmp_path)
        _file(tmp_path / "pwsh" / "pwsh.exe")
        tools = find_tools(
            {"LOCALAPPDATA": str(tmp_path / "nowhere")},
            which=lambda name: str(tmp_path / "pwsh" / "pwsh.exe") if name == "pwsh" else None,
            data_root=tmp_path / "data",
        )
        assert tools.a365 == a365 and tools.dotnet_root == dotnet.parent
        assert tools.missing == []

    def test_the_a365_steps_get_dotnet_root_and_dotnet_first_on_path(self, tmp_path: Path) -> None:
        a365, dotnet = self._private(tmp_path)
        env = {
            "PATH": "C-Windows",
            "DOTNET_ROOT_X64": "C-somewhere-else",  # would win over DOTNET_ROOT
            "DOTNET_ROOT(x86)": "C-x86",
        }
        tools = WorkIqTools(a365=a365, pwsh=tmp_path / "pwsh.exe", dotnet_root=dotnet.parent)
        use_tools(env, tools, which=lambda name: "on-path" if name == "pwsh" else None)
        assert env["DOTNET_ROOT"] == str(dotnet.parent)
        assert "DOTNET_ROOT_X64" not in env and "DOTNET_ROOT(x86)" not in env
        assert env["PATH"].split(os.pathsep) == [str(dotnet.parent), str(a365.parent), "C-Windows"]

    def test_without_its_own_dotnet_nothing_is_pointed_at_it(self, tmp_path: Path) -> None:
        # a365 installed into Entrabot's folder with an SDK the PC already had: that SDK's
        # runtime is registered for the whole PC, and DOTNET_ROOT would hide it.
        a365 = _file(tmp_path / "data" / "tools" / "a365" / "a365.exe")
        tools = find_tools({}, which=lambda name: None, data_root=tmp_path / "data")
        assert tools.a365 == a365 and tools.dotnet_root is None

    def test_an_a365_the_person_installed_is_not_given_entrabots_dotnet(
        self, tmp_path: Path
    ) -> None:
        self._private(tmp_path)
        tools = find_tools(
            {},
            which=lambda name: "C:/bin/a365.exe" if name == "a365" else None,
            data_root=tmp_path / "data",
        )
        assert tools.a365 == Path("C:/bin/a365.exe") and tools.dotnet_root is None


class TestTheInstallHint:
    def test_it_is_the_repos_helper_not_a_manual_download(self) -> None:
        assert (
            "powershell -ExecutionPolicy Bypass -File scripts\\prereqs-windows.ps1 -WithWorkIq"
            in A365_INSTALL_HINT
        )
        assert "dotnet.microsoft.com" not in A365_INSTALL_HINT
        assert "dotnet tool install" not in A365_INSTALL_HINT
        # What the helper really installs, and that nobody needs to be an administrator.
        for tool in (".NET SDK", "Agent 365 CLI", "PowerShell 7"):
            assert tool in A365_INSTALL_HINT
        assert INSTALL_COMMAND in A365_INSTALL_HINT
        assert "no administrator rights" in A365_INSTALL_HINT
