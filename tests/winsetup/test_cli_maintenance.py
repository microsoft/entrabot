"""The maintenance subcommands of ``python -m entrabot.winsetup``: rotate-cert, teardown, status."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from entrabot.winsetup import cli
from entrabot.winsetup.cli import build_parser, main
from entrabot.winsetup.wizard import World


def _world(tmp_path: Path) -> World:
    return World(
        env={"LOCALAPPDATA": str(tmp_path / "data"), "HOME": str(tmp_path)}, platform="win32"
    )


class TestParsing:
    @pytest.mark.parametrize("command", ["rotate-cert", "teardown", "status"])
    def test_each_is_a_subcommand_not_an_argument_to_setup(self, command: str) -> None:
        assert cli._with_default_command([command, "--project-root", "x"])[0] == command

    @pytest.mark.parametrize("command", ["rotate-cert", "teardown", "status"])
    def test_the_project_root_is_required(self, command: str) -> None:
        with pytest.raises(SystemExit) as exit_info:
            build_parser().parse_args([command])
        assert exit_info.value.code == 2

    def test_rotate_cert_flags(self) -> None:
        args = build_parser().parse_args(
            ["rotate-cert", "--project-root", ".", "--yes", "--verbose"]
        )
        assert (args.yes, args.verbose) == (True, True)

    def test_teardown_is_safe_unless_told_otherwise(self) -> None:
        args = build_parser().parse_args(["teardown", "--project-root", "."])
        assert (args.delete_agent, args.purge_data, args.yes) == (False, False, False)
        assert args.keep_setup_credential is False

    def test_teardown_flags(self) -> None:
        args = build_parser().parse_args(
            ["teardown", "--project-root", ".", "--delete-agent", "--purge-data", "--yes"]
        )
        assert (args.delete_agent, args.purge_data, args.yes) == (True, True, True)

    def test_status_flags(self) -> None:
        args = build_parser().parse_args(
            ["status", "--project-root", ".", "--json", "--health-only", "--strict"]
        )
        assert (args.json, args.health_only, args.strict) == (True, True, True)

    def test_a_setup_flag_is_refused_by_teardown(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            build_parser().parse_args(["teardown", "--project-root", ".", "--new-chain"])
        assert exit_info.value.code == 2


class TestDispatch:
    def test_rotate_cert_runs_the_rotation_and_returns_its_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def fake(console: Any, paths: Any, world: Any) -> int:
            seen.update(console=console, paths=paths, world=world)
            return 3

        monkeypatch.setattr(cli.rotation, "run_rotate_cert", fake)
        world = _world(tmp_path)

        code = main(
            ["rotate-cert", "--project-root", str(tmp_path), "--yes", "--verbose"], world=world
        )

        assert code == 3
        assert seen["paths"].project_root == tmp_path.resolve()
        # The caller's world, with the one addition every command gets: browser sign-in.
        assert seen["world"].platform == world.platform
        assert seen["world"].env == {**world.env, "AZURE_CORE_ENABLE_BROKER_ON_WINDOWS": "false"}
        assert seen["console"].assume_yes is True and seen["console"].verbose is True
        assert seen["console"].log_file.name.startswith("rotate-cert-")
        assert seen["console"].log_file.parent == tmp_path / "data" / "entrabot" / "logs"

    def test_teardown_passes_its_scope_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def fake(console: Any, paths: Any, world: Any, **options: Any) -> int:
            seen.update(console=console, options=options)
            return 0

        monkeypatch.setattr(cli.teardown, "run_teardown", fake)

        code = main(
            ["teardown", "--project-root", str(tmp_path), "--delete-agent", "--yes"],
            world=_world(tmp_path),
        )

        assert code == 0
        assert seen["options"] == {
            "delete_agent": True,
            "purge_data": False,
            "keep_setup_credential": False,
        }
        assert seen["console"].assume_yes is True

    def test_teardown_can_be_told_to_keep_the_setup_credential(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def fake(console: Any, paths: Any, world: Any, **options: Any) -> int:
            seen.update(options=options)
            return 0

        monkeypatch.setattr(cli.teardown, "run_teardown", fake)

        main(
            ["teardown", "--project-root", str(tmp_path), "--keep-setup-credential", "--yes"],
            world=_world(tmp_path),
        )

        assert seen["options"]["keep_setup_credential"] is True

    def test_status_prints_nothing_of_its_own_and_writes_no_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seen: dict[str, Any] = {}

        def fake(paths: Any, world: Any, **options: Any) -> int:
            seen.update(options=options)
            return 1

        monkeypatch.setattr(cli.status, "run_status", fake)

        code = main(
            ["status", "--project-root", str(tmp_path), "--json", "--strict"],
            world=_world(tmp_path),
        )

        assert code == 1
        assert seen["options"] == {"json_output": True, "health_only": False, "strict": True}
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err == ""
        assert not (tmp_path / "data").exists()  # no log folder was created for a status call

    def test_ctrl_c_is_a_cancellation_not_a_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def interrupted(*args: Any, **kwargs: Any) -> int:
            raise KeyboardInterrupt

        monkeypatch.setattr(cli.teardown, "run_teardown", interrupted)

        code = main(["teardown", "--project-root", str(tmp_path), "--yes"], world=_world(tmp_path))

        assert code == 130

    def test_teardown_on_a_computer_that_was_never_set_up(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        world = World(
            env={
                "LOCALAPPDATA": str(tmp_path / "data"),
                "COPILOT_HOME": str(tmp_path / "copilot"),
            },
            platform="win32",
        )

        code = main(["teardown", "--project-root", str(tmp_path), "--yes"], world=world)

        assert code == 0
        assert "Nothing to remove" in capsys.readouterr().out


def test_the_help_lists_every_subcommand() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "entrabot.winsetup", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0
    for command in ("setup", "diagnose", "rotate-cert", "teardown", "status"):
        assert command in result.stdout


def test_the_maintenance_modules_never_import_entrabot_config() -> None:
    # Importing it loads .env into this process, which every child would then inherit.
    code = (
        "import sys, entrabot.winsetup.rotation, entrabot.winsetup.teardown, "
        "entrabot.winsetup.status, entrabot.winsetup.directory; "
        "sys.exit(1 if 'entrabot.config' in sys.modules else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
