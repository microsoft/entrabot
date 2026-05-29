"""Per-day JSONL log of agent communications across all channels.

Every inbound and outbound message the agent handles — Teams chat,
Teams DM, email, terminal prompts — is recorded here. The daily
summary system (Phase 3) reads this log to triage what the sponsor
should know.

Format: one JSONL file per UTC day at
``<config.data_dir>/interactions/YYYY-MM-DD.jsonl``.

Schema (per entry):
- ``id``          — UUID4, unique per entry
- ``ts``          — ISO 8601 UTC timestamp
- ``channel``     — "teams_dm" | "teams_group" | "teams_unknown" | "email" | "terminal"
- ``direction``   — "inbound" | "outbound"
- ``sender``      — email or display name
- ``recipient``   — optional; chat_id, email, or None
- ``summary``     — short human-readable summary (caller-supplied)
- ``action``      — optional; what the agent did (e.g. "send_teams_message")
- ``content_ref`` — optional; message_id or other pointer to full content
- ``metadata``    — optional dict; callers pass channel-specific context

Append-only; tolerant of concurrent writers via OS-level append semantics.
Reading tolerates corrupt/partial lines (they are skipped).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from entrabot.storage.backend import get_backend

logger = logging.getLogger("entrabot.tools.interaction_log")

_VALID_DIRECTIONS = {"inbound", "outbound"}


def _now() -> datetime:
    """Indirection for test patching."""
    return datetime.now(UTC)


def _interaction_key(day: str) -> str:
    """Backend key for the day's interaction log."""
    return f"interactions/{day}.jsonl"


def detect_channel(chat_id: str | None) -> str:
    """Map a Teams chat ID to a channel label.

    - ``@unq.gbl.spaces``  → ``teams_dm``  (oneOnOne chat)
    - ``@thread.v2``       → ``teams_group``
    - None / empty string  → ``terminal``
    - anything else        → ``teams_unknown`` (new chat type defaults here)
    """
    if not chat_id:
        return "terminal"
    if chat_id.endswith("@unq.gbl.spaces"):
        return "teams_dm"
    if chat_id.endswith("@thread.v2"):
        return "teams_group"
    return "teams_unknown"


def log_interaction(
    *,
    channel: str,
    direction: str,
    sender: str,
    summary: str,
    recipient: str | None = None,
    action: str | None = None,
    content_ref: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Append one interaction entry to today's JSONL log and return it."""
    if not channel:
        raise ValueError("channel is required")
    if direction not in _VALID_DIRECTIONS:
        raise ValueError(f"direction must be one of {_VALID_DIRECTIONS}, got {direction!r}")
    if not summary or not summary.strip():
        raise ValueError("summary is required")

    ts = _now()
    entry: dict = {
        "id": str(uuid.uuid4()),
        "ts": ts.isoformat(),
        "channel": channel,
        "direction": direction,
        "sender": sender,
        "summary": summary.strip(),
    }
    if recipient is not None:
        entry["recipient"] = recipient
    if action is not None:
        entry["action"] = action
    if content_ref is not None:
        entry["content_ref"] = content_ref
    if metadata is not None:
        entry["metadata"] = metadata

    get_backend().append_text(
        _interaction_key(ts.strftime("%Y-%m-%d")),
        json.dumps(entry) + "\n",
    )

    logger.debug(
        "interaction: %s %s %s → %s",
        channel,
        direction,
        sender,
        summary[:80],
    )
    return entry


def read_day(day: str) -> list[dict]:
    """Return all interaction entries for the given UTC day (YYYY-MM-DD)."""
    raw = get_backend().read_text(_interaction_key(day))
    if raw is None:
        return []

    entries: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("skipping corrupt line in %s.jsonl: %s", day, line[:80])
            continue
    return entries
