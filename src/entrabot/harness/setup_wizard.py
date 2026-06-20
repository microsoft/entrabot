"""`entrabot init` — an interactive, cross-platform setup walkthrough.

Sets up an agent **in a chosen directory**. The identity chain has a shared root and a per-agent
leaf, so the wizard does only as much as needed:

* **First time** (no global config yet): confirm a tenant → ``az login`` → prereqs → provision a
  *new chain* (Blueprint + cert + Agent) → split the result into a shared ``~/.entrabot/global.env``
  (tenant + blueprint) and this directory's per-agent ``.env``.
* **Adding an agent** (global config exists): skip tenant/az/prereqs entirely and provision just a
  new Agent User **under the existing Blueprint** (reusing tenant + cert), writing only this
  directory's per-agent ``.env``.

So a second agent that "goes by a different name" is seconds, not the full walkthrough. Provisioning
runs the platform scripts under ``scripts/`` (a clone / unpacked sdist); a lean wheel degrades to
"clone to provision" with links, while the runtime stays repo-independent.
"""

from __future__ import annotations

import getpass
import os
import platform
import re
import subprocess
import sys
from typing import List, Optional

from . import ansi
from . import config as cfgmod
from . import globalcfg
from . import resources
from . import scaffold
from .config import HarnessConfig

# Doc links surfaced when a step needs manual setup.
LINKS = {
    "tenant": "Microsoft 365 Developer Program — https://aka.ms/m365devprogram (free test tenant)",
    "install": f"Full setup instructions: {resources.doc_url()}",
    "az": "Install Azure CLI: https://aka.ms/installazure",
    "troubleshoot": f"Troubleshooting: {resources.doc_url('Troubleshooting')}",
    "clone": f"Clone the repo to provision: git clone {resources.REPO_URL}",
}


def repo_root() -> str:
    """The cloned repo root (this package lives at <repo>/src/entrabot/harness/)."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _scripts_dir() -> Optional[str]:
    return resources.scripts_dir()


def _clone_root() -> str:
    """Where the setup scripts write their combined ``.env`` (the scripts' parent dir)."""
    sd = _scripts_dir()
    return os.path.dirname(sd) if sd else repo_root()


def _say(msg: str) -> None:
    print(msg)


class _Stepper:
    """Numbers steps as we go, since the reuse path has fewer of them."""

    def __init__(self, total: int) -> None:
        self.n = 0
        self.total = total

    def __call__(self, title: str) -> None:
        self.n += 1
        print()
        print(ansi.cyan(ansi.bold(f"═══ Step {self.n}/{self.total} — {title}")))


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


def _provisioning_available() -> bool:
    """The setup scripts only ship in a clone, not in a wheel install."""
    return _scripts_dir() is not None


def _platform() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _derive_suffix(name: str) -> str:
    """A UPN-safe suffix from the agent name (lowercase alnum, e.g. 'Sales Bot' → 'salesbot')."""
    s = re.sub(r"[^a-z0-9]", "", name.lower())
    return s[:20] or "agent"


# ---- steps -------------------------------------------------------------------------------
def _choose_directory(default_root: str) -> str:
    _say("  An agent's config (its identity + name) lives in a .entrabot/ folder in a directory.")
    if _yes(f"Set up this agent in {default_root}?", default=True):
        return default_root
    p = _ask("Directory for this agent", default=default_root)
    return os.path.abspath(os.path.expanduser(p))


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


def _run_setup(plat: str, suffix: str, reuse: bool) -> bool:
    if reuse:
        appid = globalcfg.blueprint_app_id()
        _say(f"  provisioning a new Agent User under the existing Blueprint ({appid}).")
        if plat == "windows":
            cmd = _ps("setup-windows.ps1", "-UseBlueprint", appid, "-UpnSuffix", suffix)
        else:
            cmd = _sh("setup.sh", f"--use-blueprint={appid}", f"--with-upn-suffix={suffix}")
    else:
        _say("  provisioning a new chain: Blueprint, certificate, Agent Identity + User, grants, license.")
        if plat == "windows":
            cmd = _ps("setup-windows.ps1", "-NewChain", "-UpnSuffix", suffix)
        else:
            cmd = _sh("setup.sh", "--new", f"--with-upn-suffix={suffix}")
    rc = _run(cmd)
    if rc != 0:
        _say(ansi.red(f"  setup failed (exit {rc}). See {LINKS['troubleshoot']}"))
    return rc == 0


def _persist_split(root: str, name: str) -> bool:
    """Read the combined ``.env`` the setup script just wrote, split it into the shared global
    config (written once) and this directory's per-agent ``.env``, and apply both to the current
    process for the connection test. Returns True if an agent identity was captured."""
    generated = globalcfg.read_env(os.path.join(_clone_root(), ".env"))
    glob, agent = globalcfg.split(generated)
    if not agent.get("ENTRABOT_AGENT_USER_UPN"):
        _say(ansi.red("  couldn't read the provisioned agent identity from the generated .env."))
        return False

    if not globalcfg.global_exists() and glob:
        globalcfg.write_env(
            globalcfg.global_env_path(),
            glob,
            header="ENTRABOT global config — shared tenant + Blueprint (provision once).\n"
            "All agents on this device reuse these. Do not commit.",
        )
        _say(ansi.green(f"  ✓ wrote shared global config → {globalcfg.global_env_path()}"))
    else:
        _say(ansi.dim(f"  reusing existing global config at {globalcfg.global_env_path()}"))

    agent_path = globalcfg.agent_env_path(root)
    globalcfg.write_env(
        agent_path,
        agent,
        header=f"ENTRABOT agent identity for '{name}'. Reuses the global Blueprint. Do not commit.",
    )
    _say(ansi.green(f"  ✓ wrote agent identity → {agent_path}"))

    # apply to the current process so the connection test sees the new agent
    for k, v in {**glob, **agent}.items():
        os.environ[k] = v
    return True


def _connection_test(upn: str) -> bool:
    _say(f"  acquiring an Agent-User token for {upn} via the three-hop flow…")
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


def _scaffold_config(root: str, name: str) -> None:
    if cfgmod.exists(root):
        _say(ansi.dim(f"  harness config already present at {cfgmod.config_path(root)}"))
        return
    cfg = HarnessConfig(name=name, description=f"{name}, an ENTRABOT agent reachable on Microsoft Teams.")
    scaffold.bootstrap(root, cfg)
    _say(ansi.green(f"  ✓ wrote {cfgmod.config_path(root)}"))


def run_init(root: str) -> bool:
    """Run the walkthrough for an agent rooted at ``root``. Returns True if set up + verified."""
    plat = _platform()
    print(ansi.bold(ansi.cyan("\nENTRABOT setup")) + ansi.dim(f"  ({plat})"))

    # Directory + name (always asked).
    root = _choose_directory(root)
    default_name = os.path.basename(root.rstrip("/\\")) or "entrabot"
    name = _ask("Name this agent (its Teams display name)", default=default_name)

    reuse = globalcfg.global_exists()
    step = _Stepper(total=3 if reuse else 6)

    if reuse:
        g = globalcfg.read_global()
        _say(ansi.green(
            f"\n  Found global config: tenant {g.get('ENTRABOT_TENANT_ID')} · "
            f"Blueprint {g.get('ENTRABOT_BLUEPRINT_APP_ID')}"))
        _say(ansi.dim("  Reusing it — skipping tenant, sign-in, and prerequisites."))
    else:
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
        if not _run_prereqs(plat):
            return False

    if not _provisioning_available():
        # Wheel install (no scripts). The runtime is repo-independent; provisioning is one-time
        # from a clone.
        _say(ansi.yellow("\n  Provisioning scripts aren't bundled in this install."))
        _say("  Provisioning (Entra identity, cert, .env) is a one-time step run from a clone:")
        _say(ansi.bold(f"    {LINKS['clone']}"))
        _say("    cd entrabot && python -m entrabot.harness init")
        _say(ansi.dim(f"  Once provisioned, the agent config lands under {os.path.join(root, '.entrabot')}."))
        _say(f"  {LINKS['install']}")
        return False

    suffix = _derive_suffix(name)
    if reuse:
        # The underlying provisioning scripts converge to ONE Agent User per device:
        # create_agent_user() reuses the first user under the host's Agent Identity, so they
        # can't yet mint a distinct second Teams identity. Don't run the full setup-windows.ps1
        # here — it would rebuild the venv (locking the running entrabot.exe) and rewrite the
        # repo .env. Stop honestly until a focused "add Agent User under existing Blueprint"
        # provisioning step exists.
        _say(ansi.yellow(f"\n  Adding a distinct second agent ('{name}') isn't wired up yet."))
        _say("  Reusing the Blueprint to mint a NEW Agent User needs a dedicated provisioning")
        _say("  step — the current scripts converge to a single Agent User per device, so they'd")
        _say(ansi.dim("  just re-point at the existing agent. The global-config split is in place;"))
        _say(ansi.dim(f"  the provisioning piece for '{name}' ({suffix}@…) is the remaining work."))
        return False

    step(f"Provisioning agent '{name}'")
    if not _run_setup(plat, suffix, reuse):
        return False
    if not _persist_split(root, name):
        return False

    step("Connection test")
    if not _connection_test(os.environ.get("ENTRABOT_AGENT_USER_UPN", name)):
        return False

    step("Harness config")
    _scaffold_config(root, name)

    print()
    _say(ansi.green(ansi.bold(f"✓ ENTRABOT agent '{name}' is set up and verified.")))
    _say(ansi.dim(f"  Launch it with:  cd {root} && entrabot"))
    return True
