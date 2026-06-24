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
    "entra_hi": "#5fafff",  # bright blue (top rows)
    "entra": "#0087ff",  # azure blue (body)
    "bot_hi": "#ff87ff",  # bright pink (top rows)
    "bot": "#ff5faf",  # hot pink (body)
    "shadow": "#3a3a3a",  # dark drop shadow
}


def _escape(text: str) -> str:
    return text.replace("[", "\\[")


_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # braille spinner frames (matches the C# BootProgress)


class TextualUI(UI):
    """Built lazily so importing this module never requires textual."""

    def __init__(self) -> None:
        from textual.app import App, ComposeResult
        from textual.containers import Horizontal, VerticalScroll
        from textual.screen import ModalScreen
        from textual.widgets import Button, Checkbox, Input, OptionList, RichLog, Static

        ui = self

        class _PermissionsScreen(ModalScreen):
            """Per-tool caller-class matrix on an OptionList. Columns: cli · sponsor · guest. Rows:
            a YOLO row (each cell grants ALL tools to that class) then every tool grouped by
            section. ↑/↓ pick a row, c/s/g toggle the cli/sponsor/guest cell, space toggles all,
            esc saves. Resolves {cli_all, sponsor_all, guest_all, cli:set, sponsor:set, guest:set}."""

            _COLS = ("cli", "sponsor", "guest")

            CSS = """
            _PermissionsScreen { background: #0d0d0d; }
            #perm-title { color: #569cd6; text-style: bold; padding: 1 2 0 2; }
            #perm-list { height: 1fr; background: #0d0d0d; margin: 0 2; border: round #3a3d41; }
            #perm-footer { color: #808080; padding: 0 2 1 2; }
            """

            def __init__(self, sections, state, fut):
                super().__init__()
                self._all = {c: bool(state.get(f"{c}_all", c != "guest")) for c in self._COLS}
                self._sets = {c: set(state.get(c, set())) for c in self._COLS}
                self._fut = fut
                # flat row list: yolo, then per-section (header + tools)
                self._rows = [{"kind": "yolo"}]
                for section, items in sections:
                    self._rows.append({"kind": "header", "label": section})
                    for it in items:
                        self._rows.append(
                            {"kind": "tool", "name": it["name"], "locked": bool(it.get("locked"))}
                        )

            def compose(self):
                yield Static("Tool permissions  —  cli · sponsor · guest, per tool", id="perm-title")
                yield OptionList(id="perm-list")
                yield Static(
                    "↑/↓ row   ·   c cli   ·   s sponsor   ·   g guest   ·   space all   ·   "
                    "esc save & close",
                    id="perm-footer",
                )

            def on_mount(self):
                self._rebuild()
                self.query_one(OptionList).focus()

            def on_key(self, event):
                k = event.key
                if k == "escape":
                    self.action_save_close()
                elif k == "c":
                    self._toggle("cli")
                elif k == "s":
                    self._toggle("sponsor")
                elif k == "g":
                    self._toggle("guest")
                elif k == "space":
                    for c in self._COLS:
                        self._toggle(c, rebuild=False)
                    self._rebuild()
                else:
                    return
                event.stop()

            def on_option_list_option_selected(self, event):
                for c in self._COLS:
                    self._toggle(c, rebuild=False)
                self._rebuild()

            def _cell(self, on, all_on):
                if all_on:
                    return "[yellow]✓[/]"  # granted via the YOLO row
                return "[green]✓[/]" if on else "[grey50]·[/]"

            def _cells(self, fn):
                return "  ".join(f"{c} {fn(c)}" for c in self._COLS)

            def _row_markup(self, i):
                row = self._rows[i]
                if row["kind"] == "header":
                    return f"[bold #569cd6]── {row['label']} ──[/]"
                if row["kind"] == "yolo":
                    cells = self._cells(
                        lambda c: "[bold yellow]✓ all[/]" if self._all[c] else "[grey50]·[/]")
                    return f"⚡ {'YOLO — allow ALL tools'.ljust(34)}  {cells}"
                name = row["name"]
                if row.get("locked"):  # harness reply path — locked ON, not toggleable
                    cells = self._cells(lambda c: "[bold green]✓[/]")
                    return f"   🔒 {(name + ' (required)').ljust(33)}  {cells}"
                cells = self._cells(lambda c: self._cell(name in self._sets[c], self._all[c]))
                return f"   {name.ljust(36)}  {cells}"

            def _rebuild(self):
                from rich.text import Text

                ol = self.query_one(OptionList)
                hl = ol.highlighted
                ol.clear_options()
                for i in range(len(self._rows)):
                    ol.add_option(Text.from_markup(self._row_markup(i)))
                ol.highlighted = hl if hl is not None else 0

            def _toggle(self, col, rebuild=True):
                i = self.query_one(OptionList).highlighted or 0
                row = self._rows[i]
                if row["kind"] == "yolo":
                    self._all[col] = not self._all[col]
                elif row["kind"] == "tool":
                    if row.get("locked"):
                        return  # locked ON for all callers — can't be toggled
                    target = self._sets[col]
                    target.discard(row["name"]) if row["name"] in target else target.add(row["name"])
                else:
                    return  # header
                if rebuild:
                    self._rebuild()

            def action_save_close(self):
                if not self._fut.done():
                    self._fut.set_result({
                        **{f"{c}_all": self._all[c] for c in self._COLS},
                        **{c: self._sets[c] for c in self._COLS},
                    })
                self.app.pop_screen()

        self._PermissionsScreen = _PermissionsScreen

        class _UsersScreen(ModalScreen):
            """Recipients-&-roles matrix on an OptionList. Each row is a recipient with a read-only
            Type (Entra Guest/Member) and a toggleable Role (Sponsor/Guest). ↑/↓ pick, space (or
            s/g) toggles the Role, esc saves. Resolves {"roles": {upn: bool}}."""

            CSS = """
            _UsersScreen { background: #0d0d0d; }
            #users-title { color: #569cd6; text-style: bold; padding: 1 2 0 2; }
            #users-list { height: 1fr; background: #0d0d0d; margin: 0 2; border: round #3a3d41; }
            #users-footer { color: #808080; padding: 0 2 1 2; }
            """

            def __init__(self, rows, fut):
                super().__init__()
                self._rows = [
                    {"upn": r["upn"], "type": r.get("type", "Member"), "role": bool(r.get("role"))}
                    for r in rows
                ]
                self._fut = fut

            def compose(self):
                yield Static("Recipients & roles  —  Type (Entra) · Role (toggle)", id="users-title")
                yield OptionList(id="users-list")
                yield Static(
                    "↑/↓ row   ·   space / s sponsor / g guest toggles Role   ·   esc save & close",
                    id="users-footer",
                )

            def on_mount(self):
                self._rebuild()
                self.query_one(OptionList).focus()

            def on_key(self, event):
                k = event.key
                if k == "escape":
                    self.action_save_close()
                elif k in ("space", "s"):
                    self._set(True)
                elif k == "g":
                    self._set(False)
                else:
                    return
                event.stop()

            def on_option_list_option_selected(self, event):
                i = self.query_one(OptionList).highlighted or 0
                self._set(not self._rows[i]["role"])  # click flips the Role

            def _row_markup(self, i):
                r = self._rows[i]
                role = "[bold green]Sponsor[/]" if r["role"] else "[grey50]Guest[/]"
                return f"   {r['upn'].ljust(40)}  [grey62]{r['type'].ljust(8)}[/]  {role}"

            def _rebuild(self):
                from rich.text import Text

                ol = self.query_one(OptionList)
                hl = ol.highlighted
                ol.clear_options()
                if not self._rows:
                    ol.add_option(Text.from_markup("[grey50]   (no recipients)[/]"))
                else:
                    for i in range(len(self._rows)):
                        ol.add_option(Text.from_markup(self._row_markup(i)))
                ol.highlighted = hl if hl is not None else 0

            def _set(self, sponsor):
                if not self._rows:
                    return
                i = self.query_one(OptionList).highlighted or 0
                self._rows[i]["role"] = sponsor
                self._rebuild()

            def action_save_close(self):
                if not self._fut.done():
                    self._fut.set_result({"roles": {r["upn"]: r["role"] for r in self._rows}})
                self.app.pop_screen()

        self._UsersScreen = _UsersScreen

        class _FormScreen(ModalScreen):
            """All-on-one-page editable form (agency MCP params). Tab between fields, edit each,
            then Submit (or esc to cancel). Resolves a future with {key: value} or None."""

            CSS = """
            _FormScreen { background: #0d0d0d; }
            #form-title { color: #569cd6; text-style: bold; padding: 1 2 0 2; }
            #form-body { height: 1fr; padding: 0 2; }
            .form-label { padding: 1 0 0 0; }
            #form-body Input { border: round #3a3d41; background: #0d0d0d; }
            #form-body Input:focus { border: round #569cd6; }
            #form-actions { height: auto; padding: 1 2; }
            #form-actions Button { margin: 0 2 0 0; }
            #form-footer { color: #808080; padding: 0 2 1 2; }
            """

            def __init__(self, title, fields, fut):
                super().__init__()
                self._title = title
                self._fields = list(fields)
                self._fut = fut

            def compose(self):
                yield Static(self._title, id="form-title")
                with VerticalScroll(id="form-body"):
                    for i, f in enumerate(self._fields):
                        req = "  [red]*required[/]" if f.get("required") else ""
                        desc = f.get("description", "")
                        label = f"[bold]{f['label']}[/]{req}" + (f"\n[grey50]{_escape(desc)}[/]" if desc else "")
                        yield Static(label, classes="form-label")
                        if f.get("type") == "bool":
                            yield Checkbox(value=bool(f.get("default")), id=f"field-{i}")
                        else:
                            yield Input(value=str(f.get("default") or ""), placeholder=f.get("placeholder", ""), id=f"field-{i}")
                with Horizontal(id="form-actions"):
                    yield Button("Submit", variant="primary", id="form-submit")
                    yield Button("Cancel", id="form-cancel")
                yield Static("tab between fields   ·   enter on Submit   ·   esc cancel", id="form-footer")

            def on_mount(self):
                try:
                    self.query_one("#field-0").focus()
                except Exception:
                    self.query_one("#form-submit", Button).focus()

            def on_button_pressed(self, event):
                if event.button.id == "form-submit":
                    vals = {f["key"]: self.query_one(f"#field-{i}").value for i, f in enumerate(self._fields)}
                    self._resolve(vals)
                else:
                    self._resolve(None)

            def on_key(self, event):
                if event.key == "escape":
                    self._resolve(None)
                    event.stop()

            def _resolve(self, val):
                if not self._fut.done():
                    self._fut.set_result(val)
                self.app.pop_screen()

        self._FormScreen = _FormScreen

        class _SelectScreen(ModalScreen):
            """Full-screen picker (model / effort) like the C# harness's selector: the list is
            focused so ↑/↓/PgUp/PgDn/enter work natively, printable keys type-to-filter, and esc
            always cancels (so it can never trap you). Resolves a future."""

            CSS = """
            _SelectScreen { background: #0d0d0d; }
            #sel-title { color: #569cd6; text-style: bold; padding: 1 2 0 2; }
            #sel-list { height: 1fr; background: #0d0d0d; margin: 0 2; border: round #3a3d41; }
            #sel-footer { color: #808080; padding: 0 2 1 2; }
            """

            def __init__(self, title, options, fut):
                super().__init__()
                self._title = title
                self._all = list(options)
                self._fut = fut
                self._filter = ""
                self._filtered = list(range(len(self._all)))  # original indices shown

            def compose(self):
                yield Static(self._title, id="sel-title")
                yield OptionList(*self._all, id="sel-list")
                yield Static(self._footer(), id="sel-footer")

            def on_mount(self):
                self.query_one(OptionList).focus()  # native ↑/↓/enter navigation

            def on_option_list_option_selected(self, event):  # native enter or mouse click
                if self._filtered:
                    self._resolve(self._filtered[event.option_index])

            def on_key(self, event):
                k = event.key
                if k == "escape":
                    self._resolve(None)
                    event.stop()
                elif k in ("up", "down", "enter", "pageup", "pagedown", "home", "end"):
                    return  # let the focused OptionList handle navigation + selection
                elif k == "backspace":
                    if self._filter:
                        self._filter = self._filter[:-1]
                        self._refilter()
                    event.stop()
                elif event.character and event.character.isprintable() and len(event.character) == 1:
                    self._filter += event.character
                    self._refilter()
                    event.stop()

            def _refilter(self):
                q = self._filter.lower()
                ol = self.query_one(OptionList)
                ol.clear_options()
                self._filtered = [i for i, o in enumerate(self._all) if q in str(o).lower()]
                for i in self._filtered:
                    ol.add_option(self._all[i])
                if self._filtered:
                    ol.highlighted = 0
                self.query_one("#sel-footer", Static).update(self._footer())

            def _footer(self):
                f = f"   filter: {self._filter}" if self._filter else ""
                return f"↑/↓ navigate   ·   enter select   ·   esc cancel{f}"

            def _resolve(self, idx):
                if not self._fut.done():
                    self._fut.set_result(idx)
                self.app.pop_screen()

        self._SelectScreen = _SelectScreen

        async def _boot() -> None:
            # Run the session's startup inside the mounted app so its banner/status/streaming
            # output lands in the live UI instead of a not-yet-built one.
            try:
                if ui._on_start is not None:
                    await ui._on_start()
            except Exception as e:  # surface startup failures instead of a silent blank screen
                ui.append_line(f"startup failed: {e}", UiStyle.ERROR)

        class _FocusableStatic(Static):
            can_focus = True  # so a screen that uses it isn't forced to focus itself

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
                from textual.containers import Horizontal

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
                    # Run in a worker — NOT awaited here. Awaiting on_submit inline blocks
                    # Textual's message pump, so a modal it opens (e.g. the /model picker) — and
                    # even Esc — would never receive key events, trapping the terminal.
                    self.run_worker(ui._on_submit(text), exclusive=False)

        self._App = _App
        self.app: Optional[App] = None
        self._on_submit: Optional[Callable[[str], Awaitable[None]]] = None
        self._on_interrupt: Optional[Callable[[], Awaitable[None]]] = None
        self._on_start: Optional[Callable[[], Awaitable[None]]] = None
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
        self._spin_label: Optional[str] = None
        self._spin_i = 0
        self._spin_timer = None

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
        ident = self._w("#ident")
        if ident is not None:
            ident.update(f"entrabot: [bright_magenta]{_escape(name)}[/]")

    def set_status(self, left: str, right: str) -> None:
        # left = working directory (cwd strip), right = model (hint-bar right)
        self._left, self._right = left, right
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

    def set_commands(self, names: List[str]) -> None:
        self._commands = list(names)

    # ---- boot spinner ----------------------------------------------------------------
    def start_spinner(self, label: str) -> None:
        self._spin_label = label
        self._spin_i = 0
        self._render_spin()
        if self._spin_timer is None and self.app is not None:
            self._spin_timer = self.app.set_interval(0.08, self._spin_tick)

    def update_spinner(self, label: str) -> None:
        self._spin_label = label
        self._render_spin()

    def stop_spinner(self) -> None:
        self._spin_label = None
        if self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None
        sp = self._w("#spinner")
        if sp is not None:
            sp.update("")

    def _spin_tick(self) -> None:
        self._spin_i = (self._spin_i + 1) % len(_SPIN)
        self._render_spin()

    def _render_spin(self) -> None:
        sp = self._w("#spinner")
        if sp is not None and self._spin_label:
            sp.update(f"[bright_cyan]{_SPIN[self._spin_i]}[/] [grey50]{_escape(self._spin_label)}[/]")

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

    async def select(self, title, options):
        if not self.app:
            return None
        fut = asyncio.get_event_loop().create_future()
        self.app.push_screen(self._SelectScreen(title, list(options), fut))
        return await fut

    async def edit_permissions(self, categories, state):
        if not self.app:
            return None
        fut = asyncio.get_event_loop().create_future()
        self.app.push_screen(self._PermissionsScreen(categories, state, fut))
        return await fut

    async def edit_users(self, rows):
        if not self.app:
            return None
        fut = asyncio.get_event_loop().create_future()
        self.app.push_screen(self._UsersScreen(rows, fut))
        return await fut

    async def form(self, title, fields):
        if not self.app:
            return None
        fut = asyncio.get_event_loop().create_future()
        self.app.push_screen(self._FormScreen(title, fields, fut))
        return await fut

    async def run(self, on_submit, on_interrupt=None, on_start=None) -> None:
        self._on_submit = on_submit
        self._on_interrupt = on_interrupt
        self._on_start = on_start
        self.app = self._App()
        await self.app.run_async()

    def request_stop(self) -> None:
        if self.app:
            self.app.exit()
