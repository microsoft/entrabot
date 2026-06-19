"""Full-screen Textual TUI (port of Ui/TuiUi.cs).

A richer alternative to the console UI: a scrolling conversation log, a live (character-
streamed) assistant line, slash-command autocomplete, an input box, and a status footer.
Opt-in (``ENTRABOT_TUI=1`` and ``textual`` installed); the console UI is the default so the
harness runs even without Textual.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, List, Optional

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
    """Built lazily so importing this module never requires textual."""

    def __init__(self) -> None:
        from textual.app import App, ComposeResult
        from textual.widgets import Input, RichLog, Static

        ui = self

        class _App(App):
            CSS = """
            RichLog { height: 1fr; }
            #live { height: auto; }
            #suggest { height: auto; color: $text-muted; }
            #status { height: 1; color: $text-muted; }
            Input { border: round $accent; }
            """

            def compose(self) -> ComposeResult:  # type: ignore[override]
                yield RichLog(highlight=False, markup=True, wrap=True, id="log")
                yield Static("", id="live")
                yield Static("", id="suggest")
                yield Static("", id="status")
                yield Input(placeholder="message  ·  / for commands", id="prompt")

            def on_mount(self) -> None:
                self.query_one("#prompt", Input).focus()

            def on_input_changed(self, event) -> None:  # type: ignore[no-untyped-def]
                ui._update_suggest(event.value)

            async def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
                if event.key == "tab" and ui._suggestions:
                    self.query_one("#prompt", Input).value = ui._suggestions[0] + " "
                    ui._update_suggest("")
                    event.prevent_default()
                    event.stop()

            async def on_input_submitted(self, event) -> None:  # type: ignore[no-untyped-def]
                text = event.value.strip()
                self.query_one("#prompt", Input).value = ""
                ui._update_suggest("")
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
        self._cur: str = ""  # raw text of the in-progress assistant line
        self._assist = False
        self._name = "entrabot"
        self._commands: List[str] = []
        self._suggestions: List[str] = []
        self._pending_confirm: Optional[asyncio.Future] = None

    # ---- widgets ---------------------------------------------------------------------
    def _w(self, sel: str):
        from textual.widgets import RichLog, Static  # noqa: F401

        return self.app.query_one(sel) if self.app else None

    def _render_live(self) -> None:
        live = self._w("#live")
        if live is None:
            return
        if self._assist:
            live.update(f"[bright_magenta]●[/] {_escape(self._cur)}")
        else:
            live.update("")

    def _flush(self) -> None:
        log = self._w("#log")
        if self._assist and self._cur and log is not None:
            log.write(f"[bright_magenta]●[/] {_escape(self._cur)}")
        self._cur = ""
        self._assist = False
        self._render_live()

    def _update_suggest(self, value: str) -> None:
        if value.startswith("/") and " " not in value:
            self._suggestions = [c for c in self._commands if c.startswith(value.lower())][:8]
        else:
            self._suggestions = []
        sug = self._w("#suggest")
        if sug is not None:
            if self._suggestions:
                sug.update("  ".join(f"[bright_cyan]{s}[/]" for s in self._suggestions) + "   [grey50](tab)[/]")
            else:
                sug.update("")

    # ---- UI protocol -----------------------------------------------------------------
    def banner(self, rows: BannerRows) -> None:
        log = self._w("#log")
        if log is None:
            return
        for row in rows:
            log.write(
                "".join(
                    f"[{_BANNER_MARKUP.get(color, 'white')}]{_escape(text)}[/]" if color else _escape(text)
                    for text, color in row
                )
            )

    def set_identity(self, name: str) -> None:
        self._name = name

    def set_status(self, left: str, right: str) -> None:
        st = self._w("#status")
        if st is not None:
            st.update(f"entrabot: {self._name}   ·   {left}   ·   {right}")

    def set_working(self, working: bool) -> None:
        pass

    def set_commands(self, names: List[str]) -> None:
        self._commands = list(names)

    def begin_assistant(self) -> None:
        self._flush()
        self._assist = True
        self._cur = ""
        self._render_live()

    def append_inline(self, text: str) -> None:
        if not self._assist:
            self._assist = True
        self._cur += text
        self._render_live()  # live character streaming

    def append_line(self, text: str, style: UiStyle = UiStyle.NORMAL) -> None:
        if text == "":
            self._flush()
            return
        self._flush()
        log = self._w("#log")
        if log is not None:
            color = _STYLE_MARKUP.get(style, "white")
            log.write(f"[{color}]{_escape(text)}[/]")

    def clear(self) -> None:
        log = self._w("#log")
        if log is not None:
            log.clear()

    async def confirm(self, title: str, message: str) -> bool:
        self._flush()
        log = self._w("#log")
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
