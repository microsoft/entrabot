"""The running Entrabot version, and whether its checkout is a release.

The version comes from the installed package's metadata, which is built from
``pyproject.toml``, so it cannot drift from it. A release is a tag ``vX.Y.Z`` (pre-releases
``vX.Y.Z-rc.N``; see ``RELEASING.md``); ``main`` is development and may be ahead of the
latest release. Reported by ``whoami``, ``--diagnose`` and the status report.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_RELEASE_TAGS = "v[0-9]*"


def installed_version() -> str:
    """The installed package's version, or "unknown" when it is not installed."""
    try:
        return _dist_version("entrabot")
    except PackageNotFoundError:
        return "unknown"


def _tag_as_version(tag: str) -> str:
    """``v0.3.0-rc.1`` → ``0.3.0rc1``, the form Python packaging uses."""
    return tag.removeprefix("v").replace("-rc.", "rc")


def _git(
    args: list[str], root: Path, run: Callable[..., subprocess.CompletedProcess]
) -> str | None:
    try:
        result = run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def release_status(
    *,
    version: str | None = None,
    project_root: Path | None = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> tuple[str, str]:
    """``(status, detail)``: "pass" on the release tag for this version, "warn" for code that is
    not a release (development, or an install not refreshed after a checkout), "skip" when it
    can't be told (not a git checkout, or no git). Never "fail": running development code is
    allowed."""
    current = version or installed_version()
    root = project_root or PROJECT_ROOT
    if _git(["rev-parse", "--is-inside-work-tree"], root, run) != "true":
        return "skip", f"Entrabot {current}; not a git checkout, so it can't tell which release"
    tag = _git(["describe", "--tags", "--exact-match", "--match", _RELEASE_TAGS, "HEAD"], root, run)
    if tag:
        if _tag_as_version(tag) == current:
            return "pass", f"Entrabot {current}, release {tag}"
        return (
            "warn",
            f"the checkout is at release {tag}, but the installed package says {current}: "
            "run setup again so it reinstalls the package",
        )
    near = _git(["describe", "--tags", "--match", _RELEASE_TAGS, "HEAD"], root, run)
    if near and near.count("-") >= 2:
        base, ahead, _sha = near.rsplit("-", 2)
        where = f"{ahead} commit{'s' if ahead != '1' else ''} ahead of {base}"
    else:
        where = "not on a release tag"
    return (
        "warn",
        f"Entrabot {current}, {where}: development code, which may be ahead of the latest release",
    )
