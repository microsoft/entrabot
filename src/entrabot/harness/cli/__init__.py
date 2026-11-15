"""CLI entry point package for ENTRABOT. ``main`` is the console-script target
(``entrabot.harness.cli:main``); the ``_cmd_*`` handlers are re-exported so tests and
``python -m entrabot.harness`` keep importing them from ``entrabot.harness.cli``."""

from .dispatch import main
from .subcommands import _cmd_doctor, _cmd_init, _cmd_migrate, _cmd_run, _cmd_users

__all__ = ["main", "_cmd_doctor", "_cmd_init", "_cmd_migrate", "_cmd_run", "_cmd_users"]
