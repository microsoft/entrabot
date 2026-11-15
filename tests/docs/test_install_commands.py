"""The install and upgrade commands name the latest release's tag (RELEASING.md, step 4).

People install Entrabot from a tagged release, not from main. Each release changes the tag in
these commands; this test fails while any of them names a version other than pyproject's.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL_PAGES = [
    ROOT / "README.md",
    ROOT / "docs" / "getting-started" / "quickstart.md",
    ROOT / "docs" / "getting-started" / "prerequisites.md",
    ROOT / "docs" / "getting-started" / "macos-linux.md",
    ROOT / "docs" / "getting-started" / "windows.md",
]
UPGRADE_PAGE = ROOT / "docs" / "troubleshooting" / "migrations-and-upgrades.md"
CLONE = re.compile(r"git clone\b[^\n`]*github\.com/microsoft/entrabot")
TAG = re.compile(r"--branch (v\d+\.\d+\.\d+)\b")


def _version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]


def _expected_tag() -> str | None:
    """The tag the commands must name, or None during a release candidate: a pre-release
    leaves the install commands on the latest release (RELEASING.md, "A pre-release")."""
    version = _version()
    return None if "rc" in version else f"v{version}"


@pytest.mark.parametrize("page", INSTALL_PAGES, ids=lambda p: str(p.relative_to(ROOT)))
def test_the_notes_name_the_same_release(page: Path) -> None:
    named = re.findall(r"latest release, (v\d+\.\d+\.\d+)\b", page.read_text(encoding="utf-8"))
    if _expected_tag():
        assert set(named) <= {_expected_tag()}


@pytest.mark.parametrize("page", INSTALL_PAGES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_clone_command_names_the_release_tag(page: Path) -> None:
    text = page.read_text(encoding="utf-8")
    clones = [m.group(0) for m in CLONE.finditer(text)]
    assert clones, f"{page.name} has no git clone command"
    for command in clones:
        tag = TAG.search(command)
        assert tag, f"{page.name}: clone without --branch vX.Y.Z: {command}"
        if _expected_tag():
            assert tag.group(1) == _expected_tag(), f"{page.name}: {command}"


def test_the_upgrade_steps_name_the_release_tag() -> None:
    text = UPGRADE_PAGE.read_text(encoding="utf-8")
    checkouts = re.findall(r"git checkout (v\d+\.\d+\.\d+)\b", text)
    assert checkouts, "the upgrade steps have no git checkout vX.Y.Z"
    if _expected_tag():
        assert set(checkouts) == {_expected_tag()}
    assert "git fetch --tags" in text
