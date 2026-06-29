"""Identity provisioning: mint a new agent under the existing Blueprint (reuse path) or run
the full new-chain setup script, then write this directory's per-agent ``.env``."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from ..config import globalcfg
from ..ui import ansi
from .platform import (
    LINKS,
    _clone_root,
    _derive_suffix,
    _provisioning_available,
    _ps,
    _run,
    _say,
    _scripts_dir,
    _sh,
    _venv_python,
    _yes,
)
from .steps import _az_login, _run_prereqs


def _parse_agent_json(stdout: str) -> dict[str, str] | None:
    """The agent identity add_agent.py emits as an ``AGENT_JSON=`` line (last one wins)."""
    agent_ids: dict[str, str] | None = None
    for line in stdout.splitlines():
        if line.startswith("AGENT_JSON="):
            try:
                agent_ids = json.loads(line[len("AGENT_JSON="):])
            except json.JSONDecodeError:
                agent_ids = None
    return agent_ids


def _run_add_agent(name: str, suffix: str) -> dict[str, str] | None:
    """Mint a distinct new agent (own Identity + User) under the existing Blueprint via
    add_agent.py. Returns the agent identity dict, or None on failure."""
    script = os.path.join(_scripts_dir(), "add_agent.py")
    child_env = dict(os.environ)
    child_env["_ENTRABOT_UPN_SUFFIX"] = suffix
    child_env["ENTRABOT_AGENT_DISPLAY_NAME"] = name
    child_env.pop("ENTRABOT_NEW_CHAIN", None)  # must reuse the Blueprint, not fork a new chain
    _say(ansi.dim(f"  $ python {os.path.basename(script)}  (suffix={suffix})"))
    try:
        result = subprocess.run(
            [_venv_python(), script], env=child_env, capture_output=True, text=True
        )
    except (FileNotFoundError, KeyboardInterrupt) as error:
        _say(ansi.red(f"  could not run add_agent.py: {error}"))
        return None
    if result.stdout:
        sys.stdout.write(result.stdout if result.stdout.endswith("\n") else result.stdout + "\n")
    agent_ids = _parse_agent_json(result.stdout)
    if result.returncode != 0 or not agent_ids or not agent_ids.get("ENTRABOT_AGENT_USER_UPN"):
        if result.stderr.strip():
            _say(ansi.red("  " + result.stderr.strip().splitlines()[-1]))
        _say(ansi.red(f"  agent provisioning failed. See {LINKS['troubleshoot']}"))
        return None
    return agent_ids


def _write_agent_env(root: str, name: str, agent_ids: dict[str, str]) -> None:
    """Write the per-agent .env (identity only; global supplies tenant/Blueprint) and apply it to
    the current process for the connection test."""
    agent_path = globalcfg.agent_env_path(root)
    globalcfg.write_env(
        agent_path, agent_ids,
        header=f"ENTRABOT agent identity for '{name}'. Reuses the global Blueprint. Do not commit.",
    )
    _say(ansi.green(f"  ✓ wrote agent identity → {agent_path}"))
    for key, value in agent_ids.items():
        os.environ[key] = value


def _run_setup(platform_name: str, suffix: str, reuse: bool) -> bool:
    if reuse:
        blueprint_id = globalcfg.blueprint_app_id()
        _say(f"  provisioning a new Agent User under the existing Blueprint ({blueprint_id}).")
        if platform_name == "windows":
            cmd = _ps("setup-windows.ps1", "-UseBlueprint", blueprint_id, "-UpnSuffix", suffix)
        else:
            cmd = _sh("setup.sh", f"--use-blueprint={blueprint_id}", f"--with-upn-suffix={suffix}")
    else:
        _say("  provisioning a new chain: Blueprint, certificate, Agent Identity + User, grants, "
             "license.")
        if platform_name == "windows":
            cmd = _ps("setup-windows.ps1", "-NewChain", "-UpnSuffix", suffix)
        else:
            cmd = _sh("setup.sh", "--new", f"--with-upn-suffix={suffix}")
    exit_code = _run(cmd)
    if exit_code != 0:
        _say(ansi.red(f"  setup failed (exit {exit_code}). See {LINKS['troubleshoot']}"))
    return exit_code == 0


def _persist_split(root: str, name: str) -> bool:
    """Read the combined ``.env`` the setup script just wrote, split it into the shared global
    config (written once) and this directory's per-agent ``.env``, and apply both to the current
    process for the connection test. Returns True if an agent identity was captured."""
    generated = globalcfg.read_env(os.path.join(_clone_root(), ".env"))
    global_env, agent_env = globalcfg.split(generated)
    if not agent_env.get("ENTRABOT_AGENT_USER_UPN"):
        _say(ansi.red("  couldn't read the provisioned agent identity from the generated .env."))
        return False

    if not globalcfg.global_exists() and global_env:
        globalcfg.write_env(
            globalcfg.global_env_path(),
            global_env,
            header="ENTRABOT global config — shared tenant + Blueprint (provision once).\n"
            "All agents on this device reuse these. Do not commit.",
        )
        _say(ansi.green(f"  ✓ wrote shared global config → {globalcfg.global_env_path()}"))
    else:
        _say(ansi.dim(f"  reusing existing global config at {globalcfg.global_env_path()}"))

    agent_path = globalcfg.agent_env_path(root)
    globalcfg.write_env(
        agent_path,
        agent_env,
        header=f"ENTRABOT agent identity for '{name}'. Reuses the global Blueprint. Do not commit.",
    )
    _say(ansi.green(f"  ✓ wrote agent identity → {agent_path}"))

    # apply to the current process so the connection test sees the new agent
    for key, value in {**global_env, **agent_env}.items():
        os.environ[key] = value
    return True


def _prepare_new_chain(platform_name: str, step) -> bool:
    """First-time prep before provisioning a brand-new chain: tenant confirm → az login → prereqs."""
    _say(ansi.dim("\n  No global config yet — setting up the shared tenant + Blueprint first."))
    step("Tenant")
    _say("  You need an Entra tenant where you can create app registrations (a test tenant is ideal).")
    if not _yes("Do you have a tenant to use?", default=True):
        _say(ansi.yellow(f"  Get a free test tenant: {LINKS['tenant']}"))
        _say("  Re-run `entrabot init` once you have one.")
        return False
    step("Azure sign-in")
    if not _az_login():
        return False
    step("Prerequisites")
    return _run_prereqs(platform_name)


def _provision_identity(platform_name: str, root: str, name: str, step) -> bool:
    """First-time setup (tenant + Blueprint + cert + Agent) or, when the global config already
    exists, a new Agent User under the existing Blueprint. Writes this dir's per-agent .env and
    applies it to the process. ``step`` numbers the progress. Returns True on success."""
    reuse = globalcfg.global_exists()
    if reuse:
        global_env = globalcfg.read_global()
        _say(ansi.green(
            f"\n  Found global config: tenant {global_env.get('ENTRABOT_TENANT_ID')} · "
            f"Blueprint {global_env.get('ENTRABOT_BLUEPRINT_APP_ID')}"))
        _say(ansi.dim("  Reusing it — skipping tenant, sign-in, and prerequisites."))
    elif not _prepare_new_chain(platform_name, step):
        return False

    if not _provisioning_available():
        # Wheel install (no scripts). The runtime is repo-independent; provisioning is one-time
        # from a clone.
        _say(ansi.yellow("\n  Provisioning scripts aren't bundled in this install."))
        _say("  Provisioning (Entra identity, cert, .env) is a one-time step run from a clone:")
        _say(ansi.bold(f"    {LINKS['clone']}"))
        _say("    cd entrabot && python -m entrabot.harness init")
        _say(ansi.dim(
            f"  Once provisioned, the agent config lands under {os.path.join(root, '.entrabot')}."))
        _say(f"  {LINKS['install']}")
        return False

    suffix = _derive_suffix(name)
    step(f"Provisioning agent '{name}'")
    if reuse:
        # Mint a DISTINCT new agent (own Agent Identity + User) under the existing Blueprint —
        # add_agent.py, run in the existing venv. No venv rebuild, no touching the repo .env.
        agent_ids = _run_add_agent(name, suffix)
        if not agent_ids:
            return False
        _write_agent_env(root, name, agent_ids)
    else:
        if not _run_setup(platform_name, suffix, reuse):
            return False
        if not _persist_split(root, name):
            return False
    return True
