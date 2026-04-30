"""Tests for JSONL-based IPC handler between MCP server and Bot server."""

from __future__ import annotations

import json

import pytest

from entraclaw.bot import handler


@pytest.fixture(autouse=True)
def _override_bot_dir(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect BOT_DIR to a temp directory so tests never touch ~/.entraclaw/."""
    monkeypatch.setattr(handler, "BOT_DIR", tmp_path)


# ---------------------------------------------------------------------------
# Inbound (Teams → MCP server)
# ---------------------------------------------------------------------------


class TestWriteInbound:
    def test_creates_file(self, tmp_path: object) -> None:
        msg = {"message_id": "1", "from": "alice", "content": "hi", "sent_at": "now"}
        write_inbound(msg)
        assert (tmp_path / "inbound.jsonl").exists()

    def test_appends_multiple_messages(self, tmp_path: object) -> None:
        msg1 = {"message_id": "1", "from": "alice", "content": "hi", "sent_at": "t1"}
        msg2 = {"message_id": "2", "from": "bob", "content": "hey", "sent_at": "t2"}
        write_inbound(msg1)
        write_inbound(msg2)

        lines = (tmp_path / "inbound.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == msg1
        assert json.loads(lines[1]) == msg2


class TestReadInbound:
    def test_returns_messages(self, tmp_path: object) -> None:
        msg1 = {"message_id": "1", "from": "alice", "content": "hi", "sent_at": "t1"}
        msg2 = {"message_id": "2", "from": "bob", "content": "hey", "sent_at": "t2"}
        write_inbound(msg1)
        write_inbound(msg2)

        result = read_inbound()
        assert result == [msg1, msg2]

    def test_truncates_after_read(self, tmp_path: object) -> None:
        write_inbound({"message_id": "1", "from": "a", "content": "x", "sent_at": "t"})

        first = read_inbound()
        assert len(first) == 1

        second = read_inbound()
        assert second == []

    def test_missing_file_returns_empty(self) -> None:
        result = read_inbound()
        assert result == []

    def test_corrupted_line_skipped(
        self,
        tmp_path: object,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        fpath = tmp_path / "inbound.jsonl"
        valid = {"message_id": "1", "from": "a", "content": "ok", "sent_at": "t"}
        valid2 = {"message_id": "2", "from": "b", "content": "also ok", "sent_at": "t2"}
        fpath.write_text(json.dumps(valid) + "\n" + "NOT VALID JSON\n" + json.dumps(valid2) + "\n")

        result = read_inbound()
        assert len(result) == 2
        assert result[0] == valid
        assert result[1]["message_id"] == "2"
        assert any("Skipping corrupted" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Outbound (MCP server → Teams)
# ---------------------------------------------------------------------------


class TestWriteOutbound:
    def test_creates_file(self, tmp_path: object) -> None:
        msg = {"content": "hello", "chat_id": "c1"}
        write_outbound(msg)
        assert (tmp_path / "outbound.jsonl").exists()


class TestReadOutbound:
    def test_returns_messages(self, tmp_path: object) -> None:
        msg1 = {"content": "hello", "chat_id": "c1"}
        msg2 = {"content": "world"}
        write_outbound(msg1)
        write_outbound(msg2)

        result = read_outbound()
        assert result == [msg1, msg2]

    def test_truncates_after_read(self, tmp_path: object) -> None:
        write_outbound({"content": "hi"})

        first = read_outbound()
        assert len(first) == 1

        second = read_outbound()
        assert second == []

    def test_missing_file_returns_empty(self) -> None:
        result = read_outbound()
        assert result == []

    def test_corrupted_line_skipped(
        self,
        tmp_path: object,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        fpath = tmp_path / "outbound.jsonl"
        valid = {"content": "ok", "chat_id": "c1"}
        fpath.write_text(json.dumps(valid) + "\n" + "{broken\n")

        result = read_outbound()
        assert len(result) == 1
        assert result[0] == valid
        assert any("Skipping corrupted" in rec.message for rec in caplog.records)


# Convenience aliases so tests read naturally
write_inbound = handler.write_inbound
read_inbound = handler.read_inbound
write_outbound = handler.write_outbound
read_outbound = handler.read_outbound
