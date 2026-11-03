"""Per-minute Graph poll of /me/messages for the Agent User mailbox.

Polls the agent's inbox, filters out known noise (Teams notification mail,
M365 marketing), detects Purview-encrypted messages via the ``message.rpmsg``
attachment, and returns the substantive messages for the MCP server to push
as channel notifications and write to the interaction log.

This module only fetches and filters. The cursor it takes and returns is a single
RFC 3339 timestamp owned by :mod:`entrabot.tools.poll_state`, which is also where
the first-run "start at now" rule and the fleet-safe claim live.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx

from entrabot.errors import TokenExpiredError

logger = logging.getLogger("entrabot.tools.email_poll")

GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
GRAPH_ATTACHMENTS_URL_TMPL = "https://graph.microsoft.com/v1.0/me/messages/{msg_id}/attachments"
RPMSG_ATTACHMENT_NAME = "message.rpmsg"

_NOISE_DOMAINS = (
    "teams.mail.microsoft",
    "communication.microsoft.com",
)
_NOISE_LOCAL_PART_SUBSTRINGS = (
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
)


def _parse_utc(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def advance_cursor(ts: str) -> str:
    """Return a timestamp strictly after *ts* for the next poll watermark.

    Graph's ``receivedDateTime gt {cursor}`` filter excludes messages at or
    before the cursor. When timestamps lack sub-second precision, bumping by
    1 ms prevents messages at the cursor's exact second from being re-fetched
    after a server restart (per-session dedup is lost on restart).
    """
    advanced = (_parse_utc(ts) + timedelta(milliseconds=1)).astimezone(UTC).replace(tzinfo=None)
    if advanced.microsecond:
        return advanced.strftime("%Y-%m-%dT%H:%M:%S.") + f"{advanced.microsecond // 1000:03d}Z"
    return advanced.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_substantive(address: str | None) -> bool:
    """Return True if *address* looks like a real human sender.

    Filters:
      - Teams notification mail (``teams.mail.microsoft``)
      - M365 marketing (``communication.microsoft.com``)
      - Generic ``no-reply``/``donotreply`` local parts
      - Empty or ``None`` addresses
    """
    if not address:
        return False
    addr = address.lower()
    if any(domain in addr for domain in _NOISE_DOMAINS):
        return False
    local = addr.partition("@")[0]
    return all(np not in local for np in _NOISE_LOCAL_PART_SUBSTRINGS)


async def poll_once(
    *,
    token: str,
    cursor: str | None,
) -> tuple[list[dict], str | None]:
    """Fetch new messages since *cursor* and return (substantive, new_cursor).

    ``new_cursor`` is just past the max ``receivedDateTime`` across ALL returned
    messages (including filtered noise), so the next poll doesn't re-scan
    the same noise. Timestamps are compared as times: Graph mixes second and
    sub-second precision, and a string compare can leave the cursor behind a
    message it just returned, which another instance would then push again. If
    nothing newer than *cursor* comes back, the input cursor is returned unchanged.

    Raises ``TokenExpiredError`` on HTTP 401 so the caller can refresh.
    """
    headers = {"Authorization": f"Bearer {token}"}
    params: dict[str, str] = {
        "$orderby": "receivedDateTime desc",
        "$top": "50",
    }
    if cursor:
        params["$filter"] = f"receivedDateTime gt {cursor}"

    async with httpx.AsyncClient() as client:
        resp = await client.get(GRAPH_MESSAGES_URL, params=params, headers=headers)
        if resp.status_code == 401:
            raise TokenExpiredError("Agent User token expired — re-acquire via three-hop flow")
        resp.raise_for_status()

        messages = resp.json().get("value", [])
        if not messages:
            return [], cursor

        latest_ts: str | None = cursor
        found_newer = False
        substantive: list[dict] = []
        for msg in messages:
            received = msg.get("receivedDateTime")
            if received and (latest_ts is None or _parse_utc(received) > _parse_utc(latest_ts)):
                latest_ts = received
                found_newer = True

            sender_addr = (msg.get("from") or {}).get("emailAddress", {}).get("address", "")
            if not is_substantive(sender_addr):
                continue

            if msg.get("hasAttachments") and await _has_rpmsg_attachment(
                client=client, msg_id=msg["id"], headers=headers
            ):
                msg["_encrypted"] = True

            substantive.append(msg)

        if found_newer and latest_ts is not None:
            latest_ts = advance_cursor(latest_ts)
        return substantive, latest_ts


async def _has_rpmsg_attachment(
    *,
    client: httpx.AsyncClient,
    msg_id: str,
    headers: dict[str, str],
) -> bool:
    """Return True if the message has a Purview ``message.rpmsg`` attachment."""
    url = GRAPH_ATTACHMENTS_URL_TMPL.format(msg_id=msg_id)
    try:
        resp = await client.get(url, headers=headers)
        if resp.status_code >= 400:
            return False
        attachments = resp.json().get("value", [])
    except Exception:
        logger.exception("attachment fetch failed for %s", msg_id)
        return False
    return any(a.get("name") == RPMSG_ATTACHMENT_NAME for a in attachments)
