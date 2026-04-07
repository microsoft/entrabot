"""Tests for audit event logging."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from entraclaw.tools.audit import log_event


@pytest.fixture
def audit_dir(tmp_path: Path) -> Path:
    """Override the audit directory to a temp location."""
    d = tmp_path / "audit"
    d.mkdir()
    return d


class TestAuditLogEvent:
    def test_creates_event_with_required_fields(self, audit_dir: Path) -> None:
        with patch("entraclaw.tools.audit._audit_dir", return_value=audit_dir):
            event = log_event(
                action="graph_api_call",
                resource="/v1.0/chats",
                agent_id="test-agent",
            )
        assert event["action"] == "graph_api_call"
        assert event["resource"] == "/v1.0/chats"
        assert event["agent_id"] == "test-agent"
        assert event["outcome"] == "success"
        assert event["event_id"]
        assert event["timestamp"]

    def test_writes_jsonl_file(self, audit_dir: Path) -> None:
        with patch("entraclaw.tools.audit._audit_dir", return_value=audit_dir):
            log_event(action="test", resource="r", agent_id="a")

        files = list(audit_dir.glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["action"] == "test"

    def test_appends_multiple_events(self, audit_dir: Path) -> None:
        with patch("entraclaw.tools.audit._audit_dir", return_value=audit_dir):
            log_event(action="a1", resource="r1", agent_id="a")
            log_event(action="a2", resource="r2", agent_id="a")

        files = list(audit_dir.glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 2

    def test_custom_outcome(self, audit_dir: Path) -> None:
        with patch("entraclaw.tools.audit._audit_dir", return_value=audit_dir):
            event = log_event(action="x", resource="r", outcome="failure", agent_id="a")
        assert event["outcome"] == "failure"

    def test_metadata(self, audit_dir: Path) -> None:
        with patch("entraclaw.tools.audit._audit_dir", return_value=audit_dir):
            event = log_event(
                action="x",
                resource="r",
                agent_id="a",
                metadata={"key": "value"},
            )
        assert event["metadata"] == {"key": "value"}

    def test_fallback_agent_id(self, audit_dir: Path) -> None:
        """When no agent_id provided and no cached identity, falls back to 'unknown'."""
        mock_store = type(
            "S",
            (),
            {
                "retrieve": staticmethod(lambda *_a: None),
            },
        )()
        with (
            patch("entraclaw.tools.audit._audit_dir", return_value=audit_dir),
            patch("entraclaw.platform.get_credential_store", return_value=mock_store),
        ):
            event = log_event(action="x", resource="r")
        assert event["agent_id"] == "unknown"
