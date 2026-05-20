"""Provisioner token acquisition policy for scripts.

Only setup/provisioning paths may create or repair the provisioner app. Other
scripts must use the named existing-only helper so read/status/action commands
fail fast with a clear error if the provisioner is missing.
"""

from __future__ import annotations

import ast
from pathlib import Path

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


def test_setup_embedded_token_path_is_explicitly_bootstrap_capable() -> None:
    script = (REPO_ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8")

    assert "from entra_provisioning import get_bootstrap_graph_token" in script
    assert "get_bootstrap_graph_token(wait_for_propagation=False)" in script
