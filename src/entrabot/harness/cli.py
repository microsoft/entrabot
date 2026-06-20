"""CLI entry point for ENTRABOT.

  entrabot                 start the harness (config: ./.entrabot if present, else ~/.entrabot)
  entrabot <path>          start the harness with config under <path>/.entrabot
  entrabot init [path]     guided cross-platform setup (tenant → az login → provision → test)
  entrabot doctor          check the Copilot runtime + auth + Teams token
  entrabot --version | --help

Flags: --yolo/-y (allow all tools), --new/--fresh (don't resume), --interactive (not autopilot).
Config is stored under ~/.entrabot by default; pass a path or run inside a dir with ./.entrabot
to use that instead. ENTRABOT_CONSOLE=1 forces the plain UI.
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

    root = _resolve_root(positionals[0] if positionals else None)
    ok = run_init(root)
    if not ok:
        return 1
    if _confirm("Launch the harness now?", default=True):
        try:
            return asyncio.run(_cmd_run(flags, root))
        except KeyboardInterrupt:
            return 0
    print(f"\nLater, run `entrabot` to launch (config under {os.path.join(root, cfgmod.CONFIG_DIR)}).")
    return 0


async def _cmd_run(flags: set, root: str) -> int:
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
    if cmd == "doctor":
        return asyncio.run(_cmd_doctor(_resolve_root(positionals[1] if len(positionals) > 1 else None)))

    try:
        return asyncio.run(_cmd_run(flags, _resolve_root(cmd)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
