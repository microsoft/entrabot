"""Tests for bootstrap_body_state — the body-side counterpart to bootstrap_session.

Returns a single packet with today's interaction counts, the most active
chats, all open promises, cursor freshness, and watched chat count. The
goal is "index, not content" — full message content stays in
``read_interactions``; this is what lands in the model's bootstrap turn.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from entrabot.tools import chat_cursors
from entrabot.tools import interaction_log as il
from entrabot.tools import promises as pr
from entrabot.tools.body_bootstrap import bootstrap_body_state


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    """Point storage at a temp dir and force LocalBackend."""
    monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ENTRABOT_BLOB_ENDPOINT", raising=False)
    monkeypatch.delenv("ENTRABOT_BLOB_CONTAINER", raising=False)
    monkeypatch.setenv("ENTRABOT_KEEP_MEMORY_LOCAL", "true")
    return tmp_path


def _log_at(ts: datetime, **kwargs) -> None:
    with patch.object(il, "_now", return_value=ts):
        il.log_interaction(**kwargs)


# ---------------------------------------------------------------------------
# Empty / sensible defaults
# ---------------------------------------------------------------------------
class TestEmptyState:
    def test_empty_storage_returns_zero_counts(self, tmp_data_dir: Path) -> None:
        result = bootstrap_body_state()
        assert result["today_counts"]["total"] == 0
        assert result["today_counts"]["inbound"] == 0
        assert result["today_counts"]["outbound"] == 0
        assert result["today_counts"]["by_action"] == {}
        assert result["today_counts"]["by_channel"] == {}
        assert result["top_chats_today"] == []
        assert result["open_promises"] == []
        assert result["watched_chat_count"] == 0

    def test_cursor_freshness_zero_when_no_cursors(self, tmp_data_dir: Path) -> None:
        result = bootstrap_body_state()
        cf = result["cursor_freshness"]
        assert cf["watched_chat_count"] == 0
        assert cf["cursors_present"] == 0
        assert cf["cursors_stale"] == 0
        assert cf["oldest_cursor_ts"] is None
        assert cf["newest_cursor_ts"] is None

    def test_generated_at_is_iso_utc(self, tmp_data_dir: Path) -> None:
        result = bootstrap_body_state()
        assert "generated_at" in result
        # Must parse as ISO 8601
        datetime.fromisoformat(result["generated_at"].replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# today_counts
# ---------------------------------------------------------------------------
class TestTodayCounts:
    def test_counts_inbound_and_outbound(self, tmp_data_dir: Path) -> None:
        il.log_interaction(channel="terminal", direction="inbound", sender="u", summary="in1")
        il.log_interaction(channel="terminal", direction="inbound", sender="u", summary="in2")
        il.log_interaction(channel="terminal", direction="outbound", sender="agent", summary="out1")
        result = bootstrap_body_state()
        assert result["today_counts"]["total"] == 3
        assert result["today_counts"]["inbound"] == 2
        assert result["today_counts"]["outbound"] == 1

    def test_by_action_counts_actions(self, tmp_data_dir: Path) -> None:
        il.log_interaction(
            channel="teams_dm",
            direction="outbound",
            sender="agent",
            summary="s1",
            action="send_teams_message",
        )
        il.log_interaction(
            channel="teams_dm",
            direction="outbound",
            sender="agent",
            summary="s2",
            action="send_teams_message",
        )
        il.log_interaction(
            channel="teams_dm",
            direction="outbound",
            sender="agent",
            summary="c1",
            action="send_card",
        )
        result = bootstrap_body_state()
        assert result["today_counts"]["by_action"] == {
            "send_teams_message": 2,
            "send_card": 1,
        }

    def test_by_channel_counts_channels(self, tmp_data_dir: Path) -> None:
        il.log_interaction(channel="teams_dm", direction="outbound", sender="agent", summary="a")
        il.log_interaction(channel="teams_dm", direction="inbound", sender="u", summary="b")
        il.log_interaction(channel="email", direction="inbound", sender="u", summary="c")
        result = bootstrap_body_state()
        assert result["today_counts"]["by_channel"] == {
            "teams_dm": 2,
            "email": 1,
        }

    def test_yesterdays_entries_not_counted_as_today(self, tmp_data_dir: Path) -> None:
        now = datetime.now(UTC)
        _log_at(
            now - timedelta(hours=30),
            channel="terminal",
            direction="inbound",
            sender="u",
            summary="ancient",
        )
        il.log_interaction(channel="terminal", direction="inbound", sender="u", summary="now")
        result = bootstrap_body_state()
        assert result["today_counts"]["total"] == 1


# ---------------------------------------------------------------------------
# top_chats_today
# ---------------------------------------------------------------------------
class TestTopChatsToday:
    def test_sorts_by_interaction_count_desc(self, tmp_data_dir: Path) -> None:
        # Chat A: 3 interactions; Chat B: 2; Chat C: 1
        for _ in range(3):
            il.log_interaction(
                channel="teams_group",
                direction="outbound",
                sender="agent",
                recipient="19:A@thread.v2",
                summary="a",
            )
        for _ in range(2):
            il.log_interaction(
                channel="teams_group",
                direction="outbound",
                sender="agent",
                recipient="19:B@thread.v2",
                summary="b",
            )
        il.log_interaction(
            channel="teams_group",
            direction="outbound",
            sender="agent",
            recipient="19:C@thread.v2",
            summary="c",
        )
        result = bootstrap_body_state()
        ids = [c["chat_id"] for c in result["top_chats_today"]]
        assert ids == ["19:A@thread.v2", "19:B@thread.v2", "19:C@thread.v2"]
        counts = [c["interaction_count"] for c in result["top_chats_today"]]
        assert counts == [3, 2, 1]

    def test_returns_at_most_5(self, tmp_data_dir: Path) -> None:
        for i in range(8):
            il.log_interaction(
                channel="teams_group",
                direction="outbound",
                sender="agent",
                recipient=f"19:C{i}@thread.v2",
                summary=f"to {i}",
            )
        result = bootstrap_body_state()
        assert len(result["top_chats_today"]) == 5

    def test_includes_inbound_chats_via_metadata(self, tmp_data_dir: Path) -> None:
        il.log_interaction(
            channel="teams_group",
            direction="inbound",
            sender="brandon@x.com",
            summary="hi",
            metadata={"chat_id": "19:X@thread.v2"},
        )
        result = bootstrap_body_state()
        assert len(result["top_chats_today"]) == 1
        assert result["top_chats_today"][0]["chat_id"] == "19:X@thread.v2"

    def test_includes_last_activity_and_last_sender(self, tmp_data_dir: Path) -> None:
        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        earlier_ts = now - timedelta(minutes=10)
        latest_ts = now - timedelta(minutes=1)
        _log_at(
            earlier_ts,
            channel="teams_group",
            direction="outbound",
            sender="agent",
            recipient="19:A@thread.v2",
            summary="earlier",
        )
        _log_at(
            latest_ts,
            channel="teams_group",
            direction="inbound",
            sender="brandon@x.com",
            summary="latest",
            metadata={"chat_id": "19:A@thread.v2"},
        )
        # Keep "today" fixed so a CI run crossing UTC midnight cannot split
        # these two entries across different interaction-log files.
        with patch("entrabot.tools.body_bootstrap.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = now
            result = bootstrap_body_state()
        assert len(result["top_chats_today"]) == 1
        top = result["top_chats_today"][0]
        assert top["interaction_count"] == 2
        assert top["last_sender"] == "brandon@x.com"
        # last_activity must reflect the NEWER of the two entries, not
        # the earlier one — regression for the recency selection in
        # _top_chats.
        last = datetime.fromisoformat(top["last_activity"].replace("Z", "+00:00"))
        assert last == latest_ts
        assert last > earlier_ts

    def test_excludes_entries_with_no_chat_id(self, tmp_data_dir: Path) -> None:
        """Terminal sends have no chat_id and shouldn't pollute top_chats."""
        il.log_interaction(channel="terminal", direction="outbound", sender="agent", summary="cli")
        il.log_interaction(
            channel="teams_group",
            direction="outbound",
            sender="agent",
            recipient="19:X@thread.v2",
            summary="real",
        )
        result = bootstrap_body_state()
        assert len(result["top_chats_today"]) == 1
        assert result["top_chats_today"][0]["chat_id"] == "19:X@thread.v2"

    def test_ties_broken_by_recency(self, tmp_data_dir: Path) -> None:
        """Equal counts → most-recent activity wins."""
        now = datetime.now(UTC)
        # Chat A: one old entry
        _log_at(
            now - timedelta(hours=5),
            channel="teams_group",
            direction="outbound",
            sender="agent",
            recipient="19:A@thread.v2",
            summary="a",
        )
        # Chat B: one fresh entry
        _log_at(
            now - timedelta(minutes=1),
            channel="teams_group",
            direction="outbound",
            sender="agent",
            recipient="19:B@thread.v2",
            summary="b",
        )
        result = bootstrap_body_state()
        ids = [c["chat_id"] for c in result["top_chats_today"]]
        # Same count (1 each) → B is more recent → B first
        assert ids[0] == "19:B@thread.v2"


# ---------------------------------------------------------------------------
# open_promises — ALL open promises (no top-N cap)
# ---------------------------------------------------------------------------
class TestOpenPromises:
    def test_no_promises_returns_empty(self, tmp_data_dir: Path) -> None:
        result = bootstrap_body_state()
        assert result["open_promises"] == []

    def test_returns_all_open_promises(self, tmp_data_dir: Path) -> None:
        async def _seed() -> None:
            for i in range(12):
                await pr.add_promise(
                    chat_id=f"19:C{i}@thread.v2",
                    description=f"Do thing {i}",
                )

        asyncio.run(_seed())
        result = bootstrap_body_state()
        # ALL open promises, not top-N
        assert len(result["open_promises"]) == 12

    def test_promise_shape_has_required_fields(self, tmp_data_dir: Path) -> None:
        async def _seed() -> None:
            await pr.add_promise(
                chat_id="19:A@thread.v2",
                description="The description text here is fairly long to test preview",
                due_by="2026-12-31T00:00:00+00:00",
            )

        asyncio.run(_seed())
        result = bootstrap_body_state()
        assert len(result["open_promises"]) == 1
        p = result["open_promises"][0]
        assert "id" in p
        assert p["chat_id"] == "19:A@thread.v2"
        assert "description_preview" in p
        assert "created_at" in p
        assert p["due_by"] == "2026-12-31T00:00:00+00:00"

    def test_due_by_null_when_unset(self, tmp_data_dir: Path) -> None:
        async def _seed() -> None:
            await pr.add_promise(chat_id="19:A@thread.v2", description="d")

        asyncio.run(_seed())
        result = bootstrap_body_state()
        assert result["open_promises"][0]["due_by"] is None


# ---------------------------------------------------------------------------
# cursor_freshness
# ---------------------------------------------------------------------------
class TestCursorFreshness:
    def test_counts_present_cursors(self, tmp_data_dir: Path) -> None:
        recent = (datetime.now(UTC) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        chat_cursors.save_cursor(
            "19:A@thread.v2", {"last_ts": recent, "seen_ids_tail": [], "bootstrapped": True}
        )
        chat_cursors.save_cursor(
            "19:B@thread.v2", {"last_ts": recent, "seen_ids_tail": [], "bootstrapped": True}
        )
        result = bootstrap_body_state()
        cf = result["cursor_freshness"]
        assert cf["cursors_present"] == 2
        assert cf["cursors_stale"] == 0

    def test_distinguishes_stale_from_fresh(self, tmp_data_dir: Path) -> None:
        recent = (datetime.now(UTC) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        stale = (
            datetime.now(UTC) - timedelta(seconds=chat_cursors.CURSOR_STALENESS_SECONDS + 3600)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        chat_cursors.save_cursor(
            "19:fresh@thread.v2", {"last_ts": recent, "seen_ids_tail": [], "bootstrapped": True}
        )
        chat_cursors.save_cursor(
            "19:stale@thread.v2", {"last_ts": stale, "seen_ids_tail": [], "bootstrapped": True}
        )
        result = bootstrap_body_state()
        cf = result["cursor_freshness"]
        assert cf["cursors_present"] == 2
        assert cf["cursors_stale"] == 1

    def test_oldest_and_newest_cursor_ts(self, tmp_data_dir: Path) -> None:
        old = (datetime.now(UTC) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        new = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        chat_cursors.save_cursor(
            "19:A@thread.v2", {"last_ts": old, "seen_ids_tail": [], "bootstrapped": True}
        )
        chat_cursors.save_cursor(
            "19:B@thread.v2", {"last_ts": new, "seen_ids_tail": [], "bootstrapped": True}
        )
        result = bootstrap_body_state()
        cf = result["cursor_freshness"]
        assert cf["oldest_cursor_ts"] == old
        assert cf["newest_cursor_ts"] == new


# ---------------------------------------------------------------------------
# watched_chat_count
# ---------------------------------------------------------------------------
class TestWatchedChatCount:
    def test_counts_persisted_watched_chats(self, tmp_data_dir: Path) -> None:
        """watched_chat_count reads from the persisted watched_chats file."""
        (tmp_data_dir / "watched_chats").write_text(
            "19:A@thread.v2\n19:B@thread.v2\n19:C@unq.gbl.spaces\n"
        )
        result = bootstrap_body_state()
        assert result["watched_chat_count"] == 3

    def test_skips_blank_lines_in_watched_chats(self, tmp_data_dir: Path) -> None:
        (tmp_data_dir / "watched_chats").write_text("19:A@thread.v2\n\n19:B@thread.v2\n\n")
        result = bootstrap_body_state()
        assert result["watched_chat_count"] == 2


# ---------------------------------------------------------------------------
# Indexes only — no message content
# ---------------------------------------------------------------------------
class TestNoContentLeak:
    def test_message_summaries_not_in_payload(self, tmp_data_dir: Path) -> None:
        """Bootstrap is INDEX. Full summaries don't belong here."""
        unique = "AAAAA-SECRET-SENTINEL-VALUE-XYZ"
        il.log_interaction(
            channel="teams_dm",
            direction="outbound",
            sender="agent",
            recipient="19:X@unq.gbl.spaces",
            summary=unique,
            action="send_teams_message",
        )
        result = bootstrap_body_state()
        import json

        assert unique not in json.dumps(result)
