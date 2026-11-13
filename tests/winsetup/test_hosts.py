"""Registering the MCP server with the assistants, and checking the host is ready for it."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from entrabot.winsetup.console import RunRequest
from entrabot.winsetup.helpers import Paths
from entrabot.winsetup.hosts import (
    HostError,
    HostReport,
    check_hosts,
    find_claude,
    find_git_bash,
    hook_self_test,
    parse_hosts,
    register_mcp,
    run_prereqs_helper,
    session_start_hooks,
)
from tests.winsetup.fakes import FakeSpawn, Reply, make_console, program


class TestThePrereqsHelper:
    """The one way setup installs extras: the repo's helper, through the in-box PowerShell."""

    def test_it_runs_the_switches_it_is_given_and_reports_its_exit_code(
        self, tmp_path: Path
    ) -> None:
        paths = _project(tmp_path)
        spawn = FakeSpawn()
        spawn.on(program("powershell.exe"), Reply(returncode=0))
        console, _ = make_console(spawn=spawn, typed="")
        env = {"SystemRoot": "C:\\Windows", "PSModulePath": "C:\\pwsh7\\Modules", "KEEP": "1"}
        assert run_prereqs_helper(paths, console, env, ["-WithWorkIq"]) is True
        (call,) = spawn.calls
        assert program("powershell.exe")(call) and call.interactive
        assert call.argv[1:5] == ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
        assert Path(call.argv[5]).name == "prereqs-windows.ps1" and call.argv[6:] == ["-WithWorkIq"]
        assert call.env is not None and "PSModulePath" not in call.env and call.env["KEEP"] == "1"
        spawn.on(program("powershell.exe"), Reply(returncode=1))
        assert run_prereqs_helper(paths, console, env, ["-WithWorkIq"]) is False

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_COMMAND = (
    'ENTRABOT_HOOK=inject_body_prompt.py; . "$CLAUDE_PROJECT_DIR/scripts/hooks/run-hook.sh"'
)
INJECTED = json.dumps(
    {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "body"}}
)


def _project(tmp_path: Path, *, with_binary: bool = True, hooks: dict | None = None) -> Paths:
    root = tmp_path / "proj"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "scripts" / "mcp_config.py", root / "scripts" / "mcp_config.py")
    venv = root / ".venv" / "Scripts"
    venv.mkdir(parents=True)
    (venv / "python.exe").write_bytes(b"")
    if with_binary:
        (venv / "entrabot-mcp.exe").write_bytes(b"")
    if hooks is not None:
        (root / ".claude").mkdir()
        (root / ".claude" / "settings.json").write_text(json.dumps({"hooks": hooks}), "utf-8")
    return Paths(root, venv / "python.exe", tmp_path / "data")


def _copilot_env(tmp_path: Path) -> dict[str, str]:
    return {"COPILOT_HOME": str(tmp_path / "copilot")}


def _session_start(command: str = HOOK_COMMAND, shell: str | None = "bash") -> dict:
    hook = {"type": "command", "command": command}
    if shell:
        hook["shell"] = shell
    return {"SessionStart": [{"hooks": [hook]}]}


class TestParseHosts:
    def test_default_is_both(self) -> None:
        assert parse_hosts(None) == ["claude", "copilot"]

    def test_a_list_is_split_and_normalised(self) -> None:
        assert parse_hosts(" Claude , copilot ") == ["claude", "copilot"]
        assert parse_hosts("copilot") == ["copilot"]

    @pytest.mark.parametrize("bad", ["", "cursor", "claude,vscode"])
    def test_unknown_hosts_are_refused(self, bad: str) -> None:
        with pytest.raises(HostError):
            parse_hosts(bad)


class TestRegister:
    def test_both_hosts_get_the_same_entry(self, tmp_path: Path) -> None:
        paths = _project(tmp_path)
        written = register_mcp(paths, ["claude", "copilot"], env=_copilot_env(tmp_path))
        claude = json.loads(paths.claude_mcp.read_text(encoding="utf-8"))
        copilot = json.loads((tmp_path / "copilot" / "mcp-config.json").read_text("utf-8"))
        assert claude["mcpServers"]["entrabot"]["command"] == str(paths.mcp_binary)
        assert claude["mcpServers"]["entrabot"] == copilot["mcpServers"]["entrabot"]
        assert written == [paths.claude_mcp, tmp_path / "copilot" / "mcp-config.json"]

    def test_only_the_chosen_host_is_touched(self, tmp_path: Path) -> None:
        paths = _project(tmp_path)
        register_mcp(paths, ["claude"], env=_copilot_env(tmp_path))
        assert paths.claude_mcp.is_file()
        assert not (tmp_path / "copilot").exists()

    def test_the_copilot_config_follows_the_wizards_environment_not_the_ambient_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The wizard is handed an environment; a path read from os.environ instead would
        # write to wherever the calling shell happens to point.
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "ambient"))
        paths = _project(tmp_path)
        register_mcp(paths, ["copilot"], env={"COPILOT_HOME": str(tmp_path / "given")})
        assert (tmp_path / "given" / "mcp-config.json").is_file()
        assert not (tmp_path / "ambient").exists()
        assert not paths.claude_mcp.exists()  # only the chosen host

    def test_other_servers_and_an_operators_env_block_survive(self, tmp_path: Path) -> None:
        paths = _project(tmp_path)
        paths.claude_mcp.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "persona-sati": {"command": "launcher"},
                        "entrabot": {"command": "old", "env": {"PERSONA_SATI_MCP_URL": "u"}},
                    }
                }
            ),
            encoding="utf-8",
        )
        register_mcp(paths, ["claude"], env=_copilot_env(tmp_path))
        servers = json.loads(paths.claude_mcp.read_text(encoding="utf-8"))["mcpServers"]
        assert servers["persona-sati"] == {"command": "launcher"}
        assert servers["entrabot"]["env"] == {"PERSONA_SATI_MCP_URL": "u"}
        assert servers["entrabot"]["command"] == str(paths.mcp_binary)

    def test_a_missing_server_binary_is_an_error(self, tmp_path: Path) -> None:
        paths = _project(tmp_path, with_binary=False)
        with pytest.raises(HostError, match="entrabot-mcp"):
            register_mcp(paths, ["claude"], env={})


class TestFindGitBash:
    def test_claude_codes_own_override_wins(self, tmp_path: Path) -> None:
        bash = tmp_path / "custom" / "bash.exe"
        bash.parent.mkdir()
        bash.write_bytes(b"")
        env = {"CLAUDE_CODE_GIT_BASH_PATH": str(bash)}
        assert find_git_bash(env, platform="win32", which=lambda name: None) == bash

    def test_the_standard_install_folder_is_checked(self, tmp_path: Path) -> None:
        bash = tmp_path / "Git" / "bin" / "bash.exe"
        bash.parent.mkdir(parents=True)
        bash.write_bytes(b"")
        env = {"ProgramFiles": str(tmp_path)}
        assert find_git_bash(env, platform="win32", which=lambda name: None) == bash

    def test_a_per_user_install_is_found_through_git_on_path(self, tmp_path: Path) -> None:
        git = tmp_path / "Programs" / "Git" / "cmd" / "git.exe"
        bash = tmp_path / "Programs" / "Git" / "bin" / "bash.exe"
        for item in (git, bash):
            item.parent.mkdir(parents=True, exist_ok=True)
            item.write_bytes(b"")
        found = find_git_bash(
            {}, platform="win32", which=lambda name: str(git) if name == "git" else None
        )
        assert found == bash

    def test_bash_on_path_is_never_trusted_on_windows(self, tmp_path: Path) -> None:
        # C:\Windows\System32\bash.exe is the WSL launcher, not Git Bash.
        found = find_git_bash(
            {},
            platform="win32",
            which=lambda name: r"C:\Windows\System32\bash.exe" if name == "bash" else None,
        )
        assert found is None

    def test_posix_uses_the_shell_on_path(self) -> None:
        found = find_git_bash({}, platform="linux", which=lambda name: f"/bin/{name}")
        assert found == Path("/bin/bash")


class TestFindClaude:
    def test_on_path(self) -> None:
        assert find_claude({}, platform="win32", which=lambda name: "C:/x/claude.exe") == Path(
            "C:/x/claude.exe"
        )

    def test_the_native_installers_folder_is_checked(self, tmp_path: Path) -> None:
        exe = tmp_path / ".local" / "bin" / "claude.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        env = {"USERPROFILE": str(tmp_path)}
        assert find_claude(env, platform="win32", which=lambda name: None) == exe

    def test_missing_is_none(self) -> None:
        assert find_claude({}, platform="win32", which=lambda name: None) is None


class TestHookSelfTest:
    def test_the_real_settings_file_has_a_session_start_hook(self) -> None:
        hooks = session_start_hooks(REPO_ROOT / ".claude" / "settings.json")
        assert hooks and all(hook.command for hook in hooks)

    def test_a_missing_settings_file_has_none(self, tmp_path: Path) -> None:
        assert session_start_hooks(tmp_path / "nope.json") == []

    def test_the_hook_runs_exactly_as_claude_code_would_run_it(self, tmp_path: Path) -> None:
        paths = _project(tmp_path, hooks=_session_start())
        spawn = FakeSpawn()
        spawn.default = Reply(stdout=INJECTED)
        console, _ = make_console(spawn=spawn)
        bash = Path("C:/Git/bin/bash.exe")
        result = hook_self_test(paths, console, bash=bash, env={"A": "1"})
        assert result.status == "pass"
        request = spawn.calls[0]
        assert request.argv == [str(bash), "-c", HOOK_COMMAND]
        assert request.env is not None
        assert request.env["CLAUDE_PROJECT_DIR"] == paths.project_root.as_posix()
        assert request.cwd == paths.project_root
        assert json.loads(request.input_text or "")["hook_event_name"] == "SessionStart"

    def test_the_injected_prompt_is_not_dumped_into_the_log(self, tmp_path: Path) -> None:
        paths = _project(tmp_path, hooks=_session_start())
        spawn = FakeSpawn()
        spawn.default = Reply(stdout=INJECTED)
        log = tmp_path / "setup.log"
        console, out = make_console(spawn=spawn, log_file=log)
        hook_self_test(paths, console, bash=Path("bash"), env={})
        console.close()
        assert "additionalContext" not in out.getvalue() + log.read_text(encoding="utf-8")

    def test_a_hook_that_exits_non_zero_fails_with_its_message(self, tmp_path: Path) -> None:
        paths = _project(tmp_path, hooks=_session_start())
        spawn = FakeSpawn()
        spawn.default = Reply(returncode=2, stderr="entrabot hook launcher: no Python interpreter")
        console, _ = make_console(spawn=spawn)
        result = hook_self_test(paths, console, bash=Path("bash"), env={})
        assert result.status == "fail" and "no Python interpreter" in result.detail

    def test_a_hook_that_injects_nothing_fails(self, tmp_path: Path) -> None:
        # Exit 0 with no output means the session would start without the body prompt.
        paths = _project(tmp_path, hooks=_session_start())
        console, _ = make_console(spawn=FakeSpawn())
        assert hook_self_test(paths, console, bash=Path("bash"), env={}).status == "fail"

    def test_no_bash_means_the_bash_hooks_cannot_run(self, tmp_path: Path) -> None:
        paths = _project(tmp_path, hooks=_session_start())
        spawn = FakeSpawn()
        console, _ = make_console(spawn=spawn)
        result = hook_self_test(paths, console, bash=None, env={})
        assert result.status == "fail" and "Git for Windows" in result.detail
        assert spawn.calls == []

    def test_a_powershell_hook_does_not_inherit_powershell_7s_module_path(
        self, tmp_path: Path
    ) -> None:
        paths = _project(tmp_path, hooks=_session_start("Write-Output hi", shell="powershell"))
        spawn = FakeSpawn()
        spawn.default = Reply(stdout=INJECTED)
        console, _ = make_console(spawn=spawn)
        env = {"PSModulePath": "C:\\Program Files\\PowerShell\\7\\Modules", "KEEP": "1"}
        assert hook_self_test(paths, console, bash=None, env=env).status == "pass"
        request = spawn.calls[0]
        assert program("powershell.exe")(request) and request.env is not None
        assert "PSModulePath" not in request.env and request.env["KEEP"] == "1"
        assert request.env["CLAUDE_PROJECT_DIR"] == paths.project_root.as_posix()

    def test_a_bash_hook_keeps_its_environment_as_it_is(self, tmp_path: Path) -> None:
        paths = _project(tmp_path, hooks=_session_start())
        spawn = FakeSpawn()
        spawn.default = Reply(stdout=INJECTED)
        console, _ = make_console(spawn=spawn)
        hook_self_test(paths, console, bash=Path("bash"), env={"PSModulePath": "x"})
        assert spawn.calls[0].env is not None and spawn.calls[0].env["PSModulePath"] == "x"

    def test_no_hooks_is_skipped(self, tmp_path: Path) -> None:
        paths = _project(tmp_path, hooks={})
        console, _ = make_console(spawn=FakeSpawn())
        assert hook_self_test(paths, console, bash=Path("bash"), env={}).status == "skipped"

    @pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
    def test_the_repos_own_hook_passes_for_real(self) -> None:
        # No fake: the checked-in settings.json, the launcher and the hook script, through a
        # real shell. This is the test that fails if a hook command stops being runnable.
        import io
        import sys

        from entrabot.winsetup.console import Console

        console = Console(stdin=io.StringIO(), stdout=io.StringIO(), color=False, unicode=False)
        paths = Paths(REPO_ROOT, Path(sys.executable), REPO_ROOT / ".nonexistent-data")
        import os

        result = hook_self_test(
            paths, console, bash=Path(shutil.which("sh") or "sh"), env=os.environ
        )
        assert result.status == "pass", result.detail


class TestCheckHosts:
    """Is the computer ready for Claude Code, and what is done about it when it is not.

    Interactively, setup offers to install what is missing through the repo's own helper
    (``scripts\\prereqs-windows.ps1``), which works with or without winget. Unattended, it
    never installs anything and prints that one helper command. Raw ``winget`` lines are never
    printed: winget does not exist for every user.
    """

    def _world(
        self,
        tmp_path: Path,
        *,
        claude: bool,
        bash: bool,
        helper: bool = True,
        helper_exit: int = 0,
        helper_installs: tuple[str, ...] = ("git", "claude"),
        hook_out: str = INJECTED,
        typed: str | None = None,
        answers: dict[str, str] | None = None,
        version: Reply | str = "2.1.300 (Claude Code)",
        extra_env: dict[str, str] | None = None,
    ) -> tuple[HostReport, str, FakeSpawn]:
        paths = _project(tmp_path, hooks=_session_start())
        if helper:
            (paths.scripts / "prereqs-windows.ps1").write_text("# helper\n", encoding="utf-8")
        profile = tmp_path / "profile"
        local = tmp_path / "local"
        git_bash = local / "Programs" / "Git" / "bin" / "bash.exe"
        claude_exe = profile / ".local" / "bin" / "claude.exe"

        def install(path: Path) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")

        if bash:
            install(git_bash)
        if claude:
            install(claude_exe)
        spawn = FakeSpawn()
        spawn.on(program("bash.exe"), Reply(stdout=hook_out))
        spawn.on(
            program("claude.exe"),
            version if isinstance(version, Reply) else Reply(stdout=version + "\n"),
        )

        def run_helper(request: RunRequest) -> Reply:
            if helper_exit == 0:
                if "-WithGit" in request.argv and "git" in helper_installs:
                    install(git_bash)
                if "-WithClaudeCode" in request.argv and "claude" in helper_installs:
                    install(claude_exe)
            return Reply(returncode=helper_exit)

        spawn.on(program("powershell.exe"), run_helper)
        console, out = make_console(spawn=spawn, typed=typed, answers=answers)
        env = {"LOCALAPPDATA": str(local), "USERPROFILE": str(profile), **(extra_env or {})}
        report = check_hosts(
            paths, ["claude"], console, env=env, platform="win32", which=lambda name: None
        )
        return report, out.getvalue(), spawn

    def _helper_runs(self, spawn: FakeSpawn) -> list[RunRequest]:
        return [call for call in spawn.calls if program("powershell.exe")(call)]

    # ── Claude Code's version ────────────────────────────────────────────────
    #
    # The project's two security gates are PreToolUse hooks marked onFailure: "block". That
    # field is what stops a tool call when a gate cannot even start (launcher missing, broken
    # interpreter, timeout). Claude Code understands it from 2.1.295; an older version ignores
    # it without a word, and such a gate then fails open.

    def test_the_version_is_asked_of_the_claude_that_was_found(self, tmp_path: Path) -> None:
        report, _, spawn = self._world(tmp_path, claude=True, bash=True)
        (asked,) = [call for call in spawn.calls if program("claude.exe")(call)]
        assert asked.argv == [str(report.claude), "--version"]
        assert report.claude_version == (2, 1, 300)

    @pytest.mark.parametrize("version", ["2.1.294", "2.1.99", "2.0.76", "1.0.128"])
    def test_an_older_claude_code_is_something_to_check(self, tmp_path: Path, version: str) -> None:
        report, shown, _ = self._world(
            tmp_path, claude=True, bash=True, version=f"{version} (Claude Code)"
        )
        assert not report.ready and report.attention
        assert version in shown and "2.1.295" in shown
        assert "not guaranteed" in shown
        assert any("update" in item and "2.1.295" in item for item in report.todo)

    @pytest.mark.parametrize("version", ["2.1.295", "2.1.296", "2.1.1000", "2.2.0", "3.0.0"])
    def test_the_minimum_version_and_anything_newer_is_fine(
        self, tmp_path: Path, version: str
    ) -> None:
        report, shown, _ = self._world(
            tmp_path, claude=True, bash=True, version=f"{version} (Claude Code)"
        )
        assert report.ready and report.attention == []
        assert f"Claude Code {version} is installed" in shown

    @pytest.mark.parametrize(
        "answer",
        [
            Reply(stdout="claude: unknown build\n"),
            Reply(stdout=""),
            Reply(returncode=1, stderr="error: unknown option '--version'"),
        ],
        ids=["no-number", "nothing", "failed"],
    )
    def test_a_version_that_cannot_be_read_is_a_soft_warning(
        self, tmp_path: Path, answer: Reply
    ) -> None:
        # Not knowing is not a failure: say what is needed and how to update, and carry on.
        report, shown, _ = self._world(tmp_path, claude=True, bash=True, version=answer)
        assert report.ready and report.attention == [] and report.claude_version is None
        assert "2.1.295" in shown and "update" in shown

    def test_the_update_command_names_claude_by_its_full_path_when_it_is_not_on_path(
        self, tmp_path: Path
    ) -> None:
        report, _, _ = self._world(tmp_path, claude=True, bash=True, version="2.1.200")
        assert any(f"{report.claude} update" in item for item in report.todo)

    def test_a_freshly_installed_claude_code_is_checked_too(self, tmp_path: Path) -> None:
        report, _, spawn = self._world(
            tmp_path, claude=False, bash=True, typed="\n", version="2.1.100"
        )
        assert report.claude is not None and report.attention

    # ── Windows PowerShell children ──────────────────────────────────────────

    @pytest.mark.parametrize("name", ["PSModulePath", "PSMODULEPATH", "psmodulepath"])
    def test_the_install_helper_does_not_inherit_powershell_7s_module_path(
        self, tmp_path: Path, name: str
    ) -> None:
        # Started from a PowerShell 7 terminal, this process carries PS 7's PSModulePath.
        # Windows PowerShell 5.1 handed that path looks for its in-box modules in the wrong
        # place and can fail to load them. Without the variable it builds its own.
        ps7 = "C:\\Program Files\\PowerShell\\7\\Modules"
        _, _, spawn = self._world(
            tmp_path, claude=False, bash=False, typed="\n", extra_env={name: ps7, "KEEP": "1"}
        )
        (helper,) = self._helper_runs(spawn)
        assert helper.env is not None
        assert not any(key.lower() == "psmodulepath" for key in helper.env)
        assert helper.env["KEEP"] == "1"  # everything else is passed on

    def test_everything_present_is_ready(self, tmp_path: Path) -> None:
        report, _, spawn = self._world(tmp_path, claude=True, bash=True)
        assert report.ready and report.todo == []
        assert self._helper_runs(spawn) == []

    def test_interactively_enter_installs_what_is_missing_and_checks_again(
        self, tmp_path: Path
    ) -> None:
        report, shown, spawn = self._world(tmp_path, claude=False, bash=False, typed="\n")
        runs = self._helper_runs(spawn)
        assert len(runs) == 1
        helper = runs[0]
        assert helper.interactive  # the installers talk to the person themselves
        assert helper.argv[1:5] == ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
        assert Path(helper.argv[5]).name == "prereqs-windows.ps1"
        assert helper.argv[6:] == ["-WithGit", "-WithClaudeCode"]
        # ...and afterwards the checks, including the hook self-test, ran against the result.
        assert report.claude is not None and report.git_bash is not None
        assert report.hook.status == "pass"
        assert report.ready, shown

    def test_only_what_is_missing_is_installed(self, tmp_path: Path) -> None:
        _, _, spawn = self._world(tmp_path, claude=True, bash=False, typed="\n")
        assert self._helper_runs(spawn)[0].argv[6:] == ["-WithGit"]
        _, _, spawn = self._world(tmp_path / "second", claude=False, bash=True, typed="\n")
        assert self._helper_runs(spawn)[0].argv[6:] == ["-WithClaudeCode"]

    def test_the_hook_self_test_runs_after_the_install_not_before(self, tmp_path: Path) -> None:
        _, _, spawn = self._world(tmp_path, claude=False, bash=False, typed="\n")

        def kind(call: RunRequest) -> str:
            if program("powershell.exe")(call):
                return "helper"
            return "version" if program("claude.exe")(call) else "hook"

        # The freshly installed Claude Code is asked its version too; that is not a hook.
        assert [kind(call) for call in spawn.calls] == ["helper", "version", "hook"]

    def test_saying_no_installs_nothing_and_leaves_the_one_helper_command(
        self, tmp_path: Path
    ) -> None:
        report, _, spawn = self._world(tmp_path, claude=False, bash=False, typed="n\n")
        assert self._helper_runs(spawn) == []
        assert report.todo == [
            "Install Git for Windows and Claude Code:  powershell -ExecutionPolicy Bypass "
            "-File scripts\\prereqs-windows.ps1 -WithGit -WithClaudeCode"
        ]

    def test_unattended_never_installs_and_prints_the_one_helper_command(
        self, tmp_path: Path
    ) -> None:
        # No terminal, or -Yes: nothing is installed on someone's computer without asking.
        report, shown, spawn = self._world(tmp_path, claude=False, bash=False)
        assert self._helper_runs(spawn) == []
        assert len(report.todo) == 1
        assert "scripts\\prereqs-windows.ps1 -WithGit -WithClaudeCode" in report.todo[0]
        assert "winget" not in shown + " ".join(report.todo)  # winget may not exist at all

    def test_unattended_names_only_the_missing_piece(self, tmp_path: Path) -> None:
        report, _, _ = self._world(tmp_path, claude=False, bash=True)
        assert report.todo == [
            "Install Claude Code:  powershell -ExecutionPolicy Bypass "
            "-File scripts\\prereqs-windows.ps1 -WithClaudeCode"
        ]

    def test_a_missing_helper_falls_back_to_plain_addresses(self, tmp_path: Path) -> None:
        report, shown, spawn = self._world(
            tmp_path, claude=False, bash=False, helper=False, typed="\n"
        )
        assert self._helper_runs(spawn) == []
        assert any("https://git-scm.com/download/win" in item for item in report.todo)
        assert any("https://code.claude.com/docs/en/setup" in item for item in report.todo)
        assert "prereqs-windows.ps1" not in " ".join(report.todo) and "winget" not in shown

    def test_a_helper_that_fails_falls_back_to_plain_addresses(self, tmp_path: Path) -> None:
        report, shown, _ = self._world(
            tmp_path, claude=False, bash=False, helper_exit=1, typed="\n"
        )
        assert any("https://git-scm.com/download/win" in item for item in report.todo)
        assert any("https://code.claude.com/docs/en/setup" in item for item in report.todo)
        assert "did not finish" in shown

    def test_a_helper_that_installs_only_part_names_what_is_still_missing(
        self, tmp_path: Path
    ) -> None:
        report, _, _ = self._world(
            tmp_path, claude=False, bash=False, helper_installs=("git",), typed="\n"
        )
        assert report.git_bash is not None and report.claude is None
        assert len(report.todo) == 1 and "https://code.claude.com/docs/en/setup" in report.todo[0]

    def test_a_failing_hook_is_called_out_as_a_safety_problem(self, tmp_path: Path) -> None:
        report, shown, _ = self._world(tmp_path, claude=True, bash=True, hook_out="")
        assert not report.ready
        assert "safety" in shown.lower()

    def test_copilot_only_setups_skip_the_claude_checks(self, tmp_path: Path) -> None:
        paths = _project(tmp_path, hooks=_session_start())
        spawn = FakeSpawn()
        console, _ = make_console(spawn=spawn)
        report = check_hosts(
            paths, ["copilot"], console, env={}, platform="win32", which=lambda name: None
        )
        assert report.ready and spawn.calls == []
