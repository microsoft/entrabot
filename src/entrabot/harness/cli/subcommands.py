"""The `entrabot` subcommands: init, migrate, users, run, doctor."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

from .. import config as cfgmod
from ..config import HarnessConfig
from ..session import InteractiveSession
from ..teams import make_token_provider
from .terminal import _confirm, _pick_ui, _resolve_root


def _apply_agent_identity(root: str) -> None:
    """Layer this agent's identity (root/.entrabot/.env) over the global tenant/blueprint base so
    get_config()/token-minting see the right creds. Best-effort — absent config is fine."""
    try:
        from entrabot import config as entracfg

        entracfg.apply_agent_env(root)
    except Exception:
        pass


def _cmd_init(positionals: list[str], flags: set) -> int:
    from ..setup import run_init

    # init works in the current directory by default (the wizard confirms / lets you change it).
    default_root = os.path.abspath(positionals[0]) if positionals else os.getcwd()
    if not run_init(default_root):
        return 1
    if _confirm("Launch the harness now?", default=True):
        try:
            return asyncio.run(_cmd_run(flags, default_root))
        except KeyboardInterrupt:
            return 0
    print("\nLater, run `entrabot` from that directory to launch it.")
    return 0


def _cmd_migrate(positionals: list[str], flags: set) -> int:
    """Lift an existing combined .env (the original repo flow) into the layered layout:
    ~/.entrabot/global.env (shared tenant + Blueprint) + ~/.entrabot/.env (the existing agent
    as the home default)."""
    from ..config import globalcfg
    from ..setup import _clone_root

    source = positionals[0] if positionals else os.path.join(_clone_root(), ".env")
    combined_env = globalcfg.read_env(source)
    if not combined_env:
        print(f"No .env found at {source}. Pass the path: entrabot migrate <path-to-.env>")
        return 1
    global_env, agent_env = globalcfg.split(combined_env)
    if not global_env.get("ENTRABOT_BLUEPRINT_APP_ID") or not global_env.get("ENTRABOT_TENANT_ID"):
        print(f"{source} has no tenant/Blueprint to migrate (not a provisioned .env).")
        return 1

    global_path = globalcfg.global_env_path()
    force = "force" in flags
    print(f"ENTRABOT — migrate {source}\n")
    if os.path.exists(global_path) and not force:
        print(f"  global config already exists: {global_path}")
        print("  (use --force to overwrite). Leaving it untouched.")
    else:
        globalcfg.write_env(
            global_path, global_env,
            header="ENTRABOT global config — shared tenant + Blueprint (migrated). Do not commit.",
        )
        print(f"  ✓ tenant + Blueprint → {global_path}")

    if agent_env.get("ENTRABOT_AGENT_USER_UPN"):
        agent_path = globalcfg.home_agent_env_path()
        if os.path.exists(agent_path) and not force:
            print(f"  default agent already exists: {agent_path} (use --force to overwrite).")
        else:
            globalcfg.write_env(
                agent_path, agent_env,
                header="ENTRABOT default agent identity (migrated). Do not commit.",
            )
            print(f"  ✓ existing agent ({agent_env['ENTRABOT_AGENT_USER_UPN']}) → {agent_path}")
    print("\n  Done. `entrabot` (from home) runs the migrated agent; `entrabot init` in any other")
    print("  directory adds a new agent reusing this tenant + Blueprint.")
    return 0


def _cmd_users(args: list[str], flags: set) -> int:
    """List the agent's sponsors — the Entra Agent-Identity sponsor relationship (core
    identity.sponsors), the same source the entrabot body gates on. Read-only: add/remove sponsors
    in Entra directly (or via scripts/add_agent_sponsor.py / remove_agent_sponsor.py)."""
    from ..config import globalcfg

    if not globalcfg.global_exists():
        print("No ENTRABOT config yet. Run `entrabot init` first.")
        return 1
    # Layer this agent's identity over the global base so get_config()/token-minting work.
    _apply_agent_identity(_resolve_root(None))

    from entrabot.config import get_config
    from entrabot.identity.sponsors import fetch_agent_identity_sponsors
    from entrabot.tools.teams import acquire_agent_user_token

    try:
        records = fetch_agent_identity_sponsors(
            get_config(), user_token_provider=acquire_agent_user_token)
    except ValueError:
        records = []  # no sponsors
    except Exception as error:
        print(f"Could not read sponsors: {type(error).__name__}: {error}")
        return 1
    if not records:
        print("No sponsors (manage in Entra → the agent's sponsor relationship).")
        return 0
    print(f"Agent sponsors ({len(records)}):")
    for record in records:
        print(f"  • {record.mail or record.user_principal_name or record.user_id}")
    return 0


async def _cmd_run(flags: set, root: str) -> int:
    # Layer this agent's identity (root/.entrabot/.env) over the global tenant/blueprint base
    # before anything reads creds (token provider, self_id).
    _apply_agent_identity(root)

    harness_config = cfgmod.try_load(root)
    if harness_config is None:
        # Just-run-it: scaffold a sensible default config rather than erroring.
        name = (os.path.basename(root.rstrip("/\\")) or "entrabot") + "-agent"
        harness_config = HarnessConfig(
            name=name,
            description=f"{name}, an ENTRABOT agent reachable on Microsoft Teams.",
            created_utc=datetime.now(UTC).isoformat(),
        )
        harness_config.ensure_identity()
        cfgmod.save(root, harness_config)
        print(f"(no config at {cfgmod.config_path(root)} — created a default agent '{name}'; "
              f"run `entrabot init` for guided setup)")
    elif harness_config.ensure_identity():
        cfgmod.save(root, harness_config)

    session = InteractiveSession(
        harness_config,
        root,
        _pick_ui(),
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
    token_provider = make_token_provider()
    teams_status = "available" if token_provider \
        else "none → console-only (set ENTRABOT_GRAPH_TOKEN or run `entrabot init`)"
    print(f"  Teams token: {teams_status}")

    client = copilot.CopilotClient(working_directory=root, log_level="error")
    try:
        await asyncio.wait_for(client.start(), timeout=45)
    except Exception as error:
        print(f"  Copilot runtime: FAILED to start — {error}")
        return 1
    print("  Copilot runtime: started")
    try:
        auth_status = await client.get_auth_status()
        if getattr(auth_status, "isAuthenticated", False):
            print(f"  GitHub auth: authenticated as {getattr(auth_status, 'login', '?')}")
        else:
            message = getattr(auth_status, "statusMessage", "run `copilot` to sign in")
            print(f"  GitHub auth: NOT authenticated — {message}")
    except Exception as error:
        print(f"  GitHub auth: check failed — {error}")
    try:
        models = await client.list_models()
        print(f"  Models: {len(models)} available (e.g. {', '.join(m.id for m in models[:4])})")
    except Exception as error:
        print(f"  Models: list failed — {error}")
    await client.stop()
    print("\n  done.")
    return 0
