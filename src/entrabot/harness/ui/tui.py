"""Full-screen Textual TUI (port of Ui/TuiUi.cs).

A richer alternative to the console UI: a scrolling conversation log, an input box, and a
status footer. Opt-in (``ENTRABOT_TUI=1`` and ``textual`` installed); the console UI is the
default so the harness runs even without Textual.

Note: assistant output is buffered per line (flushed on line end) rather than per character
— a deliberate simplification vs. the .NET TUI's true character streaming.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from . import UI, BannerRows, UiStyle


def available() -> bool:
    try:
        import textual  # noqa: F401

        return True
    except ImportError:
        return False


_STYLE_MARKUP = {
    UiStyle.USER: "bright_cyan",
    UiStyle.TOOL: "white",
    UiStyle.ERROR: "bright_red",
    UiStyle.WARN: "yellow",
    UiStyle.DIM: "grey50",
    UiStyle.SUCCESS: "bright_green",
    UiStyle.INFO: "bright_blue",
    UiStyle.ASSISTANT: "bright_magenta",
    UiStyle.REASONING: "grey50",
    UiStyle.ACCENT: "bright_cyan",
    UiStyle.NORMAL: "white",
}

_BANNER_MARKUP = {
    "bright_blue": "bright_blue",
    "blue": "blue",
    "bright_magenta": "bright_magenta",
    "magenta": "magenta",
    "gray": "grey50",
}


def _escape(text: str) -> str:
    return text.replace("[", "\\[")


class TextualUI(UI):
    """Implemented lazily so importing this module never requires textual."""

    def __init__(self) -> None:
        from textual.app import App, ComposeResult
        from textual.widgets import Input, RichLog, Static

        ui = self

        class _App(App):
            CSS = """
            RichLog { height: 1fr; }
            #status { height: 1; color: $text-muted; }
            Input { border: round $accent; }
            """

            def compose(self) -> ComposeResult:  # type: ignore[override]
                yield RichLog(highlight=False, markup=True, wrap=True, id="log")
                yield Static("", id="status")
                yield Input(placeholder="message  ·  / for commands", id="prompt")

            def on_mount(self) -> None:
                self.query_one("#prompt", Input).focus()

            async def on_input_submitted(self, event) -> None:  # type: ignore[no-untyped-def]
                text = event.value.strip()
                self.query_one("#prompt", Input).value = ""
                if not text:
                    return
                if ui._pending_confirm is not None:
                    fut, ui._pending_confirm = ui._pending_confirm, None
                    fut.set_result(text.lower() in ("y", "yes"))
                    return
                if ui._on_submit is not None:
                    await ui._on_submit(text)

        self._App = _App
        self.app: Optional[App] = None
        self._on_submit: Optional[Callable[[str], Awaitable[None]]] = None
        self._cur: str = ""
        self._name = "entrabot"
        self._pending_confirm: Optional[asyncio.Future] = None

    # ---- helpers ---------------------------------------------------------------------
    def _log(self):
        from textual.widgets import RichLog

        return self.app.query_one("#log", RichLog) if self.app else None

    def _flush(self) -> None:
        if self._cur and self._log() is not None:
            self._log().write(self._cur)
        self._cur = ""

    # ---- UI protocol -----------------------------------------------------------------
    def banner(self, rows: BannerRows) -> None:
        log = self._log()
        if log is None:
            return
        for row in rows:
            line = "".join(
                f"[{_BANNER_MARKUP.get(color, 'white')}]{_escape(text)}[/]" if color else _escape(text)
                for text, color in row
            )
            log.write(line)

    def set_identity(self, name: str) -> None:
        self._name = name

    def set_status(self, left: str, right: str) -> None:
        if self.app:
            from textual.widgets import Static

            self.app.query_one("#status", Static).update(
                f"entrabot: {self._name}   ·   {left}   ·   {right}"
            )

    def set_working(self, working: bool) -> None:
        pass

    def begin_assistant(self) -> None:
        self._flush()
        self._cur = "[bright_magenta]●[/] "

    def append_inline(self, text: str) -> None:
        self._cur += _escape(text)

    def append_line(self, text: str, style: UiStyle = UiStyle.NORMAL) -> None:
        if text == "":
            self._flush()  # close an open streamed line
            return
        self._flush()
        log = self._log()
        if log is not None:
            color = _STYLE_MARKUP.get(style, "white")
            log.write(f"[{color}]{_escape(text)}[/]")

    def clear(self) -> None:
        log = self._log()
        if log is not None:
            log.clear()

    async def confirm(self, title: str, message: str) -> bool:
        self._flush()
        log = self._log()
        if log is not None:
            log.write(f"[yellow]{_escape(title)}: {_escape(message)} (y/N)[/]")
        loop = asyncio.get_event_loop()
        self._pending_confirm = loop.create_future()
        return await self._pending_confirm

    async def run(self, on_submit: "Callable[[str], Awaitable[None]]") -> None:
        self._on_submit = on_submit
        self.app = self._App()
        await self.app.run_async()

    def request_stop(self) -> None:
        if self.app:
            self.app.exit()
