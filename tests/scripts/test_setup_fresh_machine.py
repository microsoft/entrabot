"""``setup.sh`` on a machine that has never run it.

From a real run on a fresh Ubuntu 24.04 machine, with the ``setup.sh`` from before the setup
wizard. With no ``.venv`` yet, setup started the provisioning scripts with the system Python.
They import this project, which was not installed anywhere, so step 4 died with
``ModuleNotFoundError: No module named 'entrabot'``. The step before it had tried
``pip install`` into the system Python, which Ubuntu refuses (PEP 668), hidden the refusal
behind ``| tail -1 || true``, and reported success.

``setup.sh`` is now the bootstrap that builds ``.venv`` and starts the wizard, and the same
rules hold for it. These read the script; ``test_setup_sh_bootstrap.py`` runs it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _script() -> str:
    return (REPO_ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8")


def _build_venv() -> str:
    script = _script()
    start = script.index("\nbuild_venv() {")
    return script[start : script.index("\n}\n", start)]


def test_the_environment_exists_before_the_wizard_or_any_provisioning_script_runs() -> None:
    script = _script()
    created = script.index('-m venv "$PROJECT_ROOT/.venv"')
    installed = script.index(
        '-m pip install --quiet --disable-pip-version-check -e ".[provisioning]"'
    )
    built = script.index('if [ "$COMMAND" = "setup" ] && ! venv_is_ready; then\n    build_venv')
    wizard = script.index('exec "$VENV_PY" -m entrabot.winsetup')
    assert created < installed < built < wizard


def test_provisioning_scripts_never_run_with_the_system_python() -> None:
    # The wizard starts every provisioning script with the interpreter it runs under
    # (Paths.python is sys.executable), and setup.sh starts the wizard with .venv's.
    script = _script()
    assert 'VENV_PY="$PROJECT_ROOT/.venv/bin/python"' in script
    assert "entra_provisioning.py" not in script and "create_entra_agent_ids.py" not in script
    cli = (REPO_ROOT / "src" / "entrabot" / "winsetup" / "cli.py").read_text(encoding="utf-8")
    assert "python=Path(sys.executable)" in cli


def test_a_failed_install_is_not_hidden_or_reported_as_success() -> None:
    step = _build_venv()
    installs = [line for line in step.splitlines() if "pip install" in line]
    assert len(installs) == 2
    for line in installs:
        assert "|| true" not in line, line
        assert "| tail" not in line, line
        assert "/dev/null" not in line, line
    # Each install is followed by a stop that says what to do, with pip's own message above it.
    assert step.count("|| die ") >= 2


def test_a_missing_venv_module_says_what_to_install() -> None:
    step = _build_venv()
    assert "python3-venv" in step  # Debian and Ubuntu ship Python without it


def test_nothing_is_installed_into_the_system_python() -> None:
    script = _script()
    assert re.search(r'"\$python" -m pip', script) is None
    assert re.search(r'"\$PYTHON" -m pip', script) is None
    assert script.count("-m pip install") == 2 and script.count('"$VENV_PY" -m pip install') == 2
