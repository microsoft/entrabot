"""Input handling, turn dispatch, steering injection, and streamed-event rendering."""

from __future__ import annotations

from typing import Any

import copilot

from ..ui import UiStyle

_ET = copilot.SessionEventType
_MAX_ARG_PREVIEW = 60  # characters of a tool's arguments shown in the transcript


def _short(args: Any) -> str:
    text = str(args) if args is not None else ""
    return (text[:_MAX_ARG_PREVIEW] + "…") if len(text) > _MAX_ARG_PREVIEW else text


class _EventsMixin:
    """Routes operator/Teams input into turns and renders the session's event stream."""

    async def _handle_input(self, line: str) -> None:
        if line.startswith("/"):
            await self._handle_slash(line)
        else:
            await self._send(line)

    async def _send(self, prompt: str) -> None:
        self._idle.clear()
        self._ui.set_working(True)
        self._ui.append_line(prompt, UiStyle.USER)  # echo the operator's line into the transcript
        # Frame local input as a [cli] turn so the agent replies in the terminal, not Teams; track
        # it as injected with no caller/chat so its echo is swallowed and the turn binds to "cli".
        framed = (
            "[cli] Terminal input from your operator (reply here in the terminal; do NOT use "
            f"entrabot_send or post to Teams for this turn).\nmessage: {prompt}"
        )
        async with self._inject_lock:
            self._injected[framed] = (None, None)
        try:
            await self._session.send(framed, agent_mode=self._mode)
        except Exception as error:
            self._ui.append_line(f"send failed: {error}", UiStyle.ERROR)
            self._idle.set()
            self._ui.set_working(False)
            return
        await self._idle.wait()

    async def _inject(self, prompt: str, caller=None, chat=None) -> None:
        """Inject steering (a Teams message or scheduled prompt) into the session.

        Delivered with ``mode="immediate"`` — the SDK *interjects* it into an in-progress turn at
        the next step boundary (e.g. between tool calls) without aborting in-flight work, rather
        than ``enqueue`` (the default), which would make it wait for the whole turn to finish. So a
        Teams DM that lands mid-turn is handled as soon as the agent is free, not after everything.

        The caller + chat travel with the prompt so the session can bind them to the turn
        that the message kicks off (see USER_MESSAGE handling in _on_event).
        """
        if not self._session:
            return
        async with self._inject_lock:
            self._injected[prompt] = (caller, chat)
            try:
                await self._session.send(prompt, mode="immediate", agent_mode=self._mode)
            except Exception as error:
                self._ui.append_line(f"[inject failed] {error}", UiStyle.ERROR)

    def _on_event(self, event: copilot.SessionEvent) -> None:
        event_type = event.type
        data = event.data
        if event_type == _ET.ASSISTANT_MESSAGE_START:
            self._streamed = False
            self._ui.begin_assistant()
        elif event_type == _ET.ASSISTANT_MESSAGE_DELTA:
            self._streamed = True
            self._ui.append_inline(getattr(data, "delta_content", "") or "")
        elif event_type == _ET.ASSISTANT_MESSAGE:
            if not self._streamed:
                self._ui.append_line(getattr(data, "content", "") or "", UiStyle.ASSISTANT)
            else:
                self._ui.append_line("")  # close the streamed line
        elif event_type == _ET.ASSISTANT_REASONING:
            text = getattr(data, "content", "") or ""
            if text:
                self._ui.append_line(text, UiStyle.REASONING)
        elif event_type == _ET.TOOL_EXECUTION_START:
            name = getattr(data, "tool_name", "tool")
            args = getattr(data, "arguments", None)
            self._ui.append_line(f"⚙ {name} {_short(args)}", UiStyle.TOOL)
        elif event_type == _ET.TOOL_EXECUTION_COMPLETE:
            if getattr(data, "success", True) is False:
                err = getattr(getattr(data, "error", None), "message", "") or "tool failed"
                self._ui.append_line(f"  ✗ {err}", UiStyle.ERROR)
        elif event_type == _ET.SESSION_ERROR:
            message = getattr(data, "message", "session error") or "session error"
            self._ui.append_line(message, UiStyle.ERROR)
        elif event_type == _ET.USER_MESSAGE:
            content = getattr(data, "content", "") or ""
            if content in self._injected:
                # our own injected steering echo: bind its caller/chat to this turn + swallow
                caller, chat = self._injected.pop(content)
                self._ctx.caller, self._ctx.chat = caller, chat
        elif event_type == _ET.SESSION_IDLE:
            self._ui.append_line("")
            self._ctx.caller = self._ctx.chat = None  # turn over; back to operator/no caller
            self._ui.set_working(False)
            self._idle.set()
