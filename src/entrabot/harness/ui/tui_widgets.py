"""The Textual ``App`` and its custom input widget. Built by :func:`build_app`, which takes the
owning :class:`TextualUI` explicitly (no ``ui = self`` outer-scope capture) and keeps the textual
import lazy so this module loads without textual installed."""

from __future__ import annotations

import asyncio

from . import UiStyle


def build_app(ui):
    """Define and return the ``_App`` class wired to the owning ``TextualUI`` (``ui``).

    The textual base classes are imported here (not at module scope) so importing this module
    never requires textual; this is called from ``TextualUI.__init__`` when the TUI starts."""
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal
    from textual.widgets import Input, RichLog, Static

    async def _boot() -> None:
        # Run the session's startup inside the mounted app so its banner/status/streaming
        # output lands in the live UI instead of a not-yet-built one.
        try:
            if ui._on_start is not None:
                await ui._on_start()
        except Exception as error:  # surface startup failures instead of a silent blank screen
            ui.append_line(f"startup failed: {error}", UiStyle.ERROR)

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
        # Uniform near-black background (no per-pane shade change) + a subtle input border
        # (not the theme's orange accent), matching the C# teammate harness.
        CSS = """
        Screen { background: #0d0d0d; }
        #log { height: 1fr; background: #0d0d0d; }
        #spinner { height: auto; background: #0d0d0d; }
        #live { height: auto; background: #0d0d0d; }
        #staged { height: auto; background: #0d0d0d; color: #d7ba7d; }
        #suggest { height: auto; background: #0d0d0d; color: #808080; }
        #identity-bar { height: 1; background: #0d0d0d; }
        #cwd { width: 1fr; background: #0d0d0d; color: #808080; }
        #ident { width: auto; background: #0d0d0d; }
        #hint-bar { height: 1; background: #0d0d0d; }
        #hint { width: 1fr; background: #0d0d0d; color: #808080; }
        #model { width: auto; background: #0d0d0d; color: #4ec9b0; }
        Input { background: #0d0d0d; border: round #3a3d41; color: #d4d4d4; }
        Input:focus { border: round #569cd6; }
        """

        def compose(self) -> ComposeResult:  # type: ignore[override]
            yield RichLog(highlight=False, markup=True, wrap=True, id="log")
            yield Static("", id="spinner")
            yield Static("", id="live")
            yield Static("", id="staged")
            yield Static("", id="suggest")
            with Horizontal(id="identity-bar"):
                yield Static("", id="cwd")
                yield Static("", id="ident")
            yield _Input(placeholder="message  ·  / for commands", id="prompt")
            with Horizontal(id="hint-bar"):
                yield Static("/ commands   ↑↓ history   esc cancel", id="hint")
                yield Static("", id="model")

        def on_mount(self) -> None:
            self.query_one("#prompt", Input).focus()
            if ui._on_start is not None:
                self.run_worker(_boot(), exclusive=False)

        def on_input_changed(self, event) -> None:  # type: ignore[no-untyped-def]
            ui._update_suggest(event.value)

        async def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
            key = event.key
            if key == "tab" and ui._suggestions:
                self._set_input(ui._suggestions[0] + " ")
                ui._update_suggest("")
                event.prevent_default()
                event.stop()
            elif key == "escape":
                if ui._working and ui._on_interrupt:
                    asyncio.create_task(ui._on_interrupt())
                else:
                    ui._update_suggest("")
                event.prevent_default()
                event.stop()
            elif key == "ctrl+c":
                if ui._working and ui._on_interrupt:
                    asyncio.create_task(ui._on_interrupt())
                    event.prevent_default()
                    event.stop()
                else:
                    self.exit()
            elif key == "up" and not ui._suggestions:
                self._set_input(ui._history_prev(self.query_one("#prompt", Input).value))
                event.prevent_default()
                event.stop()
            elif key == "down" and not ui._suggestions:
                next_value = ui._history_next()
                if next_value is not None:
                    self._set_input(next_value)
                    event.prevent_default()
                    event.stop()

        def _set_input(self, value: str) -> None:
            prompt_input = self.query_one("#prompt", Input)
            prompt_input.value = value
            prompt_input.cursor_position = len(value)

        async def on_input_submitted(self, event) -> None:  # type: ignore[no-untyped-def]
            typed = event.value.strip()
            self.query_one("#prompt", Input).value = ""
            ui._update_suggest("")
            text = ui._combine_staged(typed)
            if not text:
                return
            ui._record_history(typed)
            if ui._on_submit is not None:
                # Run in a worker — NOT awaited here. Awaiting on_submit inline blocks
                # Textual's message pump, so a modal it opens (e.g. the /model picker) — and
                # even Esc — would never receive key events, trapping the terminal.
                self.run_worker(ui._on_submit(text), exclusive=False)

    return _App
