"""Full-screen Textual TUI (port of Ui/TuiUi.cs).

A richer alternative to the console UI: a scrolling conversation log, a live (character-
streamed) assistant line, slash-command autocomplete, history recall, multi-line paste
staging, an interrupt key, and a status footer. Opt-in (``ENTRABOT_TUI=1`` and ``textual``
installed); the console UI is the default so the harness runs even without Textual.

The screens (tui_screens) and app/widgets (tui_widgets) are built lazily in ``__init__`` so
importing this module never requires textual.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from . import UI, BannerRows, UiStyle
from .tui_constants import _BANNER_MARKUP, _SPIN, _STYLE_MARKUP, _escape
from .tui_screens import build_screens
from .tui_widgets import build_app

_MAX_SUGGESTIONS = 8  # slash-command autocomplete entries shown at once
_SPIN_INTERVAL_SECONDS = 0.08  # boot-spinner tick


def available() -> bool:
    try:
        import textual  # noqa: F401

        return True
    except ImportError:
        return False


class TextualUI(UI):
    """Built lazily so importing this module never requires textual."""

    def __init__(self) -> None:
        # Modal screens don't reference this UI; the app does, so it's threaded in explicitly.
        self._PermissionsScreen, self._SelectScreen = build_screens()
        self._App = build_app(self)

        self.app = None  # the mounted textual App, set in run()
        self._on_submit: Callable[[str], Awaitable[None]] | None = None
        self._on_interrupt: Callable[[], Awaitable[None]] | None = None
        self._on_start: Callable[[], Awaitable[None]] | None = None
        self._cur: str = ""  # raw text of the in-progress assistant line
        self._assist = False
        self._working = False
        self._commands: list[str] = []
        self._suggestions: list[str] = []
        self._history: list[str] = []
        self._hist_idx: int | None = None
        self._draft = ""
        self._staged: str | None = None
        self._spin_label: str | None = None
        self._spin_i = 0
        self._spin_timer = None

    # ---- widgets ---------------------------------------------------------------------
    def _w(self, selector: str):
        return self.app.query_one(selector) if self.app else None

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
            self._suggestions = [
                command for command in self._commands if command.startswith(value.lower())
            ][:_MAX_SUGGESTIONS]
        else:
            self._suggestions = []
        suggest_widget = self._w("#suggest")
        if suggest_widget is not None:
            suggest_widget.update(
                "  ".join(f"[bright_cyan]{suggestion}[/]" for suggestion in self._suggestions)
                + "   [grey50](tab)[/]"
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

    def _history_next(self) -> str | None:
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
        staged_widget = self._w("#staged")
        if staged_widget is not None:
            line_count = self._staged.count("\n") + 1
            staged_widget.update(
                f"⎘ staged {line_count} lines — sent with your next message  "
                "([grey50]esc input to clear[/])")

    def _combine_staged(self, typed: str) -> str:
        if self._staged is None:
            return typed
        parts = [self._staged] + ([typed] if typed else [])
        self._staged = None
        staged_widget = self._w("#staged")
        if staged_widget is not None:
            staged_widget.update("")
        return "\n\n".join(parts)

    # ---- UI protocol -----------------------------------------------------------------
    def banner(self, rows: BannerRows) -> None:
        log = self._w("#log")
        if log is None:
            return
        for row in rows:
            log.write(
                "".join(
                    f"[{_BANNER_MARKUP.get(color, 'white')}]{_escape(text)}[/]"
                    if color else _escape(text)
                    for text, color in row
                )
            )

    def set_identity(self, name: str) -> None:
        ident = self._w("#ident")
        if ident is not None:
            ident.update(f"entrabot: [bright_magenta]{_escape(name)}[/]")

    def set_status(self, left: str, right: str) -> None:
        # left = working directory (cwd strip), right = model (hint-bar right)
        cwd = self._w("#cwd")
        if cwd is not None:
            cwd.update(_escape(self._abbrev(left)))
        model = self._w("#model")
        if model is not None:
            model.update(_escape(right))

    @staticmethod
    def _abbrev(path: str) -> str:
        import os

        home = os.path.expanduser("~")
        return "~" + path[len(home):] if path.startswith(home) else path

    def _render_hint(self) -> None:
        hint = self._w("#hint")
        if hint is not None:
            hint.update(
                "[bright_green]● working[/]   esc interrupt   ↵ steer"
                if self._working
                else "/ commands   ↑↓ history   esc cancel"
            )

    def set_working(self, working: bool) -> None:
        self._working = working
        self._render_hint()

    def set_commands(self, names: list[str]) -> None:
        self._commands = list(names)

    # ---- boot spinner ----------------------------------------------------------------
    def start_spinner(self, label: str) -> None:
        self._spin_label = label
        self._spin_i = 0
        self._render_spin()
        if self._spin_timer is None and self.app is not None:
            self._spin_timer = self.app.set_interval(_SPIN_INTERVAL_SECONDS, self._spin_tick)

    def update_spinner(self, label: str) -> None:
        self._spin_label = label
        self._render_spin()

    def stop_spinner(self) -> None:
        self._spin_label = None
        if self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None
        spinner_widget = self._w("#spinner")
        if spinner_widget is not None:
            spinner_widget.update("")

    def _spin_tick(self) -> None:
        self._spin_i = (self._spin_i + 1) % len(_SPIN)
        self._render_spin()

    def _render_spin(self) -> None:
        spinner_widget = self._w("#spinner")
        if spinner_widget is not None and self._spin_label:
            spinner_widget.update(
                f"[bright_cyan]{_SPIN[self._spin_i]}[/] [grey50]{_escape(self._spin_label)}[/]")

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

    async def select(self, title, options):
        if not self.app:
            return None
        future = asyncio.get_event_loop().create_future()
        self.app.push_screen(self._SelectScreen(title, list(options), future))
        return await future

    async def edit_permissions(self, categories, state):
        if not self.app:
            return None
        future = asyncio.get_event_loop().create_future()
        self.app.push_screen(self._PermissionsScreen(categories, state, future))
        return await future

    async def run(self, on_submit, on_interrupt=None, on_start=None) -> None:
        self._on_submit = on_submit
        self._on_interrupt = on_interrupt
        self._on_start = on_start
        self.app = self._App()
        await self.app.run_async()

    def request_stop(self) -> None:
        if self.app:
            self.app.exit()
