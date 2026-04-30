#!/usr/bin/env python3
"""Claude Code memory ↔ blob storage sync (ADR-005 Phase 6a).

.. deprecated::
    Memory sync is now handled by the **persona-sati** MCP server.
    The SessionStart and PostToolUse hooks that called this script have
    been removed from ``.claude/settings.json``.  This script is kept
    as a **manual migration / one-off sync tool** — it still works if
    invoked directly, but it is no longer called automatically.

Three subcommands, all idempotent and safe to call manually:

    pull            Download every ``claude_memory/`` blob into the local
                    Claude Code memory directory. Cloud is authoritative.

    push            Upload every local memory file not already in the cloud.
                    Used for initial bulk-upload and as a safety net.

    push-one PATH   Upload a single file to ``claude_memory/``.

All commands respect ``ENTRACLAW_PERSONA_SYNC`` — when unset or not ``on``
the script exits 0 without touching the backend. Every error path returns
0 and logs to stderr.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _resolve_backend():  # pragma: no cover — monkey-patched in tests
    from entraclaw.storage.backend import get_backend

    return get_backend()


def _resolve_memory_dir() -> Path:  # pragma: no cover — monkey-patched in tests
    from entraclaw.storage.persona import claude_code_memory_dir

    project_root = Path.cwd()
    return claude_code_memory_dir(project_root)


def _feature_flag_on() -> bool:
    return os.environ.get("ENTRACLAW_PERSONA_SYNC", "").lower() == "on"


def _log(msg: str) -> None:
    print(f"[persona-sync] {msg}", file=sys.stderr)


def _cmd_pull() -> int:
    if not _feature_flag_on():
        return 0
    from entraclaw.storage.persona import PersonaBackend

    mem_dir = _resolve_memory_dir()
    backend = _resolve_backend()
    persona = PersonaBackend(backend, local_root=mem_dir)
    report = persona.pull_all()
    _log(f"pull: {report.pulled} files into {mem_dir}")
    return 0


def _cmd_push() -> int:
    if not _feature_flag_on():
        return 0
    from entraclaw.storage.persona import PersonaBackend

    mem_dir = _resolve_memory_dir()
    backend = _resolve_backend()
    persona = PersonaBackend(backend, local_root=mem_dir)
    report = persona.push_all()
    _log(f"push: {report.copied} copied, {report.skipped} skipped (from {mem_dir})")
    if report.errors:
        for key, err in report.errors[:5]:
            _log(f"error: {key}: {err}")
    return 0


def _cmd_push_one(path: str) -> int:
    if not _feature_flag_on():
        return 0
    from entraclaw.storage.persona import PersonaBackend

    mem_dir = _resolve_memory_dir()
    target = Path(path)
    backend = _resolve_backend()
    persona = PersonaBackend(backend, local_root=mem_dir)
    try:
        persona.push_one(target)
    except ValueError as exc:
        _log(f"push-one: skipped (outside memory dir): {exc}")
        return 0
    except Exception as exc:  # noqa: BLE001 — hooks must not crash
        _log(f"push-one: error for {path}: {exc}")
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claude_memory_sync",
        description="Sync Claude Code per-project auto-memory with blob storage.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("pull", help="download claude_memory/ blobs into local")
    sub.add_parser("push", help="upload local memory files missing in cloud")
    one = sub.add_parser("push-one", help="upload one memory file")
    one.add_argument("path", help="absolute path to the memory file to push")

    args = parser.parse_args(argv)

    if args.cmd == "pull":
        return _cmd_pull()
    if args.cmd == "push":
        return _cmd_push()
    if args.cmd == "push-one":
        return _cmd_push_one(args.path)
    return 2  # unreachable — argparse enforces choices


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
