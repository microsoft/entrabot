"""Local→backend migration helper (ADR-005 Phase 5 + Phase 6a).

Phase 5 moved the agent's operational data (``~/.entrabot/data``) into
:class:`BlobBackend`. Phase 6a extends the signature to accept *multiple*
(local_root, blob_prefix) pairs so the Claude Code persona-memory
directory can migrate in the same atomic call as the agent data.

Behavior per pair:
- Walks *local_root* depth-first, copying each file to
  ``{blob_prefix}/{relpath}`` (or just ``{relpath}`` when prefix is ``""``).
- Idempotent: a key already present in the target is skipped — cloud is
  authoritative on rerun (ADR §"Migration").
- Source files are never deleted — that's the rollback path for
  ``ENTRABOT_KEEP_MEMORY_LOCAL=true``.
- Missing pairs (local_root doesn't exist) are skipped silently so a
  user without Claude Code's memory directory still migrates cleanly.
- Per-file errors are captured in ``report.errors`` rather than raised
  so one bad file doesn't abort the whole migration.
- Local poll state is not copied, because it may be stale or belong to another
  agent: the watched-chat list, both email cursor files, the legacy single-chat
  ``chat_id`` key, and every per-chat cursor under ``chat_cursors/``. A cloud
  backend starts its email cursor at "now", since adopting a local value could
  replay old mail as new, and discovery rebuilds the chat list from Graph, since
  a wrong list in the shared store would never shrink. A chat with no cursor takes
  the first-sight path, which surfaces its newest message only if it is under 24
  hours old. A copied cursor would instead be rehydrated as present and bypass that
  gate, so two agents sharing a group chat could replay old messages. See
  :mod:`entrabot.tools.poll_state`.
- The process-lock files are not copied. They only mean something on this machine.
- These exclusions match keys at the root of the target. A source copied under a
  prefix such as ``claude_memory`` keeps every file, whatever its name.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from entrabot.storage.backend import TEXT_ENCODING, MemoryBackend, is_temp_file_name
from entrabot.tools.poll_state import (
    EMAIL_CURSOR_KEY,
    LEGACY_DEFAULT_CHAT_KEY,
    LEGACY_EMAIL_CURSOR_KEY,
    WATCHED_CHATS_KEY,
)

# Keys that must never be carried from local disk into the target: state that may be
# stale or another agent's, and files that only mean something on this machine.
_NOT_MIGRATED_KEYS = frozenset(
    {
        EMAIL_CURSOR_KEY,
        LEGACY_EMAIL_CURSOR_KEY,
        WATCHED_CHATS_KEY,
        LEGACY_DEFAULT_CHAT_KEY,
        ".singleton.lock",
        ".singleton.lock.holder.pid",
    }
)

# Everything under these prefixes is skipped too. ``chat_cursors/`` holds one delivery
# cursor per chat (see ``chat_cursors.cursor_key``, whose prefix constant is private).
# The migration tests build their keys with ``cursor_key``, so a change to the real
# prefix fails there instead of silently letting cursors through.
_NOT_MIGRATED_PREFIXES = ("chat_cursors/",)


def _is_not_migrated(key: str) -> bool:
    return key in _NOT_MIGRATED_KEYS or key.startswith(_NOT_MIGRATED_PREFIXES)


@dataclass
class MigrationReport:
    """Summary of one ``migrate_local_to_backend`` call."""

    copied: int = 0
    skipped: int = 0
    bytes_copied: int = 0
    keys_copied: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    """List of (key, error_message) for files that failed to copy."""


def migrate_local_to_backend(
    sources: Iterable[tuple[Path, str]], target: MemoryBackend
) -> MigrationReport:
    """Copy every file under each source into *target*.

    *sources* is an iterable of ``(local_root, blob_prefix)`` pairs. An
    empty prefix (``""``) means "root of the blob container" — that's
    the Phase 5 agent-data case. A populated prefix (e.g.
    ``"claude_memory"``) groups a source under a subdirectory.

    Keys are the POSIX-style relative path from *local_root*, prefixed
    with ``{blob_prefix}/`` when the prefix is non-empty.
    """
    report = MigrationReport()
    for local_root, prefix in sources:
        _migrate_one_source(Path(local_root), prefix, target, report)
    return report


def _migrate_one_source(
    local_root: Path,
    prefix: str,
    target: MemoryBackend,
    report: MigrationReport,
) -> None:
    if not local_root.exists():
        return

    # Normalize prefix: "" stays "", else ensure single trailing slash.
    key_prefix = f"{prefix}/" if prefix else ""

    for path in sorted(local_root.rglob("*")):
        if not path.is_file() or is_temp_file_name(path.name):
            continue
        rel = path.relative_to(local_root).as_posix()
        key = f"{key_prefix}{rel}"
        if _is_not_migrated(key):
            continue
        try:
            if target.exists(key):
                report.skipped += 1
                continue
            content = path.read_text(encoding=TEXT_ENCODING)
        except Exception as exc:  # noqa: BLE001 — record + continue
            report.errors.append((key, str(exc)))
            continue
        try:
            target.write_text(key, content)
        except Exception as exc:  # noqa: BLE001
            report.errors.append((key, str(exc)))
            continue
        report.copied += 1
        report.bytes_copied += len(content)
        report.keys_copied.append(key)
