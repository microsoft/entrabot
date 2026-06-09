"""Tests for chat_cursors — per-chat poll cursor persisted through MemoryBackend.

The background Teams poll keeps a per-chat cursor (last_ts, seen_ids_tail,
bootstrapped) in ``_state["watched_chats"][chat_id]`` so it can dedupe inbound
messages and detect "new since last poll." Before issue #17 that state lived
in-process only — on every MCP restart the bootstrap path re-fired "the
newest message at boot" as if it were fresh, surfacing days-old messages or
silently dropping messages that arrived during a server-down window.

This module persists that cursor through the same ``MemoryBackend`` protocol
that ``interaction_log.py`` / ``daily_summary.py`` use. One key per chat
(``chat_cursors/<chat_id>.json``) so a busy chat's write doesn't rewrite a
giant blob.

Storage destination: ``LocalBackend`` by default; ``BlobBackend`` when blob
env vars are set (ADR-005). Never persona-sati — this is operational state.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from entrabot.tools.chat_cursors import (
    CURSOR_STALENESS_SECONDS,
    MAX_SEEN_IDS_TAIL,
    bound_seen_ids,
    cursor_key,
    is_stale,
    load_cursor,
    save_cursor,
)


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ENTRABOT_BLOB_ENDPOINT", raising=False)
    monkeypatch.delenv("ENTRABOT_BLOB_CONTAINER", raising=False)
    monkeypatch.delenv("ENTRABOT_KEEP_MEMORY_LOCAL", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# cursor_key
# ---------------------------------------------------------------------------
class TestCursorKey:
    def test_key_is_namespaced_under_chat_cursors_prefix(self) -> None:
        key = cursor_key("19:abc@thread.v2")
        assert key.startswith("chat_cursors/")

    def test_key_includes_chat_id(self) -> None:
        key = cursor_key("19:abc@thread.v2")
        assert "19:abc@thread.v2" in key or "19%3Aabc%40thread.v2" in key

    def test_key_uses_json_extension(self) -> None:
        assert cursor_key("anything").endswith(".json")

    def test_distinct_chat_ids_produce_distinct_keys(self) -> None:
        assert cursor_key("a") != cursor_key("b")


# ---------------------------------------------------------------------------
# bound_seen_ids
# ---------------------------------------------------------------------------
class TestBoundSeenIds:
    def test_returns_empty_for_empty_input(self) -> None:
        assert bound_seen_ids([]) == []

    def test_returns_unchanged_when_under_cap(self) -> None:
        ids = [f"id-{i}" for i in range(10)]
        assert bound_seen_ids(ids) == ids

    def test_keeps_only_tail_when_over_cap(self) -> None:
        ids = [f"id-{i}" for i in range(MAX_SEEN_IDS_TAIL + 50)]
        bounded = bound_seen_ids(ids)
        assert len(bounded) == MAX_SEEN_IDS_TAIL
        # Tail — keeps most recent (last)
        assert bounded[-1] == f"id-{MAX_SEEN_IDS_TAIL + 49}"
        assert bounded[0] == f"id-{50}"

    def test_accepts_set_input(self) -> None:
        # seen_ids in _state is a set; the function must handle it.
        ids = {"a", "b", "c"}
        result = bound_seen_ids(ids)
        assert set(result) == ids


# ---------------------------------------------------------------------------
# load_cursor / save_cursor round-trip
# ---------------------------------------------------------------------------
class TestLoadSaveRoundtrip:
    def test_returns_none_when_no_cursor_file_present(self, tmp_data_dir) -> None:
        assert load_cursor("19:absent@thread.v2") is None

    def test_save_then_load_returns_the_persisted_state(self, tmp_data_dir) -> None:
        state = {
            "last_ts": "2026-06-09T18:59:15.261Z",
            "seen_ids_tail": ["1781031555261", "1781031555262"],
            "bootstrapped": True,
        }
        save_cursor("19:abc@thread.v2", state)
        loaded = load_cursor("19:abc@thread.v2")
        assert loaded is not None
        assert loaded["last_ts"] == "2026-06-09T18:59:15.261Z"
        assert loaded["seen_ids_tail"] == ["1781031555261", "1781031555262"]
        assert loaded["bootstrapped"] is True

    def test_save_stamps_last_written_at(self, tmp_data_dir) -> None:
        before = datetime.now(UTC)
        save_cursor(
            "19:abc@thread.v2",
            {"last_ts": "2026-06-09T18:59:15.261Z", "seen_ids_tail": [], "bootstrapped": True},
        )
        loaded = load_cursor("19:abc@thread.v2")
        assert loaded is not None
        assert "last_written_at" in loaded
        written = datetime.fromisoformat(loaded["last_written_at"].replace("Z", "+00:00"))
        # Within 5s — generous slack for slow CI
        assert (written - before).total_seconds() < 5

    def test_save_bounds_seen_ids_tail_on_write(self, tmp_data_dir) -> None:
        oversized = [f"id-{i}" for i in range(MAX_SEEN_IDS_TAIL + 25)]
        save_cursor(
            "19:abc@thread.v2",
            {
                "last_ts": "2026-06-09T18:59:15.261Z",
                "seen_ids_tail": oversized,
                "bootstrapped": True,
            },
        )
        loaded = load_cursor("19:abc@thread.v2")
        assert loaded is not None
        assert len(loaded["seen_ids_tail"]) == MAX_SEEN_IDS_TAIL
        # Most-recent tail preserved
        assert loaded["seen_ids_tail"][-1] == f"id-{MAX_SEEN_IDS_TAIL + 24}"

    def test_save_accepts_set_for_seen_ids_tail(self, tmp_data_dir) -> None:
        save_cursor(
            "19:abc@thread.v2",
            {
                "last_ts": "2026-06-09T18:59:15.261Z",
                "seen_ids_tail": {"a", "b", "c"},
                "bootstrapped": True,
            },
        )
        loaded = load_cursor("19:abc@thread.v2")
        assert loaded is not None
        assert set(loaded["seen_ids_tail"]) == {"a", "b", "c"}

    def test_load_returns_none_on_corrupt_json(self, tmp_data_dir) -> None:
        from entrabot.storage.backend import get_backend

        backend = get_backend()
        backend.write_text(cursor_key("19:corrupt@thread.v2"), "{not-valid-json")
        # Corrupt cursor should not crash boot — return None and fall through
        # to the bootstrap path.
        assert load_cursor("19:corrupt@thread.v2") is None

    def test_save_overwrites_prior_cursor(self, tmp_data_dir) -> None:
        save_cursor(
            "19:abc@thread.v2",
            {"last_ts": "2026-06-09T18:59:15.261Z", "seen_ids_tail": ["x"], "bootstrapped": True},
        )
        save_cursor(
            "19:abc@thread.v2",
            {"last_ts": "2026-06-10T19:00:00.000Z", "seen_ids_tail": ["y"], "bootstrapped": True},
        )
        loaded = load_cursor("19:abc@thread.v2")
        assert loaded is not None
        assert loaded["last_ts"] == "2026-06-10T19:00:00.000Z"
        assert loaded["seen_ids_tail"] == ["y"]

    def test_per_chat_keys_do_not_cross_contaminate(self, tmp_data_dir) -> None:
        save_cursor(
            "chat-a",
            {"last_ts": "2026-06-09T18:00:00Z", "seen_ids_tail": ["a1"], "bootstrapped": True},
        )
        save_cursor(
            "chat-b",
            {"last_ts": "2026-06-09T19:00:00Z", "seen_ids_tail": ["b1"], "bootstrapped": True},
        )
        a = load_cursor("chat-a")
        b = load_cursor("chat-b")
        assert a is not None and b is not None
        assert a["seen_ids_tail"] == ["a1"]
        assert b["seen_ids_tail"] == ["b1"]


# ---------------------------------------------------------------------------
# is_stale
# ---------------------------------------------------------------------------
class TestIsStale:
    def test_fresh_cursor_within_cap_is_not_stale(self) -> None:
        recent = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert is_stale(recent) is False

    def test_old_cursor_beyond_cap_is_stale(self) -> None:
        old = (
            datetime.now(UTC) - timedelta(seconds=CURSOR_STALENESS_SECONDS + 60)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert is_stale(old) is True

    def test_none_or_empty_cursor_is_stale(self) -> None:
        assert is_stale(None) is True
        assert is_stale("") is True

    def test_malformed_timestamp_is_treated_as_stale(self) -> None:
        # Defensive: an unparseable timestamp must not crash the boot path.
        # Treat it as stale so we fall through to a fresh bootstrap.
        assert is_stale("not-a-timestamp") is True

    def test_subsecond_timestamp_parses(self) -> None:
        recent = (datetime.now(UTC) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        assert is_stale(recent) is False

    def test_at_cap_boundary_is_stale(self) -> None:
        # Exactly at the cap — treat as stale; better to bootstrap than to
        # surface a borderline-old message as live.
        at_cap = (
            datetime.now(UTC) - timedelta(seconds=CURSOR_STALENESS_SECONDS + 1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert is_stale(at_cap) is True

    def test_staleness_cap_is_24_hours(self) -> None:
        # Explicit assertion: 24h is the policy. If this constant changes,
        # the change should be deliberate and reflected in the issue body.
        assert CURSOR_STALENESS_SECONDS == 24 * 60 * 60


# ---------------------------------------------------------------------------
# Storage key shape — confirm the on-disk layout matches the issue spec
# ---------------------------------------------------------------------------
class TestOnDiskLayout:
    def test_cursor_written_under_chat_cursors_prefix(self, tmp_data_dir) -> None:
        save_cursor(
            "19:xyz@thread.v2",
            {"last_ts": "2026-06-09T18:00:00Z", "seen_ids_tail": [], "bootstrapped": True},
        )
        # The on-disk file should live under chat_cursors/ in the data dir.
        matches = list(tmp_data_dir.rglob("*"))
        cursor_files = [m for m in matches if m.is_file() and "chat_cursors" in str(m)]
        assert cursor_files, "expected a file under chat_cursors/ prefix"

    def test_payload_is_json(self, tmp_data_dir) -> None:
        save_cursor(
            "19:xyz@thread.v2",
            {"last_ts": "2026-06-09T18:00:00Z", "seen_ids_tail": ["x"], "bootstrapped": True},
        )
        matches = [
            m
            for m in tmp_data_dir.rglob("*")
            if m.is_file() and "chat_cursors" in str(m)
        ]
        assert matches
        payload = json.loads(matches[0].read_text())
        assert payload["last_ts"] == "2026-06-09T18:00:00Z"
