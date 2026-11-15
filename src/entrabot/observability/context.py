from __future__ import annotations

import socket
from dataclasses import dataclass, field

from microsoft.opentelemetry.a365.core import (
    AgentDetails,
    BaggageBuilder,
    CallerDetails,
    Channel,
    InvokeAgentScope,
    InvokeAgentScopeDetails,
    Request,
    ServiceEndpoint,
    UserDetails,
)

from entrabot.config import EntraBotConfig

SUPPRESSED_CONTENT = "[content suppressed]"


@dataclass
class InvocationContext:
    """Detached A365 invocation scope retained across Copilot callbacks.

    Copilot may dispatch lifecycle callbacks in different ``contextvars``
    contexts. Future child spans must use ``scope.get_context()`` or explicit
    parent ``SpanDetails`` rather than relying on a long-lived attached context.
    """

    scope: InvokeAgentScope
    _finished: bool = field(default=False, init=False, repr=False)


def _missing_required_fields(
    *,
    config: EntraBotConfig,
    agent_name: str,
    conversation_id: str | None,
    channel: str,
) -> list[str]:
    required = (
        ("tenant_id", config.tenant_id),
        ("agent_id", config.agent_id),
        ("blueprint_app_id", config.blueprint_app_id),
        ("agent_user_id", config.agent_user_id),
        ("agent_name", agent_name),
        ("conversation_id", conversation_id),
        ("channel", channel),
    )
    return [name for name, value in required if value is None or not value.strip()]


def start_invocation(
    *,
    config: EntraBotConfig,
    agent_name: str,
    session_id: str | None,
    conversation_id: str | None,
    channel: str,
    caller_id: str | None,
) -> InvocationContext | None:
    """Start an A365 invocation with content-safe request metadata."""
    if not config.a365_observability_enabled:
        return None

    missing = _missing_required_fields(
        config=config,
        agent_name=agent_name,
        conversation_id=conversation_id,
        channel=channel,
    )
    if missing:
        raise ValueError(f"Missing required A365 invocation fields: {', '.join(missing)}")

    # Values were validated above; these assignments retain precise runtime validation
    # without weakening the public config type.
    tenant_id = config.tenant_id
    agent_id = config.agent_id
    blueprint_app_id = config.blueprint_app_id
    agent_user_id = config.agent_user_id
    resolved_caller_id = (caller_id or "").strip() or (config.human_user_id or "").strip() or None
    assert tenant_id is not None
    assert agent_id is not None
    assert blueprint_app_id is not None
    assert agent_user_id is not None
    assert conversation_id is not None

    hostname = socket.gethostname()
    client_ip = "0.0.0.0" if channel.casefold() in {"teams", "msteams"} else "127.0.0.1"
    agent_details = AgentDetails(
        agent_id=agent_id,
        agent_name=agent_name,
        tenant_id=tenant_id,
        agent_blueprint_id=blueprint_app_id,
        agentic_user_id=agent_user_id,
        agentic_user_email=config.agent_user_upn,
    )
    request = Request(
        content=SUPPRESSED_CONTENT,
        session_id=session_id,
        conversation_id=conversation_id,
        channel=Channel(name=channel),
    )
    # CLI and scheduled invocations may have no known human. The SDK permits
    # omitted caller details; the required agent identity still attributes the action.
    caller_details = None
    if resolved_caller_id is not None:
        caller_details = CallerDetails(
            user_details=UserDetails(
                user_id=resolved_caller_id,
                user_email=None,
                user_client_ip=client_ip,
            )
        )
    scope_details = InvokeAgentScopeDetails(endpoint=ServiceEndpoint(hostname=hostname, port=0))
    baggage = (
        BaggageBuilder()
        .tenant_id(tenant_id)
        .agent_id(agent_id)
        .agent_name(agent_name)
        .agent_blueprint_id(blueprint_app_id)
        .agentic_user_id(agent_user_id)
        .agentic_user_email(config.agent_user_upn)
        .conversation_id(conversation_id)
        .session_id(session_id)
        .channel_name(channel)
        .invoke_agent_server(hostname, 0)
    )
    if resolved_caller_id is not None:
        baggage.user_id(resolved_caller_id).user_client_ip(client_ip)

    # BaggageSpanProcessor copies baggage into the invocation span when it is
    # created, so baggage must detach before this callback returns.
    with baggage.build():
        scope = InvokeAgentScope.start(
            request,
            scope_details,
            agent_details,
            caller_details,
        )

    return InvocationContext(scope=scope)


def record_invocation_error(
    invocation: InvocationContext | None,
    error: Exception,
) -> None:
    """Record failure without exporting provider messages, traceback, or chained exceptions."""
    if invocation is not None:
        invocation.scope.record_error(RuntimeError("Agent invocation failed (details suppressed)"))


def finish_invocation(invocation: InvocationContext | None) -> None:
    """Record a content-safe response and close an invocation once."""
    if invocation is None or invocation._finished:
        return

    invocation._finished = True
    try:
        invocation.scope.record_response(SUPPRESSED_CONTENT)
    finally:
        invocation.scope.dispose()
