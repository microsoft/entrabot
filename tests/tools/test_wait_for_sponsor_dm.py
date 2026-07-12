"""Tests for the long-blocking sponsor-DM wait tool.

This tool is the primary integration path for Copilot CLI and an
opt-in path for Claude Code. See ``docs/clients/overview.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

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


@pytest.mark.asyncio
async def test_wait_for_sponsor_dm_populates_chat_type_from_config() -> None:
    """wait_for_sponsor_dm must thread oneOnOne/group chat_type into the result."""
    from entrabot import mcp_server
    from entrabot.config import EntraBotConfig

    cfg = EntraBotConfig(
        tenant_id="tenant-id",
        blueprint_app_id="blueprint-app-id",
        agent_id="agent-id",
        agent_user_upn="entrabot@example.com",
    )
    picked = _msg(
        "message-1",
        "sponsor-user-1",
        "2026-04-28T12:00:01Z",
        chat_id="known-chat-id",
        content="<p>Hello</p>",
    )

    old_state = mcp_server._state.copy()
    old_identity = mcp_server._identity
    old_logger = mcp_server.logger
    try:
        mcp_server.logger = logging.getLogger("entrabot.mcp_server")
        mcp_server._identity = None
        mcp_server._state.clear()
        mcp_server._state.update(
            {
                "config": cfg,
                "watched_chats": {"known-chat-id": {}},
                "wait_tool_dedup": deque(),
            }
        )

        with (
            patch("entrabot.mcp_server._initialize", new=AsyncMock(return_value=None)),
            patch("entrabot.tools.audit.log_event", new=MagicMock()),
            patch(
                "entrabot.identity.sponsors.load_agent_identity_sponsor_gate",
                return_value=_make_gate(),
            ),
            patch(
                "entrabot.tools.wait_tool.wait_loop",
                new=AsyncMock(return_value=picked),
            ),
            patch(
                "entrabot.tools.teams.acquire_agent_user_token",
                return_value="agent-token",
            ) as acquire,
            patch(
                "entrabot.tools.teams.fetch_chat_type",
                new=AsyncMock(return_value="oneOnOne"),
            ) as fetch_chat_type,
        ):
            result = json.loads(await mcp_server.wait_for_sponsor_dm(timeout_seconds=1))

        assert result["chat_id"] == "known-chat-id"
        assert result["chat_type"] == "oneOnOne"
        acquire.assert_called_once_with(cfg)
        fetch_chat_type.assert_awaited_once_with(chat_id="known-chat-id", token="agent-token")
    finally:
        mcp_server._state.clear()
        mcp_server._state.update(old_state)
        mcp_server._identity = old_identity
        mcp_server.logger = old_logger


@pytest.mark.asyncio
async def test_wait_for_sponsor_dm_logs_warning_when_chat_type_lookup_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """chat_type lookup failures must be visible instead of silently swallowed."""
    from entrabot import mcp_server
    from entrabot.config import EntraBotConfig

    cfg = EntraBotConfig(
        tenant_id="tenant-id",
        blueprint_app_id="blueprint-app-id",
        agent_id="agent-id",
        agent_user_upn="entrabot@example.com",
    )
    picked = _msg(
        "message-1",
        "sponsor-user-1",
        "2026-04-28T12:00:01Z",
        chat_id="known-chat-id",
        content="<p>Hello</p>",
    )

    old_state = mcp_server._state.copy()
    old_identity = mcp_server._identity
    old_logger = mcp_server.logger
    try:
        mcp_server.logger = logging.getLogger("entrabot.mcp_server")
        mcp_server._identity = None
        mcp_server._state.clear()
        mcp_server._state.update(
            {
                "config": cfg,
                "watched_chats": {"known-chat-id": {}},
                "wait_tool_dedup": deque(),
            }
        )

        with (
            caplog.at_level("WARNING", logger="entrabot.mcp_server"),
            patch("entrabot.mcp_server._initialize", new=AsyncMock(return_value=None)),
            patch("entrabot.tools.audit.log_event", new=MagicMock()),
            patch(
                "entrabot.identity.sponsors.load_agent_identity_sponsor_gate",
                return_value=_make_gate(),
            ),
            patch("entrabot.tools.wait_tool.wait_loop", new=AsyncMock(return_value=picked)),
            patch(
                "entrabot.tools.teams.acquire_agent_user_token",
                side_effect=AttributeError("missing credential field"),
            ),
        ):
            result = json.loads(await mcp_server.wait_for_sponsor_dm(timeout_seconds=1))

        assert result["chat_type"] == ""
        assert "Failed to detect chat_type for chat known-chat-id" in caplog.text
        assert "AttributeError" in caplog.text
    finally:
        mcp_server._state.clear()
        mcp_server._state.update(old_state)
        mcp_server._identity = old_identity
        mcp_server.logger = old_logger


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


# --- Anti-regression: tool doctrine matches the host-gated wait pattern --


class TestHostGatedWaitDoctrine:
    """As of the host-gated sponsor-DM rewrite, manual waiting is no
    longer the default after any proactive Teams DM. The current
    doctrine (see ``prompts/anatomy/channel-discipline.md`` and
    Learning #54) is:

    - Channel-push hosts (Claude Code): ``send_teams_message`` returns
      immediately; the sponsor's reply arrives as a later channel
      notification. Never manually wait.
    - Non-channel-push hosts (Copilot CLI, Codex, etc.):
      ``send_teams_message`` auto-waits server-side and returns
      ``sponsor_reply``. Never manually wait.
    - ``wait_for_sponsor_dm`` is reserved for an operator's *explicit*
      request to block until a sponsor replies mid-task.
    - ``watch_teams_replies`` is explicit direct polling/fallback, never
      the normal completion path after ``send_teams_message``.

    These tests pin those constraints directly against the tool
    docstrings/source in ``src/entrabot/mcp_server.py`` so a docstring
    rewrite can't silently reintroduce double-waiting.
    """

    def test_channel_discipline_describes_proactive_dm_wait_state(self) -> None:
        from pathlib import Path

        text = (
            Path(__file__).resolve().parents[2] / "prompts/anatomy/channel-discipline.md"
        ).read_text(encoding="utf-8")
        text_lower = text.lower()
        # The doc describes *why* wait state exists (a proactive DM's
        # reply lands in Teams, not the CLI) — it must not prescribe
        # manually invoking wait_for_sponsor_dm as the default response.
        assert "any proactive" in text_lower or "any time you proactively" in text_lower
        assert "wait_for_sponsor_dm" in text
        # It must be explicit that wait_for_sponsor_dm is reserved for an
        # explicit operator request, not a default after every send.
        assert "explicitly" in text_lower
        assert "rarely the right tool" in text_lower or "only when" in text_lower

    def test_wait_for_sponsor_dm_docstring_requires_explicit_operator_request(self) -> None:
        from pathlib import Path

        mcp_src = (Path(__file__).resolve().parents[2] / "src/entrabot/mcp_server.py").read_text(
            encoding="utf-8"
        )
        from entrabot import mcp_server

        docstring = mcp_server.wait_for_sponsor_dm.__doc__ or ""
        docstring_lower = docstring.lower()

        # Stale doctrine: "call this after every proactive DM" must be gone.
        assert "any time you proactively" not in docstring_lower, (
            "wait_for_sponsor_dm docstring still tells the model to call "
            "it after every proactive DM — this causes double-waiting on "
            "hosts where send_teams_message already auto-waits."
        )
        assert "after every such proactive" not in docstring_lower, (
            "wait_for_sponsor_dm docstring still frames itself as the "
            "default follow-up to any proactive DM instead of an "
            "explicit operator request."
        )
        # Current doctrine: only call it when the operator explicitly asks
        # to block mid-task.
        assert "explicit" in docstring_lower, (
            "wait_for_sponsor_dm docstring must say it is reserved for an "
            "operator's explicit request to block until a sponsor replies."
        )
        assert (
            "operator" in docstring_lower or "sponsor" in docstring_lower
        ), "wait_for_sponsor_dm docstring must name who must ask for the wait."
        # Sanity: the whole source file shouldn't carry the stale phrase
        # anywhere else near this tool (e.g. in a stray comment).
        assert "after every such proactive 1:1 dm" not in mcp_src.lower()

    def test_watch_teams_replies_docstring_is_not_default_send_completion(self) -> None:
        from entrabot import mcp_server

        docstring = mcp_server.watch_teams_replies.__doc__ or ""
        docstring_lower = docstring.lower()

        # Stale doctrine: "Always after send_teams_message" made this look
        # like the normal completion step of every send, which conflicts
        # with send_teams_message's own built-in auto-wait.
        assert "always after send_teams_message" not in docstring_lower, (
            "watch_teams_replies docstring still claims it should always "
            "be called after send_teams_message — that is not the normal "
            "completion path; send_teams_message already auto-waits where "
            "needed, and channel-push hosts get the reply via next-turn "
            "notification."
        )
        # Current doctrine: this tool is explicit direct polling / a
        # fallback, not the standard send-then-watch loop.
        assert "fallback" in docstring_lower or "explicit" in docstring_lower, (
            "watch_teams_replies docstring must describe itself as "
            "explicit direct polling/fallback, not the default reply path."
        )
