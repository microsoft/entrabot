"""Re-runnable setup steps: directory choice, Azure sign-in, prerequisites, the connection
test, harness-config scaffolding, and loading an already-provisioned identity."""

from __future__ import annotations

import json
import os
import subprocess

from .. import config as cfgmod
from ..config import HarnessConfig, globalcfg
from ..ui import ansi
from . import scaffold
from .platform import LINKS, _ask, _ps, _run, _say, _sh, _yes


def _choose_directory(default_root: str) -> str:
    _say("  An agent's config (its identity + name) lives in a .entrabot/ folder in a directory.")
    if _yes(f"Set up this agent in {default_root}?", default=True):
        return default_root
    directory = _ask("Directory for this agent", default=default_root)
    return os.path.abspath(os.path.expanduser(directory))


def _az_cmd(*args: str) -> list[str]:
    prefix = ["cmd", "/c", "az"] if os.name == "nt" else ["az"]
    return [*prefix, *args]


def _already_signed_in_az() -> bool:
    """If az already has a session, show it and ask whether to reuse it."""
    account_show = subprocess.run(
        _az_cmd("account", "show", "-o", "json"), capture_output=True, text=True
    )
    if account_show.returncode != 0:
        return False
    try:
        account = json.loads(account_show.stdout)
        user_name = account.get("user", {}).get("name")
        tenant_id = account.get("tenantId")
        _say(ansi.green(f"  already signed in as {user_name} (tenant {tenant_id})"))
    except Exception:
        _say(ansi.green("  already signed in to az"))
    return _yes("Use this account/tenant?", default=True)


def _az_login() -> bool:
    if _already_signed_in_az():
        return True
    _say("  launching `az login --allow-no-subscription` (a browser will open)…")
    exit_code = _run(_az_cmd("login", "--allow-no-subscription"))
    if exit_code != 0:
        _say(ansi.red(f"  az login failed (exit {exit_code}). {LINKS['az']}"))
        return False
    return True


def _run_prereqs(platform_name: str) -> bool:
    _say("  installs Python 3.12+, Azure CLI, Git, and build tools as needed.")
    if platform_name == "windows":
        exit_code = _run(_ps("prereqs-windows.ps1"))
        if exit_code == 0:
            _say(ansi.yellow(
                "  ↻ if anything was installed, close & reopen the terminal, then re-run "
                "`entrabot init`."))
        return exit_code == 0
    if platform_name == "macos":
        return _run(_sh("prereqs-macos.sh")) == 0
    # linux — manual (distro-specific)
    _say(ansi.yellow(
        "  Linux prerequisites are manual. Install: python3.12 + venv, git, curl, azure-cli."))
    _say("    Ubuntu/Debian: sudo apt install python3.12 python3.12-venv git curl; "
         "curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash")
    _say(f"    {LINKS['install']} § Linux")
    return _yes("Prerequisites installed?", default=True)


def _connection_test(upn: str) -> bool:
    _say(f"  acquiring an Agent-User token for {upn} via the three-hop flow…")
    try:
        from entrabot.config import get_config
        from entrabot.tools.teams import acquire_agent_user_token

        token = acquire_agent_user_token(get_config())
        _say(ansi.green(f"  ✓ token acquired (len {len(token)}) — Teams auth works."))
        return True
    except Exception as error:
        _say(ansi.red(f"  ✗ connection test failed: {type(error).__name__}: {error}"))
        _say(ansi.yellow(f"    {LINKS['troubleshoot']}"))
        return False


def _scaffold_config(root: str, name: str) -> None:
    if cfgmod.exists(root):
        _say(ansi.dim(f"  harness config already present at {cfgmod.config_path(root)}"))
        return
    config = HarnessConfig(
        name=name, description=f"{name}, an ENTRABOT agent reachable on Microsoft Teams."
    )
    scaffold.bootstrap(root, config)
    _say(ansi.green(f"  ✓ wrote {cfgmod.config_path(root)}"))


def _existing_name(root: str) -> str:
    """The agent's name from its harness config, if one was scaffolded ('' if none)."""
    try:
        config = cfgmod.try_load(root)
        return config.name if config else ""
    except Exception:
        return ""


def _apply_existing_env(root: str) -> None:
    """Load this dir's already-provisioned identity into the process for an idempotent re-run:
    the shared global (tenant/blueprint/cert) as the base, then this agent's .env overlaid, so
    the connection re-test and recipient edits operate on the real agent."""
    for key, value in globalcfg.read_global().items():
        os.environ[key] = value
    for key, value in globalcfg.read_env(globalcfg.agent_env_path(root)).items():
        os.environ[key] = value
