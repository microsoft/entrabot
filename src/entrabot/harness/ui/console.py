"""Plain console UI (port of Ui/ConsoleUi.cs).

A streaming, line-oriented UI that works in a real terminal and when stdin/stdout are
redirected. The session reads one line, runs a turn (streaming output prints as it
arrives), then prompts again — matching the .NET redirected-IO behavior.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

from . import UI, BannerRows, UiStyle, ansi

# Width of the tool-name column in the permissions matrix.
_COL_WIDTH = 40

# Caller classes that share a column in the permissions matrix. This concept is
# shared with the rest of the package; the tuple is kept local on purpose.
_COLUMNS = ("cli", "sponsor", "guest")

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
    style_fn = _STYLE_FN.get(style)
    return style_fn(text) if style_fn else text


class ConsoleUI(UI):
    def __init__(self) -> None:
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
        print(ansi.magenta(f"entrabot: {name}"))

    def set_status(self, left: str, right: str) -> None:
        pass  # the plain console doesn't show a continuous status line

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
        pass  # no-op: the plain console UI has no autocomplete

    # The console UI prints spinner stages as plain lines (no animation in a redirected loop).
    def start_spinner(self, label: str) -> None:
        self._end_line()
        print(ansi.cyan("⠋ ") + label)

    def update_spinner(self, label: str) -> None:
        print(ansi.cyan("⠋ ") + label)

    def stop_spinner(self) -> None:
        pass

    def clear(self) -> None:
        with contextlib.suppress(Exception):
            print("\x1b[2J\x1b[H", end="")

    async def select(self, title, options):
        self._end_line()
        print(ansi.bold(title))
        for index, option in enumerate(options, 1):
            print(f"  {index}. {option}")
        try:
            answer = await asyncio.to_thread(input, ansi.cyan("select #: "))
        except (EOFError, KeyboardInterrupt):
            return None
        try:
            selected_index = int(answer.strip()) - 1
            return selected_index if 0 <= selected_index < len(options) else None
        except ValueError:
            return None

    async def edit_permissions(self, sections, state):
        # Data-driven columns so the matrix isn't hardwired to two classes (cli · sponsor · guest).
        all_on = {col: bool(state.get(f"{col}_all", col != "guest")) for col in _COLUMNS}
        sets = {col: set(state.get(col, set())) for col in _COLUMNS}
        tools = {item["name"] for _, items in sections for item in items}
        locked = {
            item["name"]
            for _, items in sections
            for item in items
            if item.get("locked")
        }

        def mark(on: bool) -> str:
            return ansi.green("✓") if on else ansi.dim("·")

        def cells(on_for):
            return "  ".join(f"{col}:{on_for(col)}" for col in _COLUMNS)

        def tool_mark(col, name):
            return mark(all_on[col] or name in sets[col])

        def show():
            print(ansi.bold("Tool permissions  (toggle: '<tool> <cli|sponsor|guest>' / "
                            "'yolo <class>'; 'done')"))
            print(f"  {'YOLO':{_COL_WIDTH}} {cells(lambda col: mark(all_on[col]))}")
            for section, items in sections:
                print(ansi.dim(f"  ── {section} ──"))
                for item in items:
                    item_name = item["name"]
                    if item_name in locked:
                        label = f"🔒 {item_name}"
                        print(
                            f"  {label:{_COL_WIDTH}} {cells(lambda col: ansi.green('✓'))}  "
                            f"{ansi.dim('(required)')}"
                        )
                    else:
                        cells_text = cells(lambda col, name=item_name: tool_mark(col, name))
                        print(f"  {item_name:{_COL_WIDTH}} {cells_text}")

        def apply_command(cmd: str) -> bool:
            """Apply one perm-editor command; return True if the matrix should redraw."""
            parts = cmd.split()
            if len(parts) != 2 or parts[1].lower() not in _COLUMNS:
                print(ansi.dim("  e.g.  yolo guest   |   powershell sponsor   |   done"))
                return False
            name, col = parts[0], parts[1].lower()
            if name.lower() == "yolo":
                all_on[col] = not all_on[col]
            elif name in locked:
                print(ansi.dim("  (required — always enabled, can't be changed)"))
                return False
            elif name in tools:
                target = sets[col]
                target.discard(name) if name in target else target.add(name)
            else:
                print(ansi.dim("  unknown tool"))
                return False
            return True

        show()
        while True:
            try:
                cmd = (await asyncio.to_thread(input, ansi.cyan("perm> "))).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if cmd.lower() in ("", "done", "save"):
                break
            if cmd.lower() in ("q", "quit", "cancel"):
                return None
            if apply_command(cmd):
                show()
        return {
            **{f"{col}_all": all_on[col] for col in _COLUMNS},
            **{col: sets[col] for col in _COLUMNS},
        }

    async def run(self, on_submit, on_interrupt=None, on_start=None) -> None:
        if on_start is not None:
            await on_start()
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
