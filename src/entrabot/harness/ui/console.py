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

    # The console UI prints spinner stages as plain lines (no animation in a redirected loop).
    def start_spinner(self, label: str) -> None:
        self._end_line()
        print(ansi.cyan("⠋ ") + label)

    def update_spinner(self, label: str) -> None:
        print(ansi.cyan("⠋ ") + label)

    def stop_spinner(self) -> None:
        pass

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

    async def select(self, title, options):
        self._end_line()
        print(ansi.bold(title))
        for i, o in enumerate(options, 1):
            print(f"  {i}. {o}")
        try:
            ans = await asyncio.to_thread(input, ansi.cyan("select #: "))
        except (EOFError, KeyboardInterrupt):
            return None
        try:
            n = int(ans.strip()) - 1
            return n if 0 <= n < len(options) else None
        except ValueError:
            return None

    async def edit_permissions(self, sections, state):
        sa = bool(state.get("sponsor_all", True))
        ga = bool(state.get("guest_all", False))
        sponsor = set(state.get("sponsor", set()))
        guest = set(state.get("guest", set()))
        tools = {it["name"] for _, items in sections for it in items}
        mark = lambda on: ansi.green("✓") if on else ansi.dim("·")

        def show():
            print(ansi.bold("Tool permissions  (toggle: '<tool> sponsor' / '<tool> guest' / 'yolo sponsor'; 'done')"))
            print(f"  {'YOLO':44} sponsor:{mark(sa)}  guest:{mark(ga)}")
            for section, items in sections:
                print(ansi.dim(f"  ── {section} ──"))
                for it in items:
                    n = it["name"]
                    print(f"  {n:44} sponsor:{mark(sa or n in sponsor)}  guest:{mark(ga or n in guest)}")

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
            parts = cmd.split()
            if len(parts) == 2 and parts[1].lower() in ("sponsor", "guest"):
                name, col = parts[0], parts[1].lower()
                if name.lower() == "yolo":
                    if col == "sponsor":
                        sa = not sa
                    else:
                        ga = not ga
                elif name in tools:
                    tgt = sponsor if col == "sponsor" else guest
                    tgt.discard(name) if name in tgt else tgt.add(name)
                else:
                    print(ansi.dim("  unknown tool"))
                    continue
                show()
            else:
                print(ansi.dim("  e.g.  yolo guest   |   powershell sponsor   |   done"))
        return {"sponsor_all": sa, "guest_all": ga, "sponsor": sponsor, "guest": guest}

    async def form(self, title, fields):
        print(ansi.bold(title))
        values = {}
        for f in fields:
            req = ansi.red(" *") if f.get("required") else ""
            desc = f"  ({f['description']})" if f.get("description") else ""
            if f.get("type") == "bool":
                try:
                    ans = await asyncio.to_thread(input, f"  {f['label']}{req} [y/N]{ansi.dim(desc)}: ")
                except (EOFError, KeyboardInterrupt):
                    return None
                values[f["key"]] = ans.strip().lower() in ("y", "yes")
            else:
                default = f.get("default") or ""
                try:
                    ans = await asyncio.to_thread(
                        input, f"  {f['label']}{req}{ansi.dim(desc)} [{default}]: "
                    )
                except (EOFError, KeyboardInterrupt):
                    return None
                values[f["key"]] = ans.strip() or default
        return values

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
