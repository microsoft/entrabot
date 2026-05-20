from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def read_script(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8-sig")


def test_root_status_wrapper_runs_consolidated_status_script() -> None:
    script = read_script("status.sh")

    assert "scripts/show_agent_status.py" in script
    assert 'exec "$VENV_PYTHON" "$PROJECT_ROOT/scripts/show_agent_status.py" "$@"' in script


def test_root_status_wrapper_bootstraps_python_environment() -> None:
    script = read_script("status.sh")

    assert "python3.12" in script
    assert "sys.version_info >= (3, 12)" in script
    assert '"$PYTHON" -m venv "$PROJECT_ROOT/.venv"' in script
    assert '"$VENV_PYTHON" -m pip install -e ".[provisioning]"' in script


def test_setup_status_delegates_to_root_status_wrapper() -> None:
    script = read_script("scripts/setup.sh")

    assert "--status" in script
    assert "STATUS_ARGS=()" in script
    assert 'exec "$PROJECT_ROOT/status.sh" "${STATUS_ARGS[@]}"' in script
    assert "./scripts/setup.sh --status --json" in script


def test_windows_status_wrapper_runs_consolidated_status_script() -> None:
    script = read_script("status-windows.ps1")

    assert "show_agent_status.py" in script
    assert "$StatusScript" in script
    assert "--health-only" in script
    assert "--json" in script
    assert "--strict" in script
    assert "@ForwardArgs" in script


def test_windows_setup_status_delegates_to_status_wrapper() -> None:
    script = read_script("scripts/setup-windows.ps1")

    assert "[switch]$Status" in script
    assert "status-windows.ps1" in script
    assert "-Json:$Json -HealthOnly:$HealthOnly -Strict:$Strict" in script


def test_windows_deploy_status_delegates_to_status_wrapper() -> None:
    script = read_script("scripts/deploy-windows.ps1")

    assert "[switch]$Status" in script
    assert "status-windows.ps1" in script
    assert "-Json:$Json -HealthOnly:$HealthOnly -Strict:$Strict" in script
