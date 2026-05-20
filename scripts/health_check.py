#!/usr/bin/env python3
"""Compatibility wrapper for the consolidated Agent Identity status command.

``show_agent_status.py`` owns the live Graph inventory and health logic. This
entry point remains for users and scripts that still call ``health_check.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import show_agent_status  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = list(argv or [])
    if "--help" in args or "-h" in args:
        return show_agent_status.main(args)
    return show_agent_status.main([*args, "--health-only"])


if __name__ == "__main__":
    sys.exit(main())
