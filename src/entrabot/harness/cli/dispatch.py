"""CLI entry point for ENTRABOT.

  entrabot                 start the harness (config: ./.entrabot if present, else ~/.entrabot)
  entrabot <path>          start the harness with config under <path>/.entrabot
  entrabot init [path]     guided setup for an agent in this directory (asks first). Reuses the
                           shared tenant/Blueprint if already set up; only mints a new agent.
                           Idempotent — re-run to continue setup of an existing agent.
  entrabot users           list the agent's sponsors (Entra Agent-Identity relationship; read-only)
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
import sys

from .. import __version__
from .subcommands import _cmd_doctor, _cmd_init, _cmd_migrate, _cmd_run, _cmd_users
from .terminal import _flags, _force_utf8, _resolve_root

_USAGE = __doc__


def main(argv: list[str] | None = None) -> int:
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
        root = _resolve_root(positionals[1] if len(positionals) > 1 else None)
        return asyncio.run(_cmd_doctor(root))

    try:
        return asyncio.run(_cmd_run(flags, _resolve_root(cmd)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
