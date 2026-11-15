"""Locate setup assets (provisioning scripts, docs) whether ENTRABOT runs from a cloned repo
or an installed wheel.

The provisioning scripts under ``<repo>/scripts`` write a venv/``.env`` into their *project
root*, so they're only meaningful from a checkout. A wheel install therefore has no scripts —
``scripts_dir()`` returns ``None`` and the ``init`` wizard degrades to "clone for provisioning"
with links, while the *runtime* (reading config + creds from ``~/.entrabot``) stays fully
repo-independent.
"""

from __future__ import annotations

import os

# The canonical source repo (used for doc links until the package is published on GitHub).
REPO_URL = "https://github.com/microsoft/entrabot"


def _repo_root() -> str:
    """Candidate cloned-repo root: this module lives at <repo>/src/entrabot/harness/setup/."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))


def _has_setup_scripts(directory: str) -> bool:
    """True when ``directory`` contains a recognized platform setup script."""
    return os.path.isfile(os.path.join(directory, "setup-windows.ps1")) or os.path.isfile(
        os.path.join(directory, "setup.sh")
    )


def scripts_dir() -> str | None:
    """Directory holding the platform setup/provisioning scripts, or ``None`` if unavailable
    (e.g. installed from a wheel without a clone). A clear sentinel beats a path that 404s."""
    candidate_scripts_dir = os.path.join(_repo_root(), "scripts")
    if _has_setup_scripts(candidate_scripts_dir):
        return candidate_scripts_dir
    return None


def doc_url(anchor: str = "") -> str:
    """Link to a doc — a local INSTALL.md when running from a clone, else the GitHub URL."""
    local = os.path.join(_repo_root(), "INSTALL.md")
    if os.path.isfile(local):
        if anchor:
            return f"{local} § {anchor}"
        return local
    suffix = "/blob/main/INSTALL.md"
    if anchor:
        suffix += f"#{anchor.lower().replace(' ', '-')}"
    return REPO_URL + suffix
