"""Plain console UI (port of Ui/ConsoleUi.cs).

A streaming, line-oriented UI that works in a real terminal and when stdin/stdout are
redirected. The session reads one line, runs a turn (streaming output prints as it
arrives), then prompts again — matching the .NET redirected-IO behavior.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Awaitable, Callable

from .. import ansi
from . import UI, BannerRows, UiStyle

_STYLE_FN = {
    UiStyle.USER: ansi.cyan,
    UiStyle.TOOL: ansi.dim,
    UiStyle.ERROR: ansi.red,
    UiStyle.WARN: ansi.yellow,
    UiStyle.DIM: ansi.dim,
    UiStyle.SUCCESS: ansi.green,
    UiStyle.INFO: ansi.blue,
    UiStyle.ASSISTANT: ansi.magenta,
    UiStyle.REASONING: ansi.dim,
    UiStyle.ACCENT: ansi.cyan,
}


def _style(text: str, style: UiStyle) -> str:
    fn = _STYLE_FN.get(style)
    return fn(text) if fn else text


class ConsoleUI(UI):
    def __init__(self) -> None:
        self._name = "entrabot"
        self._model = ""
        self._stop = asyncio.Event()
        self._open_line = False

    def banner(self, rows: BannerRows) -> None:
        if not ansi.ENABLED:
            print("ENTRABOT")
            return
        for row in rows:
            line = []
            for text, color in row:
                if color and color in ansi._CODES:
                    line.append(f"\x1b[{ansi._CODES[color]}m{text}\x1b[0m")
                else:
                    line.append(text)
            print("".join(line))

    def set_identity(self, name: str) -> None:
        self._name = name
        print(ansi.magenta(f"entrabot: {name}"))

    def set_status(self, left: str, right: str) -> None:
        self._model = right  # not continuously shown in plain mode

    def set_working(self, working: bool) -> None:
        pass

    def begin_assistant(self) -> None:
        self._end_line()
        sys.stdout.write(ansi.magenta("● "))
        sys.stdout.flush()
        self._open_line = True

    def append_inline(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()
        self._open_line = True

    def append_line(self, text: str, style: UiStyle = UiStyle.NORMAL) -> None:
        if text == "":
            self._end_line()  # close an open streamed line without printing a blank
            return
        self._end_line()
        print(_style(text, style))

    def _end_line(self) -> None:
        if self._open_line:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._open_line = False

    def set_commands(self, names) -> None:
        pass  # the plain console UI has no autocomplete

    def clear(self) -> None:
        try:
            print("\x1b[2J\x1b[H", end="")
        except Exception:
            pass

    async def confirm(self, title: str, message: str) -> bool:
        self._end_line()
        prompt = ansi.yellow(f"{title}: {message} [y/N] ")
        try:
            answer = await asyncio.to_thread(input, prompt)
        except EOFError:
            return False
        return answer.strip().lower() in ("y", "yes")

    async def run(self, on_submit, on_interrupt=None) -> None:
        while not self._stop.is_set():
            try:
                line = await asyncio.to_thread(input, ansi.cyan("› "))
            except (EOFError, KeyboardInterrupt):
                break
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            try:
                await on_submit(line)
            except (KeyboardInterrupt, asyncio.CancelledError):
                if on_interrupt:
                    await on_interrupt()  # Ctrl+C during a turn -> abort, keep the session alive

    def request_stop(self) -> None:
        self._stop.set()
