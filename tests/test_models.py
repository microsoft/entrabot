"""Tests for Pydantic domain models — especially token/credential redaction."""

from openclaw.models import (
    AgentCredentials,
    AgentIdentity,
    AuditEvent,
    TeamsChat,
    TeamsMessage,
    TokenResult,
)


class TestTokenRedaction:
    """TokenResult must NEVER expose secrets in repr or str."""

    def test_repr_redacts_access_token(self) -> None:
        t = TokenResult(access_token="super-secret-token-123")
        assert "super-secret-token-123" not in repr(t)
        assert "***REDACTED***" in repr(t)

    def test_str_redacts_access_token(self) -> None:
        t = TokenResult(access_token="super-secret-token-123")
        assert "super-secret-token-123" not in str(t)

    def test_repr_redacts_refresh_token(self) -> None:
        t = TokenResult(access_token="at", refresh_token="super-secret-refresh")
        assert "super-secret-refresh" not in repr(t)

    def test_access_token_still_accessible_via_field(self) -> None:
        t = TokenResult(access_token="my-token")
        assert t.access_token == "my-token"

    def test_f_string_redacts(self) -> None:
        t = TokenResult(access_token="secret")
        formatted = f"token = {t}"
        assert "secret" not in formatted


class TestAgentCredentials:
    """AgentCredentials must redact the password in repr/str."""

    def test_repr_redacts_password(self) -> None:
        creds = AgentCredentials(
            agent_upn="agent@example.com",
            agent_password="super-secret-pass",
            client_id="cid",
            tenant_id="tid",
        )
        assert "super-secret-pass" not in repr(creds)
        assert "***REDACTED***" in repr(creds)

    def test_str_redacts_password(self) -> None:
        creds = AgentCredentials(
            agent_upn="agent@example.com",
            agent_password="super-secret-pass",
            client_id="cid",
            tenant_id="tid",
        )
        assert "super-secret-pass" not in str(creds)

    def test_password_still_accessible_via_field(self) -> None:
        creds = AgentCredentials(
            agent_upn="a@e.com",
            agent_password="my-pass",
            client_id="c",
            tenant_id="t",
        )
        assert creds.agent_password == "my-pass"

    def test_f_string_redacts(self) -> None:
        creds = AgentCredentials(
            agent_upn="a@e.com",
            agent_password="secret",
            client_id="c",
            tenant_id="t",
        )
        formatted = f"creds = {creds}"
        assert "secret" not in formatted


class TestAgentIdentity:
    def test_defaults(self) -> None:
        ai = AgentIdentity(
            agent_id="a1",
            client_id="c1",
            tenant_id="t1",
            object_id="o1",
        )
        assert ai.display_name == "Openclaw Agent"

    def test_roundtrip(self) -> None:
        ai = AgentIdentity(
            agent_id="a1",
            client_id="c1",
            tenant_id="t1",
            object_id="o1",
            display_name="Custom",
        )
        data = ai.model_dump()
        rebuilt = AgentIdentity(**data)
        assert rebuilt == ai


class TestAuditEvent:
    def test_auto_fields(self) -> None:
        ev = AuditEvent(agent_id="a1", action="read", resource="/data")
        assert ev.event_id  # non-empty UUID string
        assert ev.timestamp  # non-empty ISO timestamp
        assert ev.outcome == "pending"

    def test_metadata_default_empty(self) -> None:
        ev = AuditEvent(agent_id="a", action="x", resource="r")
        assert ev.metadata == {}


class TestTeamsChat:
    def test_minimal(self) -> None:
        c = TeamsChat(chat_id="19:abc@thread.v2")
        assert c.chat_id == "19:abc@thread.v2"
        assert c.members == []
        assert c.created_at is None


class TestTeamsMessage:
    def test_defaults(self) -> None:
        m = TeamsMessage(message_id="m1", chat_id="c1", content="hello")
        assert m.content_type == "text"
        assert m.sent_at is None
