"""``scripts/setup.sh``: the macOS and Linux bootstrap that starts the setup wizard.

The script is run for real, under bash, in a throwaway project folder. What it would start
(the interpreter in ``.venv``, a system Python, ``status.sh``) is a stub that records how it
was called, so nothing is installed, no tenant is reached, and the repository's own ``.venv``
is never touched. What these pin: every flag ``setup.sh`` took before the wizard still does
its job, a value is passed on exactly as typed, and the environment is only built when needed.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="a bash script for macOS and Linux")

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SH = REPO_ROOT / "scripts" / "setup.sh"
# The bash that ships with macOS is 3.2: the script has to run on it.
BASH = "/bin/bash" if Path("/bin/bash").exists() else (shutil.which("bash") or "bash")

# Stands in for a Python interpreter. Every call is logged, one argument per line after a
# "--- call" line, so a test can read back exactly what arrived, spaces and all.
STUB_PYTHON = """#!/bin/sh
{
    printf '%s\\n' '--- call'
    for argument in "$@"; do printf '%s\\n' "$argument"; done
} >> "$STUB_LOG"
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
    mkdir -p "$3/bin" && cp "$0" "$3/bin/python"
    exit "${STUB_VENV_EXIT:-0}"
fi
if [ "$1" = "-c" ]; then
    case "$2" in
        *version_info*) exit "${STUB_VERSION_EXIT:-0}" ;;
        *find_spec*) exit "${STUB_IMPORT_EXIT:-0}" ;;
    esac
fi
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
    exit "${STUB_PIP_EXIT:-0}"
fi
exit "${STUB_EXIT:-0}"
"""


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@dataclass
class Project:
    root: Path
    log: Path
    bin: Path

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.root.parent / "home"),
            "STUB_LOG": str(self.log),
            **env,
        }
        return subprocess.run(  # noqa: S603 - the repository's own script, by full path
            [BASH, str(self.root / "scripts" / "setup.sh"), *args],
            capture_output=True,
            text=True,
            env=environment,
            cwd=self.root.parent,
            stdin=subprocess.DEVNULL,
            check=False,
        )

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        calls: list[list[str]] = []
        for line in self.log.read_text(encoding="utf-8").splitlines():
            if line == "--- call":
                calls.append([])
            else:
                calls[-1].append(line)
        return calls

    def wizard(self) -> list[str]:
        """The wizard's arguments, after ``-m entrabot.winsetup``."""
        started = [call for call in self.calls() if call[:2] == ["-m", "entrabot.winsetup"]]
        assert len(started) == 1, self.calls()
        return started[0][2:]

    def pip_installs(self) -> list[list[str]]:
        return [call for call in self.calls() if call[:3] == ["-m", "pip", "install"]]

    def make_ready(self) -> None:
        """A virtual environment that is already built for this ``pyproject.toml``."""
        _executable(self.root / ".venv" / "bin" / "python", STUB_PYTHON)
        stamp = subprocess.run(  # noqa: S603,S607 - cksum, as the script computes it
            ["sh", "-c", "cksum < pyproject.toml | tr -d ' \\t'"],
            capture_output=True,
            text=True,
            cwd=self.root,
            check=True,
        ).stdout
        (self.root / ".venv" / ".entrabot-setup-stamp").write_text(stamp, encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path) -> Project:
    root = tmp_path / "Development HD" / "entrabot"  # a space in the path, as on a real Mac
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SETUP_SH, root / "scripts" / "setup.sh")
    (root / "pyproject.toml").write_text('[project]\nname = "entrabot"\n', encoding="utf-8")
    _executable(
        root / "status.sh",
        "#!/bin/sh\n{ printf '%s\\n' '--- call' status; for a in \"$@\"; do printf '%s\\n' "
        '"$a"; done; } >> "$STUB_LOG"\n',
    )
    result = Project(root=root, log=tmp_path / "calls.log", bin=tmp_path / "bin")
    result.bin.mkdir()
    return result


@pytest.fixture
def ready(project: Project) -> Project:
    project.make_ready()
    return project


class TestFlagsReachTheWizard:
    def test_no_flags_starts_the_wizard_in_this_folder(self, ready: Project) -> None:
        result = ready.run()

        assert result.returncode == 0, result.stderr
        assert ready.wizard() == ["setup", "--project-root", str(ready.root)]

    @pytest.mark.parametrize(
        ("flag", "expected"),
        [
            ("--new", ["--new-chain"]),
            ("--use-blueprint=11111111-2222-3333-4444-555555555555", None),
            ("--with-upn-suffix=sati", ["--upn-suffix", "sati"]),
            ("--agent-user-upn=bot@contoso.com", ["--agent-user-upn", "bot@contoso.com"]),
            ("--sponsor=pat@contoso.com", ["--sponsor", "pat@contoso.com"]),
            ("--teams-user=pat@contoso.com", ["--sponsor", "pat@contoso.com"]),
            ("--tenant=contoso.com", ["--tenant", "contoso.com"]),
            ("--device-code", ["--device-code"]),
            ("--switch-user", ["--switch-user"]),
            ("--use-cloud-memory", ["--memory", "cloud"]),
            ("--keep-memory-local", ["--memory", "local"]),
            ("--with-storage-account=acct", ["--storage-account", "acct"]),
            ("--with-container=box", ["--container", "box"]),
            ("--create-new-storage", ["--create-new-storage"]),
            ("--location=westus2", ["--location", "westus2"]),
            ("--subscription=My Sub", ["--subscription", "My Sub"]),
            ("--configure-a365-work-iq", ["--configure-a365-work-iq"]),
            ("--remove-all-existing-certs", ["--remove-all-existing-certs"]),
            ("--hosts=claude", ["--hosts", "claude"]),
            ("--skip-smoke", ["--skip-smoke"]),
            ("--yes", ["--yes"]),
            ("-y", ["--yes"]),
            ("--verbose", ["--verbose"]),
            ("--log-file=/tmp/x y.log", ["--log-file", "/tmp/x y.log"]),
        ],
    )
    def test_each_flag(self, ready: Project, flag: str, expected: list[str] | None) -> None:
        if expected is None:
            expected = ["--use-blueprint", flag.split("=", 1)[1]]

        result = ready.run(flag)

        assert result.returncode == 0, result.stderr
        assert ready.wizard() == ["setup", "--project-root", str(ready.root), *expected]

    def test_the_documented_one_liners_still_work(self, ready: Project) -> None:
        ready.run(
            "--new",
            "--agent-user-upn=bot@contoso.com",
            "--use-cloud-memory",
            "--with-storage-account=acct",
            "--with-container=box",
            "--teams-user=a@contoso.com,b@contoso.com",
        )

        assert ready.wizard()[3:] == [
            "--new-chain",
            "--agent-user-upn",
            "bot@contoso.com",
            "--memory",
            "cloud",
            "--storage-account",
            "acct",
            "--container",
            "box",
            "--sponsor",
            "a@contoso.com,b@contoso.com",
        ]

    @pytest.mark.parametrize(
        "value", ["$(touch pwned)", "`touch pwned`", "a b'c\"d", "x; touch pwned", "*", "$HOME"]
    )
    def test_a_value_is_passed_on_exactly_as_typed_and_never_run(
        self, ready: Project, value: str
    ) -> None:
        ready.run(f"--with-upn-suffix={value}")

        assert ready.wizard()[-1] == value
        assert not (ready.root / "pwned").exists()
        assert not (ready.root.parent / "pwned").exists()

    def test_the_wizards_exit_code_is_the_scripts(self, ready: Project) -> None:
        assert ready.run(STUB_EXIT="3").returncode == 3
        assert ready.run(STUB_EXIT="130").returncode == 130


class TestAValueMayFollowItsOptionAfterASpace:
    """Seen on a real Mac run: ``--log-file <path>`` was refused as an unknown option although
    the help lists ``--log-file=PATH``."""

    @pytest.mark.parametrize(
        ("option", "wizard_flag"),
        [
            ("--use-blueprint", "--use-blueprint"),
            ("--with-upn-suffix", "--upn-suffix"),
            ("--agent-user-upn", "--agent-user-upn"),
            ("--sponsor", "--sponsor"),
            ("--teams-user", "--sponsor"),
            ("--tenant", "--tenant"),
            ("--with-storage-account", "--storage-account"),
            ("--with-container", "--container"),
            ("--location", "--location"),
            ("--subscription", "--subscription"),
            ("--hosts", "--hosts"),
            ("--log-file", "--log-file"),
        ],
    )
    def test_each_option_that_takes_a_value(
        self, ready: Project, option: str, wizard_flag: str
    ) -> None:
        result = ready.run(option, "a value with spaces", "--yes")

        assert result.returncode == 0, result.stderr
        assert ready.wizard()[3:] == [wizard_flag, "a value with spaces", "--yes"]

    def test_the_equals_form_and_the_space_form_can_be_mixed(self, ready: Project) -> None:
        ready.run(
            "--new", "--with-upn-suffix", "sati", "--tenant=contoso.com", "--log-file", "/tmp/x.log"
        )

        assert ready.wizard()[3:] == [
            "--new-chain",
            "--upn-suffix",
            "sati",
            "--tenant",
            "contoso.com",
            "--log-file",
            "/tmp/x.log",
        ]

    @pytest.mark.parametrize("value", ["$(touch pwned)", "*", "a;b", "x y"])
    def test_a_value_after_a_space_is_still_passed_on_exactly(
        self, ready: Project, value: str
    ) -> None:
        ready.run("--with-upn-suffix", value)

        assert ready.wizard()[-1] == value
        assert not (ready.root / "pwned").exists()

    @pytest.mark.parametrize(
        "args", [["--log-file"], ["--tenant", "--yes"], ["--new", "--sponsor"]]
    )
    def test_an_option_left_without_its_value_is_a_usage_error(
        self, ready: Project, args: list[str]
    ) -> None:
        result = ready.run(*args)

        assert result.returncode == 2
        assert "needs a value, for example" in result.stderr
        assert ready.calls() == []

    def test_the_older_agent_name_option_is_ignored_in_either_form(self, ready: Project) -> None:
        result = ready.run("--new", "--a365-agent-name", "Whatever")

        assert result.returncode == 0
        assert ready.wizard()[3:] == ["--new-chain"]


class TestOlderSpellings:
    def test_with_a365_work_iq_switches_work_iq_on_and_says_it_changed(
        self, ready: Project
    ) -> None:
        result = ready.run("--with-a365-work-iq")

        assert ready.wizard()[-1] == "--configure-a365-work-iq"
        assert "--with-a365-work-iq is now the same as --configure-a365-work-iq" in result.stderr

    def test_the_agent_name_flag_is_accepted_and_ignored(self, ready: Project) -> None:
        result = ready.run("--new", "--a365-agent-name=Whatever")

        assert result.returncode == 0
        assert ready.wizard()[3:] == ["--new-chain"]
        assert "ignored" in result.stderr


class TestOtherCommands:
    def test_diagnose_runs_the_health_check_and_installs_nothing(self, ready: Project) -> None:
        result = ready.run("--diagnose", "--new", "--yes")

        assert result.returncode == 0, result.stderr
        assert ready.wizard() == ["diagnose", "--project-root", str(ready.root)]
        assert ready.pip_installs() == []

    def test_rotate_cert_takes_yes_and_verbose(self, ready: Project) -> None:
        ready.run("--rotate-cert", "--yes", "--verbose", "--log-file=/tmp/r.log", "--skip-smoke")

        assert ready.wizard() == [
            "rotate-cert",
            "--project-root",
            str(ready.root),
            "--yes",
            "--verbose",
            "--log-file",
            "/tmp/r.log",
        ]

    @pytest.mark.parametrize("flag", ["--diagnose", "--rotate-cert"])
    def test_they_need_a_setup_that_is_already_here(self, project: Project, flag: str) -> None:
        result = project.run(flag)

        assert result.returncode == 1
        assert "has not been set up yet" in result.stderr
        assert project.calls() == [] and not (project.root / ".venv").exists()

    def test_status_hands_over_to_status_sh_with_the_other_arguments(
        self, project: Project
    ) -> None:
        result = project.run("--status", "--json")

        assert result.returncode == 0, result.stderr
        assert project.calls() == [["status", "--json"]]

    def test_help_needs_nothing_and_names_every_flag(self, project: Project) -> None:
        result = project.run("--help")

        assert result.returncode == 0 and project.calls() == []
        for flag in (
            "--new",
            "--use-blueprint=",
            "--with-upn-suffix=",
            "--agent-user-upn=",
            "--sponsor=",
            "--teams-user=",
            "--switch-user",
            "--use-cloud-memory",
            "--keep-memory-local",
            "--with-storage-account=",
            "--with-container=",
            "--create-new-storage",
            "--configure-a365-work-iq",
            "--with-a365-work-iq",
            "--remove-all-existing-certs",
            "--rotate-cert",
            "--diagnose",
            "--status",
            "--skip-smoke",
            "--yes",
        ):
            assert flag in result.stdout, flag

    def test_an_unknown_option_is_a_usage_error_and_starts_nothing(self, ready: Project) -> None:
        result = ready.run("--new", "--frobnicate")

        assert result.returncode == 2
        assert "Unknown option: --frobnicate" in result.stderr
        assert ready.calls() == []


class TestTheEnvironment:
    def test_a_ready_environment_is_not_installed_again(self, ready: Project) -> None:
        ready.run("--new")

        assert ready.pip_installs() == []

    def test_a_first_run_builds_it_then_starts_the_wizard(self, project: Project) -> None:
        _executable(project.bin / "python3.12", STUB_PYTHON)

        result = project.run("--new", "--with-upn-suffix=sati")

        assert result.returncode == 0, result.stderr
        assert ["-m", "venv", str(project.root / ".venv")] in project.calls()
        installs = project.pip_installs()
        assert installs[-1][-2:] == ["-e", ".[provisioning]"]
        assert (project.root / ".venv" / ".entrabot-setup-stamp").is_file()
        assert project.wizard()[3:] == ["--new-chain", "--upn-suffix", "sati"]
        # ...and a second run starts at once.
        project.log.unlink()
        project.run("--new")
        assert project.pip_installs() == []

    def test_changed_requirements_are_installed(self, ready: Project) -> None:
        (ready.root / "pyproject.toml").write_text(
            '[project]\nname = "entrabot"\ndependencies = ["x"]\n', encoding="utf-8"
        )

        ready.run("--new")

        assert len(ready.pip_installs()) == 2  # pip itself, then the project
        assert ready.wizard()[3:] == ["--new-chain"]

    def test_an_install_that_did_not_finish_is_run_again(self, ready: Project) -> None:
        ready.run("--new", STUB_IMPORT_EXIT="1")

        assert len(ready.pip_installs()) == 2

    def test_a_failed_install_stops_before_the_wizard(self, ready: Project) -> None:
        (ready.root / ".venv" / ".entrabot-setup-stamp").unlink()

        result = ready.run("--new", STUB_PIP_EXIT="1")

        assert result.returncode == 1
        assert "could not be" in result.stderr and "./scripts/setup.sh again" in result.stderr
        assert [call for call in ready.calls() if call[:2] == ["-m", "entrabot.winsetup"]] == []
        assert not (ready.root / ".venv" / ".entrabot-setup-stamp").exists()

    def test_no_python_says_how_to_get_one(self, project: Project) -> None:
        # Every name the script tries is there and too old.
        for name in ("python3", "python3.12", "python3.13", "python3.14"):
            _executable(project.bin / name, STUB_PYTHON)

        result = project.run("--new", STUB_VERSION_EXIT="1")

        assert result.returncode == 1
        assert "Python 3.12 or later" in result.stdout
        assert "./scripts/setup.sh again" in result.stdout
        assert not (project.root / ".venv").exists()

    def test_a_venv_that_cannot_be_created_says_so(self, project: Project) -> None:
        _executable(
            project.bin / "python3.12",
            STUB_PYTHON.replace(
                'mkdir -p "$3/bin" && cp "$0" "$3/bin/python"', 'echo "ensurepip missing" >&2'
            ),
        )

        result = project.run("--new", STUB_VENV_EXIT="1")

        assert result.returncode == 1
        assert "could not create a virtual environment" in result.stdout
        assert "ensurepip missing" in result.stderr  # the reason is shown, not hidden


class TestTheScriptItself:
    def test_bash_accepts_it(self) -> None:
        result = subprocess.run(  # noqa: S603
            [BASH, "-n", str(SETUP_SH)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr

    def test_it_is_executable(self) -> None:
        assert os.access(SETUP_SH, os.X_OK)

    def test_it_never_hides_an_error_stream(self) -> None:
        text = SETUP_SH.read_text(encoding="utf-8")
        assert "2>/dev/null" not in text and "&>/dev/null" not in text and "2>&1" not in text

    def test_it_never_evaluates_or_sources_anything(self) -> None:
        text = SETUP_SH.read_text(encoding="utf-8")
        for line in text.splitlines():
            code = line.split("#", 1)[0]
            assert (
                "eval " not in code and "source " not in code and not code.strip().startswith(". ")
            ), line

    def test_every_python_program_it_holds_is_single_quoted(self) -> None:
        # A double-quoted or unquoted-heredoc program would have shell values expanded into
        # its text. The one program here asks a fixed question and takes no value at all.
        text = SETUP_SH.read_text(encoding="utf-8")
        assert '-c "' not in text and "<<PY" not in text and "<< PY" not in text
        assert text.count("-c '") == 2  # the version question and the import question

    def test_it_no_longer_carries_the_agent_365_cli(self) -> None:
        text = SETUP_SH.read_text(encoding="utf-8")
        for gone in ("dotnet", "a365 setup", "a365 develop", "pwsh", "a365.config.json"):
            assert gone not in text, gone

    def test_it_takes_no_microsoft_token_and_calls_no_az(self) -> None:
        # Signing in, and every token, is the wizard's business: none passes through bash.
        text = SETUP_SH.read_text(encoding="utf-8")
        for gone in ("get-access-token", "az login", "az account", "az ad "):
            assert gone not in text, gone
