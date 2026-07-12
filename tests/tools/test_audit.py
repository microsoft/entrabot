"""Tests for audit event logging."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from entrabot.tools.audit import log_event


def _clear_identity_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENTRABOT_AGENT_ID", raising=False)
    monkeypatch.delenv("ENTRABOT_BLUEPRINT_APP_ID", raising=False)


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

    def test_missing_agent_id_from_store_raises_for_default_agent_attribution(
        self, audit_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Agent-attributed events must not silently degrade to unknown."""
        from entrabot.errors import AuditAttributionError

        _clear_identity_config(monkeypatch)
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
            pytest.raises(AuditAttributionError),
        ):
            log_event(action="x", resource="r")

        assert not list(audit_dir.glob("*.jsonl"))

    def test_missing_agent_id_from_store_raises_for_explicit_agent_attribution(
        self, audit_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit attribution_type='agent' has the same fail-closed behavior."""
        from entrabot.errors import AuditAttributionError

        _clear_identity_config(monkeypatch)
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
            pytest.raises(AuditAttributionError),
        ):
            log_event(action="x", resource="r", attribution_type="agent")

        assert not list(audit_dir.glob("*.jsonl"))

    def test_none_attribution_allows_unknown_agent_id(
        self, audit_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bootstrap/preflight callers must opt in to unknown attribution."""
        _clear_identity_config(monkeypatch)
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
            event = log_event(action="x", resource="r", attribution_type="none")

        assert event["agent_id"] == "unknown"
        assert event["attribution_type"] == "none"

    def test_uses_agent_id_from_credential_store(
        self, audit_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Existing happy path: a real active_client_id is recorded."""
        _clear_identity_config(monkeypatch)
        mock_store = type(
            "S",
            (),
            {
                "retrieve": staticmethod(lambda *_a: "agent-123"),
            },
        )()
        with (
            patch("entrabot.tools.audit._audit_dir", return_value=audit_dir),
            patch("entrabot.platform.get_credential_store", return_value=mock_store),
        ):
            event = log_event(action="x", resource="r")

        assert event["agent_id"] == "agent-123"

    @pytest.mark.asyncio
    async def test_delegated_identity_user_id_does_not_override_agent_config(
        self, audit_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Delegated user_id is a human OID; agent audit rows must use config."""
        from entrabot.identity import set_active_identity_state
        from entrabot.identity.state_machine import IdentityStateMachine
        from entrabot.models import IdentityState

        class Store:
            @staticmethod
            def retrieve(*_args: object) -> None:
                return None

        monkeypatch.setenv("ENTRABOT_AGENT_ID", "config-agent-id")
        monkeypatch.delenv("ENTRABOT_BLUEPRINT_APP_ID", raising=False)
        monkeypatch.setattr("entrabot.platform.get_credential_store", lambda: Store())
        identity = IdentityStateMachine()
        await identity.update_session(user_id="human-user-oid")
        await identity.transition(IdentityState.DELEGATED)
        set_active_identity_state(identity)

        with patch("entrabot.tools.audit._audit_dir", return_value=audit_dir):
            event = log_event(action="x", resource="r", attribution_type="agent")

        assert event["agent_id"] == "config-agent-id"

    @pytest.mark.asyncio
    async def test_unauthenticated_stale_user_id_does_not_override_agent_config(
        self, audit_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unauthenticated sessions can carry stale user_id; do not attribute to it."""
        from entrabot.identity import set_active_identity_state
        from entrabot.identity.state_machine import IdentityStateMachine

        class Store:
            @staticmethod
            def retrieve(*_args: object) -> None:
                return None

        monkeypatch.setenv("ENTRABOT_AGENT_ID", "config-agent-id")
        monkeypatch.delenv("ENTRABOT_BLUEPRINT_APP_ID", raising=False)
        monkeypatch.setattr("entrabot.platform.get_credential_store", lambda: Store())
        identity = IdentityStateMachine()
        await identity.update_session(user_id="stale-human-user-oid")
        set_active_identity_state(identity)

        with patch("entrabot.tools.audit._audit_dir", return_value=audit_dir):
            event = log_event(action="x", resource="r", attribution_type="agent")

        assert event["agent_id"] == "config-agent-id"

    def test_insecure_keyring_backend_error_propagates(
        self, audit_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the active backend is insecure, log_event MUST surface that —
        not silently swallow it into agent_id='unknown' and continue.

        Regression test for the audit fail-open path. Defense in depth: the
        very first place a misconfigured backend should be reported is the
        first audit call.
        """
        from entrabot.errors import InsecureKeyringBackendError

        _clear_identity_config(monkeypatch)

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

    def test_unrelated_credential_store_error_raises_for_agent_attribution(
        self, audit_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even non-security lookup misses must not hide agent attribution loss."""
        import keyring.errors

        from entrabot.errors import AuditAttributionError

        _clear_identity_config(monkeypatch)

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
            pytest.raises(AuditAttributionError),
        ):
            log_event(action="x", resource="r")

        assert not list(audit_dir.glob("*.jsonl"))
