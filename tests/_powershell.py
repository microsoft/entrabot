"""How the tests start a PowerShell child.

Windows PowerShell 5.1 started from a PowerShell 7 parent through an intermediate process
(this pytest process, when CI runs it from a ``pwsh`` step) inherits PowerShell 7's
``PSModulePath``. It then resolves its in-box modules (``Microsoft.PowerShell.Utility``, ...)
to PowerShell 7 versions it cannot load, and cmdlets such as ``Get-FileHash`` are "not
recognized". Microsoft's documented remedy (about_PSModulePath, "Starting Windows PowerShell
from PowerShell 7") is to remove ``PSModulePath`` from the child's environment, so Windows
PowerShell builds its own default at startup. PowerShell 7 children keep the inherited value:
PowerShell 7 puts its own paths first anyway.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import PureWindowsPath


def is_windows_powershell(shell: str) -> bool:
    """Is *shell* the in-box Windows PowerShell (``powershell.exe``), not PowerShell 7?"""
    return PureWindowsPath(shell).name.lower() in {"powershell.exe", "powershell"}


def shell_env(shell: str, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """*env* (default: this process's environment) as *shell* should be started with it:
    without ``PSModulePath``, in any spelling, when *shell* is Windows PowerShell."""
    base = dict(os.environ if env is None else env)
    if not is_windows_powershell(shell):
        return base
    return {key: value for key, value in base.items() if key.upper() != "PSMODULEPATH"}
