"""``python -m entrabot.winsetup``: the flags the PowerShell bootstrap passes through."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from entrabot.winsetup import cli
from entrabot.winsetup.cli import build_parser, main, options_from_args
from entrabot.winsetup.helpers import Helpers, Paths, default_data_root
from entrabot.winsetup.wizard import Options, World
from tests.winsetup.fakes import make_console

BP = "11111111-2222-3333-4444-555555555555"


def _parse(*argv: str) -> Options:
    return options_from_args(build_parser().parse_args(["setup", "--project-root", ".", *argv]))


class TestFlags:
    def test_nothing_but_the_project_root_is_all_defaults(self) -> None:
        opts = _parse()
        assert (opts.new_chain, opts.use_blueprint, opts.memory, opts.yes) == (
            False,
            "",
            None,
            False,
        )
        assert opts.sponsors == [] and opts.hosts is None and opts.project_root.is_absolute()

    def test_every_documented_flag_maps_to_an_option(self) -> None:
        opts = _parse(
            "--new-chain",
            "--upn-suffix",
            "sati",
            "--agent-user-upn",
            "bot@contoso.com",
            "--sponsor",
            "a@contoso.com",
            "--sponsor",
            "b@contoso.com,c@contoso.com",
            "--memory",
            "cloud",
            "--storage-account",
            "acct",
            "--container",
            "box",
            "--location",
            "westus2",
            "--subscription",
            "sub-1",
            "--configure-a365-work-iq",
            "--tenant",
            "werner.ac",
            "--device-code",
            "--hosts",
            "claude",
            "--skip-smoke",
            "--yes",
            "--log-file",
            "setup.log",
        )
        assert opts.new_chain and opts.upn_suffix == "sati"
        assert opts.agent_user_upn == "bot@contoso.com"
        assert opts.sponsors == ["a@contoso.com", "b@contoso.com", "c@contoso.com"]
        assert (opts.memory, opts.storage_account, opts.container) == ("cloud", "acct", "box")
        assert (opts.location, opts.subscription) == ("westus2", "sub-1")
        assert opts.configure_work_iq and opts.device_code and opts.skip_smoke and opts.yes
        assert (opts.tenant, opts.hosts, opts.log_file) == (
            "werner.ac",
            "claude",
            Path("setup.log"),
        )

    def test_verbose_is_off_unless_asked_for(self) -> None:
        assert _parse().verbose is False
        assert _parse("--verbose").verbose is True

    def test_use_blueprint_and_create_new_storage(self) -> None:
        opts = _parse("--use-blueprint", BP, "--memory", "cloud", "--create-new-storage")
        assert opts.use_blueprint == BP and opts.create_new_storage

    def test_new_chain_and_use_blueprint_cannot_be_combined(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exit_info:
            _parse("--new-chain", "--use-blueprint", BP)
        assert exit_info.value.code == 2

    def test_memory_takes_only_local_or_cloud(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            _parse("--memory", "elsewhere")
        assert exit_info.value.code == 2

    def test_the_project_root_is_required(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            build_parser().parse_args(["setup"])
        assert exit_info.value.code == 2


class TestMain:
    def test_setup_is_the_default_subcommand(self) -> None:
        assert cli._with_default_command(["--project-root", "x"]) == [
            "setup",
            "--project-root",
            "x",
        ]
        assert cli._with_default_command(["diagnose", "--project-root", "x"])[0] == "diagnose"
        assert cli._with_default_command(["--help"]) == ["--help"]
        assert cli._with_default_command([]) == ["setup"]

    @pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
    def test_a_folder_that_is_not_the_project_stops_before_anything_and_is_logged(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], platform: str
    ) -> None:
        world = World(env={"HOME": str(tmp_path)}, platform=platform)
        code = main(
            ["--project-root", str(tmp_path), "--yes", "--log-file", str(tmp_path / "s.log")],
            world=world,
        )
        assert code == 1
        assert "looks incomplete" in capsys.readouterr().out
        assert (tmp_path / "s.log").is_file()  # the run is logged where it was told to

    def test_every_command_signs_in_through_the_browser(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}

        class Stub:
            def __init__(self, _options: object, console: object, _paths: object, world: World):
                seen["world"] = world

            def run(self) -> int:
                return 0

        monkeypatch.setattr(cli, "Wizard", Stub)
        world = World(env={"HOME": str(tmp_path)}, platform="win32")
        log = str(tmp_path / "s.log")
        assert main(["--project-root", str(tmp_path), "--yes", "--log-file", log], world=world) == 0
        used = seen["world"]
        assert isinstance(used, World)
        assert used.env["AZURE_CORE_ENABLE_BROKER_ON_WINDOWS"] == "false"
        assert "AZURE_CORE_ENABLE_BROKER_ON_WINDOWS" not in world.env  # the caller's is untouched

    def test_diagnose_on_a_machine_that_was_never_set_up(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        world = World(env={"HOME": str(tmp_path)}, platform="win32")
        code = main(
            ["diagnose", "--project-root", str(tmp_path), "--log-file", str(tmp_path / "d.log")],
            world=world,
        )
        assert code == 1
        assert "has not been set up yet" in capsys.readouterr().out

    def test_the_module_runs_as_a_program(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "entrabot.winsetup", "setup", "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0
        for flag in ("--new-chain", "--use-blueprint", "--memory", "--device-code", "--yes"):
            assert flag in result.stdout


class TestPaths:
    def test_windows_data_lives_under_localappdata(self, tmp_path: Path) -> None:
        env = {"LOCALAPPDATA": str(tmp_path / "Local")}
        assert default_data_root(env, platform="win32") == tmp_path / "Local" / "entrabot"

    def test_windows_without_localappdata_falls_back_to_the_profile(self, tmp_path: Path) -> None:
        root = default_data_root({}, platform="win32", home=tmp_path)
        assert root == tmp_path / "AppData" / "Local" / "entrabot"

    def test_other_systems_use_dot_entrabot(self, tmp_path: Path) -> None:
        assert default_data_root({}, platform="linux", home=tmp_path) == tmp_path / ".entrabot"

    def test_the_server_program_sits_beside_the_interpreter(self, tmp_path: Path) -> None:
        windows = Paths(tmp_path, tmp_path / ".venv" / "Scripts" / "python.exe", tmp_path)
        assert windows.mcp_binary == tmp_path / ".venv" / "Scripts" / "entrabot-mcp.exe"
        posix = Paths(tmp_path, tmp_path / ".venv" / "bin" / "python3", tmp_path)
        assert posix.mcp_binary == tmp_path / ".venv" / "bin" / "entrabot-mcp"


class TestChildEnvironment:
    def _helpers(self, tmp_path: Path, base: dict[str, str]) -> Helpers:
        console, _ = make_console()
        return Helpers(console, Paths(tmp_path, Path("/venv/python"), tmp_path / "data"), base)

    def test_every_child_runs_python_in_utf8_mode(self, tmp_path: Path) -> None:
        # Helpers print check marks; on a cp1252 machine that raises when stdout is a pipe.
        helpers = self._helpers(tmp_path, {"PATH": "x"})
        for env in (helpers.base_env, helpers.provisioning_env(), helpers.runtime_env()):
            assert env["PYTHONUTF8"] == "1" and env["PYTHONIOENCODING"] == "utf-8"

    def test_a_runtime_child_takes_its_settings_from_env_not_from_this_process(
        self, tmp_path: Path
    ) -> None:
        # entrabot.config never overwrites a variable that is already set, so a value inherited
        # from the wizard's own environment would beat the .env that setup just wrote.
        (tmp_path / ".env").write_text("ENTRABOT_AGENT_ID=new\nENTRABOT_TENANT_ID=t\n", "utf-8")
        helpers = self._helpers(
            tmp_path, {"ENTRABOT_AGENT_ID": "stale", "ENTRABOT_LOG_LEVEL": "DEBUG", "PATH": "x"}
        )
        env = helpers.runtime_env()
        assert "ENTRABOT_AGENT_ID" not in env and "ENTRABOT_TENANT_ID" not in env
        assert env["ENTRABOT_LOG_LEVEL"] == "DEBUG" and env["PATH"] == "x"

    def test_the_wizard_package_never_imports_entrabot_config(self) -> None:
        # Importing it loads .env into this process, which every child would then inherit.
        code = (
            "import sys, entrabot.winsetup.cli, entrabot.winsetup.wizard; "
            "sys.exit(1 if 'entrabot.config' in sys.modules else 0)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
        )
        assert result.returncode == 0, result.stderr


class TestFlagsAddedForMacAndLinux:
    """``scripts/setup.sh`` had these before the wizard; they exist on every system now."""

    def test_switch_user_and_replacing_every_certificate_default_to_off(self) -> None:
        opts = _parse()
        assert (opts.switch_user, opts.remove_all_existing_certs) == (False, False)

    def test_they_map_to_options(self) -> None:
        opts = _parse("--switch-user", "--remove-all-existing-certs", "--use-blueprint", BP)
        assert opts.switch_user and opts.remove_all_existing_certs
        assert opts.use_blueprint == BP

    def test_the_work_iq_help_no_longer_asks_for_the_a365_cli(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["setup", "--help"])
        text = capsys.readouterr().out
        assert "needs the a365 CLI" not in text
        assert "--switch-user" in text and "--remove-all-existing-certs" in text


class TestCodeFromAnotherCopyOfTheProject:
    """The runtime reads ``.env`` from the folder its code is in, never from
    ``--project-root``. Seen for real on a Mac: a health check run with one copy's code
    against another copy's folder failed with "Agent User credentials not configured"."""

    def _copy(self, root: Path) -> Path:
        package = root / "src" / "entrabot"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        return root

    def test_another_copy_is_named(self, tmp_path: Path) -> None:
        here, there = self._copy(tmp_path / "here"), self._copy(tmp_path / "there")
        assert cli.code_is_from_another_copy(there, code_root=here) == here.resolve()

    def test_the_same_folder_is_fine(self, tmp_path: Path) -> None:
        here = self._copy(tmp_path / "here")
        assert cli.code_is_from_another_copy(here, code_root=here) is None

    def test_a_folder_with_no_code_of_its_own_is_left_to_the_later_checks(
        self, tmp_path: Path
    ) -> None:
        here = self._copy(tmp_path / "here")
        (tmp_path / "scratch").mkdir()
        assert cli.code_is_from_another_copy(tmp_path / "scratch", code_root=here) is None

    def test_by_default_it_compares_with_the_copy_these_tests_run_from(self) -> None:
        repo = Path(cli.__file__).resolve().parents[3]
        assert cli.code_is_from_another_copy(repo) is None

    @pytest.mark.parametrize(
        "command", [[], ["diagnose"], ["rotate-cert", "--yes"], ["teardown", "--yes"], ["status"]]
    )
    def test_every_command_stops_before_anything_and_says_where_to_run_it(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], command: list[str]
    ) -> None:
        there = self._copy(tmp_path / "another copy")
        (there / ".env").write_text("ENTRABOT_TENANT_ID=t\n", encoding="utf-8")
        world = World(env={"HOME": str(tmp_path)}, platform="darwin")

        code = main([*command, "--project-root", str(there)], world=world)

        captured = capsys.readouterr()
        assert code == 2
        assert "has its own copy of the code" in captured.err
        assert "nothing was changed" in captured.err
        assert f"cd {there.resolve()}" in captured.err and "./scripts/setup.sh" in captured.err
        assert captured.out == ""
        assert (there / ".env").read_text(encoding="utf-8") == "ENTRABOT_TENANT_ID=t\n"
