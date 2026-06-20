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
from typing import Optional

# The canonical source repo (used for doc links until the package is published on GitHub).
REPO_URL = "https://github.com/microsoft/entrabot"


def _repo_root() -> str:
    """Candidate cloned-repo root: this package lives at <repo>/src/entrabot/harness/."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def is_clone() -> bool:
    """True when running from a source checkout (the provisioning scripts are present)."""
    return scripts_dir() is not None


def scripts_dir() -> Optional[str]:
    """Directory holding the platform setup/provisioning scripts, or ``None`` if unavailable
    (e.g. installed from a wheel without a clone). A clear sentinel beats a path that 404s."""
    cand = os.path.join(_repo_root(), "scripts")
    if os.path.isfile(os.path.join(cand, "setup-windows.ps1")) or os.path.isfile(
        os.path.join(cand, "setup.sh")
    ):
        return cand
    return None


def doc_url(anchor: str = "") -> str:
    """Link to a doc — a local INSTALL.md when running from a clone, else the GitHub URL."""
    local = os.path.join(_repo_root(), "INSTALL.md")
    if os.path.isfile(local):
        return local + (f" § {anchor}" if anchor else "")
    suffix = f"/blob/main/INSTALL.md" + (f"#{anchor.lower().replace(' ', '-')}" if anchor else "")
    return REPO_URL + suffix
