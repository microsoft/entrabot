"""Register the MCP server with the assistants, and check the host is ready to run it.

Claude Code is where this repo's hooks matter: a SessionStart hook injects the body prompt,
and two PreToolUse hooks are security gates. They are shell-form hooks pinned to ``bash``,
which on Windows means Git Bash (from Git for Windows). The self-test here runs the
SessionStart hook exactly as Claude Code would, and only believes an exit code of 0 with
the prompt actually on stdout.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Literal

from entrabot.winsetup.console import Console
from entrabot.winsetup.helpers import Paths

KNOWN_HOSTS = ("claude", "copilot")
# The repo's own installer for what Claude Code needs. It works with or without winget, so
# setup never prints raw winget lines (winget does not exist for every user).
PREREQS_HELPER = "prereqs-windows.ps1"
HELPER_COMMAND = "powershell -ExecutionPolicy Bypass -File scripts\\prereqs-windows.ps1"
GIT_URL = "https://git-scm.com/download/win"
CLAUDE_URL = "https://code.claude.com/docs/en/setup"
# The project's two security gates are PreToolUse hooks marked onFailure: "block", which is
# what stops a tool call when a gate cannot even start (launcher missing, broken interpreter,
# timeout). Claude Code understands that field from this version on. An older one ignores it
# silently, and a gate that fails to start then lets the call through.
MIN_CLAUDE_CODE = (2, 1, 295)
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
START_ARGUMENTS = "--dangerously-load-development-channels server:entrabot"
START_COMMAND = f"claude {START_ARGUMENTS}"


class HostError(Exception):
    """The MCP server could not be registered with an assistant."""


@dataclass(frozen=True)
class HookCommand:
    command: str
    shell: str  # "bash" | "powershell"


@dataclass(frozen=True)
class HookTest:
    status: Literal["pass", "fail", "skipped"]
    detail: str = ""


@dataclass
class HostReport:
    registered: list[Path] = field(default_factory=list)
    claude: Path | None = None
    git_bash: Path | None = None
    claude_version: tuple[int, int, int] | None = None
    hook: HookTest = field(default_factory=lambda: HookTest("skipped"))
    todo: list[str] = field(default_factory=list)  # what the person still has to do, in order
    # Reasons setup can't call this computer ready even though everything is installed.
    attention: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.todo


def parse_hosts(value: str | None) -> list[str]:
    """``--hosts claude,copilot`` -> a validated list. None means both."""
    if value is None:
        return list(KNOWN_HOSTS)
    hosts = [item.strip().lower() for item in value.split(",") if item.strip()]
    unknown = [item for item in hosts if item not in KNOWN_HOSTS]
    if not hosts or unknown:
        raise HostError(
            f"--hosts takes a comma-separated list of: {', '.join(KNOWN_HOSTS)} (got {value!r})."
        )
    return [host for host in KNOWN_HOSTS if host in hosts]


def _load_mcp_config(scripts_dir: Path) -> ModuleType:
    """``scripts/mcp_config.py`` is the unit-tested writer both setups share."""
    path = scripts_dir / "mcp_config.py"
    spec = importlib.util.spec_from_file_location("entrabot_setup_mcp_config", path)
    if spec is None or spec.loader is None:
        raise HostError(f"Could not load {path}.")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except OSError as exc:
        raise HostError(f"Could not load {path}: {exc}") from exc
    return module


def copilot_mcp_path(env: Mapping[str, str]) -> Path:
    """Copilot CLI's user-level MCP config: ``$COPILOT_HOME`` or ``~/.copilot``. Resolved from
    the environment the wizard was given, never the ambient one."""
    override = env.get("COPILOT_HOME")
    return (Path(override) if override else Path.home() / ".copilot") / "mcp-config.json"


def register_mcp(paths: Paths, hosts: Sequence[str], *, env: Mapping[str, str]) -> list[Path]:
    """Write the ``entrabot`` server entry for each chosen host. Returns the files written.

    Other servers in those files, and an ``env`` block someone added to the entrabot entry
    (persona-sati wiring), are kept.
    """
    if not paths.mcp_binary.is_file():
        raise HostError(
            f"The MCP server program was not found at {paths.mcp_binary} "
            "(entrabot-mcp). Run setup again so it is reinstalled."
        )
    mcp_config = _load_mcp_config(paths.scripts)
    entry = mcp_config.build_entrabot_entry(str(paths.mcp_binary))
    targets: list[Path] = []
    if "claude" in hosts:
        targets.append(paths.claude_mcp)
    if "copilot" in hosts:
        targets.append(copilot_mcp_path(env))
    for target in targets:
        try:
            mcp_config.upsert_mcp_entry(target, "entrabot", dict(entry))
        except OSError as exc:
            raise HostError(f"Could not write {target}: {exc}") from exc
    return targets


def find_git_bash(
    env: Mapping[str, str],
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> Path | None:
    """The shell Claude Code runs ``"shell": "bash"`` hooks with.

    On Windows that is Git Bash, found the way Claude Code finds it: its own override
    variable, the standard install folders, then next to ``git`` on PATH. ``bash`` on PATH is
    not trusted there: ``System32\\bash.exe`` is the WSL launcher.
    Needs confirmation on real Windows: the per-user Git install layout.
    """
    if (platform or sys.platform) != "win32":
        found = which("bash") or which("sh")
        return Path(found) if found else None

    candidates: list[Path] = []
    override = env.get("CLAUDE_CODE_GIT_BASH_PATH")
    if override:
        candidates.append(Path(override))
    for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        if env.get(variable):
            candidates.append(Path(env[variable]) / "Git" / "bin" / "bash.exe")
    if env.get("LOCALAPPDATA"):
        candidates.append(Path(env["LOCALAPPDATA"]) / "Programs" / "Git" / "bin" / "bash.exe")
    git = which("git")
    if git:
        candidates.append(Path(git).parent.parent / "bin" / "bash.exe")
    return next((item for item in candidates if item.is_file()), None)


def find_claude(
    env: Mapping[str, str],
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> Path | None:
    found = which("claude")
    if found:
        return Path(found)
    if (platform or sys.platform) == "win32" and env.get("USERPROFILE"):
        native = Path(env["USERPROFILE"]) / ".local" / "bin" / "claude.exe"
        if native.is_file():
            return native
    return None


def windows_powershell(env: Mapping[str, str]) -> tuple[str, dict[str, str]]:
    """The in-box Windows PowerShell (5.1) and the environment to start it with.

    ``PSModulePath`` is left out. When setup was started from a PowerShell 7 terminal this
    process carries PowerShell 7's module path, and Windows PowerShell handed that path can
    fail to load its own in-box modules. Without the variable it builds the right one itself.
    Unconfirmed on real Windows: the failure is known from the field, not reproduced here.
    """
    root = env.get("SystemRoot") or env.get("SYSTEMROOT") or r"C:\Windows"
    exe = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    child = {key: value for key, value in env.items() if key.lower() != "psmodulepath"}
    return str(exe), child


def claude_program(claude: Path | None, which: Callable[[str], str | None] = shutil.which) -> str:
    """How to name Claude Code in a command someone will type. Its installer does not always
    put it on PATH, so the full path is used when plain ``claude`` would not be found."""
    if which("claude") or claude is None:
        return "claude"
    return f'"{claude}"' if " " in str(claude) else str(claude)


def claude_code_version(
    claude: Path, console: Console, env: Mapping[str, str]
) -> tuple[int, int, int] | None:
    """What ``claude --version`` says, as numbers, or None when it can't be read."""
    result = console.run([str(claude), "--version"], env=env, capture=True, quiet=True)
    found = _VERSION.search(result.stdout) if result.returncode == 0 else None
    return (int(found.group(1)), int(found.group(2)), int(found.group(3))) if found else None


def session_start_hooks(settings_path: Path) -> list[HookCommand]:
    """The command hooks Claude Code would run at session start, from ``settings.json``."""
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    found: list[HookCommand] = []
    groups = (data.get("hooks") or {}).get("SessionStart") or [] if isinstance(data, dict) else []
    for group in groups:
        for hook in (group or {}).get("hooks") or []:
            if isinstance(hook, dict) and hook.get("type") == "command" and hook.get("command"):
                found.append(HookCommand(str(hook["command"]), str(hook.get("shell") or "bash")))
    return found


def hook_self_test(
    paths: Paths, console: Console, *, bash: Path | None, env: Mapping[str, str]
) -> HookTest:
    """Run the project's SessionStart hook the way Claude Code runs it.

    Passes only when every hook exits 0 and the body prompt is on stdout. The prompt itself
    is neither shown nor logged.
    """
    hooks = session_start_hooks(paths.project_root / ".claude" / "settings.json")
    if not hooks:
        return HookTest("skipped", "This project defines no session-start hook.")
    child_env = dict(env)
    # Forward slashes: Git Bash reads C:/Users/... correctly, and so does everything else.
    child_env["CLAUDE_PROJECT_DIR"] = paths.project_root.as_posix()
    payload = json.dumps(
        {
            "hook_event_name": "SessionStart",
            "source": "startup",
            "session_id": "entrabot-setup-self-test",
            "cwd": str(paths.project_root),
        }
    )
    injected = False
    for hook in hooks:
        hook_env = child_env
        if hook.shell == "powershell":
            exe, hook_env = windows_powershell(child_env)
            argv = [exe, "-NoProfile", "-NonInteractive", "-Command", hook.command]
        elif bash is None:
            return HookTest(
                "fail",
                "The project's hooks run under Git Bash, and Git for Windows was not found.",
            )
        else:
            argv = [str(bash), "-c", hook.command]
        result = console.run(
            argv,
            env=hook_env,
            cwd=paths.project_root,
            capture=True,
            quiet=True,
            sensitive=True,
            input_text=payload,
        )
        if result.returncode != 0:
            reason = result.stderr.strip().splitlines()[-1:] or [f"exit {result.returncode}"]
            return HookTest("fail", reason[0])
        injected = injected or "additionalContext" in result.stdout
    if not injected:
        return HookTest("fail", "The session-start hook ran but injected no body prompt.")
    return HookTest("pass")


@dataclass(frozen=True)
class _Missing:
    name: str  # as a person would say it
    flag: str  # the helper's switch that installs it
    url: str  # where to get it by hand


_GIT = _Missing("Git for Windows", "-WithGit", GIT_URL)
_CLAUDE = _Missing("Claude Code", "-WithClaudeCode", CLAUDE_URL)


def _run_prereqs_helper(
    paths: Paths, console: Console, env: Mapping[str, str], missing: Sequence[_Missing]
) -> bool:
    """Install *missing* with ``scripts\\prereqs-windows.ps1``, through the in-box Windows
    PowerShell. The helper talks to the person itself (progress, a permission prompt)."""
    powershell, child_env = windows_powershell(env)
    result = console.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(paths.scripts / PREREQS_HELPER),
            *(item.flag for item in missing),
        ],
        env=child_env,
        cwd=paths.project_root,
        interactive=True,
    )
    return result.returncode == 0


def _check_claude_code_version(
    report: HostReport,
    console: Console,
    env: Mapping[str, str],
    which: Callable[[str], str | None],
) -> None:
    """Is the installed Claude Code new enough for the project's safety checks to hold?"""
    assert report.claude is not None
    needed = ".".join(str(part) for part in MIN_CLAUDE_CODE)
    update = f"{claude_program(report.claude, which)} update"
    version = claude_code_version(report.claude, console, env)
    report.claude_version = version
    if version is None:
        console.ok("Claude Code is installed")
        console.detail(
            f"Its version could not be read. This project's safety checks need {needed} or "
            f"later; to be sure, update with:  {update}"
        )
        return
    installed = ".".join(str(part) for part in version)
    if version >= MIN_CLAUDE_CODE:
        console.ok(f"Claude Code {installed} is installed")
        return
    console.warn(
        f"Claude Code {installed} is older than {needed}. On that version this project's "
        "safety checks are not guaranteed."
    )
    console.detail(
        "An older Claude Code skips a safety check that fails to start, instead of blocking "
        "the action it guards."
    )
    report.attention.append(
        f"Claude Code {installed} is older than {needed}, so this project's safety checks "
        "are not guaranteed until it is updated."
    )
    report.todo.append(f"Update Claude Code to {needed} or later:  {update}")


def check_hosts(
    paths: Paths,
    hosts: Sequence[str],
    console: Console,
    *,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> HostReport:
    """Is this computer ready to run the agent in Claude Code?

    When Git for Windows or Claude Code is missing and there is someone to ask, setup offers
    to install them with the repo's helper, then checks again. Unattended it installs nothing
    and leaves the one helper command to run. If the helper is missing or fails, it falls back
    to plain download addresses.
    """
    environ = env if env is not None else os.environ
    platform = platform or sys.platform
    report = HostReport()
    if "claude" not in hosts:
        return report

    def look() -> list[_Missing]:
        report.claude = find_claude(environ, platform=platform, which=which)
        report.git_bash = find_git_bash(environ, platform=platform, which=which)
        missing = []
        if report.git_bash is None and platform == "win32":
            missing.append(_GIT)
        if report.claude is None:
            missing.append(_CLAUDE)
        return missing

    missing = look()
    names = " and ".join(item.name for item in missing)
    helper_present = platform == "win32" and (paths.scripts / PREREQS_HELPER).is_file()
    use_urls = not helper_present
    if missing:
        console.warn(f"Not installed yet: {names}.")
        if _GIT in missing:
            console.detail("Claude Code needs Git for Windows to run this project's safety checks.")
        can_ask = console.interactive or "install_hosts" in console.answers
        if (
            helper_present
            and can_ask
            and console.confirm(f"Install {names} now?", default=True, key="install_hosts")
        ):
            finished = _run_prereqs_helper(paths, console, environ, missing)
            missing = look()
            if not finished or missing:
                console.warn("The installer did not finish everything.")
                use_urls = True
            else:
                console.ok(f"Installed {names}")

    if missing and use_urls:
        report.todo.extend(f"Install {item.name}:  {item.url}" for item in missing)
    elif missing:
        still = " and ".join(item.name for item in missing)
        flags = " ".join(item.flag for item in missing)
        report.todo.append(f"Install {still}:  {HELPER_COMMAND} {flags}")
    if report.claude is not None:
        _check_claude_code_version(report, console, environ, which)

    report.hook = hook_self_test(paths, console, bash=report.git_bash, env=environ)
    if report.hook.status == "pass":
        console.ok("The project's safety checks (Claude Code hooks) run on this computer")
    elif report.hook.status == "fail" and report.git_bash is not None:
        # Without Git Bash the reason is already on screen, with how to install it.
        console.warn(f"The project's safety checks did not run: {report.hook.detail}")
        report.todo.append(
            "The project's safety checks (Claude Code hooks) failed their self-test: "
            f"{report.hook.detail} Run setup again after fixing this."
        )
    return report
