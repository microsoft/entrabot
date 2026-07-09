"""Tests for ``scripts/migrate_cursors_to_upn.py``.

The 2026-07-09 cursor-replay incident left 62 chat cursors sitting just
before self-authored messages that the pre-rename filter no longer caught.
This migration script bumps every cursor's ``last_ts`` past ``now`` and
populates ``seen_ids_tail`` with the most recent self-authored message IDs
so the fleet-safe channel poll's per-message idempotency layer will drop
any residual replays.

Contract:

* Idempotent: two runs land the same state as one.
* ``--dry-run``: prints planned changes, writes nothing.
* Only touches the operational cursor prefix (``chat_cursors/``).
* Skips cursors whose ``last_ts`` is already past ``now`` (already migrated).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Route the MemoryBackend to a per-test LocalBackend rooted at ``tmp_path``.

    The migration script uses ``get_backend()`` — same shape every other
    operational-storage caller uses — so pointing ``ENTRABOT_DATA_DIR`` at a
    tmp dir + clearing blob env keeps this test hermetic.
    """
    monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ENTRABOT_BLOB_ENDPOINT", raising=False)
    monkeypatch.delenv("ENTRABOT_BLOB_CONTAINER", raising=False)
    monkeypatch.delenv("ENTRABOT_KEEP_MEMORY_LOCAL", raising=False)
    return tmp_path


def _seed_cursor(chat_id: str, last_ts: str, seen_ids: list[str]) -> None:
    """Write a cursor blob directly through the same path save_cursor uses.

    Uses ``chat_cursors.save_cursor`` so the on-disk shape matches production
    exactly (``last_written_at`` included, ``seen_ids_tail`` bounded).
    """
    from entrabot.tools.chat_cursors import save_cursor

    save_cursor(
        chat_id,
        {"last_ts": last_ts, "seen_ids_tail": seen_ids, "bootstrapped": True},
    )


def _read_cursor(chat_id: str) -> dict:
    from entrabot.tools.chat_cursors import load_cursor

    cursor = load_cursor(chat_id)
    assert cursor is not None, f"expected cursor for {chat_id}"
    return cursor


# ---------------------------------------------------------------------------
# The migration entry point
# ---------------------------------------------------------------------------


def _import_migrate():
    """Import the script as a module so tests can call it in-process.

    Kept behind a helper so a missing script (before implementation) fails
    with a clear ImportError at test time — that's the expected first-red.
    """
    import importlib.util
    import sys

    script_path = (
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "migrate_cursors_to_upn.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migrate_cursors_to_upn", script_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["migrate_cursors_to_upn"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMigrationIdempotent:
    def test_migration_is_idempotent(self, _local_backend: Path) -> None:
        """Two runs of the migration produce identical persisted state.

        Load once, capture state, run again, compare. Any change on the second
        run means the migration is over-writing something it should have
        recognized as already-done.
        """
        old_ts = "2026-05-20T18:00:00Z"
        _seed_cursor("19:chatA@thread.v2", old_ts, ["m-old-1"])
        _seed_cursor("19:chatB@thread.v2", old_ts, ["m-old-2"])

        migrate = _import_migrate()
        migrate.run(dry_run=False)
        after_first = {
            cid: _read_cursor(cid)
            for cid in ["19:chatA@thread.v2", "19:chatB@thread.v2"]
        }

        migrate.run(dry_run=False)
        after_second = {
            cid: _read_cursor(cid)
            for cid in ["19:chatA@thread.v2", "19:chatB@thread.v2"]
        }

        # ``last_written_at`` is a wall-clock timestamp that ``save_cursor``
        # stamps unconditionally — exclude it from the equality check.
        for cid in after_first:
            after_first[cid].pop("last_written_at", None)
            after_second[cid].pop("last_written_at", None)
        assert after_first == after_second


class TestMigrationBumpsLastTs:
    def test_migration_bumps_last_ts_past_now(self, _local_backend: Path) -> None:
        """After migration, every cursor's ``last_ts`` >= start-of-test now.

        The whole point of the migration is to prevent the poll from surfacing
        stale self-authored messages as fresh inbound. Any cursor whose
        ``last_ts`` is still older than ``now`` after migration is a bug.
        """
        started = datetime.now(UTC)
        _seed_cursor("19:chatA@thread.v2", "2026-05-20T18:00:00Z", [])
        _seed_cursor("19:chatB@thread.v2", "2026-04-01T00:00:00Z", [])

        migrate = _import_migrate()
        migrate.run(dry_run=False)

        for cid in ["19:chatA@thread.v2", "19:chatB@thread.v2"]:
            cursor = _read_cursor(cid)
            new_ts = datetime.fromisoformat(
                cursor["last_ts"].replace("Z", "+00:00")
            )
            # Migration bumps to now; the >= is defensive against clock
            # granularity on machines where ``now`` and the write happen in
            # the same microsecond.
            assert new_ts >= started - timedelta(seconds=1), (
                f"{cid} last_ts={cursor['last_ts']} not bumped past "
                f"started={started.isoformat()}"
            )


class TestMigrationPopulatesSeenIdsTail:
    def test_migration_populates_seen_ids_tail(
        self,
        _local_backend: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Recent self-authored message IDs are merged into ``seen_ids_tail``.

        The migration reads the most recent messages from Graph for each
        watched chat and adds the self-authored IDs to the persisted
        ``seen_ids_tail``. This is a belt-and-suspenders defense: even if a
        residual poll still surfaces one of these IDs as "new," the
        per-message cloud idempotency layer will see it in the tail and skip.

        Graph access is mocked — this test never touches the network.
        """
        _seed_cursor("19:chatA@thread.v2", "2026-05-20T18:00:00Z", [])

        # Mock the message-fetch surface the migration uses. It should return
        # the last N messages for the chat; only self-authored ones (by UPN)
        # count as seen-ids to persist.
        agent_upn = "entra-agent@werner.ac"

        def fake_recent_self_ids(
            chat_id: str, agent_upn: str, agent_object_id: str
        ) -> list[str]:
            assert chat_id == "19:chatA@thread.v2"
            return ["self-msg-1", "self-msg-2", "self-msg-3"]

        migrate = _import_migrate()
        monkeypatch.setattr(
            migrate, "recent_self_authored_ids", fake_recent_self_ids
        )
        migrate.run(
            dry_run=False,
            agent_upn=agent_upn,
            agent_object_id="agent-oid",
        )

        cursor = _read_cursor("19:chatA@thread.v2")
        for mid in ["self-msg-1", "self-msg-2", "self-msg-3"]:
            assert mid in cursor["seen_ids_tail"]


class TestMigrationDryRun:
    def test_migration_dry_run_makes_no_writes(
        self, _local_backend: Path
    ) -> None:
        """``--dry-run`` returns planned changes but writes nothing to disk."""
        old_ts = "2026-05-20T18:00:00Z"
        _seed_cursor("19:chatA@thread.v2", old_ts, ["m-old"])

        # Capture the pre-migration on-disk state.
        cursor_key = "chat_cursors/" + _url_quote("19:chatA@thread.v2") + ".json"
        cursor_path = _local_backend / cursor_key
        before = json.loads(cursor_path.read_text())

        migrate = _import_migrate()
        report = migrate.run(dry_run=True)

        after = json.loads(cursor_path.read_text())
        assert before == after, "dry-run must not modify persisted cursor"
        # The report surfaces what the live run WOULD do.
        assert report["inspected"] >= 1
        assert report["would_change"] >= 1


class TestMigrationScopedToCursorPrefix:
    def test_migration_ignores_non_cursor_keys(
        self, _local_backend: Path
    ) -> None:
        """Migration touches ``chat_cursors/`` only — never persona memory etc.

        Guard against a future refactor that widens the ``list()`` prefix and
        accidentally reformats persona-sati blobs or interaction logs.
        """
        from entrabot.storage.backend import get_backend

        _seed_cursor("19:chatA@thread.v2", "2026-05-20T18:00:00Z", [])

        # Seed an unrelated blob that must survive untouched.
        backend = get_backend()
        backend.write_text(
            "interactions/2026-07-09.jsonl",
            '{"sentinel": "do not touch"}\n',
        )
        backend.write_text(
            "claude_memory/some_note.md",
            "persona-sati owned — do not touch",
        )

        migrate = _import_migrate()
        migrate.run(dry_run=False)

        assert (
            backend.read_text("interactions/2026-07-09.jsonl")
            == '{"sentinel": "do not touch"}\n'
        )
        assert (
            backend.read_text("claude_memory/some_note.md")
            == "persona-sati owned — do not touch"
        )


class TestMigrationVerify:
    def test_verify_flags_unmigrated_cursors(
        self, _local_backend: Path
    ) -> None:
        """``--verify`` returns a report distinguishing migrated vs pending."""
        started = datetime.now(UTC)
        future = (started + timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        _seed_cursor("19:already@thread.v2", future, [])
        _seed_cursor("19:pending@thread.v2", "2026-04-01T00:00:00Z", [])

        migrate = _import_migrate()
        report = migrate.verify()

        assert report["migrated"] == 1
        assert report["pending"] == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _url_quote(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="")
