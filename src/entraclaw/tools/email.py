"""Graph Mail.Send helper — ``/me/sendMail`` and ``/me/messages/{id}/reply``.

The Agent User token chain has ``Mail.Send`` delegation granted during
provisioning (``scripts/create_entra_agent_ids.py``), so this helper is
the single send path used by the ``send_email`` MCP tool and (via
delegation) by the daily-summary scheduler.

Error shape intentionally mirrors ``tools/teams.py``: 401 →
``TokenExpiredError``, 429 → ``RateLimitError``, other non-2xx →
``EmailSendError`` with the Graph error body surfaced for operator
debugging.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from entraclaw.errors import EntraClawError, RateLimitError, TokenExpiredError

logger = logging.getLogger("entraclaw.tools.email")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SENDMAIL_URL = f"{GRAPH_BASE}/me/sendMail"

# Fields the agent needs when re-reading a poll-pushed message: full body
# (text + HTML), all recipient lists, message headers, attachment flag.
# The 60s email poll only ships a short preview via the channel push;
# longer mails truncate. ``read_email`` fixes that.
READ_EMAIL_SELECT = (
    "body,bodyPreview,toRecipients,ccRecipients,bccRecipients,"
    "from,sender,subject,internetMessageHeaders,hasAttachments,"
    "receivedDateTime,id"
)


class EmailSendError(EntraClawError):
    """Graph ``sendMail`` / reply endpoint returned a non-2xx (non-401/429)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


def _recipients(addrs: list[str] | None) -> list[dict]:
    return [{"emailAddress": {"address": a}} for a in (addrs or [])]


def _normalize_content_type(content_type: str) -> str:
    """Graph accepts only ``"HTML"`` or ``"Text"`` (case-sensitive)."""
    lower = (content_type or "").strip().lower()
    if lower == "text":
        return "Text"
    # Default to HTML to match the Teams channel-discipline convention.
    return "HTML"


def _build_message(
    *,
    subject: str,
    body: str,
    content_type: str,
    to: list[str],
    cc: list[str] | None,
    bcc: list[str] | None,
    include_subject: bool = True,
) -> dict:
    msg: dict = {
        "body": {
            "contentType": _normalize_content_type(content_type),
            "content": body,
        },
        "toRecipients": _recipients(to),
    }
    if include_subject:
        msg["subject"] = subject
    if cc:
        msg["ccRecipients"] = _recipients(cc)
    if bcc:
        msg["bccRecipients"] = _recipients(bcc)
    return msg


async def send_email(
    *,
    to: list[str],
    subject: str,
    body: str,
    content_type: str = "HTML",
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    reply_to_message_id: str | None = None,
    token: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Send mail as the Agent User via Graph ``/me/sendMail`` or reply endpoint.

    Returns ``{"sent_at": ISO-8601}`` on success. Raises
    ``TokenExpiredError`` on 401, ``RateLimitError`` on 429, and
    ``EmailSendError`` on any other non-2xx (with the Graph error body
    surfaced in the exception message).

    When *reply_to_message_id* is set the request goes to
    ``/me/messages/{id}/reply`` instead of ``/me/sendMail`` so Graph
    preserves the thread headers; the subject is supplied by Graph from
    the original message.
    """
    message = _build_message(
        subject=subject,
        body=body,
        content_type=content_type,
        to=to,
        cc=cc,
        bcc=bcc,
        # Graph's /reply endpoint takes its subject from the original
        # message — including one on the reply is ignored at best and
        # rejected at worst.
        include_subject=reply_to_message_id is None,
    )

    if reply_to_message_id:
        url = f"{GRAPH_BASE}/me/messages/{reply_to_message_id}/reply"
        payload: dict = {"message": message}
    else:
        url = GRAPH_SENDMAIL_URL
        payload = {"message": message, "saveToSentItems": True}

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.post(url, json=payload, headers=headers)

    if resp.status_code in (200, 202):
        logger.info(
            "Mail sent (%s) to %d recipient(s)",
            "reply" if reply_to_message_id else "new",
            len(to),
        )
        return {"sent_at": datetime.now(UTC).isoformat()}

    if resp.status_code == 401:
        raise TokenExpiredError("Agent User token expired — re-acquire via three-hop flow")
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "60"))
        raise RateLimitError(retry_after)

    # Other failure — lift the Graph error body into the exception so
    # operators can see *why* the send failed without trawling logs.
    try:
        err_body = resp.json()
    except Exception:
        err_body = {"raw": resp.text}
    raise EmailSendError(
        f"Graph rejected mail send ({resp.status_code}): {err_body}",
        status_code=resp.status_code,
    )


async def read_email(
    *,
    message_id: str,
    mailbox: str = "",
    token: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Fetch the full body + recipients + headers of a message by id.

    The 60-second email poll only ships a short body preview via the
    channel push; longer mails (forwarded recipient lists, threaded
    replies, attached metadata) get truncated. This helper calls
    ``GET /me/messages/{id}`` (or ``/users/{mailbox}/messages/{id}`` for
    shared mailboxes) with ``$select`` covering every field the agent
    needs to act on a real inbound mail.

    Returns the Graph message JSON unchanged on success — body is
    returned verbatim with no truncation on our side.

    Errors:
        * 401 → ``TokenExpiredError`` (caller refreshes + retries via
          ``_with_token_retry``).
        * 404 / 403 / 5xx → ``{"error": "...", "status": <code>}`` so
          the caller can surface "no such message" without an
          exception. The bearer token is never echoed into the result.
    """
    if mailbox:
        url = f"{GRAPH_BASE}/users/{mailbox}/messages/{message_id}"
    else:
        url = f"{GRAPH_BASE}/me/messages/{message_id}"

    headers = {"Authorization": f"Bearer {token}"}
    params = {"$select": READ_EMAIL_SELECT}

    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.get(url, params=params, headers=headers)

    if resp.status_code == 200:
        return resp.json()

    if resp.status_code == 401:
        raise TokenExpiredError("Agent User token expired — re-acquire via three-hop flow")

    # Non-2xx, non-401: surface the Graph error body so the operator
    # can see *why* the read failed, but never include the token.
    try:
        err_body = resp.json()
        err_msg = err_body.get("error", {}).get("message") or str(err_body)
    except Exception:
        err_msg = resp.text or f"Graph returned HTTP {resp.status_code}"

    logger.info(
        "read_email failed for %s (mailbox=%s): status=%d",
        message_id,
        mailbox or "<me>",
        resp.status_code,
    )
    return {"error": err_msg, "status": resp.status_code, "message_id": message_id}
