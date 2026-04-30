"""Tests for the local→cloud migration helper (ADR-005, Phase 5).

The helper walks a local data directory and copies each file's contents
to a target :class:`MemoryBackend` (typically :class:`BlobBackend`).

Behavior:
- Copies all files under *local_root* preserving relative paths
- Idempotent: skips keys that already exist in the target (so re-runs
  after a partial failure don't duplicate-write)
- Returns a :class:`MigrationReport` with counts + per-file status
- Does not delete local files (per ADR §"Migration": "Leave local files
  untouched — blob becomes the source of truth, local is now a cache").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from entraclaw.storage.backend import LocalBackend
from entraclaw.storage.migration import MigrationReport, migrate_local_to_backend


@pytest.fixture
def src_root(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "interactions").mkdir()
    (src / "interactions" / "2026-04-17.jsonl").write_text('{"event": "msg"}\n{"event": "reply"}\n')
    (src / "summaries").mkdir()
    (src / "summaries" / "2026-04-17.html").write_text("<html>day</html>")
    (src / "state").mkdir()
    (src / "state" / "watched_chats").write_text("chat-id-1\nchat-id-2\n")
    return src


class TestMigrateLocalToBackend:
    """Phase 6a signature: ``sources = list[tuple[Path, str]]``.

    Each tuple is ``(local_root, blob_prefix)``. The Phase 5 agent-data
    case becomes ``[(data_dir, "")]``; Phase 6a adds a second pair for
    Claude Code persona memory with prefix ``"claude_memory"``.
    """

    def test_copies_all_files_to_backend(self, src_root: Path, tmp_path: Path) -> None:
        target = LocalBackend(tmp_path / "dst")
        report = migrate_local_to_backend([(src_root, "")], target)

        assert target.read_text("interactions/2026-04-17.jsonl") == (
            '{"event": "msg"}\n{"event": "reply"}\n'
        )
        assert target.read_text("summaries/2026-04-17.html") == "<html>day</html>"
        assert target.read_text("state/watched_chats") == "chat-id-1\nchat-id-2\n"
        assert report.copied == 3
        assert report.skipped == 0

    def test_idempotent_skips_existing_keys(self, src_root: Path, tmp_path: Path) -> None:
        target = LocalBackend(tmp_path / "dst")
        target.write_text("interactions/2026-04-17.jsonl", "ALREADY-IN-CLOUD")

        report = migrate_local_to_backend([(src_root, "")], target)

        # Existing key must be left alone (cloud is source of truth on rerun)
        assert target.read_text("interactions/2026-04-17.jsonl") == "ALREADY-IN-CLOUD"
        assert report.skipped == 1
        assert report.copied == 2

    def test_does_not_delete_source_files(self, src_root: Path, tmp_path: Path) -> None:
        target = LocalBackend(tmp_path / "dst")
        migrate_local_to_backend([(src_root, "")], target)
        # Per ADR — local files remain
        assert (src_root / "interactions" / "2026-04-17.jsonl").exists()
        assert (src_root / "summaries" / "2026-04-17.html").exists()
        assert (src_root / "state" / "watched_chats").exists()

    def test_missing_source_returns_empty_report(self, tmp_path: Path) -> None:
        target = LocalBackend(tmp_path / "dst")
        report = migrate_local_to_backend([(tmp_path / "does-not-exist", "")], target)
        assert report.copied == 0
        assert report.skipped == 0
        assert report.errors == []

    def test_report_lists_keys(self, src_root: Path, tmp_path: Path) -> None:
        target = LocalBackend(tmp_path / "dst")
        report = migrate_local_to_backend([(src_root, "")], target)
        assert isinstance(report, MigrationReport)
        assert set(report.keys_copied) == {
            "interactions/2026-04-17.jsonl",
            "summaries/2026-04-17.html",
            "state/watched_chats",
        }

    def test_total_bytes_counted(self, src_root: Path, tmp_path: Path) -> None:
        target = LocalBackend(tmp_path / "dst")
        report = migrate_local_to_backend([(src_root, "")], target)
        expected = (
            len('{"event": "msg"}\n{"event": "reply"}\n')
            + len("<html>day</html>")
            + len("chat-id-1\nchat-id-2\n")
        )
        assert report.bytes_copied == expected


class TestMigrateMultipleSources:
    """Phase 6a — multiple (source, prefix) pairs in one atomic call."""

    def test_second_pair_is_prefixed(self, tmp_path: Path) -> None:
        agent = tmp_path / "agent"
        agent.mkdir()
        (agent / "email_cursor.txt").write_text("cursor=42")

        persona = tmp_path / "persona"
        persona.mkdir()
        (persona / "MEMORY.md").write_text("index")
        (persona / "user_brandon_role.md").write_text("Brandon")

        target = LocalBackend(tmp_path / "dst")
        report = migrate_local_to_backend([(agent, ""), (persona, "claude_memory")], target)

        assert target.read_text("email_cursor.txt") == "cursor=42"
        assert target.read_text("claude_memory/MEMORY.md") == "index"
        assert target.read_text("claude_memory/user_brandon_role.md") == "Brandon"
        assert report.copied == 3

    def test_per_pair_idempotency(self, tmp_path: Path) -> None:
        persona = tmp_path / "persona"
        persona.mkdir()
        (persona / "MEMORY.md").write_text("local")

        target = LocalBackend(tmp_path / "dst")
        target.write_text("claude_memory/MEMORY.md", "ALREADY-CLOUD")

        report = migrate_local_to_backend([(persona, "claude_memory")], target)

        assert target.read_text("claude_memory/MEMORY.md") == "ALREADY-CLOUD"
        assert report.skipped == 1

    def test_missing_pair_skipped_silently(self, tmp_path: Path) -> None:
        agent = tmp_path / "agent"
        agent.mkdir()
        (agent / "f.txt").write_text("f")

        target = LocalBackend(tmp_path / "dst")
        report = migrate_local_to_backend(
            [
                (agent, ""),
                (tmp_path / "nonexistent-persona", "claude_memory"),
            ],
            target,
        )
        assert report.copied == 1
        assert report.errors == []

    def test_empty_prefix_vs_populated_prefix_produce_distinct_keys(self, tmp_path: Path) -> None:
        # If two sources had a collision on the same relative name but
        # one uses "" and the other uses "claude_memory", the full keys
        # must differ so both land.
        src_a = tmp_path / "a"
        src_a.mkdir()
        (src_a / "README.md").write_text("A")
        src_b = tmp_path / "b"
        src_b.mkdir()
        (src_b / "README.md").write_text("B")

        target = LocalBackend(tmp_path / "dst")
        migrate_local_to_backend([(src_a, ""), (src_b, "claude_memory")], target)

        assert target.read_text("README.md") == "A"
        assert target.read_text("claude_memory/README.md") == "B"
