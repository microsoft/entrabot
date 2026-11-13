"""Tests for the settings setup.sh must keep when it is run again.

Step 7 used to write ``.env`` from scratch without reading the old file. A re-run that did not
repeat ``--use-cloud-memory`` moved an agent on cloud memory to an empty local store (the runtime
gives the local flag priority over the blob settings), and one that did not repeat
``--teams-user`` made the sponsor whoever ``az`` was signed in as. The documented repair command,
``setup.sh --use-blueprint=<id>``, passes neither flag.

setup.sh is never run for real here. These tests run slices of it under bash with every command
it would call stubbed: the argument parser, the ``read_env_value`` helper, and Step 7 plus 7b.
The helper reads a real ``.env`` with the real interpreter. Nothing else runs for real.
``bash`` here is whatever ``/bin/bash`` is: 3.2 on macOS, so the script has to work there.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

from tests.scripts._blueprint_cert_helpers import REPO_ROOT, block_real_keyring_and_network

SETUP_SH = REPO_ROOT / "scripts" / "setup.sh"

posix_shell_only = pytest.mark.skipif(sys.platform == "win32", reason="runs under POSIX bash")

AGENT_USER = "00000000-0000-0000-0000-0000000000c1"
OTHER_AGENT_USER = "00000000-0000-0000-0000-0000000000c2"

OLD_ENDPOINT = "https://contosoold.blob.core.windows.net"
OLD_CONTAINER = f"agent-{AGENT_USER}"
NEW_ENDPOINT = "https://contosonew.blob.core.windows.net"
NEW_CONTAINER = "agent-new"
ENDPOINT_LINE = f"ENTRABOT_BLOB_ENDPOINT={OLD_ENDPOINT}"
CONTAINER_LINE = f"ENTRABOT_BLOB_CONTAINER={OLD_CONTAINER}"
CLOUD = [ENDPOINT_LINE, CONTAINER_LINE]
LOCAL = ["ENTRABOT_KEEP_MEMORY_LOCAL=true"]
PROVISIONED = f"BLOB_ENDPOINT={NEW_ENDPOINT}\nBLOB_CONTAINER={NEW_CONTAINER}\n"

HUMAN_KEYS = (
    "ENTRABOT_HUMAN_USER_ID",
    "ENTRABOT_HUMAN_UPN",
    "ENTRABOT_HUMAN_USER_IDS",
    "ENTRABOT_HUMAN_UPNS",
    "ENTRABOT_HUMAN_USER_TENANT_IDS",
    "ENTRABOT_HUMAN_USER_MAILS",
    "ENTRABOT_HUMAN_USER_TYPES",
)
# Who `az` is signed in as: what Step 2 leaves in HUMAN_* when nothing else is asked for.
AZ_USER = dict(
    zip(
        HUMAN_KEYS,
        [
            "00000000-0000-0000-0000-0000000000d1",
            "admin@contoso.com",
            "00000000-0000-0000-0000-0000000000d1",
            "admin@contoso.com",
            "",
            "admin@contoso.com",
            "Member",
        ],
        strict=True,
    )
)
# The sponsors an earlier run wrote: two people, one of them from another tenant.
SPONSOR = dict(
    zip(
        HUMAN_KEYS,
        [
            "00000000-0000-0000-0000-0000000000d2",
            "sponsor@contoso.com",
            "00000000-0000-0000-0000-0000000000d2,00000000-0000-0000-0000-0000000000d3",
            "sponsor@contoso.com,colleague@contoso.com",
            ",00000000-0000-0000-0000-0000000000e1",
            "sponsor@contoso.com,colleague@contoso.com",
            "Member,Guest",
        ],
        strict=True,
    )
)
# What Step 2 leaves in HUMAN_* after resolving --teams-user.
TEAMS_USER = dict(
    zip(
        HUMAN_KEYS,
        [
            "00000000-0000-0000-0000-0000000000d4",
            "chat@contoso.com",
            "00000000-0000-0000-0000-0000000000d4",
            "chat@contoso.com",
            "",
            "chat@contoso.com",
            "Member",
        ],
        strict=True,
    )
)

# The one scratch file Step 7b writes. It is a fixed path under /tmp, so the harness moves it into
# the test's temp directory (see _harness) and the hermetic test allows exactly this one use.
SCRATCH = "/tmp/entrabot-provision-stdout.$$"
SIDE_EFFECT = re.compile(r"(?:^|[\s;|&(])(?:pip3?|az|curl|wget|source|rm|sudo|security)\s")


@pytest.fixture(autouse=True)
def _no_real_keychain_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_real_keyring_and_network(monkeypatch)


# ---------------------------------------------------------------------------
# The slices of setup.sh under test
# ---------------------------------------------------------------------------


def _script() -> str:
    return SETUP_SH.read_text(encoding="utf-8")


def _defaults_slice() -> str:
    """The option defaults, up to (not including) the --status short-circuit."""
    text = _script()
    return text[text.index("SWITCH_USER=false") : text.index("SETUP_STATUS=false")]


def _parse_slice() -> str:
    """The loop that parses the real options. Nothing in it runs a command."""
    text = _script()
    start = text.index('for arg in "$@"; do\n    case $arg in')
    return text[start : text.index("# Mutex: --create-new-storage")]


def _helper() -> str | None:
    match = re.search(
        r"^read_env_value\(\) \{\n.*?^\}\n", _script(), flags=re.DOTALL | re.MULTILINE
    )
    return match.group(0) if match else None


def _step_seven() -> str:
    """Step 7 after the dependency install, through Step 7b."""
    text = _script()
    anchor = 'success "Installed dependencies (including dev)"\n'
    return text[text.index(anchor) + len(anchor) : text.index("# Step 8: Summary")]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

# Stands in for the interpreter setup.sh calls. It logs every call and runs only the .env reader
# for real. The provisioner and the persona lookup get canned answers.
_FAKE_PYTHON = r"""#!/bin/bash
rest="${*:2}"
printf '%s %s %s\n' "${1##*/}" "$#" "${rest%%$'\n'*}" >> "$FAKE_CALLS"
case "$1" in
    *provision_blob_storage.py)
        echo "progress: creating resources" >&2
        printf '%s' "$FAKE_PROVISION_OUT"
        exit "$FAKE_PROVISION_RC" ;;
    -c)
        case "$2" in
            *sys.argv*) exec "$REAL_PYTHON" "$@" ;;
            *claude_code_memory_dir*) exit 0 ;;
        esac
        echo "unexpected interpreter program: ${2%%$'\n'*}" >&2; exit 99 ;;
    *) echo "unexpected interpreter call: $*" >&2; exit 99 ;;
esac
"""

_HARNESS = r"""
set -euo pipefail
GREEN=""; YELLOW=""; RED=""; BLUE=""; NC=""
success() { echo "SUCCESS: $1"; }
warn()    { echo "WARN: $1"; }
err()     { echo "ERR: $1"; }
fail()    { echo "FAIL: $1"; exit 1; }
MIGRATION_FAILED=false
PROJECT_ROOT="$FAKE_ROOT"
PYTHON="$FAKE_ROOT/fake-python"
cd "$PROJECT_ROOT"
@@DEFAULTS@@
@@PARSE@@
@@HELPER@@
# What Steps 2 and 5 leave behind for Step 7.
TENANT_ID="00000000-0000-0000-0000-0000000000f1"
BLUEPRINT_APP_ID="00000000-0000-0000-0000-0000000000a1"
BLUEPRINT_OBJECT_ID="00000000-0000-0000-0000-0000000000b1"
CERT_THUMBPRINT="thumbprint-placeholder"
AGENT_ID="00000000-0000-0000-0000-0000000000a2"
AGENT_OBJECT_ID="00000000-0000-0000-0000-0000000000b2"
AGENT_USER_ID="$FAKE_AGENT_USER_ID"
AGENT_USER_UPN="agent@contoso.com"
PROV_CLIENT_ID="00000000-0000-0000-0000-0000000000a3"
HUMAN_USER_ID="$FAKE_HUMAN_USER_ID"
HUMAN_UPN="$FAKE_HUMAN_UPN"
HUMAN_USER_IDS="$FAKE_HUMAN_USER_IDS"
HUMAN_UPNS="$FAKE_HUMAN_UPNS"
HUMAN_USER_TENANT_IDS="$FAKE_HUMAN_USER_TENANT_IDS"
HUMAN_USER_MAILS="$FAKE_HUMAN_USER_MAILS"
HUMAN_USER_TYPES="$FAKE_HUMAN_USER_TYPES"
@@STEP@@
echo "COMPLETED"
"""


def _harness() -> str:
    # The block keeps its provisioner output in a fixed /tmp file. Move it under the test's own
    # directory so the run never writes outside it. The quotes matter: the path has a space.
    step = _step_seven().replace(SCRATCH, '"$FAKE_ROOT"/provision-stdout.$$')
    return (
        _HARNESS.replace("@@DEFAULTS@@", _defaults_slice())
        .replace("@@PARSE@@", _parse_slice())
        .replace("@@HELPER@@", _helper() or "")
        .replace("@@STEP@@", step)
    )


def _parse_env(text: str) -> dict[str, str]:
    """Read ``.env`` the way entrabot.config._load_dotenv does: the first value of a key wins."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values.setdefault(key.strip(), value.strip())
    return values


def _keys(text: str) -> list[str]:
    return [
        line.partition("=")[0].strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#") and "=" in line
    ]


def _env_text(
    *,
    agent_user_id: str | None = AGENT_USER,
    sponsor: dict[str, str] | None = SPONSOR,
    memory: list[str] | None = None,
) -> str:
    """An earlier run's .env, in the shape setup.sh writes it."""
    lines = ["# EntraBot Identity Research - generated by scripts/setup.sh", ""]
    lines.append("ENTRABOT_TENANT_ID=00000000-0000-0000-0000-0000000000f1")
    if agent_user_id is not None:
        lines.append(f"ENTRABOT_AGENT_USER_ID={agent_user_id}")
    lines += [f"{key}={value}" for key, value in (sponsor or {}).items()]
    lines += memory or []
    return "\n".join(lines) + "\n"


@dataclass
class Rerun:
    returncode: int
    stdout: str
    stderr: str
    env_text: str
    calls: list[str]
    root: Path

    @property
    def env(self) -> dict[str, str]:
        return _parse_env(self.env_text)

    @property
    def sponsor(self) -> dict[str, str]:
        return {key: self.env.get(key, "<missing>") for key in HUMAN_KEYS}

    @property
    def provisioned(self) -> bool:
        return any(call.startswith("provision_blob_storage.py") for call in self.calls)

    @property
    def leftovers(self) -> list[str]:
        return sorted(path.name for path in self.root.glob("provision-stdout.*"))

    def completed(self) -> bool:
        return self.returncode == 0 and "COMPLETED" in self.stdout

    def output(self) -> str:
        return f"exit {self.returncode}\n--- stdout\n{self.stdout}\n--- stderr\n{self.stderr}"


def _run(
    tmp_path: Path,
    *args: str,
    old_env: str | None = None,
    agent_user_id: str = AGENT_USER,
    human: dict[str, str] = AZ_USER,
    provision: tuple[int, str] = (0, PROVISIONED),
) -> Rerun:
    root = tmp_path / "fake root with a space"
    root.mkdir()
    fake_python = root / "fake-python"
    fake_python.write_text(_FAKE_PYTHON, encoding="utf-8")
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IEXEC)
    calls_file = root / "calls.log"
    calls_file.touch()
    if old_env is not None:
        (root / ".env").write_text(old_env, encoding="utf-8")
    script = root / "harness.sh"
    script.write_text(_harness(), encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()

    # A bare environment: nothing the stubbed blocks could reach is on PATH, and HOME is empty.
    env = {
        "PATH": os.defpath,
        "HOME": str(home),
        "FAKE_ROOT": str(root),
        "FAKE_CALLS": str(calls_file),
        "REAL_PYTHON": sys.executable,
        "FAKE_AGENT_USER_ID": agent_user_id,
        "FAKE_PROVISION_RC": str(provision[0]),
        "FAKE_PROVISION_OUT": provision[1],
        **{"FAKE_" + key.removeprefix("ENTRABOT_"): value for key, value in human.items()},
    }
    result = subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
        env=env,
        cwd=root,
        stdin=subprocess.DEVNULL,
    )
    env_file = root / ".env"
    return Rerun(
        result.returncode,
        result.stdout,
        result.stderr,
        env_file.read_text(encoding="utf-8") if env_file.exists() else "",
        calls_file.read_text(encoding="utf-8").splitlines(),
        root,
    )


def _assert_cloud_kept(run: Rerun) -> None:
    assert run.completed(), run.output()
    assert run.env.get("ENTRABOT_BLOB_ENDPOINT") == OLD_ENDPOINT, run.env_text
    assert run.env.get("ENTRABOT_BLOB_CONTAINER") == OLD_CONTAINER, run.env_text
    assert "ENTRABOT_KEEP_MEMORY_LOCAL" not in run.env, run.env_text
    assert len(_keys(run.env_text)) == len(set(_keys(run.env_text))), run.env_text


def _assert_local(run: Rerun) -> None:
    assert run.completed(), run.output()
    assert run.env.get("ENTRABOT_KEEP_MEMORY_LOCAL") == "true", run.env_text
    assert "ENTRABOT_BLOB_ENDPOINT" not in run.env, run.env_text
    assert "ENTRABOT_BLOB_CONTAINER" not in run.env, run.env_text
    assert len(_keys(run.env_text)) == len(set(_keys(run.env_text))), run.env_text


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


@posix_shell_only
class TestRerunKeepsMemory:
    def test_no_flags_on_a_cloud_machine_keeps_the_blob_settings(self, tmp_path: Path) -> None:
        run = _run(tmp_path, old_env=_env_text(memory=CLOUD))

        _assert_cloud_kept(run)
        assert not run.provisioned
        assert "Memory mode: CLOUD (already set in .env" in run.stdout
        assert "--keep-memory-local" in run.stdout

    @pytest.mark.parametrize("falsy", ["false", "0", "no", "", "maybe"])
    def test_a_falsy_local_flag_is_still_cloud(self, tmp_path: Path, falsy: str) -> None:
        # config.py only reads true, 1 and yes as on.
        run = _run(
            tmp_path, old_env=_env_text(memory=[*CLOUD, f"ENTRABOT_KEEP_MEMORY_LOCAL={falsy}"])
        )

        _assert_cloud_kept(run)

    @pytest.mark.parametrize(
        "memory",
        [
            pytest.param([ENDPOINT_LINE], id="endpoint-only"),
            pytest.param([CONTAINER_LINE], id="container-only"),
            pytest.param([*CLOUD, "ENTRABOT_KEEP_MEMORY_LOCAL=true"], id="local-true"),
            pytest.param([*CLOUD, "ENTRABOT_KEEP_MEMORY_LOCAL=TRUE"], id="local-TRUE"),
            pytest.param([*CLOUD, "ENTRABOT_KEEP_MEMORY_LOCAL=1"], id="local-1"),
            pytest.param([*CLOUD, "ENTRABOT_KEEP_MEMORY_LOCAL=Yes"], id="local-Yes"),
            pytest.param([*CLOUD, "  ENTRABOT_KEEP_MEMORY_LOCAL =  yes  "], id="local-padded"),
            pytest.param([ENDPOINT_LINE, "ENTRABOT_BLOB_CONTAINER="], id="container-empty"),
            pytest.param(LOCAL, id="local-only"),
            pytest.param(None, id="no-memory-settings"),
        ],
    )
    def test_half_a_cloud_config_or_the_local_flag_is_not_cloud(
        self, tmp_path: Path, memory: list[str] | None
    ) -> None:
        run = _run(tmp_path, old_env=_env_text(memory=memory))

        _assert_local(run)
        assert not run.provisioned

    def test_a_first_run_with_no_env_file_is_local(self, tmp_path: Path) -> None:
        run = _run(tmp_path)

        _assert_local(run)
        assert run.sponsor == AZ_USER
        assert "Kept the sponsor" not in run.stdout

    @pytest.mark.parametrize("flags", [(), ("--use-cloud-memory",)])
    @pytest.mark.parametrize(
        "old_agent_user_id",
        [OTHER_AGENT_USER, None, ""],
        ids=["other-agent", "env-has-no-agent", "env-agent-empty"],
    )
    def test_a_different_agent_user_carries_nothing_over(
        self, tmp_path: Path, flags: tuple[str, ...], old_agent_user_id: str | None
    ) -> None:
        old = _env_text(agent_user_id=old_agent_user_id, memory=CLOUD)
        run = _run(tmp_path, *flags, old_env=old)

        assert run.completed(), run.output()
        assert run.sponsor == AZ_USER
        assert run.env.get("ENTRABOT_BLOB_ENDPOINT") != OLD_ENDPOINT
        assert run.env.get("ENTRABOT_BLOB_CONTAINER") != OLD_CONTAINER
        if flags:
            assert run.env["ENTRABOT_BLOB_ENDPOINT"] == NEW_ENDPOINT
        else:
            _assert_local(run)

    def test_no_agent_user_this_run_carries_nothing_over(self, tmp_path: Path) -> None:
        # Both ids empty is not "the same agent": there is nothing to scope the settings to.
        old = _env_text(agent_user_id="", memory=CLOUD)
        run = _run(tmp_path, old_env=old, agent_user_id="")

        _assert_local(run)
        assert run.sponsor == AZ_USER
        assert not run.provisioned

    @pytest.mark.parametrize("old_memory", [CLOUD, LOCAL], ids=["from-cloud", "from-local"])
    def test_keep_memory_local_wins(self, tmp_path: Path, old_memory: list[str]) -> None:
        run = _run(tmp_path, "--keep-memory-local", old_env=_env_text(memory=old_memory))

        _assert_local(run)
        assert not run.provisioned

    @pytest.mark.parametrize("old_memory", [CLOUD, LOCAL], ids=["from-cloud", "from-local"])
    def test_use_cloud_memory_wins_and_writes_the_new_settings(
        self, tmp_path: Path, old_memory: list[str]
    ) -> None:
        run = _run(tmp_path, "--use-cloud-memory", old_env=_env_text(memory=old_memory))

        assert run.completed(), run.output()
        assert run.provisioned
        assert run.env["ENTRABOT_BLOB_ENDPOINT"] == NEW_ENDPOINT
        assert run.env["ENTRABOT_BLOB_CONTAINER"] == NEW_CONTAINER
        assert "ENTRABOT_KEEP_MEMORY_LOCAL" not in run.env
        assert run.leftovers == []

    @pytest.mark.parametrize(
        "provision",
        [
            pytest.param((3, ""), id="provisioner-fails"),
            pytest.param((0, ""), id="no-output"),
            pytest.param((0, f"BLOB_ENDPOINT={NEW_ENDPOINT}\n"), id="no-container-line"),
            pytest.param((0, "BLOB_ENDPOINT=\nBLOB_CONTAINER=\n"), id="empty-values"),
        ],
    )
    def test_a_failed_provision_keeps_cloud_on_a_cloud_machine(
        self, tmp_path: Path, provision: tuple[int, str]
    ) -> None:
        run = _run(
            tmp_path, "--use-cloud-memory", old_env=_env_text(memory=CLOUD), provision=provision
        )

        _assert_cloud_kept(run)
        assert run.provisioned
        assert "keeping the cloud memory already set in .env" in run.stdout
        assert run.leftovers == []

    @pytest.mark.parametrize(
        "provision",
        [
            pytest.param((3, ""), id="provisioner-fails"),
            pytest.param((0, ""), id="no-output"),
            pytest.param((0, f"BLOB_ENDPOINT={NEW_ENDPOINT}\n"), id="no-container-line"),
            pytest.param((0, "BLOB_ENDPOINT=\nBLOB_CONTAINER=\n"), id="empty-values"),
        ],
    )
    @pytest.mark.parametrize("old_memory", [LOCAL, None], ids=["local-machine", "no-memory"])
    def test_a_failed_provision_goes_local_on_a_local_machine(
        self, tmp_path: Path, provision: tuple[int, str], old_memory: list[str] | None
    ) -> None:
        run = _run(
            tmp_path,
            "--use-cloud-memory",
            old_env=_env_text(memory=old_memory),
            provision=provision,
        )

        _assert_local(run)
        assert run.provisioned
        assert "local-only memory" in run.stdout
        assert run.leftovers == []

    def test_the_provisioners_own_output_is_shown_when_it_fails(self, tmp_path: Path) -> None:
        # The fallback has to say why. Aborting at the capture swallowed all of it.
        run = _run(tmp_path, "--use-cloud-memory", provision=(3, ""))

        assert run.completed(), run.output()
        assert "progress: creating resources" in run.stdout


# ---------------------------------------------------------------------------
# Sponsor
# ---------------------------------------------------------------------------


@posix_shell_only
class TestRerunKeepsTheSponsor:
    def test_the_sponsor_is_kept_with_no_flags(self, tmp_path: Path) -> None:
        run = _run(tmp_path, old_env=_env_text(memory=LOCAL))

        assert run.completed(), run.output()
        assert run.sponsor == SPONSOR
        kept = [line for line in run.stdout.splitlines() if "Kept the sponsor" in line]
        assert len(kept) == 1, run.stdout
        assert "--teams-user" in kept[0]
        assert "sponsor@contoso.com" in kept[0]

    def test_switch_user_makes_the_new_user_the_sponsor(self, tmp_path: Path) -> None:
        # Its help says the new user becomes the agent's owner and sponsor, so it's as
        # explicit as --teams-user. Step 2 has already left the new user in HUMAN_*.
        run = _run(tmp_path, "--switch-user", old_env=_env_text(memory=LOCAL))

        assert run.completed(), run.output()
        assert run.sponsor == AZ_USER
        assert "Kept the sponsor" not in run.stdout

    def test_a_failed_migration_says_how_to_retry_it(self) -> None:
        # Only --use-cloud-memory reaches the migration, and a plain re-run now keeps the
        # cloud settings and skips it, so the hint has to name the flag.
        hint = _script()[_script().index('if [ "$MIGRATION_FAILED" = true ]; then\n    # Name') :]
        # Step 3 refuses a run with neither --new nor --use-blueprint. --use-cloud-memory also
        # picks the default store unless it's told which one this run just provisioned.
        retry = (
            "./scripts/setup.sh --use-blueprint=$BLUEPRINT_APP_ID --use-cloud-memory "
            "--with-storage-account=${BLOB_ENDPOINT_ACCOUNT} --with-container=$BLOB_CONTAINER"
        )
        assert retry in hint[: hint.index("exit 2")]
        assert 'BLOB_ENDPOINT_ACCOUNT="${BLOB_ENDPOINT_ACCOUNT%%.*}"' in hint

    def test_env_is_never_run_as_shell_code(self) -> None:
        # Kept values come back out of .env verbatim, so sourcing it would execute a stored
        # $(...) and break on a space or an apostrophe. entrabot.config reads it as data.
        assert not re.search(r"(?m)^\s*(?:set -a;\s*)?(?:\.|source)\s+\S*\.env\b", _script())

    def test_teams_user_overrides_the_kept_sponsor(self, tmp_path: Path) -> None:
        run = _run(
            tmp_path,
            "--teams-user=chat@contoso.com",
            old_env=_env_text(memory=LOCAL),
            human=TEAMS_USER,
        )

        assert run.completed(), run.output()
        assert run.sponsor == TEAMS_USER
        assert "Kept the sponsor" not in run.stdout

    @pytest.mark.parametrize(
        "key,value",
        [
            ("ENTRABOT_HUMAN_USER_ID", "00000000-0000-0000-0000-0000000000d5"),
            ("ENTRABOT_HUMAN_USER_IDS", "00000000-0000-0000-0000-0000000000d5,x"),
            ("ENTRABOT_HUMAN_UPN", "single@contoso.com"),
            ("ENTRABOT_HUMAN_UPNS", "first@contoso.com,second@contoso.com"),
        ],
    )
    def test_any_one_sponsor_key_counts_as_naming_a_sponsor(
        self, tmp_path: Path, key: str, value: str
    ) -> None:
        run = _run(tmp_path, old_env=_env_text(sponsor={key: value}))

        assert run.completed(), run.output()
        # All seven come from the old file, so the ones it never had are written empty.
        assert run.sponsor == {k: (value if k == key else "") for k in HUMAN_KEYS}
        assert "Kept the sponsor" in run.stdout

    def test_tenant_mails_and_types_alone_do_not_name_a_sponsor(self, tmp_path: Path) -> None:
        extras = ("TENANT_IDS", "MAILS", "TYPES")
        others = {k: v for k, v in SPONSOR.items() if k.endswith(extras)}
        run = _run(tmp_path, old_env=_env_text(sponsor=others))

        assert run.completed(), run.output()
        assert run.sponsor == AZ_USER
        assert "Kept the sponsor" not in run.stdout

    @pytest.mark.parametrize(
        "old_agent_user_id",
        [OTHER_AGENT_USER, None, ""],
        ids=["other-agent", "env-has-no-agent", "env-agent-empty"],
    )
    def test_a_different_agent_user_does_not_keep_the_sponsor(
        self, tmp_path: Path, old_agent_user_id: str | None
    ) -> None:
        run = _run(tmp_path, old_env=_env_text(agent_user_id=old_agent_user_id))

        assert run.completed(), run.output()
        assert run.sponsor == AZ_USER
        assert "Kept the sponsor" not in run.stdout

    def test_values_with_awkward_characters_survive_the_rewrite(self, tmp_path: Path) -> None:
        odd = {
            "ENTRABOT_HUMAN_UPN": 'a=b "quoted" it\'s $HOME `date` \\n end',
            "ENTRABOT_HUMAN_UPNS": "x@contoso.com",
        }
        run = _run(tmp_path, old_env=_env_text(sponsor=odd))

        assert run.completed(), run.output()
        assert run.env["ENTRABOT_HUMAN_UPN"] == odd["ENTRABOT_HUMAN_UPN"]

    def test_the_first_of_a_repeated_sponsor_key_wins(self, tmp_path: Path) -> None:
        old = _env_text(sponsor={}) + "ENTRABOT_HUMAN_UPN=first@contoso.com\n"
        old += "ENTRABOT_HUMAN_UPN=second@contoso.com\n"
        run = _run(tmp_path, old_env=old)

        assert run.completed(), run.output()
        assert run.env["ENTRABOT_HUMAN_UPN"] == "first@contoso.com"
        assert _keys(run.env_text).count("ENTRABOT_HUMAN_UPN") == 1


# ---------------------------------------------------------------------------
# read_env_value
# ---------------------------------------------------------------------------


def _read_env_value(
    tmp_path: Path, env_text: str | None, key: str
) -> subprocess.CompletedProcess[str]:
    helper = _helper()
    assert helper is not None, "setup.sh defines no read_env_value helper"
    root = tmp_path / "fake root with a space"
    root.mkdir(parents=True)
    if env_text is not None:
        (root / ".env").write_text(env_text, encoding="utf-8")
    script = root / "helper.sh"
    script.write_text(
        'set -euo pipefail\nPROJECT_ROOT="$FAKE_ROOT"\nPYTHON="$REAL_PYTHON"\n'
        + helper
        + 'read_env_value "$1"\n',
        encoding="utf-8",
    )
    # Run from somewhere else, to show it reads PROJECT_ROOT's file and not the current directory's.
    return subprocess.run(
        ["bash", str(script), key],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
        env={"PATH": os.defpath, "FAKE_ROOT": str(root), "REAL_PYTHON": sys.executable},
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
    )


TRICKY = (
    "# ENTRABOT_COMMENTED=from a comment\n"
    "\n"
    "   ENTRABOT_PADDED   =   spaced value   \n"
    "ENTRABOT_EQUALS=a=b==c\n"
    "ENTRABOT_QUOTED=\"double\" and 'single'\n"
    "ENTRABOT_REPEATED=first\n"
    "ENTRABOT_REPEATED=second\n"
    "ENTRABOT_EMPTY_FIRST=\n"
    "ENTRABOT_EMPTY_FIRST=later\n"
    "NO_EQUALS_ON_THIS_LINE\n"
    "ENTRABOT_SHELLISH=$HOME `id` $(id) \\n ; echo hi\n"
    "ENTRABOT_CRLF=windows\r\n"
    "ENTRABOT_LAST=no newline at the end"
)


@posix_shell_only
class TestReadEnvValue:
    @pytest.mark.parametrize(
        "key,expected",
        [
            ("ENTRABOT_PADDED", "spaced value"),
            ("ENTRABOT_EQUALS", "a=b==c"),
            ("ENTRABOT_QUOTED", "\"double\" and 'single'"),
            ("ENTRABOT_REPEATED", "first"),
            ("ENTRABOT_EMPTY_FIRST", ""),
            ("ENTRABOT_SHELLISH", "$HOME `id` $(id) \\n ; echo hi"),
            ("ENTRABOT_CRLF", "windows"),
            ("ENTRABOT_LAST", "no newline at the end"),
            ("ENTRABOT_COMMENTED", ""),
            ("NO_EQUALS_ON_THIS_LINE", ""),
            ("ENTRABOT_NOT_THERE", ""),
        ],
    )
    def test_reads_the_value_the_way_config_py_does(
        self, tmp_path: Path, key: str, expected: str
    ) -> None:
        result = _read_env_value(tmp_path, TRICKY, key)

        assert result.returncode == 0, result.stderr
        assert result.stdout.rstrip("\n") == expected

    def test_agrees_with_config_py_on_every_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from entrabot import config

        project = tmp_path / "project"
        (project / "src" / "entrabot").mkdir(parents=True)
        (project / ".env").write_text(TRICKY.replace("\r\n", "\n"), encoding="utf-8")
        monkeypatch.setattr(config, "__file__", str(project / "src" / "entrabot" / "config.py"))
        with mock.patch.dict(os.environ):
            config._load_dotenv()
            loaded = {key: os.environ[key] for key in _parse_env(TRICKY)}

        assert loaded
        for key, value in loaded.items():
            result = _read_env_value(tmp_path / key, TRICKY, key)
            assert result.stdout.rstrip("\n") == value, key

    def test_a_missing_file_is_empty_and_not_an_error(self, tmp_path: Path) -> None:
        result = _read_env_value(tmp_path, None, "ENTRABOT_TENANT_ID")

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    def test_the_key_is_an_argument_and_never_part_of_the_program(self, tmp_path: Path) -> None:
        hostile = 'X"); open("pwned", "w").close(); ("'
        result = _read_env_value(tmp_path, "A=1\n", hostile)

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert not list(tmp_path.rglob("pwned"))

    @pytest.mark.skipif(
        getattr(os, "geteuid", lambda: 0)() == 0, reason="root can read a file with no permissions"
    )
    def test_a_file_that_cannot_be_read_stops_setup_with_a_message(self, tmp_path: Path) -> None:
        # A .env that is there but unreadable must not look empty. Setup would rewrite it as if
        # there were nothing to keep. With `set -e`, the failed capture ends the script here,
        # before anything is written.
        helper = _helper()
        assert helper is not None, "setup.sh defines no read_env_value helper"
        root = tmp_path / "root"
        root.mkdir()
        env_file = root / ".env"
        env_file.write_text("ENTRABOT_X=1\n", encoding="utf-8")
        env_file.chmod(0)
        script = tmp_path / "helper.sh"
        script.write_text(
            'set -euo pipefail\nPROJECT_ROOT="$1"\nPYTHON="$2"\n'
            + helper
            + 'OLD=$(read_env_value ENTRABOT_X)\necho "NOT REACHED"\n',
            encoding="utf-8",
        )
        try:
            result = subprocess.run(
                ["bash", str(script), str(root), sys.executable],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=False,
                env={"PATH": os.defpath},
                stdin=subprocess.DEVNULL,
            )
        finally:
            env_file.chmod(0o600)

        assert result.returncode != 0
        assert "NOT REACHED" not in result.stdout
        assert "Could not read" in result.stderr


# ---------------------------------------------------------------------------
# The option parser, the block's side effects, and --help
# ---------------------------------------------------------------------------


@posix_shell_only
class TestMemoryFlagsAreRecorded:
    @pytest.mark.parametrize(
        "args,expected",
        [
            ((), "false true false"),
            (("--use-cloud-memory",), "true false true"),
            (("--keep-memory-local",), "true true false"),
            (("--use-cloud-memory", "--keep-memory-local"), "true true false"),
            (("--keep-memory-local", "--use-cloud-memory"), "true false true"),
        ],
    )
    def test_passing_a_memory_flag_is_recorded(
        self, tmp_path: Path, args: tuple[str, ...], expected: str
    ) -> None:
        script = tmp_path / "parse.sh"
        script.write_text(
            "set -euo pipefail\n"
            + _defaults_slice()
            + _parse_slice()
            + 'echo "$MEMORY_FLAG_PASSED $KEEP_MEMORY_LOCAL $USE_CLOUD_MEMORY"\n',
            encoding="utf-8",
        )
        result = subprocess.run(
            ["bash", str(script), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
            env={"PATH": os.defpath},
            stdin=subprocess.DEVNULL,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected


@posix_shell_only
class TestTheBlocksUnderTest:
    def test_the_block_under_test_is_hermetic(self) -> None:
        # These tests execute these slices, so none may grow a real side effect.
        executed = _defaults_slice() + _parse_slice() + (_helper() or "") + _step_seven()
        code = [line.strip() for line in executed.splitlines() if not line.lstrip().startswith("#")]
        offenders = [
            line for line in code if SIDE_EFFECT.search(line) and line != f"rm -f {SCRATCH}"
        ]
        assert offenders == [], "these would run for real in these tests"

        # The provisioner's scratch file is the only thing outside the project, and the harness
        # moves it. If the block starts using another /tmp path, it would not be moved.
        paths = {path for line in code for path in re.findall(r"/tmp/[\w.$-]+", line)}
        assert paths <= {SCRATCH}, f"unexpected /tmp use: {paths - {SCRATCH}}"

    def test_the_harness_moves_the_scratch_file_out_of_tmp(self) -> None:
        assert "/tmp/" not in _harness()

    def test_the_new_code_does_not_hide_errors(self) -> None:
        helper = _helper()
        assert helper is not None, "setup.sh defines no read_env_value helper"
        assert "/dev/null" not in helper
        assert "/dev/null" not in _step_seven().split("# Step 7b")[0]


@posix_shell_only
class TestHelp:
    def test_help_says_a_rerun_keeps_cloud_memory_and_the_sponsor(self) -> None:
        result = subprocess.run(
            ["bash", str(SETUP_SH), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        output = " ".join(result.stdout.split())

        assert result.returncode == 0, result.stderr
        assert "keeps cloud memory that is already set in .env" in output
        assert "keeps the recipient already in .env" in output
        # --keep-memory-local is no longer a no-op alias for the default.
        assert "this is now the default behavior" not in output


# ---------------------------------------------------------------------------
# --use-blueprint: which agent the run is for (identity mode, then Step 5)
# ---------------------------------------------------------------------------

OLD_BP = "00000000-0000-0000-0000-00000000aaaa"
NEW_BP = "00000000-0000-0000-0000-00000000bbbb"
OLD_UPN = "entrabot-agent-old@contoso.com"
MINE_UPN = "entrabot-agent-mine@contoso.com"
OLD_STATE = {
    "PROVISIONER_CLIENT_ID": "00000000-0000-0000-0000-0000000000a3",
    "TENANT_ID": "00000000-0000-0000-0000-0000000000f1",
    "BLUEPRINT_APP_ID": OLD_BP,
    "BLUEPRINT_OBJECT_ID": "00000000-0000-0000-0000-00000000abab",
    "AGENT_ID": "00000000-0000-0000-0000-00000000acac",
    "AGENT_USER_UPN": OLD_UPN,
}


def _identity_slice() -> str:
    """From the identity-mode checks up to Step 4."""
    text = _script()
    return text[
        text.index("# ── Validate identity mode") : text.index(
            'step 4 "Bootstrapping Entra provisioner app"'
        )
    ]


def _agent_step_slice() -> str:
    """Step 5: create_entra_agent_ids.py and the checks on what it recorded, up to Step 6."""
    text = _script()
    return text[
        text.index('echo "  Running create_entra_agent_ids.py..."') : text.index(
            "# Step 6: Generate Blueprint certificate"
        )
    ]


def _function(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", _script(), flags=re.DOTALL | re.MULTILINE)
    return match.group(0) if match else ""


# Runs the state-file programs and the .env reader for real, in the fake root, and stands in for
# create_entra_agent_ids.py: it notes what the helper would see (the identity variables, and the
# state file as it was) and records the agent the way the helper does, with the Blueprint App ID
# FAKE_RECORDED_BLUEPRINT when that is set.
_FAKE_AGENT_PYTHON = r"""#!/bin/bash
case "$1" in
    *create_entra_agent_ids.py)
        {
            if [ -n "${ENTRABOT_AGENT_USER_UPN+x}" ]; then
                echo "UPN=set:$ENTRABOT_AGENT_USER_UPN"
            else
                echo "UPN=unset"
            fi
            if [ -n "${_ENTRABOT_UPN_SUFFIX+x}" ]; then
                echo "SUFFIX=set:$_ENTRABOT_UPN_SUFFIX"
            else
                echo "SUFFIX=unset"
            fi
            if [ -n "${ENTRABOT_NEW_CHAIN+x}" ]; then
                echo "NEW_CHAIN=set:$ENTRABOT_NEW_CHAIN"
            else
                echo "NEW_CHAIN=unset"
            fi
        } > "$FAKE_ROOT/helper-saw.txt"
        if [ -f "$FAKE_ROOT/.entrabot-state.json" ]; then
            cp "$FAKE_ROOT/.entrabot-state.json" "$FAKE_ROOT/state-seen-by-helper.json"
        fi
        if [ "$FAKE_CREATE_RC" = 0 ]; then
            "$REAL_PYTHON" -c '
import json, pathlib, sys
path, blueprint = pathlib.Path(sys.argv[1]), sys.argv[2]
data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
if blueprint:
    data["BLUEPRINT_APP_ID"] = blueprint
data.update(BLUEPRINT_OBJECT_ID="bp-object", AGENT_ID="agent-app", AGENT_OBJECT_ID="agent-object",
            AGENT_USER_ID="agent-user", AGENT_USER_UPN="agent@contoso.com")
path.write_text(json.dumps(data), encoding="utf-8")
' "$FAKE_ROOT/.entrabot-state.json" "$FAKE_RECORDED_BLUEPRINT"
        fi
        exit "$FAKE_CREATE_RC" ;;
    -c) exec "$REAL_PYTHON" "$@" ;;
    *) echo "unexpected interpreter call: $*" >&2; exit 99 ;;
esac
"""

_AGENT_HARNESS = r"""
set -euo pipefail
GREEN=""; YELLOW=""; RED=""; BLUE=""; NC=""
success() { echo "SUCCESS: $1"; }
warn()    { echo "WARN: $1"; }
err()     { echo "ERR: $1"; }
fail()    { echo "FAIL: $1"; exit 1; }
configure_a365_work_iq() { echo "WORK IQ CONFIGURED"; }
PROJECT_ROOT="$FAKE_ROOT"
SCRIPT_DIR="$FAKE_ROOT/scripts"
PYTHON="$FAKE_ROOT/fake-python"
SCRIPT_PYTHON="$FAKE_ROOT/fake-python"
cd "$PROJECT_ROOT"
@@DEFAULTS@@
@@PARSE@@
@@READ_STATE@@
@@READ_ENV_VALUE@@
@@SAME_ID@@
@@IDENTITY@@
@@AGENT_STEP@@
if [ -n "${ENTRABOT_AGENT_USER_UPN+x}" ]; then
    echo "LATER STEPS SEE ENTRABOT_AGENT_USER_UPN=$ENTRABOT_AGENT_USER_UPN"
fi
echo "REACHED THE CERTIFICATE STEP"
"""


def _agent_harness() -> str:
    return (
        _AGENT_HARNESS.replace("@@DEFAULTS@@", _defaults_slice())
        .replace("@@PARSE@@", _parse_slice())
        .replace("@@READ_STATE@@", _function("read_state"))
        .replace("@@READ_ENV_VALUE@@", _function("read_env_value"))
        .replace("@@SAME_ID@@", _function("same_id"))
        .replace("@@IDENTITY@@", _identity_slice())
        .replace("@@AGENT_STEP@@", _agent_step_slice())
    )


@dataclass
class AgentStepRun:
    returncode: int
    stdout: str
    stderr: str
    root: Path

    @property
    def helper_saw(self) -> dict[str, str]:
        """What create_entra_agent_ids.py would have found in its environment."""
        path = self.root / "helper-saw.txt"
        if not path.exists():
            return {}
        return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines())

    @property
    def state_seen_by_helper(self) -> dict[str, str] | None:
        path = self.root / "state-seen-by-helper.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def reached_the_certificate_step(self) -> bool:
        return self.returncode == 0 and "REACHED THE CERTIFICATE STEP" in self.stdout

    def output(self) -> str:
        return f"exit {self.returncode}\n--- stdout\n{self.stdout}\n--- stderr\n{self.stderr}"


def _run_agent_step(
    tmp_path: Path,
    *args: str,
    state: dict[str, str] | None = None,
    env_lines: list[str] | None = None,
    recorded_blueprint: str = "",
    create_rc: int = 0,
) -> AgentStepRun:
    root = tmp_path / "fake root with a space"
    (root / "scripts").mkdir(parents=True)
    fake_python = root / "fake-python"
    fake_python.write_text(_FAKE_AGENT_PYTHON, encoding="utf-8")
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IEXEC)
    if state is not None:
        (root / ".entrabot-state.json").write_text(json.dumps(state), encoding="utf-8")
    if env_lines is not None:
        (root / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    script = root / "harness.sh"
    script.write_text(_agent_harness(), encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
        env={
            "PATH": os.defpath,
            "HOME": str(home),
            "FAKE_ROOT": str(root),
            "REAL_PYTHON": sys.executable,
            "FAKE_RECORDED_BLUEPRINT": recorded_blueprint,
            "FAKE_CREATE_RC": str(create_rc),
        },
        cwd=root,
        stdin=subprocess.DEVNULL,
    )
    return AgentStepRun(result.returncode, result.stdout, result.stderr, root)


def _old_env(blueprint: str = OLD_BP, upn: str = OLD_UPN) -> list[str]:
    """The .env an earlier setup wrote for its agent."""
    return [
        "ENTRABOT_TENANT_ID=00000000-0000-0000-0000-0000000000f1",
        f"ENTRABOT_BLUEPRINT_APP_ID={blueprint}",
        f"ENTRABOT_AGENT_USER_UPN={upn}",
    ]


@posix_shell_only
class TestUseBlueprintDoesNotCarryAnotherAgentsUser:
    """The defect: .env keeps the previous agent's ENTRABOT_AGENT_USER_UPN, the helper loads
    .env for any variable that is not set, and --use-blueprint did not set it. Joining another
    Blueprint therefore looked for the previous agent's Agent User, and reused it."""

    def test_a_switch_hands_the_helper_an_empty_upn(self, tmp_path: Path) -> None:
        run = _run_agent_step(
            tmp_path, f"--use-blueprint={NEW_BP}", state=OLD_STATE, env_lines=_old_env()
        )

        assert run.reached_the_certificate_step(), run.output()
        # Set, and empty: an empty value counts as set, so the helper's .env load can't
        # bring back the old agent's.
        assert run.helper_saw["UPN"] == "set:"
        assert run.helper_saw["SUFFIX"] == "unset"
        assert run.helper_saw["NEW_CHAIN"] == "unset"
        assert run.state_seen_by_helper == {
            "PROVISIONER_CLIENT_ID": OLD_STATE["PROVISIONER_CLIENT_ID"],
            "TENANT_ID": OLD_STATE["TENANT_ID"],
            "BLUEPRINT_APP_ID": NEW_BP,
        }
        assert OLD_UPN in run.stdout and "--agent-user-upn=UPN" in run.stdout
        # For the helper alone: the smoke test after Step 7 must read the new .env, and an
        # empty value left set would also switch off its check of whose token it got.
        assert "LATER STEPS SEE" not in run.stdout

    @pytest.mark.parametrize(
        "state",
        [None, {"BLUEPRINT_APP_ID": NEW_BP}],
        ids=["state-file-lost", "switch-interrupted-before-env-was-rewritten"],
    )
    def test_an_env_written_for_another_blueprint_is_not_carried(
        self, tmp_path: Path, state: dict[str, str] | None
    ) -> None:
        # The state file no longer says this is a switch, but .env still names the old agent.
        run = _run_agent_step(
            tmp_path, f"--use-blueprint={NEW_BP}", state=state, env_lines=_old_env()
        )

        assert run.reached_the_certificate_step(), run.output()
        assert run.helper_saw["UPN"] == "set:"

    @pytest.mark.parametrize("asked", [NEW_BP, NEW_BP.upper()], ids=["same", "other-letter-case"])
    def test_the_same_blueprint_keeps_the_agent_user_in_env(
        self, tmp_path: Path, asked: str
    ) -> None:
        # The documented repair: ./scripts/setup.sh --use-blueprint=ID, for this agent.
        run = _run_agent_step(
            tmp_path,
            f"--use-blueprint={asked}",
            state={"BLUEPRINT_APP_ID": NEW_BP, "AGENT_ID": "kept"},
            env_lines=_old_env(blueprint=NEW_BP, upn=MINE_UPN),
        )

        assert run.reached_the_certificate_step(), run.output()
        assert run.helper_saw["UPN"] == "unset"  # so the helper reads this agent's from .env
        assert (run.root / ".env").read_text(encoding="utf-8").count(MINE_UPN) == 1
        # Another letter case is the same Blueprint, not a switch that wipes the state.
        assert (run.state_seen_by_helper or {}).get("AGENT_ID") == "kept"
        assert "Switching Blueprint" not in run.stdout

    def test_agent_user_upn_still_names_the_agent_user_on_a_switch(self, tmp_path: Path) -> None:
        run = _run_agent_step(
            tmp_path,
            f"--use-blueprint={NEW_BP}",
            f"--agent-user-upn={MINE_UPN}",
            state=OLD_STATE,
            env_lines=_old_env(),
        )

        assert run.reached_the_certificate_step(), run.output()
        assert run.helper_saw["UPN"] == f"set:{MINE_UPN}"

    def test_a_upn_suffix_still_selects_the_agent_user_on_a_switch(self, tmp_path: Path) -> None:
        run = _run_agent_step(
            tmp_path,
            f"--use-blueprint={NEW_BP}",
            "--with-upn-suffix=sati",
            state=OLD_STATE,
            env_lines=_old_env(),
        )

        assert run.reached_the_certificate_step(), run.output()
        assert run.helper_saw["SUFFIX"] == "set:sati"  # the helper ranks a suffix above a UPN

    def test_a_new_chain_is_unaffected(self, tmp_path: Path) -> None:
        run = _run_agent_step(
            tmp_path,
            "--new",
            "--with-upn-suffix=bob",
            state=OLD_STATE,
            env_lines=_old_env(),
            recorded_blueprint=NEW_BP,
        )

        assert run.reached_the_certificate_step(), run.output()
        assert run.helper_saw == {
            "UPN": "unset",
            "SUFFIX": "set:bob",
            "NEW_CHAIN": "set:1",
        }
        assert run.state_seen_by_helper == {
            "PROVISIONER_CLIENT_ID": OLD_STATE["PROVISIONER_CLIENT_ID"]
        }

    def test_the_comment_no_longer_describes_the_old_object_id_first_lookup(self) -> None:
        assert "over the APP_ID filter" not in _identity_slice()


@posix_shell_only
class TestStepFiveChecksTheBlueprintItGotBack:
    """The defence the Windows wizard has: the certificate step registers this machine's key on
    the Blueprint recorded in the state file (and Work IQ configures it), so that must be the one
    --use-blueprint asked for."""

    def test_another_blueprint_stops_setup_before_the_certificate_step(
        self, tmp_path: Path
    ) -> None:
        run = _run_agent_step(
            tmp_path,
            f"--use-blueprint={NEW_BP}",
            "--configure-a365-work-iq",
            state={"BLUEPRINT_APP_ID": NEW_BP},
            recorded_blueprint=OLD_BP,
        )

        assert run.returncode != 0, run.output()
        assert "REACHED THE CERTIFICATE STEP" not in run.stdout
        assert "WORK IQ CONFIGURED" not in run.stdout
        fail_lines = [line for line in run.stdout.splitlines() if line.startswith("FAIL: ")]
        assert len(fail_lines) == 1, run.output()
        report = run.stdout[run.stdout.index("ERR: ") :]
        assert NEW_BP in report and OLD_BP in report
        assert f"--use-blueprint={NEW_BP}" in report

    @pytest.mark.parametrize(
        ("asked", "recorded"),
        [(NEW_BP, NEW_BP), (NEW_BP.upper(), NEW_BP), (NEW_BP, NEW_BP.upper())],
        ids=["same", "asked-in-capitals", "recorded-in-capitals"],
    )
    def test_the_blueprint_asked_for_goes_on_in_any_letter_case(
        self, tmp_path: Path, asked: str, recorded: str
    ) -> None:
        run = _run_agent_step(
            tmp_path,
            f"--use-blueprint={asked}",
            "--configure-a365-work-iq",
            state={"BLUEPRINT_APP_ID": NEW_BP},
            recorded_blueprint=recorded,
        )

        assert run.reached_the_certificate_step(), run.output()
        assert "WORK IQ CONFIGURED" in run.stdout

    def test_a_new_chain_has_nothing_to_compare_with(self, tmp_path: Path) -> None:
        run = _run_agent_step(
            tmp_path, "--new", "--with-upn-suffix=bob", state=None, recorded_blueprint=NEW_BP
        )

        assert run.reached_the_certificate_step(), run.output()

    def test_a_failed_helper_still_stops_setup(self, tmp_path: Path) -> None:
        run = _run_agent_step(
            tmp_path, f"--use-blueprint={NEW_BP}", state={"BLUEPRINT_APP_ID": NEW_BP}, create_rc=1
        )

        assert run.returncode != 0
        assert "FAIL: Agent Identity provisioning failed" in run.stdout

    def test_the_blocks_under_test_are_hermetic(self) -> None:
        executed = (
            _identity_slice() + _agent_step_slice() + _function("read_state") + _function("same_id")
        )
        code = [line.strip() for line in executed.splitlines() if not line.lstrip().startswith("#")]
        assert [line for line in code if SIDE_EFFECT.search(line)] == []
        assert "/dev/null" not in _function("same_id")
