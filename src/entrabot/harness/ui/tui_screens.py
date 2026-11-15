"""The Textual modal screens (permissions matrix and model/effort picker).

Built by :func:`build_screens` so the textual import stays lazy (the module loads without
textual installed). None of these screens reference the owning ``TextualUI`` — they resolve a
``Future`` the caller awaits — so they live here free of the app/widget wiring in tui_widgets.
"""

from __future__ import annotations


def build_screens():
    """Define and return the modal screen classes ``(Permissions, Select)``.

    The textual base classes are imported here (not at module scope) so importing this module
    never requires textual; this is called from ``TextualUI.__init__`` when the TUI starts."""
    from textual.screen import ModalScreen
    from textual.widgets import OptionList, Static

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
            self._all = {col: bool(state.get(f"{col}_all", col != "guest")) for col in self._COLS}
            self._sets = {col: set(state.get(col, set())) for col in self._COLS}
            self._fut = fut
            # flat row list: yolo, then per-section (header + tools)
            self._rows = [{"kind": "yolo"}]
            for section, items in sections:
                self._rows.append({"kind": "header", "label": section})
                for item in items:
                    self._rows.append(
                        {"kind": "tool", "name": item["name"], "locked": bool(item.get("locked"))}
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
            key = event.key
            if key == "escape":
                self.action_save_close()
            elif key == "c":
                self._toggle("cli")
            elif key == "s":
                self._toggle("sponsor")
            elif key == "g":
                self._toggle("guest")
            elif key == "space":
                for column in self._COLS:
                    self._toggle(column, rebuild=False)
                self._rebuild()
            else:
                return
            event.stop()

        def on_option_list_option_selected(self, event):
            for column in self._COLS:
                self._toggle(column, rebuild=False)
            self._rebuild()

        def _cell(self, granted, all_on):
            if all_on:
                return "[yellow]✓[/]"  # granted via the YOLO row
            return "[green]✓[/]" if granted else "[grey50]·[/]"

        def _cells(self, render_cell):
            return "  ".join(f"{column} {render_cell(column)}" for column in self._COLS)

        def _row_markup(self, row_index):
            row = self._rows[row_index]
            if row["kind"] == "header":
                return f"[bold #569cd6]── {row['label']} ──[/]"
            if row["kind"] == "yolo":
                cells = self._cells(
                    lambda column: "[bold yellow]✓ all[/]" if self._all[column] else "[grey50]·[/]")
                return f"⚡ {'YOLO — allow ALL tools'.ljust(34)}  {cells}"
            name = row["name"]
            if row.get("locked"):  # harness reply path — locked ON, not toggleable
                cells = self._cells(lambda column: "[bold green]✓[/]")
                return f"   🔒 {(name + ' (required)').ljust(33)}  {cells}"
            cells = self._cells(
                lambda column: self._cell(name in self._sets[column], self._all[column]))
            return f"   {name.ljust(36)}  {cells}"

        def _rebuild(self):
            from rich.text import Text

            option_list = self.query_one(OptionList)
            highlighted_index = option_list.highlighted
            option_list.clear_options()
            for row_index in range(len(self._rows)):
                option_list.add_option(Text.from_markup(self._row_markup(row_index)))
            option_list.highlighted = highlighted_index if highlighted_index is not None else 0

        def _toggle(self, col, rebuild=True):
            row_index = self.query_one(OptionList).highlighted or 0
            row = self._rows[row_index]
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
                    **{f"{col}_all": self._all[col] for col in self._COLS},
                    **{col: self._sets[col] for col in self._COLS},
                })
            self.app.pop_screen()

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
            key = event.key
            if key == "escape":
                self._resolve(None)
                event.stop()
            elif key in ("up", "down", "enter", "pageup", "pagedown", "home", "end"):
                return  # let the focused OptionList handle navigation + selection
            elif key == "backspace":
                if self._filter:
                    self._filter = self._filter[:-1]
                    self._refilter()
                event.stop()
            elif event.character and event.character.isprintable() and len(event.character) == 1:
                self._filter += event.character
                self._refilter()
                event.stop()

        def _refilter(self):
            query = self._filter.lower()
            option_list = self.query_one(OptionList)
            option_list.clear_options()
            self._filtered = [
                index for index, option in enumerate(self._all) if query in str(option).lower()
            ]
            for index in self._filtered:
                option_list.add_option(self._all[index])
            if self._filtered:
                option_list.highlighted = 0
            self.query_one("#sel-footer", Static).update(self._footer())

        def _footer(self):
            filter_note = f"   filter: {self._filter}" if self._filter else ""
            return f"↑/↓ navigate   ·   enter select   ·   esc cancel{filter_note}"

        def _resolve(self, selected_index):
            if not self._fut.done():
                self._fut.set_result(selected_index)
            self.app.pop_screen()

    return _PermissionsScreen, _SelectScreen
