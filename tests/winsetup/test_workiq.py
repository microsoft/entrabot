"""Word, Excel and OneDrive files (Microsoft Agent 365 Work IQ), switched on by setup itself.

No Agent 365 CLI, .NET or PowerShell 7: setup writes the manifest Microsoft's tool wrote,
runs the permission script with the Provisioner credential (the grants and the Blueprint's
inheritable permissions), then checks as the agent that Microsoft honours them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from entrabot.a365.catalog import manifest_entry, setup_servers
from entrabot.winsetup.helpers import RESULT_MARKER, Helpers, Paths
from entrabot.winsetup.workiq import (
    CHECK_WAITS,
    PERMISSIONS_REASON,
    WorkIqError,
    configure_work_iq,
    word_tools_configured,
    write_manifest,
)
from tests.winsetup.fakes import FakeSpawn, Reply, make_console, script, snippet

VENV_PYTHON = Path("/venv/Scripts/python.exe")
WORD = "c2d0c2b6-8013-4346-9f8b-b81d3b754a29"
TOOLS = "ea9ffc3e-8a23-4a7d-836d-234d7c7565c1"
OURS = [manifest_entry(server) for server in setup_servers()]


def _results(*, ok: bool) -> Reply:
    rows = [
        {
            "audience": audience,
            "ok": ok,
            "missing": [] if ok else ["Tools.ListInvoke.All"],
            "error": "" if ok else "invalid_grant: AADSTS65001: has not consented",
        }
        for audience in (WORD, TOOLS)
    ]
    return Reply(stdout=RESULT_MARKER + json.dumps(rows))


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.spawn = FakeSpawn()
        self.checks = 0
        self.honoured_after = 1  # 0: never within the wait
        self.slept: list[float] = []
        self.spawn.on(script("ensure_a365_work_iq_permissions.py"), Reply())
        self.spawn.on(snippet("work_iq_token_scopes"), self._check)
        self.console, self.screen = make_console(spawn=self.spawn, log_file=tmp_path / "log.txt")

    def _check(self, _request: object) -> Reply:
        self.checks += 1
        return _results(ok=0 < self.honoured_after <= self.checks)

    @property
    def manifest(self) -> Path:
        return self.root / "ToolingManifest.json"

    def configure(self) -> str:
        paths = Paths(self.root, VENV_PYTHON, self.root / "data")
        return configure_work_iq(
            paths=paths,
            helpers=Helpers(self.console, paths, {}),
            console=self.console,
            blueprint_app_id="bp-app",
            sleep=self.slept.append,
        )

    def calls(self) -> list[list[str]]:
        return [call.argv for call in self.spawn.calls]

    @property
    def out(self) -> str:
        return self.screen.getvalue()


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


class TestTheManifest:
    """What ``a365 develop add-mcp-servers`` did: write ToolingManifest.json."""

    def test_a_new_one_is_the_one_microsofts_tool_wrote(self, tmp_path: Path) -> None:
        path = tmp_path / "ToolingManifest.json"
        write_manifest(path)
        assert json.loads(path.read_text("utf-8")) == {"mcpServers": OURS}

    def test_entries_setup_does_not_own_are_kept_and_its_own_are_corrected(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "ToolingManifest.json"
        theirs = {"mcpServerName": "mcp_CalendarTools", "mcpServerUniqueName": "cal", "x": 1}
        stale_word = {**OURS[0], "scope": "McpServers.Word.All", "audience": ""}
        path.write_text(
            json.dumps({"other": True, "mcpServers": [theirs, stale_word, dict(stale_word)]}),
            encoding="utf-8",
        )
        write_manifest(path)
        written = json.loads(path.read_text("utf-8"))
        assert written["other"] is True
        assert written["mcpServers"] == [theirs, OURS[0], OURS[1], OURS[2]]  # no duplicate

    def test_a_manifest_with_a_byte_order_mark_is_read(self, tmp_path: Path) -> None:
        path = tmp_path / "ToolingManifest.json"
        path.write_text('{"mcpServers":[]}', encoding="utf-8-sig")
        write_manifest(path)
        assert json.loads(path.read_text("utf-8"))["mcpServers"] == OURS

    @pytest.mark.parametrize("content", ["not json", '["a list"]', '{"mcpServers": "x"}'])
    def test_one_it_cannot_read_is_left_alone(self, tmp_path: Path, content: str) -> None:
        path = tmp_path / "ToolingManifest.json"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(WorkIqError):
            write_manifest(path)
        assert path.read_text("utf-8") == content


class TestSwitchingThemOn:
    def test_manifest_then_permissions_then_the_check(self, world: World) -> None:
        assert world.configure() == "on"
        calls = world.calls()
        (permissions,) = [i for i, argv in enumerate(calls) if argv[1].endswith(".py")]
        assert calls[permissions][-2:] == ["--blueprint-app-id", "bp-app"]
        check = next(i for i, argv in enumerate(calls) if argv[1] == "-c")
        assert permissions < check
        assert calls[check][-1] == str(world.manifest)  # the check reads this folder's manifest
        assert json.loads(world.manifest.read_text("utf-8"))["mcpServers"] == OURS
        assert "Word, Excel and OneDrive files are switched on" in world.out

    def test_nothing_of_microsofts_cli_is_used(self, world: World) -> None:
        world.configure()
        assert not any("a365" in Path(argv[0]).name.lower() for argv in world.calls())
        assert not (world.root / "a365.config.json").exists()

    def test_the_permission_script_runs_quietly_with_a_heartbeat(self, world: World) -> None:
        world.configure()
        permissions = script("ensure_a365_work_iq_permissions.py")
        (request,) = [call for call in world.spawn.calls if permissions(call)]
        assert request.heartbeat_seconds  # its own lines go to the log; the screen gets a pulse
        assert "Blueprint SP" not in world.out

    def test_honoured_after_a_wait_is_on(self, world: World) -> None:
        world.honoured_after = 3
        assert world.configure() == "on"
        assert world.checks == 3 and len(world.slept) == 2
        assert "Still waiting for Microsoft" in world.out

    def test_not_honoured_within_about_two_minutes_is_pending(self, world: World) -> None:
        # The token service took 20 to 45 seconds on a real tenant; setup waits longer, then
        # says it is set up and how to check.
        world.honoured_after = 0
        assert world.configure() == "pending"
        assert world.checks == len(CHECK_WAITS) + 1
        assert 100 <= sum(world.slept) <= 150
        assert world.manifest.exists()  # it is set up: nothing to undo

    def test_a_check_that_crashes_is_a_not_yet_not_a_failure(self, world: World) -> None:
        world.spawn.on(snippet("work_iq_token_scopes"), Reply(returncode=1, stderr="Traceback"))
        assert world.configure() == "pending"

    def test_no_token_ever_reaches_the_screen(self, world: World) -> None:
        world.configure()
        assert "eyJ" not in world.out


class TestAFailure:
    def test_a_failed_permission_step_has_a_plain_reason(self, world: World) -> None:
        world.spawn.on(
            script("ensure_a365_work_iq_permissions.py"),
            Reply(returncode=1, stderr="ERROR: ... failed (403): Authorization_RequestDenied"),
        )
        with pytest.raises(WorkIqError) as raised:
            world.configure()
        assert raised.value.reason == PERMISSIONS_REASON
        assert "403" not in raised.value.reason
        assert world.checks == 0

    @pytest.mark.parametrize("before", [None, '{"mcpServers":[{"mcpServerName":"mine"}]}'])
    def test_it_leaves_the_manifest_the_way_it_was(self, world: World, before: str | None) -> None:
        # Otherwise the next run, and the agent, would take Word, Excel and OneDrive to be on.
        if before is not None:
            world.manifest.write_text(before, encoding="utf-8")
        world.spawn.on(script("ensure_a365_work_iq_permissions.py"), Reply(returncode=1))
        with pytest.raises(WorkIqError):
            world.configure()
        if before is None:
            assert not world.manifest.exists() and not word_tools_configured(world.root)
        else:
            assert world.manifest.read_text("utf-8") == before

    def test_without_a_blueprint_nothing_is_done(self, tmp_path: Path) -> None:
        world = World(tmp_path)
        paths = Paths(tmp_path, VENV_PYTHON, tmp_path / "data")
        with pytest.raises(WorkIqError):
            configure_work_iq(
                paths=paths,
                helpers=Helpers(world.console, paths, {}),
                console=world.console,
                blueprint_app_id="",
                sleep=world.slept.append,
            )
        assert world.spawn.calls == [] and not world.manifest.exists()


class TestWordToolsConfigured:
    def test_a_manifest_naming_the_word_server_counts(self, tmp_path: Path) -> None:
        write_manifest(tmp_path / "ToolingManifest.json")
        assert word_tools_configured(tmp_path)

    def test_an_empty_or_missing_manifest_does_not(self, tmp_path: Path) -> None:
        assert not word_tools_configured(tmp_path)
        (tmp_path / "ToolingManifest.json").write_text('{"mcpServers":[]}', encoding="utf-8")
        assert not word_tools_configured(tmp_path)


def test_diagnose_checks_that_microsoft_honours_word_excel_and_onedrive() -> None:
    # -Diagnose gains the same check setup ends with, against this folder's manifest.
    from entrabot.winsetup.helpers import DIAGNOSE_SNIPPET

    assert 'work_iq_manifest=root / "ToolingManifest.json"' in DIAGNOSE_SNIPPET
