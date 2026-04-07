"""Audit event logging.

Every agent action that touches a resource emits an audit event
BEFORE the action proceeds. Events are appended to daily JSONL files
under ``~/.entraclaw/audit/``.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from entraclaw.config import get_config

logger = logging.getLogger("entraclaw.tools.audit")


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
) -> dict:
    """Write an audit event and return it as a dict.

    If *agent_id* is not provided the active agent from the credential store
    is used (best-effort; falls back to ``"unknown"``).
    """
    if agent_id is None:
        try:
            from entraclaw.platform import get_credential_store

            store = get_credential_store()
            agent_id = store.retrieve("entraclaw", "active_client_id") or "unknown"
        except Exception:
            agent_id = "unknown"

    event = {
        "event_id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "agent_id": agent_id,
        "action": action,
        "resource": resource,
        "outcome": outcome,
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
