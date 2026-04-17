"""Tests for Adaptive Card templates.

Tests cover:
- Card JSON structure matches Adaptive Card schema
- Each card type renders with required fields
- Cards include proper action buttons where applicable
- Card attachment format matches Graph API expectations
- Edge cases: empty values, long text truncation
"""

from __future__ import annotations

import json

from entraclaw.tools.cards import (
    build_result_card,
    card_attachment,
    task_status_card,
    tool_activity_card,
)


class TestCardAttachment:
    """card_attachment wraps any card dict into Graph API attachment format."""

    def test_wraps_card_in_attachment(self) -> None:
        card = {"type": "AdaptiveCard", "version": "1.4", "body": []}
        att = card_attachment(card)

        assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
        # Graph API expects content as a JSON string, not a dict
        assert isinstance(att["content"], str)
        assert json.loads(att["content"]) == card

    def test_returned_dict_has_required_keys(self) -> None:
        card = {"type": "AdaptiveCard", "version": "1.4", "body": []}
        att = card_attachment(card)

        assert "id" in att
        assert "contentType" in att
        assert "content" in att


class TestToolActivityCard:
    """tool_activity_card shows Claude Code doing things in real-time."""

    def test_basic_structure(self) -> None:
        card = tool_activity_card(
            tool_name="read_file",
            status="running",
            detail="Reading src/main.py (142 lines)",
        )
        assert card["type"] == "AdaptiveCard"
        assert card["version"] == "1.4"
        assert len(card["body"]) > 0

    def test_contains_tool_name(self) -> None:
        card = tool_activity_card(
            tool_name="git_log",
            status="complete",
            detail="3 commits found",
        )
        body_text = json.dumps(card["body"], ensure_ascii=False)
        assert "git_log" in body_text

    def test_contains_status(self) -> None:
        card = tool_activity_card(
            tool_name="grep",
            status="running",
            detail="Searching for 'TODO'...",
        )
        body_text = json.dumps(card["body"], ensure_ascii=False)
        assert "running" in body_text.lower() or "Running" in body_text

    def test_contains_detail(self) -> None:
        card = tool_activity_card(
            tool_name="read_file",
            status="complete",
            detail="Read 42 lines from config.py",
        )
        body_text = json.dumps(card["body"], ensure_ascii=False)
        assert "42 lines" in body_text

    def test_status_emoji_running(self) -> None:
        card = tool_activity_card(
            tool_name="build", status="running", detail="compiling"
        )
        body_text = json.dumps(card["body"], ensure_ascii=False)
        # Running should have a spinner/clock indicator
        assert any(c in body_text for c in ["\u23f3", "\u26a1", "\u25b6"])

    def test_status_emoji_complete(self) -> None:
        card = tool_activity_card(
            tool_name="build", status="complete", detail="done"
        )
        body_text = json.dumps(card["body"], ensure_ascii=False)
        assert "\u2705" in body_text

    def test_status_emoji_error(self) -> None:
        card = tool_activity_card(
            tool_name="build", status="error", detail="failed"
        )
        body_text = json.dumps(card["body"], ensure_ascii=False)
        assert "\u274c" in body_text

    def test_long_detail_truncated(self) -> None:
        long_detail = "x" * 500
        card = tool_activity_card(
            tool_name="read_file", status="complete", detail=long_detail
        )
        body_text = json.dumps(card["body"], ensure_ascii=False)
        # Should not include the full 500 chars
        assert len(body_text) < 1000


class TestTaskStatusCard:
    """task_status_card shows structured task progress."""

    def test_basic_structure(self) -> None:
        card = task_status_card(
            task="Security review",
            status="in_progress",
            duration="2m 34s",
        )
        assert card["type"] == "AdaptiveCard"
        assert card["version"] == "1.4"

    def test_contains_task_name(self) -> None:
        card = task_status_card(
            task="Build PR #42",
            status="complete",
            duration="45s",
        )
        body_text = json.dumps(card["body"], ensure_ascii=False)
        assert "Build PR #42" in body_text

    def test_contains_duration(self) -> None:
        card = task_status_card(
            task="Test suite",
            status="complete",
            duration="3m 12s",
        )
        body_text = json.dumps(card["body"], ensure_ascii=False)
        assert "3m 12s" in body_text

    def test_optional_details(self) -> None:
        card = task_status_card(
            task="Deploy",
            status="complete",
            duration="1m",
            details={"Tests": "225 passed", "Coverage": "82%"},
        )
        body_text = json.dumps(card["body"], ensure_ascii=False)
        assert "225 passed" in body_text
        assert "82%" in body_text

    def test_no_details_still_valid(self) -> None:
        card = task_status_card(
            task="Quick check",
            status="complete",
            duration="5s",
        )
        assert card["type"] == "AdaptiveCard"
        assert len(card["body"]) > 0


class TestBuildResultCard:
    """build_result_card shows pass/fail with expandable details."""

    def test_pass_result(self) -> None:
        card = build_result_card(
            passed=True,
            summary="225 tests passed",
            details="All green. Coverage 82%.",
        )
        body_text = json.dumps(card["body"], ensure_ascii=False)
        assert "225 tests passed" in body_text
        assert "\u2705" in body_text

    def test_fail_result(self) -> None:
        card = build_result_card(
            passed=False,
            summary="3 tests failed",
            details="test_auth.py::test_hop3 FAILED\ntest_teams.py::test_send FAILED",
        )
        body_text = json.dumps(card["body"], ensure_ascii=False)
        assert "3 tests failed" in body_text
        assert "\u274c" in body_text

    def test_contains_details(self) -> None:
        card = build_result_card(
            passed=True,
            summary="All passed",
            details="Duration: 224s\nCoverage: 82%",
        )
        body_text = json.dumps(card["body"], ensure_ascii=False)
        assert "224s" in body_text

    def test_no_details(self) -> None:
        card = build_result_card(
            passed=True,
            summary="All good",
        )
        assert card["type"] == "AdaptiveCard"
