"""`entrabot init` — an interactive, cross-platform setup walkthrough.

Orchestrates the existing platform scripts: confirm a tenant → ``az login`` → prereqs → setup
(provisioning) → connection test → scaffold the harness config. Gives doc links whenever a step
needs manual attention. Designed to be run from a cloned repo (the scripts live in ``scripts/``).
"""

from __future__ import annotations

import getpass
import os
import platform
import subprocess
import sys
from typing import List, Optional

from . import ansi
from . import config as cfgmod
from . import scaffold
from .config import HarnessConfig

# Doc links surfaced when a step needs manual setup.
LINKS = {
    "tenant": "Microsoft 365 Developer Program — https://aka.ms/m365devprogram (free test tenant)",
    "install": "Full setup instructions: INSTALL.md (in the repo root)",
    "az": "Install Azure CLI: https://aka.ms/installazure",
    "troubleshoot": "Troubleshooting: INSTALL.md § Troubleshooting",
}


def repo_root() -> str:
    """The cloned repo root (this package lives at <repo>/src/entrabot/harness/)."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _scripts_dir() -> str:
    return os.path.join(repo_root(), "scripts")


def _say(msg: str) -> None:
    print(msg)


def _step(n: int, total: int, title: str) -> None:
    print()
    print(ansi.cyan(ansi.bold(f"═══ Step {n}/{total} — {title}")))


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(ansi.bold(f"  {prompt}{suffix}: ")).strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return ans or default


def _yes(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    try:
        ans = input(ansi.bold(f"  {prompt} [{d}]: ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if not ans:
        return default
    return ans in ("y", "yes")


def _run(cmd: List[str], cwd: Optional[str] = None) -> int:
    """Run a command, streaming its output. Returns the exit code (or 127 if not found)."""
    _say(ansi.dim("  $ " + " ".join(cmd)))
    try:
        return subprocess.run(cmd, cwd=cwd).returncode
    except FileNotFoundError:
        _say(ansi.red(f"  command not found: {cmd[0]}"))
        return 127
    except KeyboardInterrupt:
        return 130


def _ps(script: str, *args: str) -> List[str]:
    return ["pwsh", "-NoProfile", "-File", os.path.join(_scripts_dir(), script), *args]


def _sh(script: str, *args: str) -> List[str]:
    return ["bash", os.path.join(_scripts_dir(), script), *args]


# ---- the steps ---------------------------------------------------------------------------
def _platform() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _az_login() -> bool:
    # already signed in?
    show = subprocess.run(
        (["cmd", "/c", "az"] if os.name == "nt" else ["az"]) + ["account", "show", "-o", "json"],
        capture_output=True,
        text=True,
    )
    if show.returncode == 0:
        import json

        try:
            acct = json.loads(show.stdout)
            who, tid = acct.get("user", {}).get("name"), acct.get("tenantId")
            _say(ansi.green(f"  already signed in as {who} (tenant {tid})"))
        except Exception:
            _say(ansi.green("  already signed in to az"))
        if _yes("Use this account/tenant?", default=True):
            return True
    _say("  launching `az login --allow-no-subscription` (a browser will open)…")
    rc = _run((["cmd", "/c", "az"] if os.name == "nt" else ["az"]) + ["login", "--allow-no-subscription"])
    if rc != 0:
        _say(ansi.red(f"  az login failed (exit {rc}). {LINKS['az']}"))
        return False
    return True


def _run_prereqs(plat: str) -> bool:
    _say("  installs Python 3.12+, Azure CLI, Git, and build tools as needed.")
    if plat == "windows":
        rc = _run(_ps("prereqs-windows.ps1"))
        if rc == 0:
            _say(ansi.yellow("  ↻ if anything was installed, close & reopen the terminal, then re-run `entrabot init`."))
        return rc == 0
    if plat == "macos":
        return _run(_sh("prereqs-macos.sh")) == 0
    # linux — manual (distro-specific)
    _say(ansi.yellow("  Linux prerequisites are manual. Install: python3.12 + venv, git, curl, azure-cli."))
    _say("    Ubuntu/Debian: sudo apt install python3.12 python3.12-venv git curl; "
         "curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash")
    _say(f"    {LINKS['install']} § Linux")
    return _yes("Prerequisites installed?", default=True)


def _run_setup(plat: str, upn_suffix: str) -> bool:
    _say("  provisions the Entra Agent Identity, certificate, license, Graph grants, and .env.")
    if plat == "windows":
        rc = _run(_ps("setup-windows.ps1", "-NewChain", "-UpnSuffix", upn_suffix))
    else:
        rc = _run(_sh("setup.sh", "--new", f"--with-upn-suffix={upn_suffix}"))
    if rc != 0:
        _say(ansi.red(f"  setup failed (exit {rc}). See {LINKS['troubleshoot']}"))
    return rc == 0


def _connection_test() -> bool:
    _say("  acquiring an Agent-User token via the three-hop flow…")
    try:
        from entrabot.config import get_config
        from entrabot.tools.teams import acquire_agent_user_token

        token = acquire_agent_user_token(get_config())
        _say(ansi.green(f"  ✓ token acquired (len {len(token)}) — Teams auth works."))
        return True
    except Exception as e:
        _say(ansi.red(f"  ✗ connection test failed: {type(e).__name__}: {e}"))
        _say(ansi.yellow(f"    {LINKS['troubleshoot']}"))
        return False


def _scaffold_config(root: str) -> None:
    if cfgmod.exists(root):
        _say(ansi.dim(f"  config already present at {cfgmod.config_path(root)}"))
        return
    name = _ask("Name this ENTRABOT agent", default="entrabot")
    cfg = HarnessConfig(name=name, description=f"{name}, an ENTRABOT agent reachable on Microsoft Teams.")
    scaffold.bootstrap(root, cfg)
    _say(ansi.green(f"  ✓ wrote {cfgmod.config_path(root)}"))


def run_init(root: str) -> bool:
    """Run the full walkthrough. Returns True if everything is set up + verified."""
    plat = _platform()
    print(ansi.bold(ansi.cyan("\nENTRABOT setup")) + ansi.dim(f"  ({plat}; config → {os.path.join(root, '.entrabot')})"))

    _step(1, 6, "Tenant")
    _say("  You need an Entra tenant where you can create app registrations (a test tenant is ideal).")
    if not _yes("Do you have a tenant to use?", default=True):
        _say(ansi.yellow(f"  Get a free test tenant: {LINKS['tenant']}"))
        _say("  Re-run `entrabot init` once you have one.")
        return False

    _step(2, 6, "Azure sign-in")
    if not _az_login():
        return False

    _step(3, 6, "Prerequisites")
    if not _run_prereqs(plat):
        return False

    _step(4, 6, "Provisioning")
    upn = _ask("UPN suffix for the agent user", default=getpass.getuser())
    if not _run_setup(plat, upn):
        return False

    _step(5, 6, "Connection test")
    if not _connection_test():
        return False

    _step(6, 6, "Harness config")
    _scaffold_config(root)

    print()
    _say(ansi.green(ansi.bold("✓ ENTRABOT is set up and verified.")))
    return True
