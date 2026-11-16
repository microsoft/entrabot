"""A computer whose only Azure CLI is the private copy setup installs.

Setup installs one when the computer has none, with pip into a virtual environment of its
own: ``~/.entrabot/tools/azure-cli`` on macOS and Linux, ``%LOCALAPPDATA%\\entrabot\\tools\\
azure-cli`` on Windows. It never puts it on PATH, so a new terminal has no ``az`` at all. Found
live on Ubuntu 24.04 with v0.3.0: setup itself worked (it puts that folder first on its
children's PATH), and every script run without it stopped with
``FileNotFoundError: [Errno 2] No such file or directory: 'az'``.

These helpers build that computer for a test, on the system the test runs on, so the same
test means the same thing on the Windows CI job: a PATH with no ``az`` on it, a home folder
of the test's own, and (optionally) the private copy where setup puts it. Only the files
setup's lookup checks for are made; nothing in them runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


def computer_without_az_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """PATH holds one empty folder, and the home folder (and ``%LOCALAPPDATA%``) is the
    test's own. Returns the Entrabot data folder setup would use there."""
    empty = tmp_path / "empty-path"
    empty.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    local = home / "AppData" / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    # Setup also looks in the standard Windows install folder, which a CI runner has: point
    # it nowhere, so only what the test makes can be found.
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        monkeypatch.setenv(variable, str(tmp_path / "no-program-files"))
    return local / "entrabot" if sys.platform == "win32" else home / ".entrabot"


def install_private_copy(data_root: Path) -> Path:
    """The private copy, laid out as ``pip install azure-cli`` into a venv leaves it.
    Returns its interpreter, which is what setup starts (``python -m azure.cli``)."""
    venv = data_root / "tools" / "azure-cli"
    if sys.platform == "win32":
        folder, python, launcher = venv / "Scripts", "python.exe", "az.bat"
    else:
        folder, python, launcher = venv / "bin", "python", "az"
    folder.mkdir(parents=True)
    (folder / python).write_bytes(b"")
    (folder / launcher).write_bytes(b"")
    return folder / python
