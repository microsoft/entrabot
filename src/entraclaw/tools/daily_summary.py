"""Daily summary — 5pm PDT triage email of the day's agent activity.

Reads one UTC day of interactions from the interaction log, triages them
into three buckets, renders an HTML email, sends it to the sponsor via
Graph ``/me/sendMail``, and archives the rendered summary for future
reference.

Triage buckets (rule-based, deterministic — no LLM):
  - ``needs_you``: inbound entries without a same-thread reply from the
    agent (pending items the sponsor should know about).
  - ``handled``: outbound replies where the agent responded to an inbound
    on the same thread (one representative entry per thread).
  - ``heads_up``: agent-initiated outbound entries (no prior inbound on
    the thread — e.g. the agent reached out first).

A "thread" here is identified by ``(channel, counterparty)`` where the
counterparty is the Teams chat_id (for Teams channels) or the email
address (for email). For inbound Teams messages the chat_id lives in
``metadata.chat_id``; for outbound it's in ``recipient``.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from html import escape
from typing import TypedDict

import httpx

from entraclaw.errors import TokenExpiredError
from entraclaw.storage.backend import get_backend

logger = logging.getLogger("entraclaw.tools.daily_summary")

GRAPH_SENDMAIL_URL = "https://graph.microsoft.com/v1.0/me/sendMail"
PDT_OFFSET = timedelta(hours=7)  # UTC - 7 (DST-assumed; West Coast in April)


class SummaryBuckets(TypedDict):
    needs_you: list[dict]
    handled: list[dict]
    heads_up: list[dict]


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------
def _counterparty(entry: dict) -> str:
    """Identify the other party on a thread for grouping purposes."""
    channel = entry.get("channel", "")
    direction = entry.get("direction", "")
    if channel.startswith("teams"):
        if direction == "inbound":
            meta = entry.get("metadata") or {}
            return meta.get("chat_id") or entry.get("sender", "")
        return entry.get("recipient") or ""
    if direction == "inbound":
        return entry.get("sender", "")
    return entry.get("recipient") or ""


def triage_interactions(
    entries: list[dict], *, agent_upn: str | None = None
) -> SummaryBuckets:
    """Sort *entries* into needs_you / handled / heads_up buckets.

    When *agent_upn* is provided, inbound entries whose ``sender`` matches
    the agent's own UPN are dropped — they're Sent-Items self-echoes that
    leaked into the log and would otherwise pollute Needs-you.
    """
    self_upn = (agent_upn or "").lower()
    threads: dict[tuple, list[dict]] = {}
    for e in entries:
        if (
            self_upn
            and e.get("direction") == "inbound"
            and (e.get("sender") or "").lower() == self_upn
        ):
            continue
        key = (e.get("channel", ""), _counterparty(e))
        threads.setdefault(key, []).append(e)

    needs_you: list[dict] = []
    handled: list[dict] = []
    heads_up: list[dict] = []

    for thread in threads.values():
        thread_sorted = sorted(thread, key=lambda x: x.get("ts", ""))
        inbounds = [e for e in thread_sorted if e.get("direction") == "inbound"]
        outbounds = [e for e in thread_sorted if e.get("direction") == "outbound"]

        if outbounds and inbounds:
            handled.append(outbounds[-1])
            last_out_ts = outbounds[-1].get("ts", "")
            unanswered = [
                ib for ib in inbounds if ib.get("ts", "") > last_out_ts
            ]
            if unanswered:
                needs_you.append(unanswered[-1])
        elif outbounds:
            heads_up.append(outbounds[-1])
        elif inbounds:
            needs_you.append(inbounds[-1])

    return {"needs_you": needs_you, "handled": handled, "heads_up": heads_up}


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------
_EMPTY_PLACEHOLDER = "—"


def _render_entry_li(entry: dict) -> str:
    who = entry.get("sender") or entry.get("recipient") or "?"
    channel = entry.get("channel", "")
    summary = entry.get("summary", "")
    ts = entry.get("ts", "")
    ts_short = ts[11:16] if len(ts) >= 16 else ts
    return (
        f"<li><strong>{escape(who)}</strong> "
        f"<em>({escape(channel)} · {escape(ts_short)})</em><br>"
        f"{escape(summary)}</li>"
    )


def _render_bucket(title: str, entries: list[dict], *, emoji: str) -> str:
    if not entries:
        body = f"<p style='color:#888;margin:4px 0 16px'>{_EMPTY_PLACEHOLDER}</p>"
    else:
        items = "\n".join(_render_entry_li(e) for e in entries)
        body = f"<ul style='margin:4px 0 16px'>{items}</ul>"
    return (
        f"<h2 style='margin:20px 0 4px'>{emoji} {escape(title)}</h2>\n{body}"
    )


_CALIBRATION_FOOTER = """
<hr style='margin-top:24px'>
<p style='color:#666;font-size:12px'>
  <strong>Calibrate me.</strong> Reply to this email with:
  <em>more detail</em>, <em>less</em>, or <em>rebucket X as Y</em>
  and I'll adjust how I triage tomorrow. This summary is feedback-driven.
</p>
"""


def render_summary_html(buckets: SummaryBuckets, *, day: str) -> str:
    """Render the triage buckets as a self-contained HTML email body."""
    needs_you = _render_bucket("Needs you", buckets["needs_you"], emoji="🎯")
    handled = _render_bucket("Handled", buckets["handled"], emoji="✅")
    heads_up = _render_bucket("Heads-up", buckets["heads_up"], emoji="👀")

    return (
        f"<div style='font-family:-apple-system,Segoe UI,sans-serif;max-width:640px'>"
        f"<h1 style='margin:0 0 4px'>Daily summary — {escape(day)}</h1>"
        f"<p style='color:#666;margin:0 0 16px'>"
        f"What happened while you were away, triaged for your attention."
        f"</p>"
        f"{needs_you}{handled}{heads_up}"
        f"{_CALIBRATION_FOOTER}"
        f"</div>"
    )


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------
async def send_summary_email(
    *,
    token: str,
    html: str,
    subject: str,
    to: list[str],
) -> None:
    """Send the rendered summary to *to* via Graph ``/me/sendMail``."""
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [
                {"emailAddress": {"address": addr}} for addr in to
            ],
        },
        "saveToSentItems": True,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(GRAPH_SENDMAIL_URL, json=payload, headers=headers)
        if resp.status_code == 401:
            raise TokenExpiredError(
                "Agent User token expired — re-acquire via three-hop flow"
            )
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------
def archive_summary(
    *, day: str, html: str, buckets: SummaryBuckets
) -> str:
    """Persist the rendered summary + sidecar counts via the memory backend.

    Returns the backend key for the HTML body (e.g. ``"summaries/2026-04-17.html"``).
    """
    backend = get_backend()
    html_key = f"summaries/{day}.html"
    backend.write_text(html_key, html)

    sidecar = {
        "day": day,
        "counts": {k: len(v) for k, v in buckets.items()},
    }
    backend.write_text(f"summaries/{day}.json", json.dumps(sidecar, indent=2))
    return html_key


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
def next_run_at(*, now: datetime, hour_pdt: int = 17) -> datetime:
    """Return the next 5pm-PDT trigger strictly in the future (UTC-aware)."""
    pdt_now = (now - PDT_OFFSET).replace(tzinfo=None)
    trigger = pdt_now.replace(hour=hour_pdt, minute=0, second=0, microsecond=0)
    if pdt_now >= trigger:
        trigger = trigger + timedelta(days=1)
    return (trigger + PDT_OFFSET).replace(tzinfo=UTC)
