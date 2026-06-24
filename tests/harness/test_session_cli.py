"""CLI caller-class + reply routing: local terminal input is its own 'cli' caller class, gets
echoed to the transcript, and is framed as a [cli] turn so the agent replies in the terminal."""

import asyncio
from types import SimpleNamespace

from entrabot.harness.session import InteractiveSession
from entrabot.harness.ui import UiStyle


def test_caller_class_local_operator_is_cli():
    s = SimpleNamespace(_ctx=SimpleNamespace(caller=None), _sponsors=set())
    assert InteractiveSession._caller_class(s) == "cli"


def test_caller_class_sponsor_vs_guest():
    sponsor = SimpleNamespace(_ctx=SimpleNamespace(caller="u1"), _sponsors={"u1"})
    guest = SimpleNamespace(_ctx=SimpleNamespace(caller="u2"), _sponsors={"u1"})
    assert InteractiveSession._caller_class(sponsor) == "sponsor"
    assert InteractiveSession._caller_class(guest) == "guest"


async def test_send_echoes_raw_line_and_frames_as_cli():
    lines: list = []
    captured: dict = {}
    idle = asyncio.Event()

    class FakeUI:
        def append_line(self, text, style=None):
            lines.append((text, style))

        def set_working(self, v):
            pass

    class FakeSession:
        async def send(self, prompt, agent_mode=None):
            captured["prompt"] = prompt
            idle.set()  # stand in for the SESSION_IDLE that unblocks _send

    s = SimpleNamespace(
        _idle=idle, _ui=FakeUI(), _inject_lock=asyncio.Lock(),
        _injected={}, _session=FakeSession(), _mode="autopilot",
    )
    await InteractiveSession._send(s, "hello there")

    # B: the operator's own line is echoed into the transcript, in the USER style
    assert ("hello there", UiStyle.USER) in lines
    # C: the agent receives a [cli]-framed prompt that says reply in the terminal, not Teams
    framed = captured["prompt"]
    assert framed.startswith("[cli]")
    assert "hello there" in framed
    assert "entrabot_send" in framed  # the do-not-use instruction
    # tracked as injected with no caller/chat so the echo is swallowed and the turn binds to "cli"
    assert s._injected[framed] == (None, None)
