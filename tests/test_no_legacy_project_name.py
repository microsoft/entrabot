"""The project is EntraBot. The pre-rename name must not return to the repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# Assembled at runtime so this guard does not trip over itself.
LEGACY_NAME = "entra" + "claw"


def _tracked_paths() -> list[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"not running from a git checkout: {exc}")
    return [p.decode() for p in result.stdout.split(b"\0") if p]


def test_no_tracked_path_uses_legacy_name() -> None:
    offenders = [p for p in _tracked_paths() if LEGACY_NAME in p.lower()]
    assert offenders == []


def test_no_tracked_file_contains_legacy_name() -> None:
    needle = LEGACY_NAME.encode()
    offenders = [
        path
        for path in _tracked_paths()
        if (REPO_ROOT / path).is_file() and needle in (REPO_ROOT / path).read_bytes().lower()
    ]
    assert offenders == []
