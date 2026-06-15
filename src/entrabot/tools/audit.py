"""Audit event logging.

Every agent action that touches a resource emits an audit event
BEFORE the action proceeds. Events are appended to daily JSONL files
under ``~/.entrabot/audit/``.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from entrabot.config import get_config

logger = logging.getLogger("entrabot.tools.audit")


def _audit_dir() -> Path:
    """Return the audit directory, creating it lazily."""
    cfg = get_config()
    cfg.audit_dir.mkdir(parents=True, exist_ok=True)
    return cfg.audit_dir


def log_event(
    action: str,
    resource: str,
    outcome: str = "success",
    agent_id: str | None = None,
    metadata: dict | None = None,
    attribution_type: str = "agent",
) -> dict:
    """Write an audit event and return it as a dict.

    If *agent_id* is not provided, the active identity state, configured Agent
    ID, configured Blueprint app ID, and finally the legacy credential-store
    key are tried in that order. Agent-attributed events fail closed when no
    active identity can be resolved; bootstrap/preflight callers that genuinely
    have no identity must pass ``attribution_type="none"``.

    *attribution_type* distinguishes agent actions from delegated-human actions:
    - ``"agent"`` — action performed as the Agent User identity
    - ``"delegated-human"`` — action performed using the human's delegated token
    - ``"none"`` — unauthenticated / unknown identity
    """
    if agent_id is None:
        # InsecureKeyringBackendError must NOT be silently swallowed — that
        # would convert the load-bearing fail-closed signal into a no-op
        # "unknown" agent attribution on every audit call.
        from entrabot.errors import AuditAttributionError, InsecureKeyringBackendError

        try:
            from entrabot.identity import get_active_identity_state
            from entrabot.models import IdentityState

            identity = get_active_identity_state()
            agent_id = (
                identity.session.user_id
                if identity and identity.session and identity.state == IdentityState.AGENT_USER
                else None
            )
        except Exception:
            agent_id = None

        if not agent_id:
            try:
                cfg = get_config()
                agent_id = cfg.agent_id or cfg.blueprint_app_id
            except Exception:
                agent_id = None

        try_credential_store = not agent_id
        if try_credential_store:
            from entrabot.platform import get_credential_store

            try:
                store = get_credential_store()
                agent_id = store.retrieve("entrabot", "active_client_id")
            except InsecureKeyringBackendError:
                raise
            except Exception:
                # Other failures (no entry, transport hiccup, no agent provisioned
                # yet) still mean attribution was not resolved. Agent-attributed
                # actions must fail closed rather than writing "unknown".
                agent_id = None

        if not agent_id:
            if attribution_type == "agent":
                raise AuditAttributionError(
                    action=action,
                    resource=resource,
                    reason="no agent identity resolvable from session, config, or credential store",
                )
            agent_id = "unknown"

    event = {
        "event_id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "agent_id": agent_id,
        "action": action,
        "resource": resource,
        "outcome": outcome,
        "attribution_type": attribution_type,
        "metadata": metadata or {},
    }

    audit_file = _audit_dir() / f"{datetime.now(UTC).strftime('%Y-%m-%d')}.jsonl"
    with open(audit_file, "a") as fh:
        fh.write(json.dumps(event) + "\n")

    logger.info(
        "audit: %s %s → %s",
        action,
        resource,
        outcome,
        extra={"agent_id": agent_id, "event_id": event["event_id"]},
    )
    return event
