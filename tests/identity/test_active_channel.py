"""Unit tests for ActiveChannelBindings — confused-deputy authorization fix store."""

from __future__ import annotations

import pytest

from entrabot.identity.active_channel import (
    ActiveChannelBindings,
)


@pytest.fixture
def bindings():
    """Bindings with deterministic clock at t=1_000_000.0 and TTL=120s."""
    return ActiveChannelBindings(ttl_seconds=120, clock=lambda: 1_000_000.0)


class TestRecordAndLookup:
    def test_record_then_lookup_returns_binding(self, bindings):
        bindings.record(
            sponsor_user_id="user-1",
            chat_id="chat-A",
            graph_sent_at_epoch=999_999.0,
            message_id="msg-1",
        )
        b = bindings.lookup("user-1")
        assert b is not None
        assert b.chat_id == "chat-A"
        assert b.message_id == "msg-1"
        assert b.sponsor_user_id == "user-1"

    def test_lookup_missing_sponsor_returns_none(self, bindings):
        assert bindings.lookup("user-unknown") is None

    def test_lookup_empty_string_returns_none(self, bindings):
        assert bindings.lookup("") is None

    def test_record_is_case_insensitive_on_user_id(self, bindings):
        bindings.record(
            sponsor_user_id="USER-1",
            chat_id="chat-A",
            graph_sent_at_epoch=999_999.0,
            message_id="msg-1",
        )
        assert bindings.lookup("user-1") is not None
        assert bindings.lookup("User-1") is not None
        assert bindings.lookup("USER-1") is not None


class TestLatestWins:
    def test_newer_record_overwrites_older(self, bindings):
        bindings.record(
            sponsor_user_id="user-1",
            chat_id="chat-A",
            graph_sent_at_epoch=999_900.0,
            message_id="msg-old",
        )
        bindings.record(
            sponsor_user_id="user-1",
            chat_id="chat-B",
            graph_sent_at_epoch=999_950.0,
            message_id="msg-new",
        )
        b = bindings.lookup("user-1")
        assert b.chat_id == "chat-B"
        assert b.message_id == "msg-new"

    def test_older_record_does_not_overwrite_newer(self, bindings):
        bindings.record(
            sponsor_user_id="user-1",
            chat_id="chat-B",
            graph_sent_at_epoch=999_950.0,
            message_id="msg-new",
        )
        result = bindings.record(
            sponsor_user_id="user-1",
            chat_id="chat-A",
            graph_sent_at_epoch=999_900.0,
            message_id="msg-old",
        )
        assert result is False
        assert bindings.lookup("user-1").chat_id == "chat-B"

    def test_equal_timestamp_does_not_overwrite(self, bindings):
        """Equal timestamps are not strictly newer — keep existing."""
        bindings.record(
            sponsor_user_id="user-1",
            chat_id="chat-A",
            graph_sent_at_epoch=999_950.0,
            message_id="msg-first",
        )
        result = bindings.record(
            sponsor_user_id="user-1",
            chat_id="chat-B",
            graph_sent_at_epoch=999_950.0,
            message_id="msg-second",
        )
        assert result is False
        assert bindings.lookup("user-1").chat_id == "chat-A"


class TestTTLEnforcement:
    def test_within_ttl_returns_binding(self, bindings):
        bindings.record(
            sponsor_user_id="user-1",
            chat_id="chat-A",
            graph_sent_at_epoch=1_000_000.0 - 60.0,
            message_id="msg-1",
        )
        assert bindings.lookup("user-1") is not None

    def test_at_ttl_boundary_returns_binding(self, bindings):
        """Exactly TTL old should still be valid (<=, not <)."""
        bindings.record(
            sponsor_user_id="user-1",
            chat_id="chat-A",
            graph_sent_at_epoch=1_000_000.0 - 120.0,
            message_id="msg-1",
        )
        assert bindings.lookup("user-1") is not None

    def test_past_ttl_returns_none_after_advancing_clock(self):
        """Record fresh, then advance clock past TTL → lookup returns None."""
        now = [1_000_000.0]
        b = ActiveChannelBindings(ttl_seconds=120, clock=lambda: now[0])
        b.record(
            sponsor_user_id="user-1",
            chat_id="chat-A",
            graph_sent_at_epoch=1_000_000.0,
            message_id="msg-1",
        )
        assert b.lookup("user-1") is not None
        now[0] = 1_000_000.0 + 200.0  # 200s later, past TTL=120
        assert b.lookup("user-1") is None

    def test_expired_binding_is_evicted(self):
        """Once evicted, lookup remains None even if clock rewinds (no resurrection)."""
        now = [1_000_000.0]
        b = ActiveChannelBindings(ttl_seconds=120, clock=lambda: now[0])
        b.record(
            sponsor_user_id="user-1",
            chat_id="chat-A",
            graph_sent_at_epoch=1_000_000.0,
            message_id="msg-1",
        )
        now[0] = 1_000_000.0 + 200.0
        b.lookup("user-1")  # triggers eviction
        now[0] = 1_000_000.0 + 10.0  # rewind
        assert b.lookup("user-1") is None


class TestRejectInvalidTimestamps:
    def test_future_sent_at_rejected(self, bindings):
        result = bindings.record(
            sponsor_user_id="user-1",
            chat_id="chat-A",
            graph_sent_at_epoch=1_000_100.0,
            message_id="msg-1",
        )
        assert result is False
        assert bindings.lookup("user-1") is None

    def test_already_expired_sent_at_rejected(self, bindings):
        """Bootstrap-replay defense: don't store a message that's already too old."""
        result = bindings.record(
            sponsor_user_id="user-1",
            chat_id="chat-A",
            graph_sent_at_epoch=1_000_000.0 - 200.0,
            message_id="msg-1",
        )
        assert result is False
        assert bindings.lookup("user-1") is None

    def test_empty_sponsor_id_rejected(self, bindings):
        result = bindings.record(
            sponsor_user_id="",
            chat_id="chat-A",
            graph_sent_at_epoch=999_999.0,
            message_id="msg-1",
        )
        assert result is False

    def test_empty_chat_id_rejected(self, bindings):
        result = bindings.record(
            sponsor_user_id="user-1",
            chat_id="",
            graph_sent_at_epoch=999_999.0,
            message_id="msg-1",
        )
        assert result is False


class TestMultiSponsorIndependence:
    def test_two_sponsors_have_independent_bindings(self, bindings):
        bindings.record(
            sponsor_user_id="user-1",
            chat_id="chat-A",
            graph_sent_at_epoch=999_999.0,
            message_id="m1",
        )
        bindings.record(
            sponsor_user_id="user-2",
            chat_id="chat-B",
            graph_sent_at_epoch=999_999.0,
            message_id="m2",
        )
        assert bindings.lookup("user-1").chat_id == "chat-A"
        assert bindings.lookup("user-2").chat_id == "chat-B"


class TestReset:
    def test_reset_clears_all_bindings(self, bindings):
        bindings.record(
            sponsor_user_id="user-1",
            chat_id="chat-A",
            graph_sent_at_epoch=999_999.0,
            message_id="m1",
        )
        bindings.reset()
        assert bindings.lookup("user-1") is None


class TestSnapshot:
    def test_snapshot_returns_copy_for_audit(self, bindings):
        bindings.record(
            sponsor_user_id="user-1",
            chat_id="chat-A",
            graph_sent_at_epoch=999_999.0,
            message_id="m1",
        )
        snap = bindings.snapshot()
        assert "user-1" in snap
        snap.clear()
        assert bindings.lookup("user-1") is not None


class TestSingleton:
    def test_get_bindings_returns_same_instance(self):
        from entrabot.identity.active_channel import get_bindings

        assert get_bindings() is get_bindings()

    def test_reset_for_tests_clears_singleton_state(self):
        import time as _t

        from entrabot.identity.active_channel import (
            get_bindings,
            reset_for_tests,
        )

        # First, write into the singleton with a fresh-enough timestamp.
        get_bindings().record(
            sponsor_user_id="user-x",
            chat_id="chat-x",
            graph_sent_at_epoch=_t.time(),
            message_id="m-x",
        )
        assert get_bindings().lookup("user-x") is not None
        reset_for_tests()
        assert get_bindings().lookup("user-x") is None
