"""Find the Azure CLI for a script run on its own.

Setup installs a private copy of the Azure CLI when the computer has none
(``~/.entrabot/tools/azure-cli``; ``%LOCALAPPDATA%\\entrabot\\tools\\azure-cli`` on Windows) and
never puts it on PATH. A script setup starts finds it, because setup puts its folder first on
the child's PATH. The same script run by hand, or by ``--diagnose``, gets the person's own
PATH and must look for it itself, the way setup does: ``entrabot.winsetup.azcli``.

The blob storage scripts start without the ``entrabot`` package too (with a plain
``python3``); there only the ``az`` on PATH can be found.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

# The exit code a shell gives a command it cannot find; scripts return it with
# :func:`missing_message` when there is no Azure CLI to start.
MISSING_EXIT = 127


def off_path() -> Any:
    """The Azure CLI that is not on PATH: the standard Windows install folder, then setup's
    private copy. An ``entrabot.winsetup.azcli.AzLocation``, or None."""
    try:
        from entrabot.winsetup import azcli
    except ImportError:  # started with a Python that is not the project's .venv
        return None
    return azcli.find_az_for_scripts(os.environ, which=lambda _name: None)


def command() -> list[str] | None:
    """How to start ``az``: the one on PATH, else the one setup uses. None when there is
    none."""
    found = shutil.which("az")
    if found:
        return [found]
    location = off_path()
    return list(location.command) if location is not None else None


def missing_message() -> str:
    """No Azure CLI at all, in a sentence, with what to do."""
    try:
        from entrabot.winsetup import azcli
    except ImportError:
        return (
            "Entrabot could not find the Azure CLI on this computer. Run this script with "
            "the project's own Python (.venv), or run setup, which installs the Azure CLI."
        )
    return azcli.not_found_message(os.environ)
