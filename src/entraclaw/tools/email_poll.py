"""Per-minute Graph poll of /me/messages for the Agent User mailbox.

Polls the agent's inbox, filters out known noise (Teams notification mail,
M365 marketing), detects Purview-encrypted messages via the ``message.rpmsg``
attachment, and returns the substantive messages for the MCP server to push
as channel notifications and write to the interaction log.

Cursor is a single RFC 3339 timestamp persisted at
``<config.data_dir>/email_cursor.txt``. On first run the cursor is absent
and the caller should initialize it to "now" so we don't flood the agent
with historical mail.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from entraclaw.config import get_config
from entraclaw.errors import TokenExpiredError

logger = logging.getLogger("entraclaw.tools.email_poll")

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


def _cursor_path() -> Path:
    cfg = get_config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg.data_dir / "email_cursor.txt"


def load_cursor() -> str | None:
    """Return the last-seen receivedDateTime, or None if no cursor yet."""
    p = _cursor_path()
    if not p.exists():
        return None
    content = p.read_text().strip()
    return content or None


def save_cursor(ts: str) -> None:
    """Persist *ts* as the new cursor (whitespace stripped)."""
    _cursor_path().write_text(ts.strip())


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

    ``new_cursor`` is the max ``receivedDateTime`` across ALL returned
    messages (including filtered noise), so the next poll doesn't re-scan
    the same noise. If no messages come back, the input cursor is returned
    unchanged.

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
        substantive: list[dict] = []
        for msg in messages:
            received = msg.get("receivedDateTime")
            if received and (latest_ts is None or received > latest_ts):
                latest_ts = received

            sender_addr = (msg.get("from") or {}).get("emailAddress", {}).get("address", "")
            if not is_substantive(sender_addr):
                continue

            if msg.get("hasAttachments") and await _has_rpmsg_attachment(
                client=client, msg_id=msg["id"], headers=headers
            ):
                msg["_encrypted"] = True

            substantive.append(msg)

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
