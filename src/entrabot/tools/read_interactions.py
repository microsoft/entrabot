"""Body-side query over the interaction log — chronological filter only.

Issue #20: the entrabot MCP server logs every inbound/outbound interaction
to ``interactions/<day>.jsonl`` via :mod:`entrabot.tools.interaction_log`,
but the model has no read path into its own operational history. This
module is that read path.

Read-only. Routes through :class:`entrabot.storage.backend.MemoryBackend`
so both ``LocalBackend`` and ``BlobBackend`` work. v1 is chronological +
structured filters; no embeddings, no scoring, no caching. JSONL files
are small (a day's worth is typically <100 KB) and re-reading is cheap.

Day-file window: defaults to "today + yesterday" (24h window). When
``since`` reaches further back, additional day files are loaded up to
:data:`_MAX_DAYS_SCAN` (7) to keep the read bounded. Anything older
than that requires a follow-up that raises the cap intentionally.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from entrabot.storage.backend import get_backend
from entrabot.tools.interaction_log import _interaction_key

logger = logging.getLogger("entrabot.tools.read_interactions")

_VALID_DIRECTIONS = {"inbound", "outbound"}
_DEFAULT_SINCE_HOURS = 24
_DEFAULT_LIMIT = 10
_MAX_DAYS_SCAN = 7  # hard cap to keep the read bounded


def _parse_since(since: str | None) -> datetime:
    """Return the UTC cutoff. Default = now - 24h."""
    if since is None:
        return datetime.now(UTC) - timedelta(hours=_DEFAULT_SINCE_HOURS)
    try:
        dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"since must be an ISO 8601 timestamp, got {since!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _days_to_scan(cutoff: datetime, now: datetime) -> list[str]:
    """Day strings (YYYY-MM-DD) from today back to *cutoff*, hard-capped.

    Always includes today + yesterday (matches the issue's default window
    even when cutoff is within today). Extends backwards if cutoff is
    older, never exceeding :data:`_MAX_DAYS_SCAN`.
    """
    today = now.date()
    cutoff_date = cutoff.date()
    span_days = (today - cutoff_date).days + 1  # inclusive of both ends
    span_days = max(span_days, 2)  # always today + yesterday
    if span_days > _MAX_DAYS_SCAN:
        logger.warning(
            "read_interactions cutoff would scan %d day files; capping at %d",
            span_days,
            _MAX_DAYS_SCAN,
        )
        span_days = _MAX_DAYS_SCAN
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(span_days)]


def _load_day(day: str) -> list[dict]:
    """Read the day's JSONL via the configured backend; corrupt lines skipped."""
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
            logger.warning(
                "skipping corrupt line in interactions/%s.jsonl: %s",
                day,
                line[:80],
            )
    return entries


def _entry_chat_id(entry: dict) -> str | None:
    """Pull the chat_id off an entry regardless of direction.

    Outbound entries store the destination in ``recipient``; inbound
    entries (Teams pushes) store the chat in ``metadata.chat_id``. This
    mirrors :func:`entrabot.tools.daily_summary._counterparty`.
    """
    direction = entry.get("direction")
    if direction == "outbound":
        return entry.get("recipient")
    meta = entry.get("metadata") or {}
    return meta.get("chat_id")


def _matches(
    entry: dict,
    *,
    chat_id: str | None,
    sender: str | None,
    action: str | None,
    direction: str | None,
    cutoff: datetime,
) -> bool:
    if direction is not None and entry.get("direction") != direction:
        return False
    if action is not None and entry.get("action") != action:
        return False
    if sender is not None:
        entry_sender = (entry.get("sender") or "").lower()
        if entry_sender != sender.lower():
            return False
    if chat_id is not None and _entry_chat_id(entry) != chat_id:
        return False
    ts_raw = entry.get("ts")
    if ts_raw is None:
        return False
    try:
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts > cutoff


def read_interactions(
    chat_id: str | None = None,
    sender: str | None = None,
    action: str | None = None,
    direction: str | None = None,
    since: str | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> list[dict]:
    """Return recent interaction entries matching the given filters.

    Args:
        chat_id: Match the Teams chat ID. For outbound entries this is
            ``recipient``; for inbound this is ``metadata.chat_id``.
        sender: Exact sender match (case-insensitive — emails are
            case-insensitive identities).
        action: Exact match on the ``action`` field
            (e.g. ``"send_teams_message"``).
        direction: ``"inbound"`` or ``"outbound"``.
        since: ISO 8601 timestamp. Default is now − 24 h. Entries at or
            before this cutoff are excluded.
        limit: Maximum entries to return (default 10).

    Returns:
        Most-recent-first list of raw entry dicts (existing JSONL schema
        preserved — caller sees what was written).
    """
    if direction is not None and direction not in _VALID_DIRECTIONS:
        raise ValueError(f"direction must be one of {_VALID_DIRECTIONS}, got {direction!r}")
    if limit <= 0:
        return []

    cutoff = _parse_since(since)
    now = datetime.now(UTC)
    days = _days_to_scan(cutoff, now)

    collected: list[dict] = []
    for day in days:
        for entry in _load_day(day):
            if _matches(
                entry,
                chat_id=chat_id,
                sender=sender,
                action=action,
                direction=direction,
                cutoff=cutoff,
            ):
                collected.append(entry)

    collected.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return collected[:limit]
