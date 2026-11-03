"""Tests for the shell side of the #124 fix: setup.sh Step 6 and the Windows wrapper.

setup.sh is never run for real here. The tests use ``bash -n``, ``--help`` (which
exits before any work), and the Step 6 decision block on its own, in bash, with
every command it calls replaced by a stub. That last one matters because the bug
this guards against lives in how exit codes survive ``set -e``, which reading the
script can't prove.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.scripts._blueprint_cert_helpers import (
    FAKE_APP_ID,
    FAKE_OBJECT_ID,
    REPO_ROOT,
    block_real_keyring_and_network,
    blueprint_certs,
    load_script,
)

SETUP_SH = REPO_ROOT / "scripts" / "setup.sh"
SETUP_PS1 = REPO_ROOT / "scripts" / "setup-windows.ps1"
DEPLOY_PS1 = REPO_ROOT / "scripts" / "deploy-windows.ps1"
TEARDOWN_SH = REPO_ROOT / "scripts" / "teardown.sh"

FLAG = "--remove-all-existing-certs"

CACHED = "c" * 43
RECOVERED = "r" * 43
REGISTERED = "n" * 43

# A keyCredentials key followed directly by a list holding an object literal: the
# shape that replaced the whole list.
ONE_ELEMENT_LITERAL = re.compile(r"""['"]?keyCredentials['"]?\s*:\s*\[\s*\{""")

posix_shell_only = pytest.mark.skipif(sys.platform == "win32", reason="runs under POSIX bash")


@pytest.fixture(autouse=True)
def _no_real_keychain_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_real_keyring_and_network(monkeypatch)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _help_run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run setup.sh with --help, which exits before doing any work.

    Tests pass --help even when checking another outcome: if a check ever regressed,
    the script still stops at the help text rather than starting real provisioning.
    """
    return subprocess.run(
        ["bash", str(SETUP_SH), *args, "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@posix_shell_only
class TestSetupScriptArguments:
    def test_bash_syntax_is_valid(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(SETUP_SH)], capture_output=True, text=True, timeout=30, check=False
        )
        assert result.returncode == 0, result.stderr

    def test_flag_is_parsed(self) -> None:
        result = _help_run(FLAG)
        assert result.returncode == 0
        assert "Unknown argument" not in result.stderr

    def test_an_unknown_flag_is_still_rejected(self) -> None:
        # Shows the parsing check above can fail.
        assert "Unknown argument" in _help_run("--not-a-real-flag").stderr

    def test_help_documents_the_flag_and_says_it_keeps_the_blueprint(self) -> None:
        output = " ".join(_help_run().stdout.split())
        assert FLAG in output
        assert "does NOT delete the Blueprint" in output
        assert "EVERY certificate" in output

    def test_help_says_a_normal_attach_keeps_existing_certificates(self) -> None:
        assert "Existing certificates are kept" in " ".join(_help_run().stdout.split())

    def test_flag_is_refused_with_new(self) -> None:
        # --new creates a Blueprint with no certificates, so the flag could only mislead.
        result = _help_run("--new", FLAG)
        assert result.returncode == 2
        assert "mutually exclusive" in result.stderr


class TestNothingReplacesTheListByAccident:
    @pytest.mark.parametrize("path", [SETUP_SH, SETUP_PS1, DEPLOY_PS1], ids=lambda p: p.name)
    def test_no_one_element_key_credentials_literal_remains(self, path: Path) -> None:
        assert ONE_ELEMENT_LITERAL.search(_read(path)) is None

    def test_the_guard_regex_recognizes_the_old_shapes(self) -> None:
        assert ONE_ELEMENT_LITERAL.search("json={'keyCredentials': [{")
        assert ONE_ELEMENT_LITERAL.search("body = {'keyCredentials': [{\n    'type': 'x'")
        assert ONE_ELEMENT_LITERAL.search('{"keyCredentials": [\n  {')

    def test_the_old_auto_proceed_and_wording_are_gone(self) -> None:
        text = " ".join(_read(SETUP_SH).split())
        assert "non-interactive shell" not in text
        assert "machine at a time" not in text
        assert "Replace existing cert(s)" not in text
        assert "REPLACE" not in text

    def test_step_6_no_longer_inlines_the_key_or_graph_handling(self) -> None:
        text = _read(SETUP_SH)
        assert "register_blueprint_cert.py" in text
        assert "keyring.set_password" not in text
        assert "requests.patch" not in text
        assert "rsa.generate_private_key" not in text

    def test_step_6_does_not_call_the_verify_script(self) -> None:
        # A cached thumbprint says nothing about whether this machine's key matches it, so
        # setup relies on find_local_blueprint_cert.py alone. verify_blueprint_cert.py is
        # an operator tool.
        code = [line for line in _read(SETUP_SH).splitlines() if not line.lstrip().startswith("#")]
        assert [line for line in code if "verify_blueprint_cert" in line] == []

    def test_windows_wrapper_goes_through_the_helper(self) -> None:
        text = _read(DEPLOY_PS1)
        assert "swap_key_credential" in text
        assert "httpx.patch" not in text
        assert "import blueprint_certs" in text

    def test_windows_driver_passes_both_ders_to_the_helper(self) -> None:
        text = _read(DEPLOY_PS1)
        assert "old_der=old_der" in text
        assert "new_der=new_der" in text
        assert "der_bytes=der_bytes" in text

    def test_windows_driver_is_valid_python_that_matches_the_helper(self) -> None:
        # PowerShell may not be available where this runs, so nothing else executes this
        # inline code before it reaches a Windows machine. PowerShell expands $Names
        # inside the here-string, so stand-ins replace them before parsing.
        here_string = re.search(r'\$driver = @"\r?\n(.*?)\r?\n"@', _read(DEPLOY_PS1), re.DOTALL)
        assert here_string is not None
        source = re.sub(r"\$(?:env:)?\w+", "stand-in", here_string.group(1))
        tree = ast.parse(source)

        graph_patch = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "graph_patch"
        )
        assert [arg.arg for arg in graph_patch.args.kwonlyargs] == ["token", "der_bytes"]

        call = graph_patch.body[0].value
        assert ast.unparse(call.func) == "blueprint_certs.swap_key_credential"
        keywords = {keyword.arg: None for keyword in call.keywords}
        inspect.signature(blueprint_certs.swap_key_credential).bind(**keywords)

        rotate_call = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "rcw.rotate"
        )
        rotate_keywords = {keyword.arg: None for keyword in rotate_call.keywords}
        rotate = load_script("rotate_cert_windows", alias="_rotate_for_driver_check").rotate
        inspect.signature(rotate).bind(**rotate_keywords)

    def test_teardown_clears_a_pending_key_too(self) -> None:
        assert "blueprint-private-key.pending" in _read(TEARDOWN_SH)


# ---------------------------------------------------------------------------
# The Step 6 decision block, executed
# ---------------------------------------------------------------------------

# Logs one line per call: the script's file name (or -c), the argument count, and the
# remaining arguments up to the first newline. The count catches broken quoting, and
# the fake root's path has a space in it, as a real checkout's can.
#
# find_local_blueprint_cert.py exits 1 whenever a pending key exists (that is tested in
# test_blueprint_cert_checks.py), so FAKE_PENDING stands in for the keyring entry here.
# The program passed to -c is kept, to show which thumbprint setup tried to save.
# A call to verify_blueprint_cert.py is logged and then fails: setup no longer makes one.
_FAKE_PYTHON = r"""#!/bin/bash
rest="${*:2}"
printf '%s %s %s\n' "${1##*/}" "$#" "${rest%%$'\n'*}" >> "$FAKE_CALLS"
case "$1" in
    *find_local_blueprint_cert.py)
        if [ -n "$FAKE_PENDING" ]; then
            echo "  A new private key is waiting to be confirmed." >&2
            exit 1
        fi
        printf '%s' "$FAKE_FIND_OUT"; exit "$FAKE_FIND_RC" ;;
    *register_blueprint_cert.py) printf '%s' "$FAKE_REGISTER_OUT"; exit "$FAKE_REGISTER_RC" ;;
    -c) printf '%s\n' "$2" > "$FAKE_ROOT/state-write.py"; exit "$FAKE_STATE_RC" ;;
    *) echo "unexpected interpreter call: $*" >&2; exit 99 ;;
esac
"""

_HARNESS = """
set -euo pipefail
GREEN=""; YELLOW=""; RED=""; BLUE=""; NC=""
success() {{ echo "SUCCESS: $1"; }}
warn()    {{ echo "WARN: $1"; }}
fail()    {{ echo "FAIL: $1"; exit 1; }}
read_state() {{
    if [ "$1" = "BLUEPRINT_CERT_THUMBPRINT" ]; then printf '%s\\n' "$FAKE_CACHED"; fi
}}
PROJECT_ROOT="$FAKE_ROOT"
VENV_PY="$FAKE_ROOT/fake-python"
PYTHON="$FAKE_ROOT/fake-python"
BLUEPRINT_OBJECT_ID="{object_id}"
BLUEPRINT_APP_ID="{app_id}"
REMOVE_ALL_EXISTING_CERTS="$FAKE_FLAG"
{step}
echo "FINAL:$CERT_THUMBPRINT"
"""


def _step_six_decisions() -> str:
    text = _read(SETUP_SH)
    start = text.index('CERT_THUMBPRINT=$(read_state "BLUEPRINT_CERT_THUMBPRINT")')
    end = text.index("# Step 7: Python venv")
    return text[start:end]


@dataclass
class StepRun:
    returncode: int
    stdout: str
    stderr: str
    calls: list[str]
    state_write: str | None = None

    @property
    def scripts(self) -> list[str]:
        """Which stubs ran, by short name, in order."""
        names = []
        for call in self.calls:
            first = call.split(" ", 1)[0]
            names.append(Path(first).stem if first != "-c" else "state-write")
        return names

    def argument_count(self, index: int) -> int:
        return int(self.calls[index].split(" ", 2)[1])

    @property
    def thumbprint(self) -> str | None:
        for line in self.stdout.splitlines():
            if line.startswith("FINAL:"):
                return line.removeprefix("FINAL:")
        return None


def _run_step(
    tmp_path: Path,
    *,
    cached: str = "",
    flag: bool = False,
    pending: bool = False,
    find: tuple[int, str] = (1, ""),
    register: tuple[int, str] = (0, REGISTERED),
    state_write_rc: int = 0,
) -> StepRun:
    root = tmp_path / "fake root with a space"
    root.mkdir()
    fake_python = root / "fake-python"
    fake_python.write_text(_FAKE_PYTHON)
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IEXEC)
    calls_file = root / "calls.log"
    calls_file.touch()
    script = root / "harness.sh"
    script.write_text(
        _HARNESS.format(object_id=FAKE_OBJECT_ID, app_id=FAKE_APP_ID, step=_step_six_decisions())
    )

    # A bare environment: nothing the stubbed block could reach is on PATH.
    env = {
        "PATH": os.defpath,
        "FAKE_ROOT": str(root),
        "FAKE_CALLS": str(calls_file),
        "FAKE_CACHED": cached,
        "FAKE_FLAG": "true" if flag else "false",
        "FAKE_PENDING": "1" if pending else "",
        "FAKE_FIND_RC": str(find[0]),
        "FAKE_FIND_OUT": find[1],
        "FAKE_REGISTER_RC": str(register[0]),
        "FAKE_REGISTER_OUT": register[1],
        "FAKE_STATE_RC": str(state_write_rc),
    }
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
        cwd=root,
    )
    state_write = root / "state-write.py"
    return StepRun(
        result.returncode,
        result.stdout,
        result.stderr,
        calls_file.read_text().splitlines(),
        state_write.read_text() if state_write.exists() else None,
    )


@posix_shell_only
class TestStepSixDecisions:
    def test_the_block_under_test_is_hermetic(self) -> None:
        # These tests execute this slice, so it must not grow a real side effect.
        code = [
            line for line in _step_six_decisions().splitlines() if not line.lstrip().startswith("#")
        ]
        side_effect = re.compile(r"(?:^|[\s;|&(])(?:pip3?|az|curl|wget|source|rm|sudo|security)\s")
        offenders = [line for line in code if side_effect.search(line)]
        assert offenders == [], "these would run for real in these tests"

    def test_a_cached_cert_the_local_key_matches_is_used_without_being_saved_again(
        self, tmp_path: Path
    ) -> None:
        run = _run_step(tmp_path, cached=CACHED, find=(0, CACHED + "\n"))

        assert run.returncode == 0, run.stdout + run.stderr
        assert run.scripts == ["find_local_blueprint_cert"]
        assert run.thumbprint == CACHED
        assert run.state_write is None

    def test_a_different_registered_cert_replaces_the_cached_one_and_is_saved(
        self, tmp_path: Path
    ) -> None:
        run = _run_step(tmp_path, cached=CACHED, find=(0, RECOVERED + "\n"))

        assert run.returncode == 0, run.stdout + run.stderr
        assert run.scripts == ["find_local_blueprint_cert", "state-write"]
        assert run.thumbprint == RECOVERED
        assert run.argument_count(1) == 4  # -c, the program, the state file, the thumbprint
        assert run.calls[1].endswith(f".entrabot-state.json {RECOVERED}")
        assert CACHED not in run.calls[1]

    def test_the_recovered_thumbprint_is_saved_by_the_atomic_writer(self, tmp_path: Path) -> None:
        run = _run_step(tmp_path, cached=CACHED, find=(0, RECOVERED + "\n"))
        state = tmp_path / "state.json"
        state.write_text(
            json.dumps({"BLUEPRINT_CERT_THUMBPRINT": CACHED, "BLUEPRINT_SECRET": "x", "KEEP": "me"})
        )

        # Truncating the file in place could leave invalid JSON after a full disk or an
        # interruption, and later runs couldn't read the saved identity. So run the program
        # setup hands to Python, for real, against a scratch state file.
        assert "write_state" in run.state_write
        assert "write_text" not in run.state_write
        result = subprocess.run(
            [sys.executable, "-c", run.state_write, str(state), RECOVERED],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "scripts")},
        )

        assert result.returncode == 0, result.stderr
        assert json.loads(state.read_text()) == {
            "BLUEPRINT_CERT_THUMBPRINT": RECOVERED,
            "KEEP": "me",
        }

    def test_a_failed_state_write_stops_setup_and_says_what_to_do(self, tmp_path: Path) -> None:
        run = _run_step(tmp_path, cached=CACHED, find=(0, RECOVERED + "\n"), state_write_rc=1)

        assert run.returncode != 0
        assert "FAIL: Could not save the certificate thumbprint" in run.stdout
        assert "re-run setup" in run.stdout
        assert run.thumbprint is None

    def test_a_stale_cache_adds_without_the_flag(self, tmp_path: Path) -> None:
        run = _run_step(tmp_path, cached=CACHED, find=(1, ""))

        assert run.returncode == 0, run.stdout + run.stderr
        assert run.scripts == ["find_local_blueprint_cert", "register_blueprint_cert"]
        assert FLAG not in run.calls[-1]
        assert run.argument_count(-1) == 7  # the script, then three name/value pairs
        assert f"--blueprint-object-id {FAKE_OBJECT_ID}" in run.calls[-1]
        assert f"--blueprint-app-id {FAKE_APP_ID}" in run.calls[-1]
        assert ".entrabot-state.json" in run.calls[-1]
        assert run.thumbprint == REGISTERED

    def test_a_cached_cert_is_not_trusted_while_a_new_key_is_pending(self, tmp_path: Path) -> None:
        # The cached thumbprint used to be accepted without asking register_blueprint_cert.py,
        # the one script that reconciles a pending key. After an unconfirmed
        # --remove-all-existing-certs that credential is revoked once the removal shows.
        run = _run_step(tmp_path, cached=CACHED, pending=True)

        assert run.returncode == 0, run.stdout + run.stderr
        assert run.scripts == ["find_local_blueprint_cert", "register_blueprint_cert"]
        assert FLAG not in run.calls[-1]
        assert run.thumbprint == REGISTERED
        assert run.state_write is None

    @pytest.mark.parametrize("cached", ["", CACHED], ids=["no-cache", "cached"])
    @pytest.mark.parametrize("code", [2, 3, 4, 127, 137, 139])
    def test_find_could_not_check_stops_and_never_regenerates(
        self, tmp_path: Path, cached: str, code: int
    ) -> None:
        run = _run_step(tmp_path, cached=cached, find=(code, ""))

        assert run.returncode != 0
        assert run.scripts == ["find_local_blueprint_cert"]
        assert "FAIL:" in run.stdout
        assert "Could not check" in run.stdout
        assert run.thumbprint is None

    def test_a_recovered_cert_is_recorded_and_reused(self, tmp_path: Path) -> None:
        run = _run_step(tmp_path, find=(0, RECOVERED + "\n"))

        assert run.returncode == 0, run.stdout + run.stderr
        assert run.scripts == ["find_local_blueprint_cert", "state-write"]
        assert run.thumbprint == RECOVERED

    def test_no_cache_and_no_match_adds(self, tmp_path: Path) -> None:
        run = _run_step(tmp_path, find=(1, ""))

        assert run.returncode == 0, run.stdout + run.stderr
        assert run.scripts == ["find_local_blueprint_cert", "register_blueprint_cert"]
        assert FLAG not in run.calls[-1]
        assert run.thumbprint == REGISTERED

    def test_the_flag_regenerates_even_when_the_cached_cert_is_valid(self, tmp_path: Path) -> None:
        run = _run_step(tmp_path, cached=CACHED, flag=True, find=(0, RECOVERED))

        assert run.returncode == 0, run.stdout + run.stderr
        assert run.scripts == ["register_blueprint_cert"]
        assert FLAG in run.calls[0]
        assert run.argument_count(0) == 8
        assert run.thumbprint == REGISTERED

    @pytest.mark.parametrize("code", [1, 2, 3, 127])
    def test_a_failed_registration_stops_setup(self, tmp_path: Path, code: int) -> None:
        run = _run_step(tmp_path, find=(1, ""), register=(code, ""))

        assert run.returncode != 0
        assert "FAIL:" in run.stdout
        assert run.thumbprint is None

    def test_an_unconfirmed_registration_says_to_re_run(self, tmp_path: Path) -> None:
        run = _run_step(tmp_path, find=(1, ""), register=(3, ""))
        assert run.returncode != 0
        assert "re-run" in run.stdout.lower()

    @pytest.mark.parametrize(
        "captured",
        ["", "too-short", "  Ensuring 25 Graph permissions...\n" + REGISTERED, "x" * 44],
    )
    def test_a_bad_thumbprint_from_registration_never_reaches_env(
        self, tmp_path: Path, captured: str
    ) -> None:
        run = _run_step(tmp_path, find=(1, ""), register=(0, captured))
        assert run.returncode != 0
        assert run.thumbprint is None

    def test_a_bad_thumbprint_from_recovery_is_not_recorded(self, tmp_path: Path) -> None:
        run = _run_step(tmp_path, find=(0, "  Ensuring 25 Graph permissions...\n" + RECOVERED))

        assert run.returncode != 0
        assert run.scripts == ["find_local_blueprint_cert"]
        assert run.thumbprint is None
