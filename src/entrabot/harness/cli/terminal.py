"""Leaf terminal helpers for the CLI: root resolution, UI selection, flag parsing, prompts."""

from __future__ import annotations

import os
import sys

from .. import config as cfgmod
from ..ui import UI
from ..ui.console import ConsoleUI


def _resolve_root(path_arg: str | None) -> str:
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
        from ..ui.tui import TextualUI, available

        if available():
            return TextualUI()
    except Exception:
        pass
    return ConsoleUI()


def _flags(args: list[str]):
    flags = {arg.lstrip("-").lower() for arg in args if arg.startswith("-")}
    positionals = [arg for arg in args if not arg.startswith("-")]
    return flags, positionals


def _confirm(prompt: str, default: bool = True) -> bool:
    if not sys.stdin.isatty():
        return default
    prompt_suffix = "Y/n" if default else "y/N"
    try:
        answer = input(f"  {prompt} [{prompt_suffix}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return default if not answer else answer in ("y", "yes")


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
