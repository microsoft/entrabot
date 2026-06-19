"""Full-screen Textual TUI (port of Ui/TuiUi.cs).

A richer alternative to the console UI: a scrolling conversation log, a live (character-
streamed) assistant line, slash-command autocomplete, history recall, multi-line paste
staging, an interrupt key, and a status footer. Opt-in (``ENTRABOT_TUI=1`` and ``textual``
installed); the console UI is the default so the harness runs even without Textual.
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
    "entra": "#0087ff",  # azure blue
    "bot": "#ff5faf",  # hot pink
}


def _escape(text: str) -> str:
    return text.replace("[", "\\[")


class TextualUI(UI):
    """Built lazily so importing this module never requires textual."""

    def __init__(self) -> None:
        from textual.app import App, ComposeResult
        from textual.widgets import Input, RichLog, Static

        ui = self

        class _Input(Input):
            """Intercept paste so multi-line pastes are *staged* (single-line Input)."""

            def on_paste(self, event) -> None:  # type: ignore[no-untyped-def]
                text = event.text
                event.prevent_default()
                event.stop()
                if "\n" in text:
                    ui._stage(text)
                else:
                    self.insert_text_at_cursor(text)

        class _App(App):
            CSS = """
            RichLog { height: 1fr; }
            #live { height: auto; }
            #staged { height: auto; color: $warning; }
            #suggest { height: auto; color: $text-muted; }
            #status { height: 1; color: $text-muted; }
            Input { border: round $accent; }
            """

            def compose(self) -> ComposeResult:  # type: ignore[override]
                yield RichLog(highlight=False, markup=True, wrap=True, id="log")
                yield Static("", id="live")
                yield Static("", id="staged")
                yield Static("", id="suggest")
                yield Static("", id="status")
                yield _Input(placeholder="message  ·  / for commands", id="prompt")

            def on_mount(self) -> None:
                self.query_one("#prompt", Input).focus()

            def on_input_changed(self, event) -> None:  # type: ignore[no-untyped-def]
                ui._update_suggest(event.value)

            async def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
                key = event.key
                if key == "tab" and ui._suggestions:
                    self._set_input(ui._suggestions[0] + " ")
                    ui._update_suggest("")
                    event.prevent_default(); event.stop()
                elif key == "escape":
                    if ui._working and ui._on_interrupt:
                        asyncio.create_task(ui._on_interrupt())
                    else:
                        ui._update_suggest("")
                    event.prevent_default(); event.stop()
                elif key == "ctrl+c":
                    if ui._working and ui._on_interrupt:
                        asyncio.create_task(ui._on_interrupt())
                        event.prevent_default(); event.stop()
                    else:
                        self.exit()
                elif key == "up" and not ui._suggestions:
                    self._set_input(ui._history_prev(self.query_one("#prompt", Input).value))
                    event.prevent_default(); event.stop()
                elif key == "down" and not ui._suggestions:
                    nxt = ui._history_next()
                    if nxt is not None:
                        self._set_input(nxt)
                        event.prevent_default(); event.stop()

            def _set_input(self, value: str) -> None:
                inp = self.query_one("#prompt", Input)
                inp.value = value
                inp.cursor_position = len(value)

            async def on_input_submitted(self, event) -> None:  # type: ignore[no-untyped-def]
                typed = event.value.strip()
                self.query_one("#prompt", Input).value = ""
                ui._update_suggest("")
                text = ui._combine_staged(typed)
                if not text:
                    return
                if ui._pending_confirm is not None:
                    fut, ui._pending_confirm = ui._pending_confirm, None
                    fut.set_result(text.lower() in ("y", "yes"))
                    return
                ui._record_history(typed)
                if ui._on_submit is not None:
                    await ui._on_submit(text)

        self._App = _App
        self.app: Optional[App] = None
        self._on_submit: Optional[Callable[[str], Awaitable[None]]] = None
        self._on_interrupt: Optional[Callable[[], Awaitable[None]]] = None
        self._cur: str = ""  # raw text of the in-progress assistant line
        self._assist = False
        self._working = False
        self._name = "entrabot"
        self._left = ""
        self._right = ""
        self._commands: List[str] = []
        self._suggestions: List[str] = []
        self._history: List[str] = []
        self._hist_idx: Optional[int] = None
        self._draft = ""
        self._staged: Optional[str] = None
        self._pending_confirm: Optional[asyncio.Future] = None

    # ---- widgets ---------------------------------------------------------------------
    def _w(self, sel: str):
        return self.app.query_one(sel) if self.app else None

    def _render_live(self) -> None:
        live = self._w("#live")
        if live is None:
            return
        live.update(f"[bright_magenta]●[/] {_escape(self._cur)}" if self._assist else "")

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
            sug.update(
                "  ".join(f"[bright_cyan]{s}[/]" for s in self._suggestions) + "   [grey50](tab)[/]"
                if self._suggestions
                else ""
            )

    # ---- history ---------------------------------------------------------------------
    def _record_history(self, typed: str) -> None:
        if typed and (not self._history or self._history[-1] != typed):
            self._history.append(typed)
        self._hist_idx = None
        self._draft = ""

    def _history_prev(self, current: str) -> str:
        if not self._history:
            return current
        if self._hist_idx is None:
            self._draft = current
            self._hist_idx = len(self._history)
        self._hist_idx = max(0, self._hist_idx - 1)
        return self._history[self._hist_idx]

    def _history_next(self) -> Optional[str]:
        if self._hist_idx is None:
            return None
        self._hist_idx += 1
        if self._hist_idx >= len(self._history):
            self._hist_idx = None
            return self._draft
        return self._history[self._hist_idx]

    # ---- paste staging ---------------------------------------------------------------
    def _stage(self, text: str) -> None:
        self._staged = f"{self._staged}\n{text}" if self._staged else text
        staged = self._w("#staged")
        if staged is not None:
            lines = self._staged.count("\n") + 1
            staged.update(f"⎘ staged {lines} lines — sent with your next message  ([grey50]esc input to clear[/])")

    def _combine_staged(self, typed: str) -> str:
        if self._staged is None:
            return typed
        parts = [self._staged] + ([typed] if typed else [])
        self._staged = None
        st = self._w("#staged")
        if st is not None:
            st.update("")
        return "\n\n".join(parts)

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
        self._left, self._right = left, right
        self._render_status()

    def _render_status(self) -> None:
        st = self._w("#status")
        if st is not None:
            work = "  ·  [bright_green]working — esc to interrupt[/]" if self._working else ""
            st.update(f"entrabot: {self._name}   ·   {self._left}   ·   {self._right}{work}")

    def set_working(self, working: bool) -> None:
        self._working = working
        self._render_status()

    def set_commands(self, names: List[str]) -> None:
        self._commands = list(names)

    def begin_assistant(self) -> None:
        self._flush()
        self._assist = True
        self._cur = ""
        self._render_live()

    def append_inline(self, text: str) -> None:
        self._assist = True
        self._cur += text
        self._render_live()

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

    async def run(self, on_submit, on_interrupt=None) -> None:
        self._on_submit = on_submit
        self._on_interrupt = on_interrupt
        self.app = self._App()
        await self.app.run_async()

    def request_stop(self) -> None:
        if self.app:
            self.app.exit()
