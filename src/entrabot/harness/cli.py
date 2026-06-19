"""CLI entry point for the ENTRABOT harness (port of Program.cs).

Commands:
  entrabot-harness                 start a session in the current directory
  entrabot-harness init [name] [description...]   scaffold a new ENTRABOT agent here
  entrabot-harness --version
  entrabot-harness --help

Flags: --yolo/-y (auto-approve), --new/--fresh (don't resume), --interactive (not autopilot).
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import List

from . import __version__, banner
from . import config as cfgmod
from . import scaffold
from .auth import make_token_provider
from .config import HarnessConfig
from .session import InteractiveSession
from .ui import UI
from .ui.console import ConsoleUI

_USAGE = """ENTRABOT — a Copilot harness that routes Microsoft Teams traffic per caller

Usage:
  entrabot-harness                              start a session (config in the current dir)
  entrabot-harness init [name] [description…]   create a new ENTRABOT agent here
  entrabot-harness doctor                       check the Copilot runtime + auth + Teams token
  entrabot-harness --version
  entrabot-harness --help

Options:
  --yolo, -y      auto-approve all tool calls (skip the per-caller permission gate)
  --new, --fresh  start fresh instead of resuming the persisted session
  --interactive   interactive mode instead of autopilot (autonomous is the default)

Config + context (.entrabot/harness.json, AGENT.md) live in the current directory.
Set ENTRABOT_TUI=1 for the full-screen Textual UI; ENTRABOT_GRAPH_TOKEN to enable Teams.
"""


def _pick_ui() -> UI:
    if os.environ.get("ENTRABOT_TUI") == "1" and sys.stdout.isatty():
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


def _cmd_init(positionals: List[str]) -> int:
    root = os.getcwd()
    if cfgmod.exists(root):
        print("an ENTRABOT agent already exists here (.entrabot/harness.json).", file=sys.stderr)
        return 1
    if positionals:
        name = positionals[0]
        description = " ".join(positionals[1:]) or f"An ENTRABOT agent named {name}."
    else:
        if not sys.stdin.isatty():
            print("init needs a name (non-interactive): entrabot-harness init <name> <description>", file=sys.stderr)
            return 1
        name = input("ENTRABOT name: ").strip() or "entrabot"
        description = input("Description: ").strip() or f"An ENTRABOT agent named {name}."
    cfg = HarnessConfig(name=name, description=description, created_utc=datetime.now(timezone.utc).isoformat())
    result = scaffold.bootstrap(root, cfg)
    print("created:")
    for p in result.created:
        print(f"  {os.path.relpath(p, root)}")
    print("\nstart it with: entrabot-harness")
    return 0


async def _cmd_run(flags: set) -> int:
    root = os.getcwd()
    cfg = cfgmod.try_load(root)
    if cfg is None:
        print("no ENTRABOT agent here. create one with: entrabot-harness init <name>", file=sys.stderr)
        return 1
    if cfg.ensure_identity():
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


async def _cmd_doctor() -> int:
    import copilot

    print("ENTRABOT harness — doctor\n")
    tp = make_token_provider()
    print(f"  Teams token: {'available' if tp else 'none → console-only (set ENTRABOT_GRAPH_TOKEN or three-hop creds)'}")

    client = copilot.CopilotClient(working_directory=os.getcwd(), log_level="error")
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
        sample = ", ".join(m.id for m in models[:4])
        print(f"  Models: {len(models)} available (e.g. {sample})")
    except Exception as e:
        print(f"  Models: list failed — {e}")
    await client.stop()
    print("\n  done.")
    return 0


def _force_utf8() -> None:
    # The banner + status lines use Unicode (block glyphs, ●, em-dashes); Windows consoles
    # often default to cp1252 and would crash on print(). Reconfigure to UTF-8 best-effort.
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
        print(f"entrabot-harness {__version__}")
        return 0
    if positionals and positionals[0] == "init":
        return _cmd_init(positionals[1:])
    if positionals and positionals[0] == "doctor":
        return asyncio.run(_cmd_doctor())

    try:
        return asyncio.run(_cmd_run(flags))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
