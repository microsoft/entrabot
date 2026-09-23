"""Leaf helpers for the setup wizard: platform detection, script paths, subprocess running,
and the prompt/echo primitives. No dependencies on the other setup modules."""

from __future__ import annotations

import os
import re
import subprocess
import sys

from ..ui import ansi
from . import resources

# Doc links surfaced when a step needs manual setup.
LINKS = {
    "tenant": "Microsoft 365 Developer Program — https://aka.ms/m365devprogram (free test tenant)",
    "install": f"Full setup instructions: {resources.doc_url()}",
    "az": "Install Azure CLI: https://aka.ms/installazure",
    "troubleshoot": f"Troubleshooting: {resources.doc_url('Troubleshooting')}",
    "clone": f"Clone the repo to provision: git clone {resources.REPO_URL}",
}


def repo_root() -> str:
    """The cloned repo root (this package lives at <repo>/src/entrabot/harness/setup/)."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))


def _scripts_dir() -> str | None:
    return resources.scripts_dir()


def _clone_root() -> str:
    """Where the setup scripts write their combined ``.env`` (the scripts' parent dir)."""
    scripts_dir = _scripts_dir()
    return os.path.dirname(scripts_dir) if scripts_dir else repo_root()


def _say(msg: str) -> None:
    print(msg)


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(ansi.bold(f"  {prompt}{suffix}: ")).strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return answer or default


def _yes(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(ansi.bold(f"  {prompt} [{hint}]: ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if not answer:
        return default
    return answer in ("y", "yes")


def _run(cmd: list[str], cwd: str | None = None) -> int:
    """Run a command, streaming its output. Returns the exit code (or 127 if not found)."""
    _say(ansi.dim("  $ " + " ".join(cmd)))
    try:
        return subprocess.run(cmd, cwd=cwd).returncode
    except FileNotFoundError:
        _say(ansi.red(f"  command not found: {cmd[0]}"))
        return 127
    except KeyboardInterrupt:
        return 130


def _ps(script: str, *args: str) -> list[str]:
    return ["pwsh", "-NoProfile", "-File", os.path.join(_scripts_dir(), script), *args]


def _sh(script: str, *args: str) -> list[str]:
    return ["bash", os.path.join(_scripts_dir(), script), *args]


def _provisioning_available() -> bool:
    """The setup scripts only ship in a clone, not in a wheel install."""
    return _scripts_dir() is not None


def _platform() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _venv_python() -> str:
    """The clone's venv python (has the provisioning deps: azure-identity, requests)."""
    if os.name == "nt":
        candidate = os.path.join(_clone_root(), ".venv", "Scripts", "python.exe")
    else:
        candidate = os.path.join(_clone_root(), ".venv", "bin", "python")
    return candidate if os.path.isfile(candidate) else sys.executable


def _derive_suffix(name: str) -> str:
    """A UPN-safe suffix from the agent name (lowercase alnum, e.g. 'Sales Bot' → 'salesbot')."""
    sanitized = re.sub(r"[^a-z0-9]", "", name.lower())
    return sanitized[:20] or "agent"
