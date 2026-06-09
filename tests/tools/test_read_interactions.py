"""Tests for read_interactions — chronological filter over the interaction log.

The body-side analogue of persona-sati's ``recall``: lets the model query
its own operational history (interaction_log) before speaking. Cheap, not
precious. Chronological + structured filters, no semantic scoring.

Storage path goes through MemoryBackend so BlobBackend works in cloud
mode. JSONL on-disk schema is the existing ``interactions/<day>.jsonl``
shape — this module is read-only and must not change writes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from entrabot.tools import interaction_log as il
from entrabot.tools.read_interactions import read_interactions


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    """Point storage at a temp directory and force LocalBackend."""
    monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ENTRABOT_BLOB_ENDPOINT", raising=False)
    monkeypatch.delenv("ENTRABOT_BLOB_CONTAINER", raising=False)
    monkeypatch.setenv("ENTRABOT_KEEP_MEMORY_LOCAL", "true")
    return tmp_path


def _log_at(ts: datetime, **kwargs) -> None:
    """Write one interaction at a specific UTC timestamp."""
    with patch.object(il, "_now", return_value=ts):
        il.log_interaction(**kwargs)


# ---------------------------------------------------------------------------
# Basic shape + default behavior
# ---------------------------------------------------------------------------
class TestReadInteractionsBasic:
    def test_empty_storage_returns_empty_list(self, tmp_data_dir: Path) -> None:
        assert read_interactions() == []

    def test_returns_list_of_dicts_in_original_schema(self, tmp_data_dir: Path) -> None:
        il.log_interaction(
            channel="teams_dm",
            direction="outbound",
            sender="agent@x.com",
            recipient="19:chat@unq.gbl.spaces",
            summary="hi",
            action="send_teams_message",
        )
        results = read_interactions()
        assert len(results) == 1
        entry = results[0]
        assert entry["channel"] == "teams_dm"
        assert entry["direction"] == "outbound"
        assert entry["sender"] == "agent@x.com"
        assert entry["recipient"] == "19:chat@unq.gbl.spaces"
        assert entry["summary"] == "hi"
        assert entry["action"] == "send_teams_message"
        # Existing schema preserved — id + ts always present
        assert "id" in entry
        assert "ts" in entry

    def test_sort_order_is_most_recent_first(self, tmp_data_dir: Path) -> None:
        now = datetime.now(UTC)
        _log_at(
            now - timedelta(hours=3),
            channel="terminal",
            direction="inbound",
            sender="u",
            summary="oldest",
        )
        _log_at(
            now - timedelta(hours=2),
            channel="terminal",
            direction="inbound",
            sender="u",
            summary="middle",
        )
        _log_at(
            now - timedelta(hours=1),
            channel="terminal",
            direction="inbound",
            sender="u",
            summary="newest",
        )
        results = read_interactions()
        summaries = [e["summary"] for e in results]
        assert summaries == ["newest", "middle", "oldest"]


# ---------------------------------------------------------------------------
# limit
# ---------------------------------------------------------------------------
class TestLimit:
    def test_limit_default_is_10(self, tmp_data_dir: Path) -> None:
        now = datetime.now(UTC)
        for i in range(15):
            _log_at(
                now - timedelta(minutes=i),
                channel="terminal",
                direction="inbound",
                sender="u",
                summary=f"m{i}",
            )
        results = read_interactions()
        assert len(results) == 10
        # Most-recent-first, so m0..m9 (m0 is newest)
        assert [e["summary"] for e in results] == [f"m{i}" for i in range(10)]

    def test_limit_honored_when_explicit(self, tmp_data_dir: Path) -> None:
        now = datetime.now(UTC)
        for i in range(5):
            _log_at(
                now - timedelta(minutes=i),
                channel="terminal",
                direction="inbound",
                sender="u",
                summary=f"m{i}",
            )
        results = read_interactions(limit=2)
        assert len(results) == 2
        assert [e["summary"] for e in results] == ["m0", "m1"]

    def test_limit_zero_returns_empty(self, tmp_data_dir: Path) -> None:
        il.log_interaction(channel="terminal", direction="inbound", sender="u", summary="x")
        assert read_interactions(limit=0) == []


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
class TestChatIdFilter:
    def test_matches_outbound_recipient(self, tmp_data_dir: Path) -> None:
        il.log_interaction(
            channel="teams_group",
            direction="outbound",
            sender="agent",
            recipient="19:A@thread.v2",
            summary="to A",
        )
        il.log_interaction(
            channel="teams_group",
            direction="outbound",
            sender="agent",
            recipient="19:B@thread.v2",
            summary="to B",
        )
        results = read_interactions(chat_id="19:A@thread.v2")
        assert len(results) == 1
        assert results[0]["summary"] == "to A"

    def test_matches_inbound_metadata_chat_id(self, tmp_data_dir: Path) -> None:
        il.log_interaction(
            channel="teams_group",
            direction="inbound",
            sender="brandon@x.com",
            summary="from A",
            metadata={"chat_id": "19:A@thread.v2"},
        )
        il.log_interaction(
            channel="teams_group",
            direction="inbound",
            sender="brandon@x.com",
            summary="from B",
            metadata={"chat_id": "19:B@thread.v2"},
        )
        results = read_interactions(chat_id="19:A@thread.v2")
        assert len(results) == 1
        assert results[0]["summary"] == "from A"

    def test_matches_mixed_directions_for_same_chat(self, tmp_data_dir: Path) -> None:
        il.log_interaction(
            channel="teams_group",
            direction="outbound",
            sender="agent",
            recipient="19:A@thread.v2",
            summary="sent",
        )
        il.log_interaction(
            channel="teams_group",
            direction="inbound",
            sender="brandon@x.com",
            summary="received",
            metadata={"chat_id": "19:A@thread.v2"},
        )
        results = read_interactions(chat_id="19:A@thread.v2")
        assert {e["summary"] for e in results} == {"sent", "received"}


class TestSenderFilter:
    def test_matches_exact_sender(self, tmp_data_dir: Path) -> None:
        il.log_interaction(channel="email", direction="inbound", sender="alice@x.com", summary="a")
        il.log_interaction(channel="email", direction="inbound", sender="bob@x.com", summary="b")
        results = read_interactions(sender="alice@x.com")
        assert len(results) == 1
        assert results[0]["summary"] == "a"

    def test_sender_match_is_case_insensitive(self, tmp_data_dir: Path) -> None:
        il.log_interaction(channel="email", direction="inbound", sender="Alice@X.com", summary="a")
        results = read_interactions(sender="alice@x.com")
        assert len(results) == 1
        assert results[0]["summary"] == "a"


class TestActionFilter:
    def test_matches_exact_action(self, tmp_data_dir: Path) -> None:
        il.log_interaction(
            channel="teams_dm",
            direction="outbound",
            sender="agent",
            summary="m1",
            action="send_teams_message",
        )
        il.log_interaction(
            channel="teams_dm",
            direction="outbound",
            sender="agent",
            summary="m2",
            action="send_card",
        )
        results = read_interactions(action="send_card")
        assert len(results) == 1
        assert results[0]["summary"] == "m2"

    def test_action_filter_skips_entries_with_no_action(self, tmp_data_dir: Path) -> None:
        il.log_interaction(channel="terminal", direction="inbound", sender="u", summary="no action")
        il.log_interaction(
            channel="teams_dm",
            direction="outbound",
            sender="agent",
            summary="has action",
            action="send_teams_message",
        )
        results = read_interactions(action="send_teams_message")
        assert len(results) == 1
        assert results[0]["summary"] == "has action"


class TestDirectionFilter:
    def test_inbound_only(self, tmp_data_dir: Path) -> None:
        il.log_interaction(channel="terminal", direction="inbound", sender="u", summary="in")
        il.log_interaction(channel="terminal", direction="outbound", sender="agent", summary="out")
        results = read_interactions(direction="inbound")
        assert len(results) == 1
        assert results[0]["summary"] == "in"

    def test_outbound_only(self, tmp_data_dir: Path) -> None:
        il.log_interaction(channel="terminal", direction="inbound", sender="u", summary="in")
        il.log_interaction(channel="terminal", direction="outbound", sender="agent", summary="out")
        results = read_interactions(direction="outbound")
        assert len(results) == 1
        assert results[0]["summary"] == "out"

    def test_invalid_direction_raises(self, tmp_data_dir: Path) -> None:
        with pytest.raises(ValueError, match="direction"):
            read_interactions(direction="sideways")


# ---------------------------------------------------------------------------
# since — chronological window
# ---------------------------------------------------------------------------
class TestSinceFilter:
    def test_default_since_is_24h_ago(self, tmp_data_dir: Path) -> None:
        now = datetime.now(UTC)
        _log_at(
            now - timedelta(hours=2),
            channel="terminal",
            direction="inbound",
            sender="u",
            summary="recent",
        )
        _log_at(
            now - timedelta(hours=48),
            channel="terminal",
            direction="inbound",
            sender="u",
            summary="too old",
        )
        results = read_interactions()
        summaries = [e["summary"] for e in results]
        assert "recent" in summaries
        assert "too old" not in summaries

    def test_explicit_since_includes_older_entries(self, tmp_data_dir: Path) -> None:
        now = datetime.now(UTC)
        _log_at(
            now - timedelta(hours=48),
            channel="terminal",
            direction="inbound",
            sender="u",
            summary="48h ago",
        )
        since = (now - timedelta(hours=72)).isoformat()
        results = read_interactions(since=since)
        assert len(results) == 1
        assert results[0]["summary"] == "48h ago"

    def test_since_excludes_entries_at_or_before_cutoff(self, tmp_data_dir: Path) -> None:
        now = datetime.now(UTC)
        cutoff = now - timedelta(hours=2)
        _log_at(
            cutoff - timedelta(seconds=1),
            channel="terminal",
            direction="inbound",
            sender="u",
            summary="just before",
        )
        _log_at(
            cutoff + timedelta(seconds=1),
            channel="terminal",
            direction="inbound",
            sender="u",
            summary="just after",
        )
        results = read_interactions(since=cutoff.isoformat())
        summaries = [e["summary"] for e in results]
        assert "just after" in summaries
        assert "just before" not in summaries

    def test_day_boundary_crossover(self, tmp_data_dir: Path) -> None:
        """24h window must read today's AND yesterday's file."""
        now = datetime.now(UTC)
        # Force one entry 18h ago (definitely yesterday in UTC)
        _log_at(
            now - timedelta(hours=18),
            channel="terminal",
            direction="inbound",
            sender="u",
            summary="yesterday-ish",
        )
        _log_at(
            now - timedelta(minutes=5),
            channel="terminal",
            direction="inbound",
            sender="u",
            summary="today",
        )
        results = read_interactions()
        summaries = [e["summary"] for e in results]
        assert "today" in summaries
        assert "yesterday-ish" in summaries

    def test_seven_day_cap_does_not_scan_further(self, tmp_data_dir: Path, caplog) -> None:
        """Pass since=10d ago — we cap at 7 day files. 10d-old entry NOT returned."""
        now = datetime.now(UTC)
        _log_at(
            now - timedelta(days=10),
            channel="terminal",
            direction="inbound",
            sender="u",
            summary="too-old-cap",
        )
        _log_at(
            now - timedelta(days=3),
            channel="terminal",
            direction="inbound",
            sender="u",
            summary="within-cap",
        )
        since = (now - timedelta(days=10)).isoformat()
        results = read_interactions(since=since)
        summaries = [e["summary"] for e in results]
        assert "within-cap" in summaries
        assert "too-old-cap" not in summaries

    def test_invalid_since_raises(self, tmp_data_dir: Path) -> None:
        with pytest.raises(ValueError, match="since"):
            read_interactions(since="not-a-timestamp")


# ---------------------------------------------------------------------------
# Filter composition
# ---------------------------------------------------------------------------
class TestFilterComposition:
    def test_chat_id_plus_direction_plus_sender(self, tmp_data_dir: Path) -> None:
        il.log_interaction(
            channel="teams_group",
            direction="inbound",
            sender="brandon@x.com",
            summary="match",
            metadata={"chat_id": "19:A@thread.v2"},
        )
        il.log_interaction(
            channel="teams_group",
            direction="outbound",
            sender="agent",
            recipient="19:A@thread.v2",
            summary="wrong direction",
        )
        il.log_interaction(
            channel="teams_group",
            direction="inbound",
            sender="bob@x.com",
            summary="wrong sender",
            metadata={"chat_id": "19:A@thread.v2"},
        )
        il.log_interaction(
            channel="teams_group",
            direction="inbound",
            sender="brandon@x.com",
            summary="wrong chat",
            metadata={"chat_id": "19:B@thread.v2"},
        )
        results = read_interactions(
            chat_id="19:A@thread.v2",
            direction="inbound",
            sender="brandon@x.com",
        )
        assert len(results) == 1
        assert results[0]["summary"] == "match"

    def test_all_filters_plus_limit_plus_since(self, tmp_data_dir: Path) -> None:
        now = datetime.now(UTC)
        # 5 matching entries spread over 5 hours
        for i in range(5):
            _log_at(
                now - timedelta(hours=i),
                channel="teams_dm",
                direction="outbound",
                sender="agent",
                recipient="19:C@unq.gbl.spaces",
                summary=f"m{i}",
                action="send_teams_message",
            )
        # An older non-matching entry
        _log_at(
            now - timedelta(hours=10),
            channel="teams_dm",
            direction="inbound",
            sender="u",
            summary="no",
            metadata={"chat_id": "19:C@unq.gbl.spaces"},
        )
        results = read_interactions(
            chat_id="19:C@unq.gbl.spaces",
            direction="outbound",
            action="send_teams_message",
            since=(now - timedelta(hours=3)).isoformat(),
            limit=10,
        )
        # since cuts at 3h → entries 0,1,2 (3h-old is at cutoff, excluded)
        summaries = [e["summary"] for e in results]
        assert summaries == ["m0", "m1", "m2"]


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------
class TestResilience:
    def test_missing_day_file_is_handled_gracefully(self, tmp_data_dir: Path) -> None:
        """No entries today; only one yesterday's file — must not crash."""
        now = datetime.now(UTC)
        _log_at(
            now - timedelta(hours=18),
            channel="terminal",
            direction="inbound",
            sender="u",
            summary="y",
        )
        # Today's file may or may not exist depending on now; the only
        # invariant we need is that read_interactions doesn't choke on a
        # missing file.
        results = read_interactions()
        assert any(e["summary"] == "y" for e in results)

    def test_corrupt_line_is_skipped(self, tmp_data_dir: Path) -> None:
        il.log_interaction(channel="terminal", direction="inbound", sender="u", summary="good")
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        log_file = tmp_data_dir / "interactions" / f"{day}.jsonl"
        with open(log_file, "a") as fh:
            fh.write("not-json\n")
        il.log_interaction(channel="terminal", direction="inbound", sender="u", summary="after")
        results = read_interactions()
        summaries = [e["summary"] for e in results]
        assert "good" in summaries
        assert "after" in summaries
        assert "not-json" not in str(results)
