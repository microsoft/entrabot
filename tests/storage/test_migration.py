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

import json
from pathlib import Path

import pytest

from entrabot.storage.backend import LocalBackend
from entrabot.storage.migration import MigrationReport, migrate_local_to_backend
from entrabot.tools.chat_cursors import CursorOutcome, cursor_key, resolve_cursor


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

    def test_a_temp_file_left_by_a_crashed_write_is_not_migrated(
        self, src_root: Path, tmp_path: Path
    ) -> None:
        leftover = ".tmp-0123456789abcdef"
        (src_root / "state" / leftover).write_text("half a wri")
        target = LocalBackend(tmp_path / "dst")

        report = migrate_local_to_backend([(src_root, "")], target)

        assert report.copied == 3
        assert not target.exists(f"state/{leftover}")
        assert not (tmp_path / "dst" / "state" / leftover).exists()

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
        (agent / "promises.jsonl").write_text("{}\n")

        persona = tmp_path / "persona"
        persona.mkdir()
        (persona / "MEMORY.md").write_text("index")
        (persona / "user_brandon_role.md").write_text("Brandon")

        target = LocalBackend(tmp_path / "dst")
        report = migrate_local_to_backend([(agent, ""), (persona, "claude_memory")], target)

        assert target.read_text("promises.jsonl") == "{}\n"
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


class TestMigrationAndPollState:
    """Setup's migration walks the whole data directory, poll state included.

    Everything that decides what gets delivered is left behind: the watched-chat list,
    the email cursors, the legacy single-chat ``chat_id`` key, and every per-chat
    cursor under ``chat_cursors/``. A machine's copies may be stale or belong to
    another agent, so none of them may reach the shared container.
    """

    CHATS = ("19:a@thread.v2", "19:b@thread.v2")

    def _local_install(self, tmp_path: Path) -> Path:
        data = tmp_path / "local-data"
        (data / "poll_state").mkdir(parents=True)
        (data / "chat_cursors").mkdir()
        (data / "watched_chats").write_text("\n".join(self.CHATS) + "\n")
        (data / "chat_id").write_text(f"{self.CHATS[0]}\n")
        (data / "email_cursor.txt").write_text("2026-05-01T00:00:00Z")
        (data / "poll_state" / "email_cursor.txt").write_text("2026-05-02T00:00:00Z")
        for chat_id in self.CHATS:
            (data / cursor_key(chat_id)).write_text(
                json.dumps(
                    {
                        "last_ts": "2026-05-01T00:00:00.000Z",
                        "seen_ids_tail": ["m1"],
                        "bootstrapped": True,
                        "last_written_at": "2026-05-01T00:00:01.000Z",
                    }
                )
            )
        return data

    def test_the_local_chat_list_is_not_carried_into_the_cloud(
        self, tmp_path: Path, fake_cloud_store
    ) -> None:
        """A machine's list may belong to another agent, and the shared list never
        shrinks, so those chats would be polled forever. Discovery rebuilds the real
        list from Graph about two minutes after boot."""
        from entrabot.storage.backend import get_backend
        from entrabot.tools.poll_state import PollStateOutcome, read_watched_chats

        report = migrate_local_to_backend([(self._local_install(tmp_path), "")], get_backend())

        assert read_watched_chats().outcome is PollStateOutcome.ABSENT
        assert "watched_chats" not in report.keys_copied

    def test_per_machine_files_are_not_carried_into_the_cloud(
        self, tmp_path: Path, fake_cloud_store
    ) -> None:
        from entrabot.storage.backend import get_backend

        data = self._local_install(tmp_path)
        (data / ".singleton.lock").write_text("")
        (data / ".singleton.lock.holder.pid").write_text("123")
        (data / "promises.jsonl").write_text("{}\n")

        report = migrate_local_to_backend([(data, "")], get_backend())

        assert report.keys_copied == ["promises.jsonl"]
        assert report.errors == []

    def test_no_local_cursor_can_become_the_cloud_cursor(
        self, tmp_path: Path, fake_cloud_store
    ) -> None:
        """Cloud starts its cursor at now, and never consults a local file. Copying
        the local cursor across would break that the moment setup runs, and a
        machine's local value may be stale or belong to another agent's mailbox."""
        from entrabot.storage.backend import get_backend
        from entrabot.tools.poll_state import PollStateOutcome, read_email_cursor

        report = migrate_local_to_backend([(self._local_install(tmp_path), "")], get_backend())

        assert read_email_cursor().outcome is PollStateOutcome.ABSENT
        assert "poll_state/email_cursor.txt" not in report.keys_copied
        assert report.errors == []

    def test_the_legacy_single_chat_key_is_not_carried_into_the_cloud(
        self, tmp_path: Path, fake_cloud_store
    ) -> None:
        """Nothing writes ``chat_id`` any more, so a copy is as old as the install. A
        cloud container may hold a stale one already, which is why catch_up.py reads
        this key from a local backend only."""
        from entrabot.storage.backend import get_backend

        report = migrate_local_to_backend([(self._local_install(tmp_path), "")], get_backend())

        assert "chat_id" not in report.keys_copied
        assert "chat_id" not in fake_cloud_store.data
        assert report.errors == []

    def test_per_chat_cursors_are_not_carried_into_the_cloud(
        self, tmp_path: Path, fake_cloud_store
    ) -> None:
        """A cursor copied from another agent's install would be rehydrated as PRESENT
        when two agents share a group chat. That skips the 24-hour first-sight gate and
        replays old messages. With no cursor a chat takes the first-sight path."""
        from entrabot.storage.backend import get_backend

        report = migrate_local_to_backend([(self._local_install(tmp_path), "")], get_backend())

        assert not [key for key in report.keys_copied if key.startswith("chat_cursors/")]
        assert not [key for key in fake_cloud_store.data if key.startswith("chat_cursors/")]
        for chat_id in self.CHATS:
            assert resolve_cursor(chat_id).outcome is CursorOutcome.ABSENT
        assert report.errors == []

    def test_exclusions_apply_only_at_the_container_root(self, tmp_path: Path) -> None:
        """The persona source can hold any file name; don't reach into its prefix."""
        persona = tmp_path / "persona"
        (persona / "poll_state").mkdir(parents=True)
        (persona / "chat_cursors").mkdir()
        (persona / "poll_state" / "email_cursor.txt").write_text("a note")
        (persona / "watched_chats").write_text("a list")
        (persona / "chat_id").write_text("an id")
        (persona / "chat_cursors" / "notes.json").write_text("{}")

        target = LocalBackend(tmp_path / "dst")
        migrate_local_to_backend([(persona, "claude_memory")], target)

        assert target.read_text("claude_memory/poll_state/email_cursor.txt") == "a note"
        assert target.read_text("claude_memory/watched_chats") == "a list"
        assert target.read_text("claude_memory/chat_id") == "an id"
        assert target.read_text("claude_memory/chat_cursors/notes.json") == "{}"

    def test_only_the_exact_keys_are_excluded(self, tmp_path: Path) -> None:
        """Names that merely look similar are ordinary data."""
        data = tmp_path / "data"
        (data / "chat_cursors_archive").mkdir(parents=True)
        (data / "chat_cursors_archive" / "old.json").write_text("{}")
        (data / "chat_id_notes.txt").write_text("notes")
        (data / "chat_cursors.bak").write_text("backup")

        target = LocalBackend(tmp_path / "dst")
        report = migrate_local_to_backend([(data, "")], target)

        assert sorted(report.keys_copied) == [
            "chat_cursors.bak",
            "chat_cursors_archive/old.json",
            "chat_id_notes.txt",
        ]


def test_migration_reads_local_files_as_utf8_on_any_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A UTF-8 local file reaches the target intact even where the default codec is cp1252.

    Issue #60: read with the Windows ANSI code page, an accent became mojibake in the cloud
    copy and an emoji's bytes could fail to decode, which dropped the file from the migration.
    """
    from tests.locale_codec import force_locale_codec

    force_locale_codec(monkeypatch)
    content = "<h2>🎯 Needs you</h2><p>José — café ✅</p>\n"
    src = tmp_path / "src"
    (src / "summaries").mkdir(parents=True)
    (src / "summaries" / "2026-04-17.html").write_bytes(content.encode())
    target = LocalBackend(tmp_path / "dst")

    report = migrate_local_to_backend([(src, "")], target)

    assert report.errors == []
    assert target.read_text("summaries/2026-04-17.html") == content


def test_migration_writes_exact_bytes_and_normalises_old_crlf_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What lands in the target is the text itself, with ``"\\n"`` line endings.

    A local store written on Windows before writes were byte-exact has CRLF in it. Reading
    stays tolerant of that, and the copy must not gain a second ``"\\r"`` on the way out.
    """
    from tests.locale_codec import force_windows_newlines

    force_windows_newlines(monkeypatch)
    src = tmp_path / "src"
    (src / "interactions").mkdir(parents=True)
    (src / "interactions" / "old.jsonl").write_bytes(b'{"n": 1}\r\n{"n": 2}\r\n')
    (src / "interactions" / "new.jsonl").write_bytes(b'{"n": 3}\n')
    target = LocalBackend(tmp_path / "dst")

    report = migrate_local_to_backend([(src, "")], target)

    assert report.errors == []
    assert (tmp_path / "dst" / "interactions" / "old.jsonl").read_bytes() == b'{"n": 1}\n{"n": 2}\n'
    assert (tmp_path / "dst" / "interactions" / "new.jsonl").read_bytes() == b'{"n": 3}\n'
