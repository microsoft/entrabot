"""Tests for daily_summary — 5pm PDT triage of the interaction log.

Reads one UTC day of interactions, triages them into three buckets
(needs_you, handled, heads_up), renders an HTML email, sends via
Graph Mail.Send, and archives the rendered summary at
``<data_dir>/summaries/YYYY-MM-DD.html``.

The triage heuristic is deterministic and rule-based (no LLM):

- **needs_you**: inbound entries without a subsequent outbound from
  the agent to the same counterparty on the same day (pending).
- **handled**: outbound entries that came after an inbound from the
  same counterparty (a reply).
- **heads_up**: outbound entries with no prior inbound from the same
  counterparty (agent-initiated).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from entraclaw.tools.daily_summary import (
    GRAPH_SENDMAIL_URL,
    archive_summary,
    next_run_at,
    render_summary_html,
    send_summary_email,
    triage_interactions,
)


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ENTRACLAW_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ENTRACLAW_BLOB_ENDPOINT", raising=False)
    monkeypatch.delenv("ENTRACLAW_BLOB_CONTAINER", raising=False)
    return tmp_path


def _entry(
    *,
    ts: str,
    channel: str,
    direction: str,
    sender: str,
    recipient: str | None = None,
    summary: str = "msg",
    metadata: dict | None = None,
) -> dict:
    e: dict = {
        "id": f"id-{ts}",
        "ts": ts,
        "channel": channel,
        "direction": direction,
        "sender": sender,
        "summary": summary,
    }
    if recipient is not None:
        e["recipient"] = recipient
    if metadata is not None:
        e["metadata"] = metadata
    return e


# ---------------------------------------------------------------------------
# triage_interactions
# ---------------------------------------------------------------------------
class TestTriage:
    def test_empty_returns_empty_buckets(self) -> None:
        buckets = triage_interactions([])
        assert buckets == {"needs_you": [], "handled": [], "heads_up": []}

    def test_unanswered_inbound_goes_to_needs_you(self) -> None:
        entries = [
            _entry(
                ts="2026-04-16T10:00:00+00:00",
                channel="email",
                direction="inbound",
                sender="alice.example@example.com",
                summary="Re: Project Apollo",
            ),
        ]
        buckets = triage_interactions(entries)
        assert len(buckets["needs_you"]) == 1
        assert buckets["needs_you"][0]["summary"] == "Re: Project Apollo"
        assert buckets["handled"] == []
        assert buckets["heads_up"] == []

    def test_answered_inbound_is_handled(self) -> None:
        entries = [
            _entry(
                ts="2026-04-16T10:00:00+00:00",
                channel="email",
                direction="inbound",
                sender="alice.example@example.com",
                summary="Re: Project Apollo",
            ),
            _entry(
                ts="2026-04-16T10:30:00+00:00",
                channel="email",
                direction="outbound",
                sender="entraclaw-agent",
                recipient="alice.example@example.com",
                summary="Thanks for sharing — three quick thoughts",
            ),
        ]
        buckets = triage_interactions(entries)
        assert buckets["needs_you"] == []
        assert len(buckets["handled"]) == 1
        assert buckets["handled"][0]["summary"].startswith("Thanks for sharing")
        assert buckets["heads_up"] == []

    def test_agent_initiated_outbound_is_heads_up(self) -> None:
        entries = [
            _entry(
                ts="2026-04-16T09:00:00+00:00",
                channel="teams_dm",
                direction="outbound",
                sender="entraclaw-agent",
                recipient="19:xyz@unq.gbl.spaces",
                summary="Heads up — I updated the phase plan",
            ),
        ]
        buckets = triage_interactions(entries)
        assert buckets["needs_you"] == []
        assert buckets["handled"] == []
        assert len(buckets["heads_up"]) == 1

    def test_multiple_threads_independent(self) -> None:
        entries = [
            # Thread A: inbound from Alice, no reply → needs_you
            _entry(
                ts="2026-04-16T10:00:00+00:00",
                channel="email",
                direction="inbound",
                sender="alice@example.com",
                summary="Question about Apollo",
            ),
            # Thread B: inbound from Dave, agent replied → handled
            _entry(
                ts="2026-04-16T11:00:00+00:00",
                channel="teams_group",
                direction="inbound",
                sender="Dave Fixture",
                summary="What's your take?",
                metadata={"chat_id": "19:group@thread.v2"},
            ),
            _entry(
                ts="2026-04-16T11:05:00+00:00",
                channel="teams_group",
                direction="outbound",
                sender="entraclaw-agent",
                recipient="19:group@thread.v2",
                summary="My take is ...",
            ),
            # Thread C: agent DM'd the user unprompted → heads_up
            _entry(
                ts="2026-04-16T12:00:00+00:00",
                channel="teams_dm",
                direction="outbound",
                sender="entraclaw-agent",
                recipient="19:brandon@unq.gbl.spaces",
                summary="Phase 2 shipped",
            ),
        ]
        buckets = triage_interactions(entries)
        assert len(buckets["needs_you"]) == 1
        assert buckets["needs_you"][0]["sender"] == "alice@example.com"
        assert len(buckets["handled"]) == 1
        assert buckets["handled"][0]["recipient"] == "19:group@thread.v2"
        assert len(buckets["heads_up"]) == 1
        assert buckets["heads_up"][0]["summary"] == "Phase 2 shipped"

    def test_inbound_from_agent_upn_is_filtered(self) -> None:
        """Agent's own emails echoed back (Sent-Items leak) must not surface.

        /me/messages returns the whole mailbox including Sent Items. Before
        the mcp_server self-echo filter shipped (commit 85c8d78), outbound
        agent emails were being logged as ``direction=inbound`` with
        ``sender=<agent_upn>`` and then showing up in Needs-you. Belt-and-
        suspenders: the summary must also drop those entries so stale log
        lines don't haunt the sponsor.
        """
        entries = [
            _entry(
                ts="2026-04-17T23:11:00+00:00",
                channel="email",
                direction="inbound",
                sender="entraclaw-agent@fabrikam.onmicrosoft.com",
                summary="EntraClaw email pipeline test",
            ),
            _entry(
                ts="2026-04-17T23:12:00+00:00",
                channel="email",
                direction="inbound",
                sender="alice@example.com",
                summary="Actually needs attention",
            ),
        ]
        buckets = triage_interactions(entries, agent_upn="entraclaw-agent@fabrikam.onmicrosoft.com")
        assert len(buckets["needs_you"]) == 1
        assert buckets["needs_you"][0]["sender"] == "alice@example.com"

    def test_inbound_from_agent_upn_filter_is_case_insensitive(self) -> None:
        entries = [
            _entry(
                ts="2026-04-17T23:11:00+00:00",
                channel="email",
                direction="inbound",
                sender="EntraClaw-Agent@Fabrikam.Onmicrosoft.com",
                summary="self-echo with mixed case",
            ),
        ]
        buckets = triage_interactions(entries, agent_upn="entraclaw-agent@fabrikam.onmicrosoft.com")
        assert buckets == {"needs_you": [], "handled": [], "heads_up": []}

    def test_no_agent_upn_means_no_filtering(self) -> None:
        """Backwards compat: callers that don't pass agent_upn get old behavior."""
        entries = [
            _entry(
                ts="2026-04-17T23:11:00+00:00",
                channel="email",
                direction="inbound",
                sender="entraclaw-agent@fabrikam.onmicrosoft.com",
                summary="Would be filtered if upn were passed",
            ),
        ]
        buckets = triage_interactions(entries)
        assert len(buckets["needs_you"]) == 1


# ---------------------------------------------------------------------------
# render_summary_html
# ---------------------------------------------------------------------------
class TestRender:
    def test_html_includes_all_bucket_headers(self) -> None:
        buckets = {"needs_you": [], "handled": [], "heads_up": []}
        html = render_summary_html(buckets, day="2026-04-16")
        # Headers are present regardless of whether the bucket has entries
        assert "Needs you" in html
        assert "Handled" in html
        assert "Heads-up" in html
        assert "2026-04-16" in html

    def test_html_renders_entries(self) -> None:
        buckets = {
            "needs_you": [
                {
                    "sender": "alice@example.com",
                    "summary": "Question about Apollo",
                    "channel": "email",
                    "ts": "2026-04-16T10:00:00+00:00",
                }
            ],
            "handled": [],
            "heads_up": [],
        }
        html = render_summary_html(buckets, day="2026-04-16")
        assert "alice@example.com" in html
        assert "Question about Apollo" in html

    def test_html_empty_bucket_shows_placeholder(self) -> None:
        buckets = {"needs_you": [], "handled": [], "heads_up": []}
        html = render_summary_html(buckets, day="2026-04-16")
        # Empty buckets show a "nothing here" placeholder, not a bare header.
        assert "—" in html or "Nothing" in html or "nothing" in html

    def test_html_has_calibration_footer(self) -> None:
        """The summary invites the sponsor to recalibrate what lands where."""
        buckets = {"needs_you": [], "handled": [], "heads_up": []}
        html = render_summary_html(buckets, day="2026-04-16")
        # Footer should mention how to calibrate (feedback loop)
        lower = html.lower()
        assert "calibrat" in lower or "feedback" in lower or "reply" in lower


# ---------------------------------------------------------------------------
# send_summary_email
# ---------------------------------------------------------------------------
class TestSendSummary:
    @pytest.mark.asyncio
    async def test_sends_to_graph_sendmail(self) -> None:
        captured: dict = {}

        def handler(request):
            captured["url"] = str(request.url)
            captured["json"] = request.read()
            captured["headers"] = dict(request.headers)
            return httpx.Response(202)

        with respx.mock:
            respx.post(GRAPH_SENDMAIL_URL).mock(side_effect=handler)
            await send_summary_email(
                token="tok",
                html="<p>hi</p>",
                subject="Daily summary — 2026-04-16",
                to=["alice@example.com"],
            )

        assert "sendMail" in captured["url"]
        assert captured["headers"]["authorization"] == "Bearer tok"
        body = captured["json"].decode()
        assert "alice@example.com" in body
        assert "Daily summary" in body
        assert "&lt;p&gt;hi&lt;/p&gt;" in body or "<p>hi</p>" in body

    @pytest.mark.asyncio
    async def test_401_raises_token_expired(self) -> None:
        from entraclaw.errors import TokenExpiredError

        with respx.mock:
            respx.post(GRAPH_SENDMAIL_URL).mock(return_value=httpx.Response(401))
            with pytest.raises(TokenExpiredError):
                await send_summary_email(
                    token="tok",
                    html="<p>hi</p>",
                    subject="s",
                    to=["alice@example.com"],
                )


# ---------------------------------------------------------------------------
# archive_summary
# ---------------------------------------------------------------------------
class TestArchive:
    def test_writes_html_to_summaries_dir(self, tmp_data_dir: Path) -> None:
        buckets = {"needs_you": [], "handled": [], "heads_up": []}
        key = archive_summary(day="2026-04-16", html="<p>hi</p>", buckets=buckets)
        assert key == "summaries/2026-04-16.html"
        # LocalBackend (the default) writes into <data_dir>/<key>
        assert (tmp_data_dir / key).read_text() == "<p>hi</p>"

    def test_writes_sidecar_json_with_bucket_counts(self, tmp_data_dir: Path) -> None:
        """A sidecar .json captures counts for quick programmatic access."""
        import json

        buckets = {
            "needs_you": [{"summary": "a"}, {"summary": "b"}],
            "handled": [{"summary": "c"}],
            "heads_up": [],
        }
        archive_summary(day="2026-04-16", html="<p>x</p>", buckets=buckets)

        sidecar = tmp_data_dir / "summaries" / "2026-04-16.json"
        assert sidecar.exists()
        data = json.loads(sidecar.read_text())
        assert data["counts"] == {"needs_you": 2, "handled": 1, "heads_up": 0}
        assert data["day"] == "2026-04-16"


# ---------------------------------------------------------------------------
# next_run_at — 5pm PDT cron
# ---------------------------------------------------------------------------
class TestNextRunAt:
    def test_before_5pm_pdt_returns_today_5pm(self) -> None:
        # 10:00 PDT = 17:00 UTC
        now = datetime(2026, 4, 16, 17, 0, tzinfo=UTC)
        nxt = next_run_at(now=now)
        # 5pm PDT on 2026-04-16 is 00:00 UTC 2026-04-17 (PDT = UTC-7)
        assert nxt == datetime(2026, 4, 17, 0, 0, tzinfo=UTC)

    def test_after_5pm_pdt_returns_tomorrow_5pm(self) -> None:
        # 01:00 UTC on Apr 17 = 18:00 PDT on Apr 16 (past the 5pm PDT trigger)
        now = datetime(2026, 4, 17, 1, 0, tzinfo=UTC)
        nxt = next_run_at(now=now)
        assert nxt == datetime(2026, 4, 18, 0, 0, tzinfo=UTC)

    def test_exactly_5pm_pdt_returns_tomorrow(self) -> None:
        # Exactly at the trigger — don't run it twice, bump to next day.
        now = datetime(2026, 4, 17, 0, 0, tzinfo=UTC)
        nxt = next_run_at(now=now)
        assert nxt == datetime(2026, 4, 18, 0, 0, tzinfo=UTC)
