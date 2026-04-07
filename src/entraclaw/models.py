"""Pydantic models for EntraClaw domain objects.

SECURITY: TokenResult.__repr__ is overridden so access_token,
refresh_token, and password values are NEVER exposed in logs or
debug output.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field


class AgentIdentity(BaseModel):
    """Represents a bootstrapped agent identity in Entra ID."""

    agent_id: str
    client_id: str
    tenant_id: str
    object_id: str
    display_name: str = "EntraClaw Agent"


class TokenResult(BaseModel):
    """Wraps a token response. Secrets are REDACTED in repr/str."""

    access_token: str
    refresh_token: str | None = None
    expires_in: int = 3600
    scopes: list[str] = Field(default_factory=list)
    token_type: str = "Bearer"

    def __repr__(self) -> str:
        return (
            f"TokenResult(access_token='***REDACTED***', "
            f"refresh_token='***REDACTED***', "
            f"expires_in={self.expires_in}, "
            f"scopes={self.scopes!r}, "
            f"token_type={self.token_type!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()


class BlueprintCredentials(BaseModel):
    """Credentials for the Agent Identity Blueprint. Secrets are REDACTED in repr/str."""

    blueprint_app_id: str
    blueprint_secret: str
    tenant_id: str
    agent_id: str | None = None

    def __repr__(self) -> str:
        return (
            f"BlueprintCredentials(blueprint_app_id={self.blueprint_app_id!r}, "
            f"blueprint_secret='***REDACTED***', "
            f"tenant_id={self.tenant_id!r}, "
            f"agent_id={self.agent_id!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()


class AuditEvent(BaseModel):
    """Immutable audit record for an agent action."""

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
    )
    agent_id: str
    action: str
    resource: str
    outcome: str = "pending"
    metadata: dict[str, str] = Field(default_factory=dict)


class TeamsChat(BaseModel):
    """A 1:1 Teams conversation between agent and human."""

    chat_id: str
    members: list[str] = Field(default_factory=list)
    created_at: str | None = None


class TeamsMessage(BaseModel):
    """A message sent in a Teams chat."""

    message_id: str
    chat_id: str
    content: str
    content_type: str = "text"
    sent_at: str | None = None
