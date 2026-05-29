"""Tests for Pydantic domain models — especially token/credential redaction."""

from entrabot.models import (
    AgentIdentity,
    AuditEvent,
    BlueprintCredentials,
    IdentitySession,
    IdentityState,
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


class TestBlueprintCredentials:
    """BlueprintCredentials must redact the secret in repr/str."""

    def test_repr_redacts_secret(self) -> None:
        creds = BlueprintCredentials(
            blueprint_app_id="bp-app-id",
            blueprint_secret="super-secret-value",
            tenant_id="tid",
            agent_id="aid",
        )
        assert "super-secret-value" not in repr(creds)
        assert "***REDACTED***" in repr(creds)

    def test_str_redacts_secret(self) -> None:
        creds = BlueprintCredentials(
            blueprint_app_id="bp-app-id",
            blueprint_secret="super-secret-value",
            tenant_id="tid",
        )
        assert "super-secret-value" not in str(creds)

    def test_secret_still_accessible_via_field(self) -> None:
        creds = BlueprintCredentials(
            blueprint_app_id="bp",
            blueprint_secret="my-secret",
            tenant_id="t",
        )
        assert creds.blueprint_secret == "my-secret"

    def test_f_string_redacts(self) -> None:
        creds = BlueprintCredentials(
            blueprint_app_id="bp",
            blueprint_secret="my-super-secret-value",
            tenant_id="t",
        )
        formatted = f"creds = {creds}"
        assert "my-super-secret-value" not in formatted
        assert "***REDACTED***" in formatted

    def test_agent_id_optional(self) -> None:
        creds = BlueprintCredentials(
            blueprint_app_id="bp",
            blueprint_secret="s",
            tenant_id="t",
        )
        assert creds.agent_id is None


class TestAgentIdentity:
    def test_defaults(self) -> None:
        ai = AgentIdentity(
            agent_id="a1",
            client_id="c1",
            tenant_id="t1",
            object_id="o1",
        )
        assert ai.display_name == "EntraBot Agent"

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


class TestIdentityState:
    """IdentityState enum values and str behavior."""

    def test_enum_values(self) -> None:
        assert IdentityState.UNAUTHENTICATED == "unauthenticated"
        assert IdentityState.DELEGATED == "delegated"
        assert IdentityState.PROVISIONING == "provisioning"
        assert IdentityState.AGENT_USER == "agent_user"
        assert IdentityState.ERROR == "error"

    def test_is_str_enum(self) -> None:
        """IdentityState values can be used as plain strings."""
        state = IdentityState.DELEGATED
        assert isinstance(state, str)
        assert state == "delegated"


class TestIdentitySession:
    """IdentitySession defaults and token redaction."""

    def test_defaults(self) -> None:
        session = IdentitySession()
        assert session.state == IdentityState.UNAUTHENTICATED
        assert session.token is None
        assert session.token_acquired_at is None
        assert session.user_id is None
        assert session.display_name is None
        assert session.attribution_type == "none"
        assert session.auth_mode is None
        assert session.account_id is None
        assert session.tenant_id is None
        assert session.provisioning_state is None

    def test_repr_redacts_token(self) -> None:
        session = IdentitySession(token="super-secret-token-xyz")
        assert "super-secret-token-xyz" not in repr(session)
        assert "***REDACTED***" in repr(session)

    def test_str_redacts_token(self) -> None:
        session = IdentitySession(token="super-secret-token-xyz")
        assert "super-secret-token-xyz" not in str(session)
        assert "***REDACTED***" in str(session)

    def test_token_accessible_via_field(self) -> None:
        session = IdentitySession(token="my-token")
        assert session.token == "my-token"

    def test_f_string_redacts(self) -> None:
        session = IdentitySession(token="my-secret")
        formatted = f"session = {session}"
        assert "my-secret" not in formatted


class TestAuditEventAttribution:
    """AuditEvent attribution_type field."""

    def test_default_attribution_type(self) -> None:
        ev = AuditEvent(agent_id="a1", action="read", resource="/data")
        assert ev.attribution_type == "agent"

    def test_custom_attribution_type(self) -> None:
        ev = AuditEvent(
            agent_id="a1",
            action="read",
            resource="/data",
            attribution_type="delegated-human",
        )
        assert ev.attribution_type == "delegated-human"
