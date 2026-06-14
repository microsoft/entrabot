"""Tests for audit event logging."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from entrabot.tools.audit import log_event


@pytest.fixture
def audit_dir(tmp_path: Path) -> Path:
    """Override the audit directory to a temp location."""
    d = tmp_path / "audit"
    d.mkdir()
    return d


class TestAuditLogEvent:
    def test_creates_event_with_required_fields(self, audit_dir: Path) -> None:
        with patch("entrabot.tools.audit._audit_dir", return_value=audit_dir):
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
        with patch("entrabot.tools.audit._audit_dir", return_value=audit_dir):
            log_event(action="test", resource="r", agent_id="a")

        files = list(audit_dir.glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["action"] == "test"

    def test_appends_multiple_events(self, audit_dir: Path) -> None:
        with patch("entrabot.tools.audit._audit_dir", return_value=audit_dir):
            log_event(action="a1", resource="r1", agent_id="a")
            log_event(action="a2", resource="r2", agent_id="a")

        files = list(audit_dir.glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 2

    def test_custom_outcome(self, audit_dir: Path) -> None:
        with patch("entrabot.tools.audit._audit_dir", return_value=audit_dir):
            event = log_event(action="x", resource="r", outcome="failure", agent_id="a")
        assert event["outcome"] == "failure"

    def test_metadata(self, audit_dir: Path) -> None:
        with patch("entrabot.tools.audit._audit_dir", return_value=audit_dir):
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
            patch("entrabot.tools.audit._audit_dir", return_value=audit_dir),
            patch("entrabot.platform.get_credential_store", return_value=mock_store),
        ):
            event = log_event(action="x", resource="r")
        assert event["agent_id"] == "unknown"

    def test_insecure_keyring_backend_error_propagates(self, audit_dir: Path) -> None:
        """If the active backend is insecure, log_event MUST surface that —
        not silently swallow it into agent_id='unknown' and continue.

        Regression test for the audit fail-open path. Defense in depth: the
        very first place a misconfigured backend should be reported is the
        first audit call.
        """
        from entrabot.errors import InsecureKeyringBackendError

        def raise_insecure() -> None:
            raise InsecureKeyringBackendError(
                "keyrings.alt.file.PlaintextKeyring",
                ("keyring.backends.macOS.Keyring",),
            )

        with (
            patch("entrabot.tools.audit._audit_dir", return_value=audit_dir),
            patch("entrabot.platform.get_credential_store", side_effect=raise_insecure),
            pytest.raises(InsecureKeyringBackendError),
        ):
            log_event(action="x", resource="r")

        # And the audit file must NOT have been written
        assert not list(audit_dir.glob("*.jsonl"))

    def test_unrelated_credential_store_error_falls_back_to_unknown(
        self, audit_dir: Path
    ) -> None:
        """Non-security errors during credential lookup still fall back to
        'unknown' so the audit record is preserved.

        Example: no agent has been provisioned yet (KeyringError on retrieve).
        """
        import keyring.errors

        def raise_unrelated() -> object:
            class _Store:
                @staticmethod
                def retrieve(*_a: object) -> None:
                    raise keyring.errors.KeyringError("no entry")

            return _Store()

        with (
            patch("entrabot.tools.audit._audit_dir", return_value=audit_dir),
            patch(
                "entrabot.platform.get_credential_store",
                side_effect=raise_unrelated,
            ),
        ):
            event = log_event(action="x", resource="r")
        assert event["agent_id"] == "unknown"
