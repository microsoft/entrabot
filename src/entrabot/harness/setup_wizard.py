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
import json
import os
import platform
import re
import subprocess
import sys
from typing import Dict, List, Optional

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


# ---- Teams recipient resolution ----------------------------------------------------------
# Who the agent talks to (the ENTRABOT_HUMAN_* block). B2B guests (external/Microsoft-tenant users
# invited into this tenant) must be addressed by email + their HOME tenant GUID so federated chat
# reaches their real identity — the local guest object id silently never receives messages
# (hard-won learning #28). This is the Python port of the bash resolution in scripts/setup.sh, so
# `init` wires recipients uniformly on every platform (setup-windows.ps1 has no such flag).


class TeamsUserNotFound(Exception):
    """One or more recipient emails were not found in the signed-in tenant (invite as a guest
    first). Carries the unresolved addresses for the wizard to surface."""

    def __init__(self, emails: list[str]) -> None:
        self.emails = emails
        super().__init__("not found in tenant: " + ", ".join(emails))


_AZ_USER_QUERY = "{id:id, userType:userType, mail:mail, upn:userPrincipalName}"


def _run_az(args: list[str]) -> tuple[int, str]:
    """Run an ``az`` command, returning (returncode, stdout). 127 if az isn't on PATH."""
    az = (["cmd", "/c", "az"] if os.name == "nt" else ["az"])
    try:
        proc = subprocess.run(az + args, capture_output=True, text=True)
    except (FileNotFoundError, KeyboardInterrupt):
        return 127, ""
    return proc.returncode, proc.stdout


def _az_first(args: list[str]) -> dict[str, str | None] | None:
    """Run an az query expected to yield a single JSON object, or None on miss/empty/null."""
    rc, out = _run_az(args)
    out = out.strip()
    if rc != 0 or not out or out == "null":
        return None
    try:
        obj = json.loads(out)
    except json.JSONDecodeError:
        return None
    return obj or None


def _az_user_show(email: str, *, run=_az_first) -> dict[str, str | None] | None:
    """Look up a user in the signed-in tenant. Returns {id,userType,mail,upn} or None if absent.

    A B2B guest can't be found by ``--id <home-email>``: their UPN in this tenant is the mangled
    ``user_home.com#EXT#@thistenant.onmicrosoft.com`` and the home email lives only in ``mail`` /
    the UPN prefix. So we try, in order: direct (members / objectId / UPN), then a ``mail``
    filter, then the ``#EXT#`` UPN prefix the invite encodes."""
    direct = run(["ad", "user", "show", "--id", email, "--query", _AZ_USER_QUERY, "-o", "json"])
    if direct:
        return direct
    esc = email.replace("'", "''")  # OData string-literal escaping
    by_mail = run(["ad", "user", "list", "--filter", f"mail eq '{esc}'",
                   "--query", f"[0].{_AZ_USER_QUERY}", "-o", "json"])
    if by_mail:
        return by_mail
    prefix = email.replace("@", "_").replace("'", "''") + "#EXT#"
    return run(["ad", "user", "list", "--filter", f"startsWith(userPrincipalName, '{prefix}')",
                "--query", f"[0].{_AZ_USER_QUERY}", "-o", "json"])


def _home_tenant_guid(domain: str) -> str:
    """Resolve a verified domain to its Entra tenant GUID via OpenID discovery ('' on failure)."""
    import urllib.request

    url = f"https://login.microsoftonline.com/{domain}/.well-known/openid-configuration"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 (fixed MS endpoint)
            issuer = json.loads(resp.read().decode("utf-8")).get("issuer", "")
    except Exception:
        return ""
    parts = issuer.rstrip("/").split("/")
    return parts[-1] if len(parts) > 3 else ""


def _home_domain_from_guest_upn(upn: str) -> str:
    """Extract the home domain from a guest UPN, e.g.
    'jaly_microsoft.com#EXT#@sdnnm.onmicrosoft.com' → 'microsoft.com'."""
    if "#EXT#" not in upn:
        return ""
    local = upn.split("#EXT#")[0]  # jaly_microsoft.com
    bits = local.rsplit("_", 1)  # domain is after the LAST underscore
    return bits[1] if len(bits) > 1 else ""


def resolve_teams_user(
    emails: str,
    *,
    az_show=_az_user_show,
    tenant_lookup=_home_tenant_guid,
) -> dict[str, str]:
    """Resolve recipient email(s) (comma-separated for a group chat) into the ENTRABOT_HUMAN_*
    block. Guests (userType == 'Guest', or a '#EXT#' UPN when az reports userType: null) get their
    home tenant GUID resolved so federated chat addresses their real identity. Lists stay
    positionally aligned — a member's empty tenant slot is preserved, not dropped — so the
    runtime's position-sensitive ENTRABOT_HUMAN_USER_TENANT_IDS / _TYPES parse stays in sync.
    Raises TeamsUserNotFound if any address is missing from the tenant. '' input → {}."""
    ids: list[str] = []
    upns: list[str] = []
    tids: list[str] = []
    mails: list[str] = []
    types: list[str] = []
    unresolved: list[str] = []

    for raw in emails.split(","):
        email = raw.strip()
        if not email:
            continue
        info = az_show(email)
        if not info:
            unresolved.append(email)
            continue
        upn = info.get("upn") or email
        utype = (info.get("userType") or "").strip()
        if not utype:  # az returns null for some guests → fall back to the #EXT# pattern
            utype = "Guest" if "#EXT#" in upn else "Member"
        tid = ""
        if utype == "Guest":
            home_domain = _home_domain_from_guest_upn(upn)
            if home_domain:
                tid = tenant_lookup(home_domain)
        ids.append(info.get("id") or "")
        upns.append(email)  # the input address is the federated bind target (mirrors setup.sh)
        mails.append(info.get("mail") or "")
        types.append(utype)
        tids.append(tid)

    if unresolved:
        raise TeamsUserNotFound(unresolved)
    if not ids:
        return {}
    return {
        "ENTRABOT_HUMAN_USER_IDS": ",".join(ids),
        "ENTRABOT_HUMAN_UPNS": ",".join(upns),
        "ENTRABOT_HUMAN_USER_TENANT_IDS": ",".join(tids),
        "ENTRABOT_HUMAN_USER_MAILS": ",".join(mails),
        "ENTRABOT_HUMAN_USER_TYPES": ",".join(types),
        # backward-compat singulars track the primary (first) recipient
        "ENTRABOT_HUMAN_USER_ID": ids[0],
        "ENTRABOT_HUMAN_UPN": upns[0],
    }


def _apply_existing_env(root: str) -> None:
    """Load this dir's already-provisioned identity into the process for an idempotent re-run:
    the shared global (tenant/blueprint/cert) as the base, then this agent's .env overlaid, so
    the connection re-test and recipient edits operate on the real agent."""
    for k, v in globalcfg.read_global().items():
        os.environ[k] = v
    for k, v in globalcfg.read_env(globalcfg.agent_env_path(root)).items():
        os.environ[k] = v


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


def _venv_python() -> str:
    """The clone's venv python (has the provisioning deps: azure-identity, requests)."""
    if os.name == "nt":
        cand = os.path.join(_clone_root(), ".venv", "Scripts", "python.exe")
    else:
        cand = os.path.join(_clone_root(), ".venv", "bin", "python")
    return cand if os.path.isfile(cand) else sys.executable


def _run_add_agent(name: str, suffix: str) -> Optional[Dict[str, str]]:
    """Mint a distinct new agent (own Identity + User) under the existing Blueprint via
    add_agent.py. Returns the agent identity dict, or None on failure."""
    script = os.path.join(_scripts_dir(), "add_agent.py")
    env = dict(os.environ)
    env["_ENTRABOT_UPN_SUFFIX"] = suffix
    env["ENTRABOT_AGENT_DISPLAY_NAME"] = name
    env.pop("ENTRABOT_NEW_CHAIN", None)  # must reuse the Blueprint, not fork a new chain
    _say(ansi.dim(f"  $ python {os.path.basename(script)}  (suffix={suffix})"))
    try:
        proc = subprocess.run([_venv_python(), script], env=env, capture_output=True, text=True)
    except (FileNotFoundError, KeyboardInterrupt) as e:
        _say(ansi.red(f"  could not run add_agent.py: {e}"))
        return None
    if proc.stdout:
        sys.stdout.write(proc.stdout if proc.stdout.endswith("\n") else proc.stdout + "\n")
    ids: Optional[Dict[str, str]] = None
    for line in proc.stdout.splitlines():
        if line.startswith("AGENT_JSON="):
            try:
                ids = json.loads(line[len("AGENT_JSON="):])
            except json.JSONDecodeError:
                ids = None
    if proc.returncode != 0 or not ids or not ids.get("ENTRABOT_AGENT_USER_UPN"):
        if proc.stderr.strip():
            _say(ansi.red("  " + proc.stderr.strip().splitlines()[-1]))
        _say(ansi.red(f"  agent provisioning failed. See {LINKS['troubleshoot']}"))
        return None
    return ids


def _write_agent_env(root: str, name: str, ids: Dict[str, str]) -> None:
    """Write the per-agent .env (identity only; global supplies tenant/Blueprint) and apply it to
    the current process for the connection test."""
    agent_path = globalcfg.agent_env_path(root)
    globalcfg.write_env(
        agent_path, ids,
        header=f"ENTRABOT agent identity for '{name}'. Reuses the global Blueprint. Do not commit.",
    )
    _say(ansi.green(f"  ✓ wrote agent identity → {agent_path}"))
    for k, v in ids.items():
        os.environ[k] = v


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


def _setup_teams_user() -> None:
    """Ask who this agent should talk to on Teams and merge them into the ENTRABOT_HUMAN_* list.
    Additive (idempotent re-runs add, never wipe) and skippable. External/Microsoft-tenant users
    must already be invited as B2B guests here; the lookup then resolves their home tenant for
    federated chat. Manage the list later with `entrabot users` or `/users` in the harness."""
    from . import recipients

    existing = recipients.load_global()
    if existing:
        _say(ansi.dim("  current recipients: "
                      + ", ".join(f"{r.upn} ({r.user_type})" for r in existing)))
    _say("  Who should this agent talk to on Teams? External users (e.g. a Microsoft-tenant")
    _say("  address) must already be invited as a B2B guest here; they're auto-detected and wired")
    _say("  up for federated chat. Comma-separate addresses for a group chat. Blank to skip.")
    while True:
        emails = _ask("Teams recipient email(s) to add", default="")
        if not emails.strip():
            _say(ansi.dim("  skipped — add recipients later with `entrabot users add <email>`."))
            return
        try:
            resolved = recipients.parse(resolve_teams_user(emails))
        except TeamsUserNotFound as e:
            _say(ansi.red(f"  not found in this tenant: {', '.join(e.emails)}"))
            _say(ansi.yellow("  Invite them as a guest first (Entra → External Identities), then "
                             "retry."))
            if _yes("Try a different address?", default=True):
                continue
            return
        except Exception as e:  # az missing, network, etc. — non-fatal, recipient is optional
            _say(ansi.red(f"  couldn't resolve recipient: {type(e).__name__}: {e}"))
            _say(ansi.yellow(f"    {LINKS['troubleshoot']}"))
            return
        if not resolved:
            return
        recipients.save_global(recipients.upsert(existing, resolved))
        for r in resolved:
            tail = "  (Guest → federated chat via home tenant, learning #28)" if r.is_guest else ""
            _say(ansi.green(f"  ✓ recipient: {r.upn} ({r.user_type}){tail}"))
        return


def _scaffold_config(root: str, name: str) -> None:
    if cfgmod.exists(root):
        _say(ansi.dim(f"  harness config already present at {cfgmod.config_path(root)}"))
        return
    cfg = HarnessConfig(name=name, description=f"{name}, an ENTRABOT agent reachable on Microsoft Teams.")
    scaffold.bootstrap(root, cfg)
    _say(ansi.green(f"  ✓ wrote {cfgmod.config_path(root)}"))


def _existing_name(root: str) -> str:
    """The agent's name from its harness config, if one was scaffolded ('' if none)."""
    try:
        cfg = cfgmod.try_load(root)
        return cfg.name if cfg else ""
    except Exception:
        return ""


def _provision_identity(plat: str, root: str, name: str, step) -> bool:
    """First-time setup (tenant + Blueprint + cert + Agent) or, when the global config already
    exists, a new Agent User under the existing Blueprint. Writes this dir's per-agent .env and
    applies it to the process. ``step`` numbers the progress. Returns True on success."""
    reuse = globalcfg.global_exists()
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
    step(f"Provisioning agent '{name}'")
    if reuse:
        # Mint a DISTINCT new agent (own Agent Identity + User) under the existing Blueprint —
        # add_agent.py, run in the existing venv. No venv rebuild, no touching the repo .env.
        ids = _run_add_agent(name, suffix)
        if not ids:
            return False
        _write_agent_env(root, name, ids)
    else:
        if not _run_setup(plat, suffix, reuse):
            return False
        if not _persist_split(root, name):
            return False
    return True


def run_init(root: str) -> bool:
    """Run the walkthrough for an agent rooted at ``root``. Returns True if set up + verified."""
    plat = _platform()
    print(ansi.bold(ansi.cyan("\nENTRABOT setup")) + ansi.dim(f"  ({plat})"))

    # Directory (always asked). An already-provisioned dir resumes instead of re-minting.
    root = _choose_directory(root)
    resume = globalcfg.agent_exists(root)

    if resume:
        # Idempotent re-run: this dir already has an agent. Skip provisioning; load its identity
        # and continue with the remaining (and re-runnable) setup — recipients + connection test.
        existing = globalcfg.read_env(globalcfg.agent_env_path(root))
        name = _existing_name(root) or _derive_suffix(os.path.basename(root.rstrip("/\\")))
        _say(ansi.green(f"\n  Found an existing agent here: {existing['ENTRABOT_AGENT_USER_UPN']}"))
        _say(ansi.dim("  Re-running to continue setup — identity already provisioned, skipping it."))
        _apply_existing_env(root)
        step = _Stepper(total=3)
    else:
        default_name = os.path.basename(root.rstrip("/\\")) or "entrabot"
        name = _ask("Name this agent (its Teams display name)", default=default_name)
        step = _Stepper(total=4 if globalcfg.global_exists() else 7)
        if not _provision_identity(plat, root, name, step):
            return False

    step("Teams recipient")
    _setup_teams_user()

    step("Connection test")
    verified = _connection_test(os.environ.get("ENTRABOT_AGENT_USER_UPN", name))
    if not verified:
        _say(ansi.yellow("  A new agent's Teams/mailbox can take 10-15 min to provision. The"))
        _say(ansi.yellow("  identity is created and saved — re-check later with `entrabot doctor`."))

    step("Harness config")
    _scaffold_config(root, name)

    print()
    if verified:
        _say(ansi.green(ansi.bold(f"✓ ENTRABOT agent '{name}' is set up and verified.")))
    else:
        _say(ansi.green(ansi.bold(f"✓ ENTRABOT agent '{name}' is set up (token not live yet).")))
    _say(ansi.dim(f"  Launch it with:  cd {root} && entrabot"))
    return True
