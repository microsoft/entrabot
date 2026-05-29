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
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from entrabot.storage.backend import MemoryBackend


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
        if not path.is_file():
            continue
        rel = path.relative_to(local_root).as_posix()
        key = f"{key_prefix}{rel}"
        try:
            if target.exists(key):
                report.skipped += 1
                continue
            content = path.read_text()
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
