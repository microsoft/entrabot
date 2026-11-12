"""``status``: a thin runner for scripts/show_agent_status.py.

It starts the consolidated status script with the virtual-environment interpreter, from the
project folder, with the same Azure CLI on PATH that setup used. It installs nothing, and
its own output never mixes into the script's (``--json`` must stay parseable).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from entrabot.winsetup import status
from tests.winsetup.maintenance_fakes import Project, make_project


class Recorder:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[dict[str, object]] = []

    def __call__(self, argv: list[str], *, cwd: Path, env: dict[str, str]) -> int:
        self.calls.append({"argv": argv, "cwd": cwd, "env": env})
        return self.returncode


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return make_project(tmp_path)


def test_runs_the_status_script_with_the_venv_interpreter_from_the_project_folder(
    project: Project,
) -> None:
    run = Recorder()

    code = status.run_status(project.paths, project.world, run=run)

    assert code == 0
    (call,) = run.calls
    assert call["argv"] == [
        str(project.paths.python),
        str(project.paths.scripts / "show_agent_status.py"),
    ]
    assert call["cwd"] == project.paths.project_root


@pytest.mark.parametrize(
    ("options", "flags"),
    [
        ({"json_output": True}, ["--json"]),
        ({"health_only": True}, ["--health-only"]),
        ({"strict": True}, ["--strict"]),
        (
            {"json_output": True, "health_only": True, "strict": True},
            ["--json", "--health-only", "--strict"],
        ),
    ],
)
def test_flags_are_passed_through(
    project: Project, options: dict[str, bool], flags: list[str]
) -> None:
    run = Recorder()

    status.run_status(project.paths, project.world, run=run, **options)

    assert run.calls[0]["argv"][2:] == flags  # type: ignore[index]


@pytest.mark.parametrize("returncode", [0, 1, 2])
def test_the_scripts_exit_code_is_returned(project: Project, returncode: int) -> None:
    assert status.run_status(project.paths, project.world, run=Recorder(returncode)) == returncode


def test_the_child_finds_the_same_azure_cli_and_prints_utf8(project: Project) -> None:
    run = Recorder()

    status.run_status(project.paths, project.world, run=run)

    env = run.calls[0]["env"]
    assert isinstance(env, dict)
    az_folder = project.env["PATH"]  # the fake az lives in the only folder on PATH
    assert env["PATH"] == f"{Path(az_folder)}{os.pathsep}{az_folder}"  # it leads the PATH
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_without_an_azure_cli_the_script_still_runs(tmp_path: Path) -> None:
    project = make_project(tmp_path, with_az=False)
    run = Recorder()

    assert status.run_status(project.paths, project.world, run=run) == 0
    assert run.calls[0]["env"]["PATH"] == project.env["PATH"]  # type: ignore[index]


def test_a_missing_status_script_is_one_clear_line(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    (project.paths.scripts / "show_agent_status.py").unlink()
    run = Recorder()

    code = status.run_status(project.paths, project.world, run=run)

    assert code == 1
    assert run.calls == []
    captured = capsys.readouterr()
    assert captured.out == ""  # nothing that could be mistaken for the script's JSON
    assert "show_agent_status.py" in captured.err
    assert "setup-windows.cmd" in captured.err


def test_nothing_is_installed_at_status_time(project: Project) -> None:
    run = Recorder()

    status.run_status(project.paths, project.world, run=run)

    assert len(run.calls) == 1
    assert "pip" not in " ".join(run.calls[0]["argv"])  # type: ignore[arg-type]


class TestAFolderThatIsNotSetUp:
    """Right after a teardown (or before any setup) there is no .env and no state file. The
    status script then fails with a developer's message about bootstrapping the Provisioner
    app. That is the moment a non-developer checks what is left, so say it plainly."""

    @pytest.fixture
    def bare(self, tmp_path: Path) -> Project:
        project = make_project(tmp_path, env_lines=[])
        project.paths.state_file.unlink()
        return project

    def test_one_plain_line_and_exit_1(
        self, bare: Project, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run = Recorder()

        code = status.run_status(bare.paths, bare.world, run=run)

        assert code == 1
        assert run.calls == []  # the status script is not started at all
        captured = capsys.readouterr()
        assert captured.err == ""
        (line,) = captured.out.splitlines()
        assert "not connected to an agent" in line
        assert r"scripts\setup-windows.cmd" in line
        assert "Provisioner" not in line and "python" not in line.lower()

    def test_health_only_and_strict_say_the_same(
        self, bare: Project, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = status.run_status(
            bare.paths, bare.world, health_only=True, strict=True, run=Recorder()
        )

        assert code == 1
        assert "not connected to an agent" in capsys.readouterr().out

    def test_json_readers_get_valid_json(
        self, bare: Project, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        run = Recorder()

        code = status.run_status(bare.paths, bare.world, json_output=True, run=run)

        assert code == 1
        assert run.calls == []
        captured = capsys.readouterr()
        assert captured.err == ""
        data = json.loads(captured.out)
        assert data["configured"] is False
        assert "not connected to an agent" in data["message"]
        assert r"scripts\setup-windows.cmd" in data["message"]

    @pytest.mark.parametrize("keep", [".env", ".entrabot-state.json"])
    def test_a_folder_with_either_file_is_passed_through_as_before(
        self, tmp_path: Path, keep: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        project = make_project(tmp_path)
        other = {".env": project.paths.state_file, ".entrabot-state.json": project.paths.env_file}
        other[keep].unlink()
        run = Recorder(returncode=0)

        code = status.run_status(project.paths, project.world, json_output=True, run=run)

        assert code == 0
        assert len(run.calls) == 1  # the status script decides what a half-set-up folder means
        assert capsys.readouterr().out == ""
