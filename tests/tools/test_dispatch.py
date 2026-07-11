"""Tests for the write-shaped tool-name recognizer.

Per plan §"Deny-list guard on outbound tool names" — a lightweight regex
that identifies MCP tool names as write-shaped ("outbound") so future
tools default to gated behavior even before their explicit gate is
written. This test pins the pattern; behavior (registration-time debug
log, actual gating) rides on top later.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Pattern shape — pins the regex
# ---------------------------------------------------------------------------


class TestWriteShapedToolNameRecognizer:
    @pytest.mark.parametrize(
        "name",
        [
            # existing write-shaped MCP tools in this repo whose names
            # trip the plan's regex verbatim.
            "send_teams_message",
            "send_email",
            "send_card",
            "reply_to_word_comment",
            "create_chat",
            "create_word_document",
            "delete_teams_message",
            "upload_file",
            "share_file",
            "resolve_promise",
            "resolve_placeholder",
            "resolve_file_url",
            # The real ``add_*`` tools shipped in this repo — all
            # write-shaped. The regex was broadened from the plan's
            # narrow ``add_(?:member|comment)`` to plain ``add_`` so
            # these actually match; see the dispatch module docstring.
            "add_teams_member",
            "add_file_comment",
            "add_word_comment",
            "add_promise",
            # Hypothetical plan-shaped names, still pinned.
            "add_member",
            "add_comment",
        ],
    )
    def test_recognizes_write_shaped_names(self, name: str) -> None:
        from entrabot.tools.dispatch import is_write_shaped_tool_name

        assert is_write_shaped_tool_name(name) is True, (
            f"expected {name!r} to be recognized as write-shaped"
        )

    @pytest.mark.parametrize(
        "name",
        [
            # explicit read tools — the deny-list must not match these
            "read_teams_messages",
            "read_email",
            "read_file",
            "read_word_document",
            "read_a365_text_file",
            "read_a365_binary_file",
            "read_interactions",
            "list_chat_members",
            "list_recent_files",
            "list_promises",
            "get_a365_file_metadata_by_url",
            "whoami",
            "audit_log",
            "bootstrap_body_state",
            "view_image",
            "watch_teams_replies",
            "wait_for_sponsor_dm",
            "post_thinking_placeholder",  # starts with "post", not "send"
            "update_placeholder",  # progress update, not final commitment
        ],
    )
    def test_does_not_match_read_or_neutral_names(self, name: str) -> None:
        from entrabot.tools.dispatch import is_write_shaped_tool_name

        assert is_write_shaped_tool_name(name) is False, (
            f"expected {name!r} to NOT be recognized as write-shaped"
        )
