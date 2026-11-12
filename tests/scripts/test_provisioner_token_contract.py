"""Provisioner token acquisition policy for scripts.

Only setup/provisioning paths may create or repair the provisioner app. Other
scripts must use the named existing-only helper so read/status/action commands
fail fast with a clear error if the provisioner is missing. Teardown deletes
only the Provisioner app recorded for this machine, never one found by name.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _script_files() -> list[Path]:
    return sorted(path for path in SCRIPTS_DIR.glob("*.py") if path.name != "entra_provisioning.py")


def test_scripts_do_not_auto_bootstrap_provisioner_app() -> None:
    offenders: list[str] = []

    for path in _script_files():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "get_graph_token":
                continue
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")

    assert offenders == []


def test_teardown_uses_existing_provisioner_token_only() -> None:
    script = (REPO_ROOT / "scripts" / "teardown.sh").read_text(encoding="utf-8")

    assert "get_existing_graph_token()" in script


def test_setup_certificate_registration_is_explicitly_bootstrap_capable() -> None:
    """setup.sh Step 6 hands certificate registration to this script, so it's a setup path."""
    setup = (REPO_ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8")
    script = (SCRIPTS_DIR / "register_blueprint_cert.py").read_text(encoding="utf-8")

    assert "register_blueprint_cert.py" in setup
    assert "from entra_provisioning import get_bootstrap_graph_token" in script
    assert "get_bootstrap_graph_token(wait_for_propagation=False)" in script


def test_teardown_no_token_warning_does_not_promise_az_cli_fallback() -> None:
    """The Agent Identity/Blueprint deletion paths only warn and skip (or
    require manual cleanup) when no Provisioner token exists — they never
    fall back to az CLI, which is rejected by Agent Identity APIs (Learning
    #1). The top-level warning text must not claim otherwise."""
    script = (REPO_ROOT / "scripts" / "teardown.sh").read_text(encoding="utf-8")

    assert "will try az CLI" not in script
    assert "No Provisioner token — manual cleanup may be required for Agent Identity APIs" in script


# ---------------------------------------------------------------------------
# teardown.sh deletes only the Provisioner app recorded for this machine
# ---------------------------------------------------------------------------
#
# The defect: teardown looked Provisioner apps up BY DISPLAY NAME and deleted what it found, so it
# could delete an app that merely shares the name (display names are not unique, and anyone who
# may register applications can use this one).
#
# teardown.sh is never run in place here: it removes .env and .entrabot-state.json from its own
# project root. Each test runs a copy inside a temporary project, with az, python3 and the venv's
# interpreter replaced by stubs. The stubs run nothing real: the state file is read by the real
# interpreter, and that is all.

PROV_APP = "00000000-0000-0000-0000-0000000000a3"
SOMEONE_ELSES_APP = "99999999-9999-9999-9999-999999999999"  # what a search by name would find
PLACEHOLDER_COMMAND = "az ad app delete --id <PROVISIONER_APP_ID>"

_FAKE_AZ = r"""#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_AZ_CALLS"
case "$1 $2" in
    "account show") exit 0 ;;
esac
case "$1 $2 $3" in
    "ad app list") printf '%s\n' "$FAKE_APP_FOUND_BY_NAME"; exit 0 ;;
    "ad app delete")
        if [ "$FAKE_AZ_DELETE_RC" != 0 ]; then
            echo "ERROR: Insufficient privileges to complete the operation." >&2
        fi
        exit "$FAKE_AZ_DELETE_RC" ;;
esac
exit 0
"""

_FAKE_PYTHON3 = '#!/bin/bash\nexec "$REAL_PYTHON" "$@"\n'

# The venv interpreter teardown uses for keyring cleanup and the targeted helper: logged, never run.
_FAKE_VENV_PYTHON = '#!/bin/bash\nprintf \'%s\\n\' "${1:0:40}" >> "$FAKE_VENV_CALLS"\nexit 0\n'


@dataclass
class Teardown:
    returncode: int
    stdout: str
    stderr: str
    az_calls: list[str]

    @property
    def deletes(self) -> list[str]:
        return [call for call in self.az_calls if call.startswith("ad app delete")]

    def output(self) -> str:
        return f"exit {self.returncode}\n--- stdout\n{self.stdout}\n--- stderr\n{self.stderr}"


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _teardown(
    tmp_path: Path,
    *args: str,
    state: dict[str, str] | None,
    answer: str = "",
    delete_rc: int = 0,
) -> Teardown:
    root = tmp_path / "project root with a space"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "scripts" / "teardown.sh", root / "scripts" / "teardown.sh")
    if state is not None:
        (root / ".entrabot-state.json").write_text(json.dumps(state), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    _executable(bin_dir / "az", _FAKE_AZ)
    _executable(bin_dir / "python3", _FAKE_PYTHON3)
    _executable(root / ".venv" / "bin" / "python3", _FAKE_VENV_PYTHON)
    az_calls = tmp_path / "az-calls.log"
    az_calls.touch()
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        ["bash", str(root / "scripts" / "teardown.sh"), *args],
        input=answer,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
            "HOME": str(home),
            "REAL_PYTHON": sys.executable,
            "FAKE_AZ_CALLS": str(az_calls),
            "FAKE_VENV_CALLS": str(tmp_path / "venv-calls.log"),
            "FAKE_APP_FOUND_BY_NAME": SOMEONE_ELSES_APP,
            "FAKE_AZ_DELETE_RC": str(delete_rc),
        },
        cwd=tmp_path,
    )
    return Teardown(
        result.returncode,
        result.stdout,
        result.stderr,
        az_calls.read_text(encoding="utf-8").splitlines(),
    )


def _state(**extra: str) -> dict[str, str]:
    return {
        "TENANT_ID": "00000000-0000-0000-0000-0000000000f1",
        "AGENT_USER_ID": "00000000-0000-0000-0000-0000000000c1",
        **extra,
    }


posix_shell_only = pytest.mark.skipif(sys.platform == "win32", reason="runs under POSIX bash")


def test_teardown_never_looks_for_a_provisioner_app_by_its_name() -> None:
    script = (REPO_ROOT / "scripts" / "teardown.sh").read_text(encoding="utf-8")

    assert "--display-name" not in script
    assert "PROVISIONER_CLIENT_ID" in script


@posix_shell_only
class TestTeardownDeletesOnlyTheRecordedProvisionerApp:
    def test_the_recorded_app_is_deleted_by_its_application_id(self, tmp_path: Path) -> None:
        run = _teardown(tmp_path, "--yes", state=_state(PROVISIONER_CLIENT_ID=PROV_APP))

        assert run.returncode == 0, run.output()
        assert run.deletes == [f"ad app delete --id {PROV_APP}"]
        assert not any(call.startswith("ad app list") for call in run.az_calls)
        assert SOMEONE_ELSES_APP not in "\n".join(run.az_calls)
        assert f"Deleted Provisioner app ({PROV_APP})" in run.stdout

    def test_with_none_recorded_no_app_is_deleted_and_the_command_is_shown(
        self, tmp_path: Path
    ) -> None:
        run = _teardown(tmp_path, "--yes", state=_state())

        assert run.returncode == 0, run.output()
        assert run.deletes == []
        assert not any(call.startswith("ad app list") for call in run.az_calls)
        assert "No Provisioner app is recorded for this machine" in run.stdout
        assert PLACEHOLDER_COMMAND in run.stdout
        assert "Removed .entrabot-state.json" in run.stdout  # the rest of teardown still runs

    @pytest.mark.parametrize(
        "recorded",
        ["api://entrabot-provisioner", "EntraBot Agent ID Provisioner", "00000000-0000"],
        ids=["identifier-uri", "a-name", "cut-short"],
    )
    def test_a_recorded_value_that_is_not_an_application_id_is_not_deleted(
        self, tmp_path: Path, recorded: str
    ) -> None:
        # az ad app delete --id also takes an identifier URI: only an application ID is passed.
        run = _teardown(tmp_path, "--yes", state=_state(PROVISIONER_CLIENT_ID=recorded))

        assert run.returncode == 0, run.output()
        assert run.deletes == []
        assert "not an application ID" in run.stdout
        assert PLACEHOLDER_COMMAND in run.stdout

    def test_preserve_provisioner_deletes_no_app(self, tmp_path: Path) -> None:
        run = _teardown(
            tmp_path,
            "--yes",
            "--preserve-provisioner",
            state=_state(PROVISIONER_CLIENT_ID=PROV_APP),
        )

        assert run.returncode == 0, run.output()
        assert run.deletes == []
        assert "Preserving Provisioner app" in run.stdout

    def test_the_confirmation_still_guards_the_deletion(self, tmp_path: Path) -> None:
        declined = _teardown(
            tmp_path / "declined", state=_state(PROVISIONER_CLIENT_ID=PROV_APP), answer="n"
        )
        accepted = _teardown(
            tmp_path / "accepted", state=_state(PROVISIONER_CLIENT_ID=PROV_APP), answer="y"
        )

        assert declined.returncode == 0 and "Aborted." in declined.stdout, declined.output()
        assert declined.deletes == []
        assert accepted.returncode == 0, accepted.output()
        assert accepted.deletes == [f"ad app delete --id {PROV_APP}"]

    def test_the_summary_names_the_recorded_app(self, tmp_path: Path) -> None:
        run = _teardown(tmp_path, "--dry-run", state=_state(PROVISIONER_CLIENT_ID=PROV_APP))

        assert run.returncode == 0, run.output()
        line = next(line for line in run.stdout.splitlines() if "Provisioner:" in line)
        assert PROV_APP in line and ".entrabot-state.json" in line
        assert run.deletes == []

    def test_a_failed_delete_shows_the_error_and_the_command_for_that_app(
        self, tmp_path: Path
    ) -> None:
        run = _teardown(
            tmp_path, "--yes", state=_state(PROVISIONER_CLIENT_ID=PROV_APP), delete_rc=1
        )

        assert run.returncode == 0, run.output()
        assert "Insufficient privileges" in run.stderr  # az's own error is not hidden
        assert f"Could not delete Provisioner app ({PROV_APP})" in run.stdout
        assert f"az ad app delete --id {PROV_APP}" in run.stdout

    def test_nothing_to_clean_up_still_says_no_app_was_looked_for_by_name(
        self, tmp_path: Path
    ) -> None:
        run = _teardown(tmp_path, "--yes", state=None)

        assert run.returncode == 0, run.output()
        assert "Nothing to clean up" in run.stdout
        assert run.az_calls == []
        assert PLACEHOLDER_COMMAND in run.stdout
