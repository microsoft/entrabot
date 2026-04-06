"""Tests for watch_teams_replies and supporting functions."""

from __future__ import annotations

from openclaw.tools.teams import filter_human_messages


class TestFilterHumanMessages:
    def test_filters_agent_messages(self) -> None:
        messages = [
            {
                "message_id": "m1",
                "from": "Human User",
                "content": "hello",
                "sent_at": "2026-04-06T12:00:00Z",
            },
            {
                "message_id": "m2",
                "from": "Openclaw Agent",
                "content": "hi back",
                "sent_at": "2026-04-06T12:00:01Z",
            },
            {
                "message_id": "m3",
                "from": "Human User",
                "content": "do something",
                "sent_at": "2026-04-06T12:00:02Z",
            },
        ]
        result = filter_human_messages(messages, agent_user_display_name="Openclaw Agent")
        assert len(result) == 2
        assert result[0]["message_id"] == "m1"
        assert result[1]["message_id"] == "m3"

    def test_filters_no_from_field(self) -> None:
        messages = [
            {
                "message_id": "m1",
                "from": "Human User",
                "content": "hello",
                "sent_at": "2026-04-06T12:00:00Z",
            },
            {
                "message_id": "m2",
                "from": "unknown",
                "content": "",
                "sent_at": "2026-04-06T12:00:01Z",
            },
        ]
        # "unknown" is what teams.read() returns when from is None — see existing code
        # System messages have from="unknown", so filter those out too
        result = filter_human_messages(messages, agent_user_display_name="Openclaw Agent")
        assert len(result) == 1
        assert result[0]["message_id"] == "m1"

    def test_empty_list(self) -> None:
        result = filter_human_messages([], agent_user_display_name="Openclaw Agent")
        assert result == []

    def test_all_agent_messages(self) -> None:
        messages = [
            {
                "message_id": "m1",
                "from": "Openclaw Agent",
                "content": "hi",
                "sent_at": "2026-04-06T12:00:00Z",
            },
        ]
        result = filter_human_messages(messages, agent_user_display_name="Openclaw Agent")
        assert result == []
