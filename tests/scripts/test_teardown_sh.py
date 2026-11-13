"""scripts/teardown.sh: .env is data, the Provisioner token stays off every command line, a failed
delete says why, and --dry-run does what its help says.

The defects these cover:

* ``.env`` was run as shell code (``source .env``). It is a data file that other tools write, so a
  value holding ``$(...)``, backticks or ``;`` ran as a command.
* The Provisioner access token was put into the text of a ``python -c`` program, which is on the
  command line, where any local user can read it from the process list. A token holding a quote
  broke the program, and so did a value from ``.env`` or the state file.
* The Agent User, Agent Identity and Blueprint deletes hid their errors (``2>/dev/null``).
* ``--dry-run`` was documented as resolving the targeted chains, and exited before doing so.

teardown.sh is never run in place: it removes .env and .entrabot-state.json from its own project
root. Each test runs a copy inside a temporary project. ``az`` and the virtual environment's
interpreter are stand-ins that record every call (its arguments and its environment). The
interpreter runs teardown's own programs for real, against a fake ``requests`` that makes no
request and a fake ``entra_provisioning`` that hands out a made-up token. The keyring cleanup is
never run: it would reach this machine's real keychain.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest import mock
from urllib.parse import quote

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEARDOWN_SH = REPO_ROOT / "scripts" / "teardown.sh"

posix_shell_only = pytest.mark.skipif(sys.platform == "win32", reason="runs under POSIX bash")

AGENT_USER = "00000000-0000-0000-0000-0000000000c1"
AGENT_OBJECT = "00000000-0000-0000-0000-0000000000b2"
BLUEPRINT_APP = "00000000-0000-0000-0000-0000000000a1"
BLUEPRINT_OBJECT = "00000000-0000-0000-0000-0000000000b1"
PROV_APP = "00000000-0000-0000-0000-0000000000a3"

# Made up. Long enough for teardown to take it as a token, and holding every character that broke
# the old program text: quotes, a backslash, a dollar sign and a command substitution.
TOKEN = "eyJ0eXAiOiJKV1QifQ." + "t0k3n" * 60 + "'\"\\$HOME$(touch pwned-from-token)"

_FAKE_AZ = r"""#!/bin/bash
n=0; [ -f "$FAKE_LOG/count" ] && n=$(cat "$FAKE_LOG/count")
n=$((n + 1)); echo "$n" > "$FAKE_LOG/count"
printf '%s\0' az "$@" > "$FAKE_LOG/call-$n.argv"
env > "$FAKE_LOG/call-$n.env"
case "$1 $2 $3" in
    "ad user delete")
        case "$FAKE_AZ_USER_DELETE" in
            not-found)
                printf '%s %s\n' "ERROR: Resource '$5' does not exist or one of its queried" \
                    'reference-property objects are not present.' >&2
                exit 1 ;;
            denied)
                echo "ERROR: Insufficient privileges to complete the operation." >&2
                exit 1 ;;
        esac
        exit 0 ;;
esac
exit 0
"""

_FAKE_PYTHON3 = '#!/bin/bash\nexec "$REAL_PYTHON" "$@"\n'

# The virtual environment's interpreter. Records the call, then runs teardown's own programs for
# real, with the fake modules first on the path. The keyring cleanup is recorded and not run.
_FAKE_VENV_PYTHON = r"""#!/bin/bash
n=0; [ -f "$FAKE_LOG/count" ] && n=$(cat "$FAKE_LOG/count")
n=$((n + 1)); echo "$n" > "$FAKE_LOG/count"
printf '%s\0' venv "$@" > "$FAKE_LOG/call-$n.argv"
env > "$FAKE_LOG/call-$n.env"
case "$1" in
    *deprovision_entra_agent_identity.py)
        echo "  Agent User:     agent@contoso.com (resolved by the targeted helper)"
        exit "${FAKE_HELPER_RC:-0}" ;;
    -c)
        case "$2" in
            *keyring*) exit 0 ;;
        esac
        PYTHONPATH="$FAKE_MODULES" exec "$REAL_PYTHON" "$@" ;;
esac
echo "unexpected interpreter call: $1" >&2
exit 99
"""

_FAKE_REQUESTS = r'''"""Stands in for requests: records each call, sends nothing."""
import json
import os


class RequestException(Exception):
    pass


class _Response:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


def delete(url, headers=None, timeout=None, **kwargs):
    with open(os.environ["FAKE_GRAPH_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps({"url": url, "headers": headers or {}, "timeout": timeout}) + "\n")
    for fragment, (status, text) in json.loads(os.environ.get("FAKE_GRAPH_ANSWERS", "{}")).items():
        if fragment in url:
            if status == "raise":
                raise RequestException(text)
            return _Response(int(status), text)
    return _Response(204, "")
'''

_FAKE_KEYRING = 'raise ImportError("the real keychain is not reachable from these tests")\n'

_FAKE_ENTRA_PROVISIONING = r'''"""Stands in for entra_provisioning.py: a made-up token, no Entra."""
import os

TOKEN = @@TOKEN@@


def get_existing_graph_token():
    mode = os.environ.get("FAKE_TOKEN_MODE", "ok")
    if mode == "fail":
        raise RuntimeError("Provisioner app is not bootstrapped")
    if mode == "chatter":
        print("  Using existing Provisioner cert")  # on stdout, as the real code can
    return TOKEN
'''


@dataclass
class Call:
    tool: str
    argv: list[str]
    env: str

    @property
    def program(self) -> str:
        return self.argv[1] if self.argv[:1] == ["-c"] and len(self.argv) > 1 else ""


@dataclass
class Run:
    returncode: int
    stdout: str
    stderr: str
    calls: list[Call]
    graph: list[dict]
    root: Path
    tmp_path: Path

    def az(self, *prefix: str) -> list[list[str]]:
        return [
            c.argv for c in self.calls if c.tool == "az" and c.argv[: len(prefix)] == list(prefix)
        ]

    def venv(self, needle: str) -> list[Call]:
        return [c for c in self.calls if c.tool == "venv" and needle in "\0".join(c.argv)]

    def output(self) -> str:
        return f"exit {self.returncode}\n--- stdout\n{self.stdout}\n--- stderr\n{self.stderr}"


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _state(**extra: str) -> dict[str, str]:
    return {
        "TENANT_ID": "00000000-0000-0000-0000-0000000000f1",
        "AGENT_USER_ID": AGENT_USER,
        "AGENT_OBJECT_ID": AGENT_OBJECT,
        "BLUEPRINT_APP_ID": BLUEPRINT_APP,
        "BLUEPRINT_OBJECT_ID": BLUEPRINT_OBJECT,
        "PROVISIONER_CLIENT_ID": PROV_APP,
        **extra,
    }


def _teardown(
    tmp_path: Path,
    *args: str,
    env_text: str | bytes | None = None,
    state: dict[str, str] | None = None,
    token_mode: str = "ok",
    az_user_delete: str = "ok",
    graph_answers: dict[str, tuple[str, str]] | None = None,
    helper_rc: int = 0,
    extra_env: dict[str, str] | None = None,
) -> Run:
    root = tmp_path / "project root with a space"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(TEARDOWN_SH, root / "scripts" / "teardown.sh")
    (root / "scripts" / "entra_provisioning.py").write_text(
        _FAKE_ENTRA_PROVISIONING.replace("@@TOKEN@@", repr(TOKEN)), encoding="utf-8"
    )
    (root / "scripts" / "deprovision_entra_agent_identity.py").write_text(
        "raise SystemExit('the real helper never runs here')\n", encoding="utf-8"
    )
    if state is not None:
        (root / ".entrabot-state.json").write_text(json.dumps(state), encoding="utf-8")
    if env_text is not None:
        data = env_text if isinstance(env_text, bytes) else env_text.encode("utf-8")
        (root / ".env").write_bytes(data)
    modules = tmp_path / "fake modules"
    modules.mkdir()
    (modules / "requests.py").write_text(_FAKE_REQUESTS, encoding="utf-8")
    (modules / "keyring.py").write_text(_FAKE_KEYRING, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    _executable(bin_dir / "az", _FAKE_AZ)
    _executable(bin_dir / "python3", _FAKE_PYTHON3)
    _executable(root / ".venv" / "bin" / "python3", _FAKE_VENV_PYTHON)
    log = tmp_path / "calls"
    log.mkdir()
    graph_log = tmp_path / "graph.jsonl"
    graph_log.touch()
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "HOME": str(home),
        "REAL_PYTHON": sys.executable,
        "FAKE_LOG": str(log),
        "FAKE_MODULES": str(modules),
        "FAKE_GRAPH_LOG": str(graph_log),
        "FAKE_GRAPH_ANSWERS": json.dumps(graph_answers or {}),
        "FAKE_TOKEN_MODE": token_mode,
        "FAKE_AZ_USER_DELETE": az_user_delete,
        "FAKE_HELPER_RC": str(helper_rc),
        **(extra_env or {}),
    }
    result = subprocess.run(
        ["bash", str(root / "scripts" / "teardown.sh"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
        env=env,
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
    )
    calls = []
    for argv_file in sorted(log.glob("call-*.argv"), key=lambda p: int(p.stem.split("-")[1])):
        tool, *argv = argv_file.read_bytes().decode("utf-8").split("\0")[:-1]
        env_dump = argv_file.with_suffix(".env").read_text(encoding="utf-8", errors="replace")
        calls.append(Call(tool, argv, env_dump))
    graph = [json.loads(line) for line in graph_log.read_text(encoding="utf-8").splitlines()]
    return Run(result.returncode, result.stdout, result.stderr, calls, graph, root, tmp_path)


def _pwned(tmp_path: Path) -> list[str]:
    return sorted(str(path) for path in tmp_path.rglob("pwned*"))


# ---------------------------------------------------------------------------
# Static: what the script must never contain again
# ---------------------------------------------------------------------------


def _script() -> str:
    return TEARDOWN_SH.read_text(encoding="utf-8")


def test_env_is_never_run_as_shell_code() -> None:
    assert not re.search(r"(?m)^\s*(?:set -a;?\s*)?(?:\.|source)\s+\S*\.env\b", _script())
    assert "eval" not in re.findall(r"\b\w+\b", _script())


def test_no_error_output_is_thrown_away() -> None:
    assert re.search(r"[0-9&]>\s*/dev/null", _script()) is None


def test_no_python_program_is_built_from_shell_values() -> None:
    # A double-quoted program, or an unquoted heredoc, would expand $VALUES into Python source.
    for program in re.findall(r'-c "((?:[^"\\]|\\.)*)"', _script(), flags=re.DOTALL):
        assert "$" not in program, program
    assert not re.search(r"python3?\S*\"? +-[^\n]*<<\s*[A-Za-z_]", _script())


def test_the_token_never_goes_into_program_text() -> None:
    # Behaviour is covered below (TestTheProvisionerToken); this keeps the old form out.
    assert "Bearer $PROV_TOKEN" not in _script()


# ---------------------------------------------------------------------------
# .env is read as data, the way entrabot.config reads it
# ---------------------------------------------------------------------------

HOSTILE_ENV = (
    "# a comment $(touch pwned-from-a-comment)\n"
    "ENTRABOT_AGENT_USER_ID=$(touch pwned-command-substitution)\n"
    "ENTRABOT_AGENT_OBJECT_ID=`touch pwned-backticks`\n"
    "ENTRABOT_BLUEPRINT_APP_ID=x; touch pwned-semicolon\n"
    "ENTRABOT_BLUEPRINT_OBJECT_ID=\"quoted value\" 'and more' | touch pwned-pipe\n"
    "touch pwned-a-bare-line\n"
    "export ENTRABOT_TENANT_ID=$(touch pwned-export)\n"
    "PATH=/nowhere\n"
)


@posix_shell_only
class TestEnvIsData:
    def test_nothing_in_env_runs_and_the_values_arrive_intact(self, tmp_path: Path) -> None:
        run = _teardown(tmp_path, "--dry-run", env_text=HOSTILE_ENV, state=_state())

        assert run.returncode == 0, run.output()
        assert _pwned(tmp_path) == []
        # The summary shows each value exactly as .env holds it.
        assert (
            "$(touch pwned-command-substitution) (not an object ID, so not deleted)" in run.stdout
        )
        assert "Agent Identity: `touch pwned-backticks`" in run.stdout
        assert "Blueprint:      x; touch pwned-semicolon" in run.stdout
        # PATH in .env is a value like any other: the state file was still read with python3
        # from the real PATH.
        assert f"Provisioner:    {PROV_APP} (recorded in .entrabot-state.json)" in run.stdout
        assert run.calls == []

    def test_values_from_env_reach_the_deletes_verbatim_and_as_data(self, tmp_path: Path) -> None:
        run = _teardown(tmp_path, "--yes", env_text=HOSTILE_ENV, state=_state())

        assert run.returncode == 0, run.output()
        assert _pwned(tmp_path) == []
        urls = [request["url"] for request in run.graph]
        assert urls == [
            "https://graph.microsoft.com/beta/servicePrincipals/"
            + quote("`touch pwned-backticks`", safe=""),
            "https://graph.microsoft.com/beta/applications/"
            + quote("\"quoted value\" 'and more' | touch pwned-pipe", safe=""),
        ]
        # Each ID is one argument of the program, never part of its text.
        (agent_identity,) = run.venv("servicePrincipals")
        assert agent_identity.argv[2:] == ["servicePrincipals", "`touch pwned-backticks`"]

    def test_an_agent_user_id_that_is_not_an_object_id_is_not_handed_to_az(
        self, tmp_path: Path
    ) -> None:
        # az ad user delete --id also takes a sign-in name: only an object ID is passed.
        env = "ENTRABOT_AGENT_USER_ID=someone.else@contoso.com\n"
        run = _teardown(tmp_path, "--yes", env_text=env, state=_state())

        assert run.returncode == 0, run.output()
        assert run.az("ad", "user", "delete") == []
        assert "The Agent User ID recorded for this machine is not an object ID" in run.stdout
        assert "It is: someone.else@contoso.com" in run.stdout

    def test_bom_crlf_padding_and_the_first_of_a_repeated_key(self, tmp_path: Path) -> None:
        env = (
            b"\xef\xbb\xbfENTRABOT_AGENT_USER_ID =  " + AGENT_USER.encode() + b"  \r\n"
            b"\r\n"
            b"# ENTRABOT_AGENT_OBJECT_ID=00000000-0000-0000-0000-00000000dead\r\n"
            b"\tENTRABOT_AGENT_OBJECT_ID\t=\t" + AGENT_OBJECT.encode() + b"\r\n"
            b"ENTRABOT_AGENT_OBJECT_ID=00000000-0000-0000-0000-00000000beef\r\n"
            b"ENTRABOT_BLUEPRINT_OBJECT_ID=" + BLUEPRINT_OBJECT.encode()
        )
        # The state file names other objects: .env wins, as it always has.
        state = _state(
            AGENT_USER_ID="00000000-0000-0000-0000-00000000aaa1",
            AGENT_OBJECT_ID="00000000-0000-0000-0000-00000000aaa2",
            BLUEPRINT_OBJECT_ID="00000000-0000-0000-0000-00000000aaa3",
        )
        run = _teardown(tmp_path, "--yes", env_text=env, state=state)

        assert run.returncode == 0, run.output()
        assert run.az("ad", "user", "delete") == [["ad", "user", "delete", "--id", AGENT_USER]]
        assert [request["url"] for request in run.graph] == [
            f"https://graph.microsoft.com/beta/servicePrincipals/{AGENT_OBJECT}",
            f"https://graph.microsoft.com/beta/applications/{BLUEPRINT_OBJECT}",
        ]

    def test_without_env_the_state_file_is_used(self, tmp_path: Path) -> None:
        run = _teardown(tmp_path, "--yes", state=_state())

        assert run.returncode == 0, run.output()
        assert run.az("ad", "user", "delete") == [["ad", "user", "delete", "--id", AGENT_USER]]
        assert len(run.graph) == 2

    @pytest.mark.skipif(
        getattr(os, "geteuid", lambda: 0)() == 0, reason="root can read a file with no permissions"
    )
    def test_an_env_that_cannot_be_read_stops_teardown_before_anything_is_deleted(
        self, tmp_path: Path
    ) -> None:
        # Sourcing it used to stop the script (set -e). Read as data, it must not look empty.
        env_file = tmp_path / "project root with a space" / ".env"
        try:
            original_write = Path.write_bytes

            def write_then_lock(path: Path, data: bytes) -> int:
                written = original_write(path, data)
                if path == env_file:
                    path.chmod(0)
                return written

            with mock.patch.object(Path, "write_bytes", write_then_lock):
                run = _teardown(
                    tmp_path,
                    "--yes",
                    env_text=f"ENTRABOT_AGENT_USER_ID={AGENT_USER}\n",
                    state=_state(),
                )
        finally:
            env_file.chmod(0o600)

        assert run.returncode == 1, run.output()
        assert ".env exists but cannot be read" in run.stderr
        assert run.calls == []
        assert env_file.exists() and (run.root / ".entrabot-state.json").exists()


def _reader() -> str:
    match = re.search(
        r"^env_file_value\(\) \{\n.*?^\}\n", _script(), flags=re.DOTALL | re.MULTILINE
    )
    assert match, "teardown.sh defines no env_file_value reader"
    return match.group(0)


def _read_with_teardown(tmp_path: Path, data: bytes, key: str) -> tuple[int, str]:
    folder = tmp_path / quote(key, safe="")
    folder.mkdir()
    (folder / ".env").write_bytes(data)
    script = folder / "reader.sh"
    script.write_text(
        "set -euo pipefail\n" + _reader() + 'if value=$(env_file_value "$1" "$2"); then '
        'printf "FOUND:%s" "$value"; else printf "ABSENT"; fi\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(script), str(folder / ".env"), key],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
        env={"PATH": os.defpath},
        stdin=subprocess.DEVNULL,
    )
    return result.returncode, result.stdout


TRICKY = (
    b"\xef\xbb\xbfENTRABOT_FIRST=after a byte-order mark\r\n"
    b"# ENTRABOT_COMMENTED=from a comment\n"
    b"\n"
    b"   ENTRABOT_PADDED   =   spaced value   \n"
    b"ENTRABOT_EQUALS=a=b==c\n"
    b"ENTRABOT_QUOTED=\"double\" and 'single'\n"
    b"ENTRABOT_REPEATED=first\n"
    b"ENTRABOT_REPEATED=second\n"
    b"ENTRABOT_EMPTY_FIRST=\n"
    b"ENTRABOT_EMPTY_FIRST=later\n"
    b"NO_EQUALS_ON_THIS_LINE\n"
    b"ENTRABOT_SHELLISH=$HOME `id` $(id) \\n ; echo hi\n"
    b"ENTRABOT_CRLF=windows\r\n"
    b"\tENTRABOT_TABS\t=\ttabbed\t\n"
    b"ENTRABOT_LAST=no newline at the end"
)


@posix_shell_only
class TestTheReaderAgreesWithConfigPy:
    def test_every_key_reads_as_entrabot_config_reads_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from entrabot import config

        project = tmp_path / "project"
        (project / "src" / "entrabot").mkdir(parents=True)
        (project / ".env").write_bytes(TRICKY)
        monkeypatch.setattr(config, "__file__", str(project / "src" / "entrabot" / "config.py"))
        fresh: dict[str, str] = {}
        with mock.patch.object(config, "os", mock.Mock(environ=fresh)):
            config._load_dotenv()

        assert fresh["ENTRABOT_FIRST"] == "after a byte-order mark"
        assert fresh["ENTRABOT_REPEATED"] == "first" and fresh["ENTRABOT_EMPTY_FIRST"] == ""
        for key, value in fresh.items():
            assert _read_with_teardown(tmp_path, TRICKY, key) == (0, f"FOUND:{value}"), key

    @pytest.mark.parametrize(
        "key", ["ENTRABOT_COMMENTED", "NO_EQUALS_ON_THIS_LINE", "ENTRABOT_NOT_THERE", "# ENTRABOT"]
    )
    def test_what_config_py_skips_is_absent(self, tmp_path: Path, key: str) -> None:
        assert _read_with_teardown(tmp_path, TRICKY, key) == (0, "ABSENT")


# ---------------------------------------------------------------------------
# The Provisioner token
# ---------------------------------------------------------------------------


@posix_shell_only
class TestTheProvisionerToken:
    def test_the_token_reaches_graph_intact_and_no_command_line_holds_it(
        self, tmp_path: Path
    ) -> None:
        run = _teardown(tmp_path, "--yes", state=_state())

        assert run.returncode == 0, run.output()
        assert "Got Provisioner token" in run.stdout
        assert [request["headers"]["Authorization"] for request in run.graph] == [
            f"Bearer {TOKEN}"
        ] * 2
        assert _pwned(tmp_path) == []
        for call in run.calls:
            assert TOKEN not in "\0".join(call.argv), call.argv[:1]
            assert "t0k3nt0k3n" not in "\0".join(call.argv), call.argv[:1]

    def test_only_the_two_graph_deletes_get_the_token_in_their_environment(
        self, tmp_path: Path
    ) -> None:
        # Exported in the calling shell: neither name may carry the real token to anything else.
        inherited = {"PROV_TOKEN": "inherited", "PROVISIONER_TOKEN": "inherited"}
        run = _teardown(tmp_path, "--yes", state=_state(), extra_env=inherited)

        assert run.returncode == 0, run.output()
        holders = [call for call in run.calls if "t0k3nt0k3n" in call.env]
        assert [call.argv[2] for call in holders] == ["servicePrincipals", "applications"]
        later = run.calls[run.calls.index(holders[-1]) + 1 :]
        assert later, "teardown ran nothing after the Graph deletes"
        assert run.az("ad", "app", "delete") == [["ad", "app", "delete", "--id", PROV_APP]]
        for call in later:
            assert "t0k3nt0k3n" not in call.env

    def test_what_the_provisioning_code_prints_is_shown_and_not_taken_for_the_token(
        self, tmp_path: Path
    ) -> None:
        run = _teardown(tmp_path, "--yes", state=_state(), token_mode="chatter")

        assert run.returncode == 0, run.output()
        assert "Using existing Provisioner cert" in run.stderr
        assert run.graph[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"

    def test_why_no_token_could_be_had_is_shown(self, tmp_path: Path) -> None:
        run = _teardown(tmp_path, "--yes", state=_state(), token_mode="fail")

        assert run.returncode == 0, run.output()
        assert "Could not get provisioner token: Provisioner app is not bootstrapped" in run.stderr
        assert "No Provisioner token — manual cleanup may be required" in run.stdout
        assert run.graph == []

    def test_the_scripts_folder_is_an_argument_not_program_text(self, tmp_path: Path) -> None:
        run = _teardown(tmp_path, "--yes", state=_state())

        (acquire,) = run.venv("get_existing_graph_token")
        assert acquire.argv[2:] == [str(run.root / "scripts")]
        assert str(run.root) not in acquire.program


# ---------------------------------------------------------------------------
# A failed delete says why; "already gone" is told from the answer
# ---------------------------------------------------------------------------


@posix_shell_only
class TestDeletesSayWhy:
    def test_a_deleted_agent_user(self, tmp_path: Path) -> None:
        run = _teardown(tmp_path, "--yes", state=_state())

        assert f"Deleted Agent User ({AGENT_USER})" in run.stdout

    def test_an_agent_user_that_is_already_gone(self, tmp_path: Path) -> None:
        run = _teardown(tmp_path, "--yes", state=_state(), az_user_delete="not-found")

        assert run.returncode == 0, run.output()
        assert "does not exist or one of its queried reference-property objects" in run.stderr
        assert "Agent User not found — already deleted" in run.stdout
        assert "Could not delete Agent User" not in run.stdout

    def test_an_agent_user_that_could_not_be_deleted(self, tmp_path: Path) -> None:
        run = _teardown(tmp_path, "--yes", state=_state(), az_user_delete="denied")

        assert run.returncode == 0, run.output()
        assert "Insufficient privileges to complete the operation." in run.stderr
        assert f"Could not delete Agent User ({AGENT_USER}): see the az error above" in run.stdout
        assert f"az ad user delete --id {AGENT_USER}" in run.stdout
        assert "already deleted" not in run.stdout

    @pytest.mark.parametrize(
        ("fragment", "label"),
        [("servicePrincipals", "Agent Identity SP"), ("applications", "Blueprint")],
    )
    def test_already_gone_in_graph(self, tmp_path: Path, fragment: str, label: str) -> None:
        answers = {fragment: ("404", '{"error":{"code":"Request_ResourceNotFound"}}')}
        run = _teardown(tmp_path, "--yes", state=_state(), graph_answers=answers)

        assert run.returncode == 0, run.output()
        assert f"{label} not found — already deleted" in run.stdout

    @pytest.mark.parametrize(
        ("fragment", "label", "object_id"),
        [
            ("servicePrincipals", "Agent Identity SP", AGENT_OBJECT),
            ("applications", "Blueprint", BLUEPRINT_OBJECT),
        ],
    )
    def test_a_graph_refusal_is_shown(
        self, tmp_path: Path, fragment: str, label: str, object_id: str
    ) -> None:
        body = (
            '{"error":{"code":"Authorization_RequestDenied","message":"Insufficient privileges"}}'
        )
        run = _teardown(tmp_path, "--yes", state=_state(), graph_answers={fragment: ("403", body)})

        assert run.returncode == 0, run.output()
        assert f"Microsoft Graph answered 403: {body}" in run.stderr
        assert f"Could not delete {label} ({object_id}, status: 403): see the error above" in (
            run.stdout
        )

    def test_graph_out_of_reach_is_shown(self, tmp_path: Path) -> None:
        answers = {"servicePrincipals": ("raise", "Name or service not known")}
        run = _teardown(tmp_path, "--yes", state=_state(), graph_answers=answers)

        assert run.returncode == 0, run.output()
        assert "Could not reach Microsoft Graph: RequestException: Name or service not known" in (
            run.stderr
        )
        assert f"Could not delete Agent Identity SP ({AGENT_OBJECT}, status: error)" in run.stdout
        assert f"Deleted Blueprint app ({BLUEPRINT_APP})" in run.stdout  # the rest still runs

    def test_each_graph_delete_has_a_time_limit(self, tmp_path: Path) -> None:
        run = _teardown(tmp_path, "--yes", state=_state())

        assert [request["timeout"] for request in run.graph] == [60, 60]


# ---------------------------------------------------------------------------
# --dry-run does what its help says
# ---------------------------------------------------------------------------


@posix_shell_only
class TestDryRun:
    def test_targeted_mode_resolves_each_chain_read_only_and_deletes_nothing(
        self, tmp_path: Path
    ) -> None:
        run = _teardown(
            tmp_path,
            "--dry-run",
            "--agent-user-upn=a@contoso.com, b@contoso.com",
            state=_state(),
            env_text="ENTRABOT_TENANT_ID=00000000-0000-0000-0000-0000000000f1\n",
        )

        assert run.returncode == 0, run.output()
        (helper,) = run.venv("deprovision_entra_agent_identity.py")
        assert helper.argv[1:] == [
            "--dry-run",
            "--agent-user-upn",
            "a@contoso.com",
            "--agent-user-upn",
            "b@contoso.com",
        ]
        assert "resolved by the targeted helper" in run.stdout
        assert run.az() == [] and run.graph == []
        assert run.venv("get_existing_graph_token") == []  # no Provisioner token of its own
        assert (run.root / ".env").exists() and (run.root / ".entrabot-state.json").exists()
        assert "Dry run only — no tenant or local state will be deleted." in run.stdout

    def test_targeted_mode_does_not_list_the_chain_it_would_not_delete(
        self, tmp_path: Path
    ) -> None:
        run = _teardown(tmp_path, "--dry-run", "--agent-user-upn=a@contoso.com", state=_state())

        assert "Target Agent User UPN: a@contoso.com" in run.stdout
        assert AGENT_USER not in run.stdout and AGENT_OBJECT not in run.stdout

    def test_a_targeted_dry_run_that_fails_says_so_and_exits_1(self, tmp_path: Path) -> None:
        run = _teardown(
            tmp_path, "--dry-run", "--agent-user-upn=a@contoso.com", state=_state(), helper_rc=1
        )

        assert run.returncode == 1, run.output()
        assert "Targeted dry run failed: see the error above" in run.stdout
        assert (run.root / ".entrabot-state.json").exists()

    def test_state_based_mode_runs_nothing_at_all(self, tmp_path: Path) -> None:
        run = _teardown(tmp_path, "--dry-run", state=_state())

        assert run.returncode == 0, run.output()
        assert run.calls == []
        assert f"Agent User:     {AGENT_USER}" in run.stdout
        assert f"Agent Identity: {AGENT_OBJECT}" in run.stdout

    def test_targeted_teardown_passes_the_upns_without_dry_run(self, tmp_path: Path) -> None:
        run = _teardown(tmp_path, "--yes", "--agent-user-upn=a@contoso.com", state=_state())

        assert run.returncode == 0, run.output()
        (helper,) = run.venv("deprovision_entra_agent_identity.py")
        assert helper.argv[1:] == ["--agent-user-upn", "a@contoso.com"]
        assert run.az("ad", "user", "delete") == [] and run.graph == []

    def test_a_upn_is_passed_on_exactly_as_given(self, tmp_path: Path) -> None:
        # It used to be trimmed with xargs, which reads quotes: an apostrophe stopped teardown.
        run = _teardown(
            tmp_path,
            "--dry-run",
            '--agent-user-upn= o\'brien@contoso.com ,"q"@x.com',
            state=_state(),
        )

        assert run.returncode == 0, run.output()
        (helper,) = run.venv("deprovision_entra_agent_identity.py")
        assert helper.argv[1:] == [
            "--dry-run",
            "--agent-user-upn",
            "o'brien@contoso.com",
            "--agent-user-upn",
            '"q"@x.com',
        ]

    def test_the_help_says_what_dry_run_does(self, tmp_path: Path) -> None:
        run = _teardown(tmp_path, "--help", state=_state())
        text = " ".join(run.stdout.split())

        assert run.returncode == 0, run.output()
        assert run.calls == []
        assert "Resolve and print targeted chains without deleting." not in text
        assert (
            "--dry-run Print what would be deleted, and delete nothing. With --agent-user-upn, "
            "each Agent User's chain is looked up in Entra (read-only) and printed."
        ) in text
