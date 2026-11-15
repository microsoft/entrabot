"""status.sh and scripts/setup_delegated.sh read .env as data, never as shell code.

Both used to source it (``set -a; . .env`` and ``source .env``), so a value holding
``$(...)``, backticks or ``;`` ran as a command. Other tools write .env too.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("bash"), reason="POSIX shell scripts"
)

HOSTILE = (
    "ENTRABOT_CLIENT_ID=client-id\n"
    'ENTRABOT_TENANT_ID=tenant-id $(touch "$PWNED_DIR/dollar")\n'
    'ENTRABOT_EVIL=`touch "$PWNED_DIR/backtick"`\n'
    'touch "$PWNED_DIR/bare"\n'
    'ENTRABOT_SEMI=x; touch "$PWNED_DIR/semicolon"\n'
)


def _stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _run(argv: list[str], cwd: Path, pwned: Path, path_prefix: Path) -> subprocess.CompletedProcess:
    env = {
        "PATH": f"{path_prefix}:{os.defpath}",
        "HOME": str(cwd),
        "PWNED_DIR": str(pwned),
    }
    return subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL
    )


def test_status_sh_runs_nothing_from_env(tmp_path: Path) -> None:
    project = tmp_path / "project"
    pwned = tmp_path / "pwned"
    pwned.mkdir()
    project.mkdir()
    shutil.copy(REPO / "status.sh", project / "status.sh")
    (project / ".env").write_text(HOSTILE, encoding="utf-8")
    # The venv interpreter: answers the dependency probe and records the status call.
    _stub(project / ".venv" / "bin" / "python3", 'cat >/dev/null 2>&1 || true\necho "RAN $*"\n')
    result = _run(["bash", str(project / "status.sh")], project, pwned, tmp_path / "nobin")
    assert result.returncode == 0, result.stderr
    assert "show_agent_status.py" in result.stdout
    assert list(pwned.iterdir()) == []


def test_setup_delegated_reads_only_its_two_values_as_text(tmp_path: Path) -> None:
    project = tmp_path / "project"
    pwned = tmp_path / "pwned"
    pwned.mkdir()
    (project / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "setup_delegated.sh", project / "scripts" / "setup_delegated.sh")
    (project / ".env").write_text(HOSTILE, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    # python3 on PATH: prints what it was handed instead of signing in.
    _stub(
        bin_dir / "python3",
        'cat >/dev/null\necho "CLIENT=$ENTRABOT_CLIENT_ID"\necho "TENANT=$ENTRABOT_TENANT_ID"\n'
        'echo "EVIL=${ENTRABOT_EVIL:-unset}"\n',
    )
    result = _run(
        ["bash", str(project / "scripts" / "setup_delegated.sh")], project, pwned, bin_dir
    )
    assert result.returncode == 0, result.stderr
    assert "CLIENT=client-id" in result.stdout
    assert 'TENANT=tenant-id $(touch "$PWNED_DIR/dollar")' in result.stdout  # text, not run
    assert "EVIL=unset" in result.stdout  # nothing else is exported
    assert list(pwned.iterdir()) == []


@pytest.mark.parametrize("script", ["status.sh", "scripts/setup_delegated.sh"])
def test_neither_script_sources_env(script: str) -> None:
    text = (REPO / script).read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for form in ("source .env", '. "$PROJECT_ROOT/.env"', ". .env", "set -a"):
        assert form not in code
