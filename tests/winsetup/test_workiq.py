"""The optional Microsoft Agent 365 Work IQ step (port of Configure-A365WorkIq)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from entrabot.winsetup.azcli import AzCli, AzLocation
from entrabot.winsetup.helpers import Helpers, Paths
from entrabot.winsetup.workiq import WorkIqError, configure_work_iq, find_a365
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
