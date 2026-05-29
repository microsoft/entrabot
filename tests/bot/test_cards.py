"""Tests for Adaptive Card templates."""

from __future__ import annotations

from entrabot.bot.cards import (
    CONTENT_TYPE,
    build_card,
    pr_card,
    status_card,
    task_complete_card,
)


class TestStatusCard:
    def test_basic(self) -> None:
        card = status_card(status="building", message="Running pytest")
        assert card["contentType"] == CONTENT_TYPE
        body = card["content"]["body"]
        assert any("BUILDING" in str(b.get("text", "")) for b in body)
        assert any("Running pytest" in str(b.get("text", "")) for b in body)

    def test_with_details(self) -> None:
        card = status_card(
            status="done",
            message="All tests passed",
            details={"Tests": "266", "Duration": "3m 45s"},
        )
        facts = [b for b in card["content"]["body"] if b.get("type") == "FactSet"]
        assert len(facts) == 1
        assert len(facts[0]["facts"]) == 2

    def test_defaults(self) -> None:
        card = status_card()
        assert card["content"]["version"] == "1.5"
        assert card["content"]["type"] == "AdaptiveCard"


class TestPRCard:
    def test_open_pr(self) -> None:
        card = pr_card(
            title="Add bot gateway",
            url="https://github.com/example/repo/pull/1",
            state="open",
            author="Brandon",
            files_changed=12,
        )
        assert card["contentType"] == CONTENT_TYPE
        actions = card["content"].get("actions", [])
        assert any(a["url"] == "https://github.com/example/repo/pull/1" for a in actions)

    def test_merged_pr(self) -> None:
        card = pr_card(
            title="Ship it",
            url="https://example.com/pr/2",
            state="merged",
        )
        body = card["content"]["body"]
        assert any("MERGED" in str(b) for b in body)


class TestBuildCard:
    def test_success(self) -> None:
        card = build_card(
            pipeline="CI",
            status="succeeded",
            duration="3m 42s",
            url="https://example.com/build/1",
            commit="abc1234",
        )
        assert card["contentType"] == CONTENT_TYPE
        body_text = str(card["content"]["body"])
        assert "SUCCEEDED" in body_text
        assert "✅" in body_text

    def test_failed_with_errors(self) -> None:
        card = build_card(
            pipeline="Deploy",
            status="failed",
            errors=["TypeError: x is not a function", "Build timeout"],
        )
        body_text = str(card["content"]["body"])
        assert "FAILED" in body_text
        assert "TypeError" in body_text

    def test_no_url_no_actions(self) -> None:
        card = build_card(status="succeeded")
        actions = card["content"].get("actions", [])
        assert len(actions) == 0


class TestTaskCompleteCard:
    def test_basic(self) -> None:
        card = task_complete_card(
            task="Implement bot gateway",
            summary="All 266 tests pass, lint clean.",
        )
        assert card["contentType"] == CONTENT_TYPE
        body_text = str(card["content"]["body"])
        assert "Task Complete" in body_text
        assert "266 tests" in body_text

    def test_with_next_steps(self) -> None:
        card = task_complete_card(
            task="Setup complete",
            next_steps=["Run start_bot.sh", "Message the bot in Teams"],
        )
        body_text = str(card["content"]["body"])
        assert "start_bot.sh" in body_text
