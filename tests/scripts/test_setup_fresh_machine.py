"""``setup.sh`` on a machine that has never run it.

From a real run on a fresh Ubuntu 24.04 machine. With no ``.venv`` yet, setup started the
provisioning scripts with the system Python. They import this project, which was not
installed anywhere, so step 4 died with ``ModuleNotFoundError: No module named 'entrabot'``.
The step before it had tried ``pip install`` into the system Python, which Ubuntu refuses
(PEP 668), hidden the refusal behind ``| tail -1 || true``, and reported success.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _script() -> str:
    return (REPO_ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8")


def _step_three() -> str:
    script = _script()
    start = script.index("\nstep 3 ")
    return script[start : script.index("\n# ── Validate identity mode", start)]


def test_the_environment_exists_before_any_provisioning_script_runs() -> None:
    script = _script()
    created = script.index('-m venv "$PROJECT_ROOT/.venv"')
    installed = script.index('-m pip install --quiet -e "$PROJECT_ROOT[provisioning]"')
    first_script = script.index('"$SCRIPT_PYTHON" "$SCRIPT_DIR/entra_provisioning.py"')
    assert created < installed < first_script


def test_provisioning_scripts_never_run_with_the_system_python() -> None:
    script = _script()
    assert 'SCRIPT_PYTHON="$PYTHON"' not in script
    assert 'SCRIPT_PYTHON="$PROJECT_ROOT/.venv/bin/python3"' in script


def test_a_failed_install_is_not_hidden_or_reported_as_success() -> None:
    step = _step_three()
    for line in step.splitlines():
        if "pip install" in line:
            assert "|| true" not in line, line
            assert "| tail" not in line, line
            assert "/dev/null" not in line, line


def test_a_missing_venv_module_says_what_to_install() -> None:
    step = _step_three()
    assert "python3-venv" in step  # Debian and Ubuntu ship Python without it


def test_nothing_is_installed_into_the_system_python() -> None:
    step = _step_three()
    assert re.search(r'"\$PYTHON" -m pip', step) is None
