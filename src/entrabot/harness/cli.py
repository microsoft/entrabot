"""CLI entry point for ENTRABOT.

  entrabot                 start the harness (config: ./.entrabot if present, else ~/.entrabot)
  entrabot <path>          start the harness with config under <path>/.entrabot
  entrabot init [path]     guided setup for an agent in this directory (asks first). Reuses the
                           shared tenant/Blueprint if already set up; only mints a new agent.
                           Idempotent — re-run to continue setup of an existing agent.
  entrabot users [...]     manage the federated Teams recipient list:
                             entrabot users                 list recipients (Type + Role)
                             entrabot users add EMAIL...    resolve + add (B2B guests federated)
                             entrabot users remove EMAIL    remove a recipient
                             entrabot users sponsor EMAIL   elevate to the sponsor Role
                             entrabot users guest EMAIL     demote to the guest Role
  entrabot migrate [.env]  lift an existing combined .env into ~/.entrabot/global.env + default agent
  entrabot doctor          check the Copilot runtime + auth + Teams token
  entrabot --version | --help

Flags: --yolo/-y (allow all tools), --new/--fresh (don't resume), --interactive (not autopilot),
--force (migrate: overwrite existing global config).

Config layers: ~/.entrabot/global.env (shared tenant + Blueprint) + <dir>/.entrabot/.env (per-agent
identity). Bare `entrabot` uses ./.entrabot if present, else ~/.entrabot. ENTRABOT_CONSOLE=1 forces
the plain UI.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import List, Optional

from . import __version__
from . import config as cfgmod
from .auth import make_token_provider
from .config import HarnessConfig
from .session import InteractiveSession
from .ui import UI
from .ui.console import ConsoleUI

_USAGE = __doc__


def _resolve_root(path_arg: Optional[str]) -> str:
    """Where the harness config (.entrabot/) lives: an explicit path, else ./.entrabot if it
    exists, else the home directory."""
    if path_arg:
        return os.path.abspath(path_arg)
    if os.path.isdir(os.path.join(os.getcwd(), cfgmod.CONFIG_DIR)):
        return os.getcwd()
    return os.path.expanduser("~")


def _pick_ui() -> UI:
    if os.environ.get("ENTRABOT_CONSOLE") == "1" or not sys.stdout.isatty():
        return ConsoleUI()
    try:
        from .ui.tui import TextualUI, available

        if available():
            return TextualUI()
    except Exception:
        pass
    return ConsoleUI()


def _flags(args: List[str]):
    flags = {a.lstrip("-").lower() for a in args if a.startswith("-")}
    positionals = [a for a in args if not a.startswith("-")]
    return flags, positionals


def _confirm(prompt: str, default: bool = True) -> bool:
    if not sys.stdin.isatty():
        return default
    d = "Y/n" if default else "y/N"
    try:
        ans = input(f"  {prompt} [{d}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return default if not ans else ans in ("y", "yes")


def _cmd_init(positionals: List[str], flags: set) -> int:
    from .setup_wizard import run_init

    # init works in the current directory by default (the wizard confirms / lets you change it).
    default_root = os.path.abspath(positionals[0]) if positionals else os.getcwd()
    ok = run_init(default_root)
    if not ok:
        return 1
    if _confirm("Launch the harness now?", default=True):
        try:
            return asyncio.run(_cmd_run(flags, default_root))
        except KeyboardInterrupt:
            return 0
    print(f"\nLater, run `entrabot` from that directory to launch it.")
    return 0


def _cmd_migrate(positionals: List[str], flags: set) -> int:
    """Lift an existing combined .env (the original repo flow) into the layered layout:
    ~/.entrabot/global.env (shared tenant + Blueprint) + ~/.entrabot/.env (the existing agent
    as the home default)."""
    from . import globalcfg, setup_wizard

    src = positionals[0] if positionals else os.path.join(setup_wizard._clone_root(), ".env")
    env = globalcfg.read_env(src)
    if not env:
        print(f"No .env found at {src}. Pass the path: entrabot migrate <path-to-.env>")
        return 1
    glob, agent = globalcfg.split(env)
    if not glob.get("ENTRABOT_BLUEPRINT_APP_ID") or not glob.get("ENTRABOT_TENANT_ID"):
        print(f"{src} has no tenant/Blueprint to migrate (not a provisioned .env).")
        return 1

    gpath = globalcfg.global_env_path()
    force = "force" in flags
    print(f"ENTRABOT — migrate {src}\n")
    if os.path.exists(gpath) and not force:
        print(f"  global config already exists: {gpath}")
        print("  (use --force to overwrite). Leaving it untouched.")
    else:
        globalcfg.write_env(
            gpath, glob,
            header="ENTRABOT global config — shared tenant + Blueprint (migrated). Do not commit.",
        )
        print(f"  ✓ tenant + Blueprint → {gpath}")

    if agent.get("ENTRABOT_AGENT_USER_UPN"):
        apath = globalcfg.home_agent_env_path()
        if os.path.exists(apath) and not force:
            print(f"  default agent already exists: {apath} (use --force to overwrite).")
        else:
            globalcfg.write_env(
                apath, agent,
                header="ENTRABOT default agent identity (migrated). Do not commit.",
            )
            print(f"  ✓ existing agent ({agent['ENTRABOT_AGENT_USER_UPN']}) → {apath}")
    print("\n  Done. `entrabot` (from home) runs the migrated agent; `entrabot init` in any other")
    print("  directory adds a new agent reusing this tenant + Blueprint.")
    return 0


def _load_sponsor_ids() -> tuple[set, str]:
    """Best-effort: the Agent Identity's sponsor user ids (Entra) for the Role column. Returns
    (ids, note); note is non-empty when the gate couldn't be loaded (e.g. no token/agent)."""
    try:
        from entrabot.config import get_config
        from entrabot.identity.sponsors import load_agent_identity_sponsor_gate

        return set(load_agent_identity_sponsor_gate(get_config()).user_ids), ""
    except Exception:
        return set(), "(sponsor status unavailable — could not read the Agent Identity sponsors)"


def _cmd_users(args: list[str], flags: set) -> int:
    """Manage the federated recipient (talk-to) list and the agent's sponsors. Recipients live in
    the shared global config; sponsors are the Entra Agent-Identity relationship (core
    identity.sponsors), elevated/demoted here as a convenience."""
    from . import globalcfg, recipients, setup_wizard

    if not globalcfg.global_exists():
        print("No ENTRABOT config yet. Run `entrabot init` first.")
        return 1
    # Layer this agent's identity over the global base so get_config()/token-minting work.
    try:
        from entrabot import config as entracfg

        entracfg.apply_agent_env(_resolve_root(None))
    except Exception:
        pass

    sub = args[0] if args else "list"
    rest = args[1:]

    if sub == "list":
        recs = recipients.load_global()
        if not recs:
            print("No recipients configured. Add one: entrabot users add <email>")
            return 0
        sponsor_ids, note = _load_sponsor_ids()
        print(f"Federated recipients ({len(recs)}):")
        print(f"  {'User':40} {'Type':8} Role")
        print(f"  {'-' * 40} {'-' * 8} {'-' * 7}")
        for r in recs:
            role = "Sponsor" if r.user_id and r.user_id in sponsor_ids else "Guest"
            print(f"  {r.upn:40} {r.user_type:8} {role}")
        if note:
            print(f"\n  {note}")
        print("  Elevate/demote: entrabot users sponsor|guest <email>")
        return 0

    if sub in ("sponsor", "guest"):
        if not rest:
            print(f"Usage: entrabot users {sub} <email>")
            return 1
        from entrabot.config import get_config
        from entrabot.identity import sponsors as core_sponsors

        try:
            if sub == "sponsor":
                _id, name = core_sponsors.add_sponsor_by_email(get_config(), rest[0])
                print(f"  ✓ {name or rest[0]} is now a sponsor of this agent")
            else:
                name, removed = core_sponsors.remove_sponsor_by_email(get_config(), rest[0])
                if not removed:
                    print(f"  {rest[0]} was not a sponsor.")
                    return 1
                print(f"  ✓ {name or rest[0]} is no longer a sponsor")
        except LookupError:
            print(f"  {rest[0]} not found in the tenant (invite as a guest first).")
            return 1
        except Exception as e:
            print(f"  Could not update sponsor: {type(e).__name__}: {e}")
            return 1
        return 0

    if sub == "add":
        if not rest:
            print("Usage: entrabot users add <email> [<email> ...]")
            return 1
        try:
            resolved = recipients.parse(setup_wizard.resolve_teams_user(",".join(rest)))
        except setup_wizard.TeamsUserNotFound as e:
            print(f"Not found in this tenant (invite as a guest first): {', '.join(e.emails)}")
            return 1
        except Exception as e:
            print(f"Could not resolve recipient(s): {type(e).__name__}: {e}")
            return 1
        merged = recipients.upsert(recipients.load_global(), resolved)
        recipients.save_global(merged)
        for r in resolved:
            tail = " — federated (guest)" if r.is_guest else ""
            print(f"  ✓ added {r.upn} [{r.user_type}]{tail}")
        return 0

    if sub == "remove":
        if not rest:
            print("Usage: entrabot users remove <email>")
            return 1
        kept, changed = recipients.remove(recipients.load_global(), rest[0])
        if not changed:
            print(f"  {rest[0]} is not in the recipient list.")
            return 1
        recipients.save_global(kept)
        print(f"  ✓ removed {rest[0]}")
        return 0

    print(f"Unknown users subcommand: {sub}. Try: list | add | remove | sponsor | guest")
    return 1


async def _cmd_run(flags: set, root: str) -> int:
    # Layer this agent's identity (root/.entrabot/.env) over the global tenant/blueprint base
    # before anything reads creds (token provider, self_id).
    try:
        from entrabot import config as entracfg

        entracfg.apply_agent_env(root)
    except Exception:
        pass

    cfg = cfgmod.try_load(root)
    if cfg is None:
        # Just-run-it: scaffold a sensible default config rather than erroring.
        name = (os.path.basename(root.rstrip("/\\")) or "entrabot") + "-agent"
        cfg = HarnessConfig(
            name=name,
            description=f"{name}, an ENTRABOT agent reachable on Microsoft Teams.",
            created_utc=datetime.now(timezone.utc).isoformat(),
        )
        cfg.ensure_identity()
        cfgmod.save(root, cfg)
        print(f"(no config at {cfgmod.config_path(root)} — created a default agent '{name}'; "
              f"run `entrabot init` for guided setup)")
    elif cfg.ensure_identity():
        cfgmod.save(root, cfg)

    ui = _pick_ui()
    session = InteractiveSession(
        cfg,
        root,
        ui,
        yolo="yolo" in flags or "y" in flags,
        fresh="new" in flags or "fresh" in flags,
        autopilot="interactive" not in flags,
        token_provider=make_token_provider(),
        self_id=os.environ.get("ENTRABOT_AGENT_USER_ID"),
    )
    await session.run()
    return 0


async def _cmd_doctor(root: str) -> int:
    import copilot

    print("ENTRABOT — doctor\n")
    tp = make_token_provider()
    print(f"  Teams token: {'available' if tp else 'none → console-only (set ENTRABOT_GRAPH_TOKEN or run `entrabot init`)'}")

    client = copilot.CopilotClient(working_directory=root, log_level="error")
    try:
        await asyncio.wait_for(client.start(), timeout=45)
    except Exception as e:
        print(f"  Copilot runtime: FAILED to start — {e}")
        return 1
    print("  Copilot runtime: started")
    try:
        st = await client.get_auth_status()
        if getattr(st, "isAuthenticated", False):
            print(f"  GitHub auth: authenticated as {getattr(st, 'login', '?')}")
        else:
            print(f"  GitHub auth: NOT authenticated — {getattr(st, 'statusMessage', 'run `copilot` to sign in')}")
    except Exception as e:
        print(f"  GitHub auth: check failed — {e}")
    try:
        models = await client.list_models()
        print(f"  Models: {len(models)} available (e.g. {', '.join(m.id for m in models[:4])})")
    except Exception as e:
        print(f"  Models: list failed — {e}")
    await client.stop()
    print("\n  done.")
    return 0


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def main(argv: List[str] | None = None) -> int:
    _force_utf8()
    args = list(argv if argv is not None else sys.argv[1:])
    flags, positionals = _flags(args)

    if "help" in flags or "h" in flags:
        print(_USAGE)
        return 0
    if "version" in flags or "v" in flags:
        print(f"entrabot {__version__}")
        return 0

    cmd = positionals[0] if positionals else None
    if cmd == "init":
        return _cmd_init(positionals[1:], flags)
    if cmd == "users":
        return _cmd_users(positionals[1:], flags)
    if cmd == "migrate":
        return _cmd_migrate(positionals[1:], flags)
    if cmd == "doctor":
        return asyncio.run(_cmd_doctor(_resolve_root(positionals[1] if len(positionals) > 1 else None)))

    try:
        return asyncio.run(_cmd_run(flags, _resolve_root(cmd)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
