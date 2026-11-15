"""The version is stated in two places, and each release has a CHANGELOG section (RELEASING.md)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from entrabot.harness import __version__

ROOT = Path(__file__).resolve().parents[1]
# 0.2.0, or a release candidate in Python's form, 0.3.0rc1 (tagged v0.3.0-rc.1).
VERSION = re.compile(r"(?P<base>\d+\.\d+\.\d+)(?:rc\d+)?")


def project_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]


def test_the_version_is_x_y_z_or_a_release_candidate() -> None:
    assert VERSION.fullmatch(project_version())


def test_the_harness_states_the_same_version() -> None:
    assert __version__ == project_version()


def test_the_changelog_has_a_section_for_it() -> None:
    base = VERSION.fullmatch(project_version())["base"]
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## {re.escape(base)} ", changelog, re.MULTILINE)
