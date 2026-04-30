"""Tests for the interaction log — per-day JSONL of agent communications.

The interaction log captures every communication the agent sends or
receives across channels (Teams chat, Teams DM, email, terminal) so
the daily summary system can triage what the sponsor should know.

Log format:
- One JSONL file per UTC day: ``<data_dir>/interactions/YYYY-MM-DD.jsonl``
- Append-only; multiple background tasks may write concurrently
- Each entry has a stable schema (see TestInteractionSchema)

Channel detection:
- Chat IDs ending ``@unq.gbl.spaces`` → ``teams_dm`` (oneOnOne)
- Chat IDs ending ``@thread.v2`` → ``teams_group``
- Empty / None chat_id → ``terminal``
- Other → ``teams_unknown`` (new channel types default here)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from entraclaw.tools.interaction_log import (
    detect_channel,
    log_interaction,
    read_day,
)


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    """Point the interaction log at a temp directory for each test.

    EntraClawConfig is frozen, so we redirect by setting the env var
    that from_env() reads each call. get_config() has no caching.
    """
    monkeypatch.setenv("ENTRACLAW_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ENTRACLAW_BLOB_ENDPOINT", raising=False)
    monkeypatch.delenv("ENTRACLAW_BLOB_CONTAINER", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# detect_channel
# ---------------------------------------------------------------------------
class TestDetectChannel:
    def test_dm_chat_id(self) -> None:
        assert (
            detect_channel(
                "19:44444444-4444-4444-4444-444444444444_4d4a65ef-e9b3-4ec2-a1e2-b430a5855118@unq.gbl.spaces"
            )
            == "teams_dm"
        )

    def test_group_chat_id(self) -> None:
        assert detect_channel("19:4c8d47b5ea0b4177810fbdb1103ab013@thread.v2") == "teams_group"

    def test_none_is_terminal(self) -> None:
        assert detect_channel(None) == "terminal"

    def test_empty_is_terminal(self) -> None:
        assert detect_channel("") == "terminal"

    def test_unknown_pattern_is_teams_unknown(self) -> None:
        assert detect_channel("19:something@futureformat") == "teams_unknown"


# ---------------------------------------------------------------------------
# log_interaction — writing
# ---------------------------------------------------------------------------
class TestLogInteraction:
    def test_appends_entry_to_daily_file(self, tmp_data_dir: Path) -> None:
        log_interaction(
            channel="teams_dm",
            direction="outbound",
            sender="entraclaw-agent@werner.ac",
            recipient="19:xyz@unq.gbl.spaces",
            summary="Sent Brandon the phase plan",
            action="send_teams_message",
            content_ref="msg-123",
        )
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        log_file = tmp_data_dir / "interactions" / f"{day}.jsonl"
        assert log_file.exists()
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["channel"] == "teams_dm"
        assert entry["direction"] == "outbound"
        assert entry["sender"] == "entraclaw-agent@werner.ac"
        assert entry["summary"] == "Sent Brandon the phase plan"

    def test_creates_directory(self, tmp_data_dir: Path) -> None:
        # interactions/ should not exist yet
        assert not (tmp_data_dir / "interactions").exists()
        log_interaction(
            channel="terminal",
            direction="inbound",
            sender="user",
            summary="Asked about Phase 1",
        )
        assert (tmp_data_dir / "interactions").exists()

    def test_multiple_entries_same_day_append(self, tmp_data_dir: Path) -> None:
        for i in range(3):
            log_interaction(
                channel="email",
                direction="inbound",
                sender=f"s{i}@microsoft.com",
                summary=f"Message {i}",
            )
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        lines = (tmp_data_dir / "interactions" / f"{day}.jsonl").read_text().strip().splitlines()
        assert len(lines) == 3
        summaries = [json.loads(ln)["summary"] for ln in lines]
        assert summaries == ["Message 0", "Message 1", "Message 2"]

    def test_rotates_by_utc_date(self, tmp_data_dir: Path) -> None:
        # Simulate two calls on different UTC days by patching datetime
        from entraclaw.tools import interaction_log as il

        day1 = datetime(2026, 4, 16, 23, 45, tzinfo=UTC)
        day2 = datetime(2026, 4, 17, 0, 15, tzinfo=UTC)

        with patch.object(il, "_now", return_value=day1):
            log_interaction(channel="terminal", direction="inbound", sender="u", summary="late")
        with patch.object(il, "_now", return_value=day2):
            log_interaction(channel="terminal", direction="inbound", sender="u", summary="early")

        f1 = tmp_data_dir / "interactions" / "2026-04-16.jsonl"
        f2 = tmp_data_dir / "interactions" / "2026-04-17.jsonl"
        assert f1.exists() and f2.exists()
        assert json.loads(f1.read_text().strip())["summary"] == "late"
        assert json.loads(f2.read_text().strip())["summary"] == "early"

    def test_channel_required(self, tmp_data_dir: Path) -> None:
        with pytest.raises(ValueError, match="channel"):
            log_interaction(
                channel="",
                direction="outbound",
                sender="me",
                summary="x",
            )

    def test_direction_must_be_valid(self, tmp_data_dir: Path) -> None:
        with pytest.raises(ValueError, match="direction"):
            log_interaction(
                channel="terminal",
                direction="sideways",
                sender="me",
                summary="x",
            )

    def test_summary_required(self, tmp_data_dir: Path) -> None:
        with pytest.raises(ValueError, match="summary"):
            log_interaction(
                channel="terminal",
                direction="inbound",
                sender="me",
                summary="",
            )


# ---------------------------------------------------------------------------
# log_interaction — schema
# ---------------------------------------------------------------------------
class TestInteractionSchema:
    def test_entry_has_required_fields(self, tmp_data_dir: Path) -> None:
        log_interaction(
            channel="teams_dm",
            direction="outbound",
            sender="agent",
            summary="hi",
        )
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        entry = json.loads((tmp_data_dir / "interactions" / f"{day}.jsonl").read_text().strip())
        # Required fields
        assert "id" in entry
        assert "ts" in entry
        assert "channel" in entry
        assert "direction" in entry
        assert "sender" in entry
        assert "summary" in entry
        # ISO 8601 UTC
        assert entry["ts"].endswith("+00:00") or entry["ts"].endswith("Z")

    def test_optional_fields_preserved(self, tmp_data_dir: Path) -> None:
        log_interaction(
            channel="email",
            direction="inbound",
            sender="diana.smetters@microsoft.com",
            recipient="entraclaw-agent@werner.ac",
            summary="Re: Project Apollo",
            action="noted",
            content_ref="AAMk...message-id",
            metadata={"subject": "Re: Project Apollo", "conversationId": "AAQk..."},
        )
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        entry = json.loads((tmp_data_dir / "interactions" / f"{day}.jsonl").read_text().strip())
        assert entry["recipient"] == "entraclaw-agent@werner.ac"
        assert entry["action"] == "noted"
        assert entry["content_ref"] == "AAMk...message-id"
        assert entry["metadata"]["subject"] == "Re: Project Apollo"

    def test_ids_are_unique(self, tmp_data_dir: Path) -> None:
        for _ in range(5):
            log_interaction(channel="terminal", direction="inbound", sender="u", summary="x")
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        lines = (tmp_data_dir / "interactions" / f"{day}.jsonl").read_text().strip().splitlines()
        ids = [json.loads(ln)["id"] for ln in lines]
        assert len(set(ids)) == len(ids)


# ---------------------------------------------------------------------------
# read_day
# ---------------------------------------------------------------------------
class TestReadDay:
    def test_returns_entries_for_today(self, tmp_data_dir: Path) -> None:
        log_interaction(channel="teams_dm", direction="outbound", sender="a", summary="one")
        log_interaction(channel="email", direction="inbound", sender="b", summary="two")
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        entries = read_day(day)
        assert len(entries) == 2
        assert entries[0]["summary"] == "one"
        assert entries[1]["summary"] == "two"

    def test_missing_day_returns_empty(self, tmp_data_dir: Path) -> None:
        assert read_day("1999-01-01") == []

    def test_explicit_date_string(self, tmp_data_dir: Path) -> None:
        from entraclaw.tools import interaction_log as il

        fixed = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
        with patch.object(il, "_now", return_value=fixed):
            log_interaction(channel="terminal", direction="inbound", sender="u", summary="hi")
        entries = read_day("2026-04-10")
        assert len(entries) == 1
        assert entries[0]["summary"] == "hi"

    def test_corrupt_line_skipped(self, tmp_data_dir: Path) -> None:
        """Tolerate a partial write or manual edit without losing other entries."""
        log_interaction(channel="terminal", direction="inbound", sender="u", summary="good")
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        log_file = tmp_data_dir / "interactions" / f"{day}.jsonl"
        # Inject a corrupt line
        with open(log_file, "a") as fh:
            fh.write("this is not json\n")
        log_interaction(channel="terminal", direction="outbound", sender="a", summary="after")

        entries = read_day(day)
        summaries = [e["summary"] for e in entries]
        assert "good" in summaries
        assert "after" in summaries
        assert len(entries) == 2  # corrupt line skipped
