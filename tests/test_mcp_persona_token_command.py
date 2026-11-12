"""How ``PERSONA_SATI_MCP_TOKEN_COMMAND`` is turned into a process, on POSIX and on Windows.

The command used to be handed to the OS as one bare program name. That is all a POSIX
config needs, and it must keep working unchanged. On Windows it only starts an ``.exe``,
``.cmd`` or ``.bat`` named in full: a PowerShell or Python script, a name that relies on
``PATHEXT``, or a command with arguments raised ``OSError``, so the persona never loaded
and the heartbeat logged ``token_mint_failed`` every five minutes.

The Windows rules run everywhere: ``_IS_WINDOWS`` is the switch, ``check_output`` is
replaced so no Windows program is needed, and where a test needs a path that only exists
on Windows (``C:\\Program Files\\...``) the lookup of what exists is replaced too. The
POSIX rules are only asserted on POSIX: what counts as a runnable file is decided by the
real platform, so pretending to be POSIX on a Windows host proves nothing.
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from entrabot import mcp_server


class _Recorder:
    """Stands in for ``subprocess.check_output`` and remembers how it was called."""

    def __init__(self, output: str = "fake.jwt.token\n") -> None:
        self.output = output
        self.argv: list[str] | None = None
        self.kwargs: dict[str, Any] = {}

    def __call__(self, argv: list[str], **kwargs: Any) -> str:
        self.argv = argv
        self.kwargs = kwargs
        return self.output


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "check_output", rec)
    return rec


@pytest.fixture
def windows_files(monkeypatch: pytest.MonkeyPatch):
    """Declare which Windows files exist, for paths this machine can't have.

    Stands in for the check that a path is a file, so a test can talk about
    ``C:\\Program Files\\...`` on any host. Names compare case-insensitively, as they do
    on Windows.
    """

    def declare(*files: str) -> None:
        known = {name.casefold() for name in files}
        monkeypatch.setattr(mcp_server, "_is_file", lambda path: path.casefold() in known)

    return declare


@pytest.fixture
def on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "_IS_WINDOWS", True)
    # Pinned so a found program is spelled the same on every host, real Windows included.
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")


IN_BOX_POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


@pytest.fixture
def system_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")


def _program(path: Path) -> Path:
    """Create an empty file the OS would agree to run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# ---------------------------------------------------------------------------
# POSIX: a config that works today is run exactly as it was
# ---------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform == "win32", reason="asserts POSIX rules, which need a POSIX host")
class TestPosixIsUnchanged:
    def test_bare_path_is_passed_as_the_only_argument(self, recorder: _Recorder) -> None:
        token = mcp_server._mint_persona_sati_token("/tmp/fake-token-cli")

        assert token == "fake.jwt.token"
        assert recorder.argv == ["/tmp/fake-token-cli"]
        assert recorder.kwargs["text"] is True
        assert recorder.kwargs["timeout"] == 30
        assert not recorder.kwargs.get("shell")

    def test_path_with_spaces_stays_one_program(self, recorder: _Recorder, tmp_path: Path) -> None:
        tool = _program(tmp_path / "My Tools" / "persona token")

        mcp_server._mint_persona_sati_token(str(tool))

        assert recorder.argv == [str(tool)]

    def test_name_on_path_is_left_for_the_os_to_look_up(
        self, recorder: _Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _program(tmp_path / "persona-token")
        monkeypatch.setenv("PATH", str(tmp_path))

        mcp_server._mint_persona_sati_token("persona-token")

        assert recorder.argv == ["persona-token"]

    def test_missing_command_still_raises_oserror(self) -> None:
        with pytest.raises(OSError):
            mcp_server._mint_persona_sati_token("/no/such/dir/token-cli")

    def test_stdin_is_not_the_servers(self, recorder: _Recorder) -> None:
        """The server's stdin is the MCP stream. A child that read it would eat requests."""
        mcp_server._mint_persona_sati_token("/tmp/fake-token-cli")

        assert recorder.kwargs["stdin"] == subprocess.DEVNULL

    def test_arguments_are_split_by_posix_shell_rules(self, recorder: _Recorder) -> None:
        mcp_server._mint_persona_sati_token(r"token-cli --scope 'api://a b' --path a\ b")

        assert recorder.argv == ["token-cli", "--scope", "api://a b", "--path", "a b"]

    def test_command_with_arguments_really_runs(self, tmp_path: Path) -> None:
        script = tmp_path / "token cli.sh"
        script.write_text('#!/bin/sh\nprintf "tok:%s:%s\\n" "$1" "$2"\n', encoding="utf-8")
        script.chmod(0o700)

        token = mcp_server._mint_persona_sati_token(f'"{script}" --audience "api://persona sati"')

        assert token == "tok:--audience:api://persona sati"


# ---------------------------------------------------------------------------
# Windows: scripts, PATHEXT names and arguments all start
# ---------------------------------------------------------------------------
class TestWindows:
    def test_cmd_file_runs_directly_without_a_shell(
        self, on_windows: None, recorder: _Recorder, tmp_path: Path
    ) -> None:
        tool = _program(tmp_path / "get-token.cmd")

        token = mcp_server._mint_persona_sati_token(str(tool))

        assert token == "fake.jwt.token"
        assert recorder.argv == [str(tool)]
        assert not recorder.kwargs.get("shell")
        assert recorder.kwargs["timeout"] == 30

    def test_unquoted_path_with_spaces_is_one_program(
        self, on_windows: None, recorder: _Recorder, tmp_path: Path
    ) -> None:
        tool = _program(tmp_path / "Jo Smith" / "persona sati" / "get-token.exe")

        mcp_server._mint_persona_sati_token(str(tool))

        assert recorder.argv == [str(tool)]

    def test_quoted_path_with_arguments(
        self, on_windows: None, recorder: _Recorder, tmp_path: Path
    ) -> None:
        tool = _program(tmp_path / "Program Files" / "persona" / "token.cmd")

        mcp_server._mint_persona_sati_token(
            f'"{tool}" --tenant contoso --scope "api://a b/.default"'
        )

        assert recorder.argv == [str(tool), "--tenant", "contoso", "--scope", "api://a b/.default"]

    def test_unquoted_program_files_path_with_arguments(
        self, on_windows: None, recorder: _Recorder, windows_files
    ) -> None:
        """Nobody quotes ``C:\\Program Files``. The program is the longest part that exists."""
        tool = r"C:\Program Files\persona\token.cmd"
        windows_files(tool)

        mcp_server._mint_persona_sati_token(rf'{tool} --flag "a b"')

        assert recorder.argv == [tool, "--flag", "a b"]

    def test_unquoted_path_with_spaces_to_an_existing_file(
        self, on_windows: None, recorder: _Recorder, windows_files
    ) -> None:
        tool = r"C:\Users\Jo Smith\persona sati\get-token.exe"
        windows_files(tool)

        mcp_server._mint_persona_sati_token(tool)

        assert recorder.argv == [tool]

    def test_shorter_program_cannot_hijack_an_unquoted_path(
        self, on_windows: None, recorder: _Recorder, windows_files
    ) -> None:
        """Windows itself would run a planted ``C:\\Program.exe`` here. The longest match wins."""
        tool = r"C:\Program Files\persona\token.cmd"
        windows_files(tool, r"C:\Program.exe")

        mcp_server._mint_persona_sati_token(rf"{tool} --flag")

        assert recorder.argv == [tool, "--flag"]

    def test_arguments_that_look_like_paths_are_not_taken_for_the_program(
        self, on_windows: None, recorder: _Recorder, windows_files
    ) -> None:
        tool = r"C:\tools\token.exe"
        windows_files(tool)

        mcp_server._mint_persona_sati_token(rf"{tool} --cache C:\Users\Jo Smith\cache dir")

        assert recorder.argv == [tool, "--cache", r"C:\Users\Jo", r"Smith\cache", "dir"]

    def test_path_that_does_not_exist_keeps_every_backslash(
        self, on_windows: None, recorder: _Recorder, windows_files
    ) -> None:
        """Nothing to resolve, so nothing is started and the error names the path as written."""
        windows_files()
        command = r"C:\nowhere dir\sub\token.cmd --out C:\Temp\x"

        assert mcp_server._persona_token_words(command) == [
            r"C:\nowhere",
            r"dir\sub\token.cmd",
            "--out",
            r"C:\Temp\x",
        ]
        with pytest.raises(FileNotFoundError) as excinfo:
            mcp_server._mint_persona_sati_token(command)
        assert excinfo.value.filename == r"C:\nowhere"
        assert recorder.argv is None

    def test_existing_file_without_an_extension_is_never_split(
        self, on_windows: None, recorder: _Recorder, tmp_path: Path
    ) -> None:
        """Windows can't run a file with no extension, so this fails to start (fail-soft at
        the call sites). It still isn't cut at its spaces: a value that names a file that
        exists is that file, and the error then names it in full."""
        tool = _program(tmp_path / "My Tools" / "persona token")

        mcp_server._mint_persona_sati_token(str(tool))

        assert recorder.argv == [str(tool)]

    def test_backslashes_in_arguments_survive(
        self, on_windows: None, recorder: _Recorder, tmp_path: Path
    ) -> None:
        tool = _program(tmp_path / "token.exe")

        mcp_server._mint_persona_sati_token(rf"{tool} --cache C:\Users\jo\AppData\Local\persona")

        assert recorder.argv == [str(tool), "--cache", r"C:\Users\jo\AppData\Local\persona"]

    def test_name_is_resolved_through_pathext(
        self, on_windows: None, recorder: _Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``get-token`` meaning ``get-token.cmd``: CreateProcess would only ever try ``.exe``."""
        tool = _program(tmp_path / "bin" / "get-token.CMD")
        monkeypatch.setenv("PATH", str(tool.parent))
        monkeypatch.chdir(tmp_path)

        mcp_server._mint_persona_sati_token("get-token --audience api://persona")

        assert recorder.argv == [str(tool), "--audience", "api://persona"]

    def test_runnable_file_wins_over_a_same_named_file_with_no_extension(
        self, on_windows: None, recorder: _Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """npm-style shims put a shell script ``tool`` beside ``tool.cmd``: the ``.cmd`` runs."""
        _program(tmp_path / "bin" / "get-token")
        tool = _program(tmp_path / "bin" / "get-token.CMD")
        monkeypatch.setenv("PATH", str(tool.parent))
        monkeypatch.chdir(tmp_path)

        mcp_server._mint_persona_sati_token("get-token")

        assert recorder.argv == [str(tool)]

    def test_powershell_script_runs_under_the_in_box_powershell(
        self, on_windows: None, system_root: None, recorder: _Recorder, tmp_path: Path
    ) -> None:
        script = _program(tmp_path / "Get Token.ps1")

        mcp_server._mint_persona_sati_token(f'"{script}" -Audience api://persona')

        assert recorder.argv == [
            IN_BOX_POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "RemoteSigned",
            "-File",
            str(script),
            "-Audience",
            "api://persona",
        ]

    def test_powershell_7_on_path_is_not_picked_for_a_script(
        self,
        on_windows: None,
        system_root: None,
        recorder: _Recorder,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One interpreter, always the same one. A script that needs 7 says ``pwsh -File``."""
        _program(tmp_path / "ps7" / "pwsh.EXE")
        monkeypatch.setenv("PATH", str(tmp_path / "ps7"))
        script = _program(tmp_path / "get-token.ps1")

        mcp_server._mint_persona_sati_token(str(script))

        assert recorder.argv is not None
        assert recorder.argv[0] == IN_BOX_POWERSHELL

    def test_explicit_pwsh_is_found_on_path(
        self,
        on_windows: None,
        recorder: _Recorder,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pwsh = _program(tmp_path / "ps7" / "pwsh.EXE")
        monkeypatch.setenv("PATH", str(pwsh.parent))
        monkeypatch.chdir(tmp_path)
        script = _program(tmp_path / "get-token.ps1")

        mcp_server._mint_persona_sati_token(f'pwsh -NoProfile -File "{script}"')

        assert recorder.argv == [str(pwsh), "-NoProfile", "-File", str(script)]

    def test_system_root_falls_back_to_windir_then_the_default(
        self, on_windows: None, recorder: _Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script = _program(tmp_path / "get-token.ps1")
        monkeypatch.delenv("SYSTEMROOT", raising=False)
        monkeypatch.setenv("WINDIR", r"D:\WinNT")

        mcp_server._mint_persona_sati_token(str(script))
        assert recorder.argv is not None
        assert recorder.argv[0] == r"D:\WinNT\System32\WindowsPowerShell\v1.0\powershell.exe"

        monkeypatch.delenv("WINDIR")
        mcp_server._mint_persona_sati_token(str(script))
        assert recorder.argv[0] == IN_BOX_POWERSHELL

    def test_python_script_runs_under_this_interpreter(
        self, on_windows: None, recorder: _Recorder, tmp_path: Path
    ) -> None:
        script = _program(tmp_path / "get_token.py")

        mcp_server._mint_persona_sati_token(f"{script} --audience api://persona")

        assert recorder.argv == [sys.executable, str(script), "--audience", "api://persona"]

    def test_script_that_pathext_does_not_list_is_still_found(
        self, on_windows: None, recorder: _Recorder, tmp_path: Path
    ) -> None:
        """``shutil.which`` goes by PATHEXT on Windows, and PATHEXT never lists ``.ps1``."""
        script = tmp_path / "get-token.ps1"
        script.write_text("", encoding="utf-8")
        script.chmod(0o600)  # as PATHEXT would have it: not "executable"

        mcp_server._mint_persona_sati_token(f"{script} -Audience api://persona")

        assert recorder.argv is not None
        assert recorder.argv[-4:] == ["-File", str(script), "-Audience", "api://persona"]

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            (r"C:\bin\t.exe --cache C:\Users\jo", [r"C:\bin\t.exe", "--cache", r"C:\Users\jo"]),
            (r'"C:\Program Files\t.cmd"  -a   b', [r"C:\Program Files\t.cmd", "-a", "b"]),
            (r't.exe --scope="api://a b"', ["t.exe", "--scope=api://a b"]),
            (r't.exe --json "{\"k\": 1}"', ["t.exe", "--json", '{"k": 1}']),
            (r't.exe ""', ["t.exe", ""]),
            ("   ", []),
        ],
    )
    def test_command_line_is_split_by_windows_rules(
        self, command: str, expected: list[str]
    ) -> None:
        assert mcp_server._split_windows_command(command) == expected

    def test_unbalanced_quote_is_not_guessed_at(
        self, on_windows: None, recorder: _Recorder
    ) -> None:
        command = 'token.exe --scope "api://persona'

        assert mcp_server._persona_token_words(command) == [command]
        with pytest.raises(FileNotFoundError):
            mcp_server._mint_persona_sati_token(command)
        assert recorder.argv is None

    def test_no_console_window_is_opened(
        self, on_windows: None, recorder: _Recorder, tmp_path: Path
    ) -> None:
        """A host with no console would otherwise flash one every heartbeat."""
        mcp_server._mint_persona_sati_token(str(_program(tmp_path / "token.exe")))

        assert recorder.kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
        assert recorder.kwargs["stdin"] == subprocess.DEVNULL

    def test_command_that_cannot_be_found_is_never_handed_to_windows(
        self, on_windows: None, recorder: _Recorder, windows_files
    ) -> None:
        """Windows would go looking for it, starting with the current directory."""
        windows_files()

        with pytest.raises(FileNotFoundError) as excinfo:
            mcp_server._mint_persona_sati_token(r"C:\nowhere\token-cli.exe")

        assert excinfo.value.filename == r"C:\nowhere\token-cli.exe"
        assert recorder.argv is None


# ---------------------------------------------------------------------------
# Windows: nothing is ever run out of the current directory
# ---------------------------------------------------------------------------
class TestWindowsNeverRunsFromTheCurrentDirectory:
    """The server runs wherever its host was started, which may be a checkout nobody vetted.

    Windows looks in the current directory before ``PATH``, both when it starts a program
    and in ``shutil.which``. A file there named like PowerShell, or like the configured
    command, was run at boot and on every heartbeat. On Windows the command must be an
    absolute path or a program in an absolute ``PATH`` directory other than this one.
    """

    @pytest.fixture
    def untrusted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """A current directory with everything planted in it that could be picked up."""
        cwd = tmp_path / "untrusted checkout"
        for name in ("pwsh", "powershell", "get-token"):
            for extension in (".CMD", ".EXE", ".BAT", ".COM", ""):
                _program(cwd / f"{name}{extension}")
        _program(cwd / "sub" / "get-token.CMD")
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        return cwd

    def test_planted_powershell_is_not_used_for_a_script(
        self,
        on_windows: None,
        system_root: None,
        recorder: _Recorder,
        untrusted: Path,
        tmp_path: Path,
    ) -> None:
        script = _program(tmp_path / "persona" / "get-token.ps1")

        mcp_server._mint_persona_sati_token(str(script))

        assert recorder.argv is not None
        assert recorder.argv[0] == IN_BOX_POWERSHELL
        assert str(untrusted) not in " ".join(recorder.argv)

    def test_planted_file_named_like_the_command_is_not_run(
        self, on_windows: None, recorder: _Recorder, untrusted: Path
    ) -> None:
        with pytest.raises(FileNotFoundError) as excinfo:
            mcp_server._mint_persona_sati_token("get-token --audience api://persona")

        assert excinfo.value.filename == "get-token"
        assert recorder.argv is None, "nothing may be started when the command isn't found"

    @pytest.mark.parametrize("path_entry", [".", "", "{cwd}", "sub"])
    def test_current_directory_listed_on_path_is_still_skipped(
        self,
        on_windows: None,
        recorder: _Recorder,
        untrusted: Path,
        monkeypatch: pytest.MonkeyPatch,
        path_entry: str,
    ) -> None:
        """``.``, an empty entry, a relative entry, or this directory by name: all mean "here"."""
        monkeypatch.setenv("PATH", path_entry.format(cwd=untrusted))

        with pytest.raises(FileNotFoundError):
            mcp_server._mint_persona_sati_token("get-token")

        assert recorder.argv is None

    @pytest.mark.skipif(sys.platform == "win32", reason="creating a symlink needs privilege")
    def test_path_entry_that_links_to_the_current_directory_is_skipped(
        self,
        on_windows: None,
        recorder: _Recorder,
        untrusted: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        link = tmp_path / "looks-harmless"
        link.symlink_to(untrusted, target_is_directory=True)
        monkeypatch.setenv("PATH", str(link))

        with pytest.raises(FileNotFoundError):
            mcp_server._mint_persona_sati_token("get-token")

        assert recorder.argv is None

    def test_program_on_path_is_chosen_over_the_planted_one(
        self,
        on_windows: None,
        recorder: _Recorder,
        untrusted: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tool = _program(tmp_path / "bin" / "get-token.CMD")
        monkeypatch.setenv("PATH", os.pathsep.join([".", str(untrusted), str(tool.parent)]))

        mcp_server._mint_persona_sati_token("get-token --audience api://persona")

        assert recorder.argv == [str(tool), "--audience", "api://persona"]

    @pytest.mark.parametrize(
        "relative",
        [r".\get-token.CMD", "./get-token.CMD", r"sub\get-token.CMD", "sub/get-token.CMD"],
    )
    def test_relative_path_is_refused(
        self, on_windows: None, recorder: _Recorder, untrusted: Path, relative: str
    ) -> None:
        """A relative path means a different file in every directory the host is started in."""
        with pytest.raises(FileNotFoundError):
            mcp_server._mint_persona_sati_token(relative)

        assert recorder.argv is None

    def test_absolute_path_still_works(
        self, on_windows: None, recorder: _Recorder, untrusted: Path, tmp_path: Path
    ) -> None:
        tool = _program(tmp_path / "persona" / "get-token.CMD")

        mcp_server._mint_persona_sati_token(f'"{tool}" --audience api://persona')

        assert recorder.argv == [str(tool), "--audience", "api://persona"]

    def test_boot_falls_back_without_starting_anything(
        self,
        on_windows: None,
        recorder: _Recorder,
        untrusted: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("PERSONA_SATI_MCP_URL", "https://persona.example")
        monkeypatch.setenv("PERSONA_SATI_MCP_TOKEN_COMMAND", "get-token")

        mcp_server._load_agent_instructions()

        assert recorder.argv is None
        assert "could not mint persona-sati token (get-token)" in capsys.readouterr().err


class TestWindowsPowerShellEnvironment:
    """``PSModulePath`` inherited from PowerShell 7 breaks module loading in 5.1."""

    def test_psmodulepath_is_not_passed_to_windows_powershell(
        self,
        on_windows: None,
        system_root: None,
        recorder: _Recorder,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PSModulePath", r"C:\Program Files\PowerShell\7\Modules")
        monkeypatch.setenv("ENTRABOT_KEPT", "yes")

        mcp_server._mint_persona_sati_token(str(_program(tmp_path / "get-token.ps1")))

        env = recorder.kwargs["env"]
        assert "psmodulepath" not in {name.casefold() for name in env}
        assert env["ENTRABOT_KEPT"] == "yes"
        assert env["SYSTEMROOT"] == r"C:\Windows"

    def test_explicit_windows_powershell_gets_the_same_treatment(
        self,
        on_windows: None,
        recorder: _Recorder,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        powershell = _program(tmp_path / "System32" / "powershell.EXE")
        monkeypatch.setenv("PATH", str(powershell.parent))
        monkeypatch.setenv("PSModulePath", r"C:\Program Files\PowerShell\7\Modules")
        monkeypatch.chdir(tmp_path)

        mcp_server._mint_persona_sati_token("powershell -File C:\\persona\\get-token.ps1")

        assert recorder.argv is not None and recorder.argv[0] == str(powershell)
        assert "psmodulepath" not in {name.casefold() for name in recorder.kwargs["env"]}

    def test_other_programs_inherit_the_environment_untouched(
        self,
        on_windows: None,
        recorder: _Recorder,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PSModulePath", r"C:\Program Files\PowerShell\7\Modules")

        mcp_server._mint_persona_sati_token(str(_program(tmp_path / "get-token.exe")))

        assert "env" not in recorder.kwargs


# ---------------------------------------------------------------------------
# Failures never carry the command's arguments or output
# ---------------------------------------------------------------------------
class TestFailuresAreQuiet:
    SECRET_ARG = "s3cr3t-client-secret"
    PARTIAL_TOKEN = "eyJhbGciOi.partial.token"

    @pytest.fixture
    def command(self, tmp_path: Path) -> str:
        """A token command with a secret on its command line, naming a program that exists."""
        return f'"{_program(tmp_path / "token-cli.exe")}" --secret {self.SECRET_ARG}'

    def _fail_with(self, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
        def _raise(argv: list[str], **kwargs: Any) -> str:
            raise exc

        monkeypatch.setattr(subprocess, "check_output", _raise)

    def test_non_zero_exit_reports_only_the_status(
        self, monkeypatch: pytest.MonkeyPatch, command: str
    ) -> None:
        self._fail_with(
            monkeypatch,
            subprocess.CalledProcessError(
                3, ["token-cli", "--secret", self.SECRET_ARG], output=self.PARTIAL_TOKEN
            ),
        )

        with pytest.raises(subprocess.SubprocessError) as excinfo:
            mcp_server._mint_persona_sati_token(command)

        assert "3" in str(excinfo.value)
        assert self.SECRET_ARG not in str(excinfo.value)
        assert self.PARTIAL_TOKEN not in str(excinfo.value)
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True

    def test_timeout_reports_only_the_limit(
        self, monkeypatch: pytest.MonkeyPatch, command: str
    ) -> None:
        self._fail_with(
            monkeypatch,
            subprocess.TimeoutExpired(
                ["token-cli", "--secret", self.SECRET_ARG], 30, output=self.PARTIAL_TOKEN
            ),
        )

        with pytest.raises(subprocess.SubprocessError) as excinfo:
            mcp_server._mint_persona_sati_token(command)

        assert "30" in str(excinfo.value)
        assert self.SECRET_ARG not in str(excinfo.value)
        assert self.PARTIAL_TOKEN not in str(excinfo.value)

    def test_boot_failure_names_the_program_but_not_its_arguments(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
        command: str,
    ) -> None:
        monkeypatch.setenv("PERSONA_SATI_MCP_URL", "https://persona.example")
        monkeypatch.setenv("PERSONA_SATI_MCP_TOKEN_COMMAND", command)
        self._fail_with(
            monkeypatch,
            subprocess.CalledProcessError(
                1, ["token-cli", "--secret", self.SECRET_ARG], output=self.PARTIAL_TOKEN
            ),
        )

        with caplog.at_level(logging.DEBUG, logger="entrabot"):
            mcp_server._load_agent_instructions()

        seen = capsys.readouterr().err + "\n".join(r.getMessage() for r in caplog.records)
        assert "could not mint persona-sati token" in seen
        assert "token-cli" in seen
        assert self.SECRET_ARG not in seen
        assert self.PARTIAL_TOKEN not in seen

    async def test_heartbeat_failure_names_the_program_but_not_its_arguments(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, command: str
    ) -> None:
        monkeypatch.setenv("PERSONA_SATI_MCP_URL", "https://persona.example")
        monkeypatch.setenv("PERSONA_SATI_MCP_TOKEN_COMMAND", command)
        self._fail_with(
            monkeypatch,
            subprocess.CalledProcessError(
                1, ["token-cli", "--secret", self.SECRET_ARG], output=self.PARTIAL_TOKEN
            ),
        )

        with caplog.at_level(logging.DEBUG, logger="entrabot"):
            outcome = await mcp_server._persona_sati_heartbeat_once()

        seen = "\n".join(r.getMessage() for r in caplog.records)
        assert outcome == "token_mint_failed"
        assert "token-cli" in seen
        assert self.SECRET_ARG not in seen
        assert self.PARTIAL_TOKEN not in seen

    async def test_heartbeat_empty_output_names_the_program_but_not_its_arguments(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, command: str
    ) -> None:
        monkeypatch.setenv("PERSONA_SATI_MCP_URL", "https://persona.example")
        monkeypatch.setenv("PERSONA_SATI_MCP_TOKEN_COMMAND", command)
        monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "\n")

        with caplog.at_level(logging.DEBUG, logger="entrabot"):
            outcome = await mcp_server._persona_sati_heartbeat_once()

        seen = "\n".join(r.getMessage() for r in caplog.records)
        assert outcome == "token_mint_failed"
        assert self.SECRET_ARG not in seen


@pytest.mark.skipif(sys.platform != "win32", reason="starts real Windows scripts")
class TestRealWindows:
    """No simulation: these start a batch file, a PowerShell script and a Python script."""

    def test_batch_file_with_arguments(self, tmp_path: Path) -> None:
        tool = tmp_path / "token dir" / "get-token.cmd"
        tool.parent.mkdir()
        tool.write_text("@echo off\r\necho tok:%1:%2\r\n", encoding="ascii")

        assert mcp_server._mint_persona_sati_token(f'"{tool}" --aud api') == "tok:--aud:api"

    def test_powershell_script(self, tmp_path: Path) -> None:
        tool = tmp_path / "token dir" / "get-token.ps1"
        tool.parent.mkdir()
        tool.write_text("Write-Output ('tok:' + $args[0])\r\n", encoding="ascii")

        assert mcp_server._mint_persona_sati_token(f'"{tool}" api') == "tok:api"

    def test_python_script(self, tmp_path: Path) -> None:
        tool = tmp_path / "get_token.py"
        tool.write_text("import sys\nprint('tok:' + sys.argv[1])\n", encoding="ascii")

        assert mcp_server._mint_persona_sati_token(f"{tool} api") == "tok:api"

    def test_name_without_extension_on_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "get-token.cmd").write_text("@echo off\r\necho tok\r\n", encoding="ascii")
        monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])

        assert mcp_server._mint_persona_sati_token("get-token") == "tok"
