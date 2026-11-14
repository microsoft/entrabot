"""``scripts/teardown.sh``: which teardown a command line starts.

By default it disconnects THIS computer (the wizard's teardown, as on Windows). The older
tenant-wide teardown, which deletes the agent's whole chain, runs only when one of its own
flags asks for it. The script is run for real under bash in a throwaway folder, with the
interpreter in ``.venv`` replaced by a stub that records how it was called; nothing reaches a
tenant or a key store.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="a bash script for macOS and Linux")

REPO_ROOT = Path(__file__).resolve().parents[2]
TEARDOWN_SH = REPO_ROOT / "scripts" / "teardown.sh"
BASH = "/bin/bash" if Path("/bin/bash").exists() else (shutil.which("bash") or "bash")

STUB_PYTHON = """#!/bin/sh
{
    printf '%s\\n' '--- call'
    for argument in "$@"; do printf '%s\\n' "$argument"; done
} >> "$STUB_LOG"
exit "${STUB_EXIT:-0}"
"""


class Folder:
    def __init__(self, tmp_path: Path, *, venv: bool = True) -> None:
        self.root = tmp_path / "My Projects" / "entrabot"
        (self.root / "scripts").mkdir(parents=True)
        shutil.copy(TEARDOWN_SH, self.root / "scripts" / "teardown.sh")
        self.log = tmp_path / "calls.log"
        if venv:
            python = self.root / ".venv" / "bin" / "python3"
            python.parent.mkdir(parents=True)
            python.write_text(STUB_PYTHON, encoding="utf-8")
            python.chmod(python.stat().st_mode | stat.S_IXUSR)

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - the repository's own script, by full path
            [BASH, str(self.root / "scripts" / "teardown.sh"), *args],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.root.parent), "STUB_LOG": str(self.log)}
            | env,
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


@pytest.fixture
def folder(tmp_path: Path) -> Folder:
    return Folder(tmp_path)


class TestTheDefaultDisconnectsThisComputer:
    def test_no_flags_starts_the_wizards_teardown(self, folder: Folder) -> None:
        result = folder.run()

        assert result.returncode == 0, result.stderr
        assert folder.calls() == [
            ["-m", "entrabot.winsetup", "teardown", "--project-root", str(folder.root)]
        ]

    def test_its_flags_are_passed_on(self, folder: Folder) -> None:
        folder.run(
            "--delete-agent",
            "--purge-data",
            "--keep-setup-credential",
            "--verbose",
            "--log-file=/tmp/t d.log",
            "--yes",
        )

        assert folder.calls()[0][5:] == [
            "--delete-agent",
            "--purge-data",
            "--keep-setup-credential",
            "--verbose",
            "--log-file",
            "/tmp/t d.log",
            "--yes",
        ]

    def test_yes_alone_no_longer_deletes_the_agent(self, folder: Folder) -> None:
        # It used to: plain "teardown.sh --yes" removed the agent from the tenant.
        folder.run("-y")

        (call,) = folder.calls()
        assert "--delete-agent" not in call and call[-1] == "--yes"

    def test_the_wizards_exit_code_is_the_scripts(self, folder: Folder) -> None:
        assert folder.run(STUB_EXIT="130").returncode == 130

    def test_a_folder_that_was_never_set_up_has_nothing_to_remove(self, tmp_path: Path) -> None:
        result = Folder(tmp_path, venv=False).run()

        assert result.returncode == 0
        assert "Nothing to remove" in result.stdout

    def test_settings_without_the_environment_are_not_silently_left(self, tmp_path: Path) -> None:
        folder = Folder(tmp_path, venv=False)
        (folder.root / ".env").write_text("ENTRABOT_TENANT_ID=t\n", encoding="utf-8")

        result = folder.run("--yes")

        assert result.returncode == 1
        assert ".venv" in result.stderr and (folder.root / ".env").exists()


class TestTheTenantWideTeardownIsAskedForByItsOwnFlags:
    @pytest.mark.parametrize("flag", ["--tenant-wide", "--dry-run", "--preserve-provisioner"])
    def test_its_flags_do_not_start_the_wizard(self, folder: Folder, flag: str) -> None:
        # Nothing is set up in this folder, so the older script finds nothing and stops.
        result = folder.run(flag, "--preserve-local-state")

        assert result.returncode == 0, result.stderr
        assert "Nothing to clean up" in result.stdout
        assert [call for call in folder.calls() if "entrabot.winsetup" in call] == []

    @pytest.mark.parametrize(
        ("new", "old"),
        [
            ("--delete-agent", "--agent-user-upn=a@contoso.com"),
            ("--purge-data", "--dry-run"),
            ("--keep-setup-credential", "--preserve-provisioner"),
        ],
    )
    def test_the_two_teardowns_cannot_be_mixed(self, folder: Folder, new: str, old: str) -> None:
        result = folder.run(new, old, "--yes")

        assert result.returncode == 2
        assert "can't be combined" in result.stderr
        assert folder.calls() == []


class TestHelpAndShape:
    def test_help_says_what_the_default_does_and_names_every_flag(self, folder: Folder) -> None:
        result = folder.run("--help")

        assert result.returncode == 0 and folder.calls() == []
        text = " ".join(result.stdout.split())
        assert "Disconnects THIS computer" in text
        for flag in (
            "--delete-agent",
            "--purge-data",
            "--keep-setup-credential",
            "--yes",
            "--tenant-wide",
            "--agent-user-upn=UPN",
            "--dry-run",
            "--preserve-provisioner",
            "--preserve-local-state",
        ):
            assert flag in text, flag

    def test_bash_accepts_it(self) -> None:
        result = subprocess.run(  # noqa: S603
            [BASH, "-n", str(TEARDOWN_SH)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr

    def test_the_agents_own_key_store_item_is_removed_with_the_local_state(self) -> None:
        # The tenant-wide path's local cleanup: the wizard keeps one item per certificate.
        text = TEARDOWN_SH.read_text(encoding="utf-8")
        cleanup = text[text.index("# This agent's own key store item") :]
        assert "keycert.delete_cert(sha1)" in cleanup
        assert "read_env(Path(sys.argv[1]))" in cleanup  # .env read as data, never run
        assert cleanup.index("keycert.delete_cert(sha1)") < cleanup.index("rm -f .env")


def test_an_unknown_option_is_a_usage_error_and_starts_nothing(folder: Folder) -> None:
    result = folder.run("--yes", "--frobnicate")

    assert result.returncode == 2
    assert "Unknown argument: --frobnicate" in result.stderr
    assert folder.calls() == []


def test_the_tenant_wide_teardown_uses_setups_private_azure_cli_when_no_other_is_on_path(
    tmp_path: Path,
) -> None:
    # Setup installs its own Azure CLI when the computer has none, and does not put it on
    # PATH. Nothing is set up in this folder, so the run stops at "Nothing to clean up";
    # the stub az proves which az a later step would have found.
    folder = Folder(tmp_path)
    private = folder.root.parent / ".entrabot" / "tools" / "azure-cli" / "bin" / "az"
    private.parent.mkdir(parents=True)
    private.write_text("#!/bin/sh\n", encoding="utf-8")
    private.chmod(private.stat().st_mode | stat.S_IXUSR)
    text = TEARDOWN_SH.read_text(encoding="utf-8")
    block = text[text.index("# Setup installs its own Azure CLI") :]
    block = block[: block.index("fi\n") + 3]
    probe = folder.root / "probe.sh"
    probe.write_text("set -euo pipefail\n" + block + "command -v az\n", encoding="utf-8")

    found = subprocess.run(  # noqa: S603
        [BASH, str(probe)],
        capture_output=True,
        text=True,
        # A PATH with no az on it, whatever this machine has installed.
        env={"PATH": str(folder.root / "no-tools-here"), "HOME": str(folder.root.parent)},
        check=False,
    )

    assert found.returncode == 0, found.stderr
    assert found.stdout.strip() == str(private)
    # ...and the same lines are in status.sh.
    assert block in (REPO_ROOT / "status.sh").read_text(encoding="utf-8")


def test_a_log_file_may_follow_its_option_after_a_space(folder: Folder) -> None:
    folder.run("--log-file", "/tmp/t d.log", "--yes")

    assert folder.calls()[0][5:] == ["--log-file", "/tmp/t d.log", "--yes"]


def test_an_option_left_without_its_value_is_a_usage_error(folder: Folder) -> None:
    result = folder.run("--yes", "--log-file")

    assert result.returncode == 2
    assert "--log-file needs a value" in result.stderr
    assert folder.calls() == []
