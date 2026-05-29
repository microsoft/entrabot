"""Tests for the long-blocking sponsor-DM wait tool.

This tool is the primary integration path for Copilot CLI and an
opt-in path for Claude Code. See ``docs/architecture/PLAN-copilot-cli-watcher.md``.
"""

from __future__ import annotations

import asyncio
from collections import deque

import pytest

from entrabot.identity.sponsors import (
    AgentIdentitySponsor,
    SponsorGate,
)
from entrabot.tools.wait_tool import (
    DEDUP_MAX,
    WaitForSponsorDmResult,
    _injection_dedupe_key,
    select_sponsor_message,
    wait_animation_frame,
    wait_listener_banner,
    wait_loop,
)


def _make_gate() -> SponsorGate:
    sponsor = AgentIdentitySponsor(
        user_id="sponsor-user-1",
        user_principal_name="alice@example.com",
        mail="alice@example.com",
    )
    return SponsorGate.from_agent_identity_sponsors([sponsor])


def _msg(message_id: str, sender_id: str, sent_at: str, **extra) -> dict:
    return {
        "message_id": message_id,
        "sender_id": sender_id,
        "sender": extra.pop("sender", "alice@example.com"),
        "sent_at": sent_at,
        "content_text": extra.pop("content_text", "hello"),
        **extra,
    }


def test_dedup_key_uses_chat_id_and_message_id() -> None:
    msg = {"chat_id": "chat-1", "message_id": "m-1"}
    assert _injection_dedupe_key(msg) == ("chat-1", "m-1")


def test_dedup_key_returns_none_for_missing_ids() -> None:
    assert _injection_dedupe_key({}) is None


def test_select_sponsor_message_returns_sponsor_match() -> None:
    gate = _make_gate()
    dedup: deque[tuple[str, str]] = deque()
    messages = [
        _msg("m-1", "stranger-id", "2026-01-01T00:00:00Z", sender="bob@example.com"),
        _msg("m-2", "sponsor-user-1", "2026-01-01T00:00:01Z"),
    ]
    picked = select_sponsor_message(messages, gate=gate, dedup=dedup)
    assert picked is not None and picked["message_id"] == "m-2"


def test_select_sponsor_message_rejects_non_sponsor() -> None:
    gate = _make_gate()
    dedup: deque[tuple[str, str]] = deque()
    messages = [
        _msg("m-1", "stranger-id", "2026-01-01T00:00:00Z", sender="bob@example.com"),
    ]
    assert select_sponsor_message(messages, gate=gate, dedup=dedup) is None


def test_select_sponsor_message_skips_dedup_hits() -> None:
    gate = _make_gate()
    dedup: deque[tuple[str, str]] = deque([("chat-1", "m-2")])
    messages = [_msg("m-2", "sponsor-user-1", "2026-01-01T00:00:01Z", chat_id="chat-1")]
    assert select_sponsor_message(messages, gate=gate, dedup=dedup) is None


def test_select_sponsor_message_filters_messages_at_or_before_started_at() -> None:
    gate = _make_gate()
    dedup: deque[tuple[str, str]] = deque()
    messages = [
        _msg("m-1", "sponsor-user-1", "2026-01-01T00:00:00Z"),
        _msg("m-2", "sponsor-user-1", "2026-01-01T00:00:05Z"),
    ]
    picked = select_sponsor_message(
        messages, gate=gate, dedup=dedup, after_iso="2026-01-01T00:00:00Z"
    )
    assert picked is not None and picked["message_id"] == "m-2"


def test_select_sponsor_message_returns_oldest_eligible() -> None:
    gate = _make_gate()
    dedup: deque[tuple[str, str]] = deque()
    messages = [
        _msg("m-late", "sponsor-user-1", "2026-01-01T00:00:10Z"),
        _msg("m-early", "sponsor-user-1", "2026-01-01T00:00:01Z"),
    ]
    picked = select_sponsor_message(messages, gate=gate, dedup=dedup)
    assert picked is not None and picked["message_id"] == "m-early"


@pytest.mark.asyncio
async def test_wait_loop_returns_first_sponsor_message() -> None:
    gate = _make_gate()
    dedup: deque[tuple[str, str]] = deque()
    sponsor_msg = _msg("m-1", "sponsor-user-1", "2026-04-28T12:00:01Z", chat_id="chat-1")

    async def read_chat(chat_id: str) -> list[dict]:
        return [sponsor_msg]

    picked = await wait_loop(
        list_chat_ids=lambda: ["chat-1"],
        read_chat=read_chat,
        gate=gate,
        dedup=dedup,
        sleep=lambda _s: asyncio.sleep(0),
        started_at_iso="2026-04-28T12:00:00Z",
        poll_interval_s=0.0,
    )
    assert picked["message_id"] == "m-1"
    assert ("chat-1", "m-1") in dedup


@pytest.mark.asyncio
async def test_wait_loop_skips_non_sponsor_until_sponsor_arrives() -> None:
    gate = _make_gate()
    dedup: deque[tuple[str, str]] = deque()
    state = {"calls": 0}

    async def read_chat(chat_id: str) -> list[dict]:
        state["calls"] += 1
        if state["calls"] < 3:
            return [
                _msg(
                    "m-x",
                    "stranger-id",
                    "2026-04-28T12:00:01Z",
                    chat_id="chat-1",
                    sender="bob@example.com",
                )
            ]
        return [_msg("m-1", "sponsor-user-1", "2026-04-28T12:00:05Z", chat_id="chat-1")]

    picked = await wait_loop(
        list_chat_ids=lambda: ["chat-1"],
        read_chat=read_chat,
        gate=gate,
        dedup=dedup,
        sleep=lambda _s: asyncio.sleep(0),
        started_at_iso="2026-04-28T12:00:00Z",
        poll_interval_s=0.0,
    )
    assert picked["message_id"] == "m-1"
    assert state["calls"] >= 3


@pytest.mark.asyncio
async def test_wait_loop_dedup_evicts_oldest_when_full() -> None:
    gate = _make_gate()
    dedup: deque[tuple[str, str]] = deque([(f"chat-{i}", f"m-{i}") for i in range(DEDUP_MAX)])
    sponsor_msg = _msg("m-new", "sponsor-user-1", "2026-04-28T12:00:01Z", chat_id="chat-x")

    async def read_chat(chat_id: str) -> list[dict]:
        return [sponsor_msg]

    await wait_loop(
        list_chat_ids=lambda: ["chat-x"],
        read_chat=read_chat,
        gate=gate,
        dedup=dedup,
        sleep=lambda _s: asyncio.sleep(0),
        started_at_iso="2026-04-28T12:00:00Z",
        poll_interval_s=0.0,
    )
    assert len(dedup) == DEDUP_MAX
    assert ("chat-x", "m-new") in dedup
    assert ("chat-0", "m-0") not in dedup


@pytest.mark.asyncio
async def test_wait_loop_cancellation_propagates() -> None:
    gate = _make_gate()
    dedup: deque[tuple[str, str]] = deque()

    async def read_chat(chat_id: str) -> list[dict]:
        return []

    async def slow_sleep(_s: float) -> None:
        await asyncio.sleep(0.1)

    task = asyncio.create_task(
        wait_loop(
            list_chat_ids=lambda: ["chat-1"],
            read_chat=read_chat,
            gate=gate,
            dedup=dedup,
            sleep=slow_sleep,
            started_at_iso="2026-04-28T12:00:00Z",
            poll_interval_s=0.05,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_wait_loop_continues_when_one_chat_read_fails() -> None:
    gate = _make_gate()
    dedup: deque[tuple[str, str]] = deque()
    sponsor_msg = _msg("m-1", "sponsor-user-1", "2026-04-28T12:00:01Z", chat_id="chat-2")

    async def read_chat(chat_id: str) -> list[dict]:
        if chat_id == "chat-1":
            raise RuntimeError("graph blew up")
        return [sponsor_msg]

    picked = await wait_loop(
        list_chat_ids=lambda: ["chat-1", "chat-2"],
        read_chat=read_chat,
        gate=gate,
        dedup=dedup,
        sleep=lambda _s: asyncio.sleep(0),
        started_at_iso="2026-04-28T12:00:00Z",
        poll_interval_s=0.0,
    )
    assert picked["chat_id"] == "chat-2"
    assert picked["message_id"] == "m-1"


def test_wait_for_sponsor_dm_result_serializes_to_json() -> None:
    result = WaitForSponsorDmResult(
        chat_id="chat-1",
        message_id="m-1",
        sender="alice@example.com",
        sender_id="sponsor-user-1",
        sent_at="2026-04-28T12:00:01Z",
        content_text="hi",
        chat_type="oneOnOne",
    )
    import json

    payload = json.loads(result.to_json())
    assert payload["chat_id"] == "chat-1"
    assert payload["message_id"] == "m-1"
    assert payload["content_text"] == "hi"
    assert payload["timed_out"] is False
    assert payload["chat_type"] == "oneOnOne"


def test_wait_for_sponsor_dm_result_timeout_is_structured() -> None:
    """Timeout MUST return a structured payload, not raise. A bare TimeoutError
    surfaces as an empty MCP error in Copilot CLI / Claude Code, leaving the
    LLM unable to recover."""
    import json

    result = WaitForSponsorDmResult.timeout(timeout_seconds=20)
    payload = json.loads(result.to_json())
    assert payload["timed_out"] is True
    assert payload["chat_id"] == ""
    assert payload["message_id"] == ""
    assert payload["metadata"]["timeout_seconds"] == 20
    assert payload["chat_type"] == ""


# --- ASCII wait animation -----------------------------------------------


class TestWaitAnimationFrame:
    """The animation is the operator-facing signal that this CLI is parked
    in a Teams wait. The terminal looks idle, the model has 'returned
    control' from the operator's POV, but a Teams DM will land here as
    next-turn input. The frame must scream 'I'M LISTENING TO TEAMS, NOT
    YOUR KEYBOARD' so the operator knows to either wait or break out
    with Ctrl+C."""

    def test_returns_a_nonempty_string(self) -> None:
        assert wait_animation_frame(elapsed_s=0.0)
        assert isinstance(wait_animation_frame(elapsed_s=0.0), str)

    def test_frame_advances_with_elapsed_time(self) -> None:
        # Two distant elapsed values must not produce the same frame, or
        # the animation looks frozen and the operator can't tell whether
        # the wait is alive.
        a = wait_animation_frame(elapsed_s=0.0)
        b = wait_animation_frame(elapsed_s=10.0)
        assert a != b

    def test_frame_is_deterministic_for_same_elapsed(self) -> None:
        # Pure function — same input, same output. Required for the test
        # above to be meaningful and for the heartbeat to be replayable.
        assert wait_animation_frame(elapsed_s=42.0) == wait_animation_frame(elapsed_s=42.0)

    def test_frame_mentions_ctrl_c_break_path(self) -> None:
        # The operator MUST know how to leave the wait. Hiding the escape
        # hatch behind documentation is a footgun. Surface it in every frame.
        frame = wait_animation_frame(elapsed_s=0.0)
        assert "Ctrl" in frame or "ctrl" in frame.lower()

    def test_frame_signals_teams_listening_state(self) -> None:
        # The frame must name the channel so the operator knows their
        # keyboard input won't reach the agent — Teams will.
        frame = wait_animation_frame(elapsed_s=5.0).lower()
        assert "teams" in frame or "dm" in frame or "sponsor" in frame

    def test_frame_includes_elapsed_seconds(self) -> None:
        # Elapsed-time hint helps the operator decide whether to wait
        # another beat or break out and try a different approach.
        frame = wait_animation_frame(elapsed_s=125.0)
        # Either "2m" / "125s" / "2:05" — any human-readable elapsed
        # marker counts; we just want the number to surface somewhere.
        assert any(token in frame for token in ("125", "2m", "2:0"))


class TestWaitListenerBanner:
    """One-shot startup splash shown when the agent enters
    ``wait_for_sponsor_dm``. Operators looking at an idle terminal must
    know (a) the CLI is alive, (b) it's listening to Teams not their
    keyboard, (c) how to escape, (d) which host CLI gives the full
    push experience. The banner answers all four in one beat before
    the cycling status frames take over."""

    def test_returns_a_nonempty_multiline_string(self) -> None:
        banner = wait_listener_banner()
        assert isinstance(banner, str)
        assert banner.strip()
        assert "\n" in banner, "banner should be multi-line ASCII art"

    def test_banner_mentions_listening(self) -> None:
        banner = wait_listener_banner().lower()
        assert "listen" in banner

    def test_banner_mentions_ctrl_c_escape(self) -> None:
        banner = wait_listener_banner()
        assert "Ctrl" in banner or "ctrl" in banner.lower()

    def test_banner_mentions_claude_code_for_full_experience(self) -> None:
        # Copilot CLI doesn't subscribe to notifications/claude/channel,
        # so the operator should know that Claude Code gives the full
        # push experience.
        banner = wait_listener_banner().lower()
        assert "claude" in banner

    def test_banner_includes_color_codes_when_color_enabled(self) -> None:
        # ANSI escape sequence presence — the banner is supposed to be
        # colorful in a real terminal.
        banner = wait_listener_banner(color=True)
        assert "\x1b[" in banner

    def test_banner_strips_color_when_disabled(self) -> None:
        # NO_COLOR / dumb terminals get a plain version with no escapes.
        banner = wait_listener_banner(color=False)
        assert "\x1b[" not in banner

    def test_banner_is_deterministic(self) -> None:
        # Same input, same output. No randomness — this is a splash, not
        # a slot machine.
        assert wait_listener_banner(color=False) == wait_listener_banner(color=False)

    def test_banner_contains_a_dog(self) -> None:
        # The user explicitly asked for a dog. Loose check: at least one
        # canonical dog-art glyph or 'dog' word should appear so we don't
        # accidentally regress to a cat or a penguin.
        banner = wait_listener_banner(color=False)
        # Common ASCII-dog glyphs: U+1F436 emoji, the "(__)`" snout, or
        # the literal word "dog" in adjacent prose. Any one suffices.
        assert "🐕" in banner or "🐶" in banner or "(__)" in banner or "dog" in banner.lower()

    def test_banner_with_elapsed_shows_time(self) -> None:
        # Heartbeats re-emit the banner with an elapsed-seconds suffix
        # so the dog stays visible while the operator still gets a
        # liveness signal. Otherwise Copilot CLI's progress overwrite
        # would replace the dog with a single-line frame.
        banner = wait_listener_banner(color=False, elapsed_s=125.0)
        assert "2m" in banner or "2:0" in banner or "125" in banner

    def test_banner_without_elapsed_omits_time(self) -> None:
        # Initial splash has no elapsed time — clean banner.
        banner = wait_listener_banner(color=False)
        assert "0s" not in banner
        assert "[0" not in banner

    def test_banner_elapsed_does_not_break_color(self) -> None:
        banner = wait_listener_banner(color=True, elapsed_s=42.0)
        assert "\x1b[" in banner


# --- Anti-regression: tool doctrine names the broadened trigger ---------


class TestBroadenedWaitDoctrine:
    """After 2026-04-28 we broadened the wait protocol from 'long-running
    work + ping me when done' to 'any proactive 1:1 Teams DM'. The
    triggering rule must live (1) in the body prompt anatomy fragment
    and (2) in the wait tool's MCP description, because per Learning #48
    those are the only two reliable injection vectors into Copilot CLI."""

    def test_channel_discipline_broadened_trigger_present(self) -> None:
        from pathlib import Path

        text = (
            Path(__file__).resolve().parents[2] / "prompts/anatomy/channel-discipline.md"
        ).read_text(encoding="utf-8")
        # The broadened rule must be findable as a single phrase. Lock
        # to a canonical wording so the doctrine can't silently drift
        # back to the narrow 'long-running' framing.
        assert "any proactive" in text.lower() or "any time you proactively" in text.lower()
        # And it must connect that broadened trigger to wait_for_sponsor_dm.
        assert "wait_for_sponsor_dm" in text

    def test_mcp_tool_docstring_carries_broadened_trigger(self) -> None:
        from pathlib import Path

        mcp_src = (Path(__file__).resolve().parents[2] / "src/entrabot/mcp_server.py").read_text(
            encoding="utf-8"
        )
        # The wait_for_sponsor_dm tool body in mcp_server.py must
        # surface the broadened rule, since Copilot CLI only reads tool
        # descriptions reliably (Learning #48).
        assert "any proactive" in mcp_src.lower() or "any time you proactively" in mcp_src.lower()
