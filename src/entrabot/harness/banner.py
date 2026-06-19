"""The ENTRABOT splash wordmark (port of Cli/Banner.cs).

Two-line block-font wordmark with a drop shadow: ``ENTRA`` in blue, ``BOT`` in pink.
``render()`` returns UI-agnostic colored runs so both the console and TUI draw the same
picture; ``print_banner()`` writes it to the terminal with ANSI codes.
"""

from __future__ import annotations

from typing import List, Tuple

from . import ansi

# 5-row block glyphs, each 5 cells wide (a 1-cell gap is added between letters).
_FONT = {
    "E": ["█████", "█    ", "████ ", "█    ", "█████"],
    "N": ["█   █", "██  █", "█ █ █", "█  ██", "█   █"],
    "T": ["█████", "  █  ", "  █  ", "  █  ", "  █  "],
    "R": ["████ ", "█   █", "████ ", "█  █ ", "█   █"],
    "A": ["█████", "█   █", "█████", "█   █", "█   █"],
    "B": ["████ ", "█   █", "████ ", "█   █", "████ "],
    "O": ["█████", "█   █", "█   █", "█   █", "█████"],
}

_MARGIN = 2
_SHADOW = "░"

# Color names map to ansi._CODES; the banner uses blue for ENTRA, magenta(=pink) for BOT.
Run = Tuple[str, str]  # (text, color-name or "")


def _blank_grid(h: int, w: int) -> Tuple[List[List[str]], List[List[str]]]:
    chars = [[" "] * w for _ in range(h)]
    colors = [[""] * w for _ in range(h)]
    return chars, colors


def _stamp(chars, colors, word: str, top: int, left: int, bright: str, body: str) -> None:
    for i, ch in enumerate(word):
        glyph = _FONT.get(ch)
        if not glyph:
            continue
        base = left + i * 6
        for r, row in enumerate(glyph):
            color = bright if r < 2 else body
            for c, cell in enumerate(row):
                if cell != " ":
                    chars[top + r][base + c] = cell
                    colors[top + r][base + c] = color


def _stamp_shadow(chars, colors) -> None:
    h, w = len(chars), len(chars[0])
    for r in range(h):
        for c in range(w):
            if chars[r][c] != " ":
                tr, tc = r + 1, c + 1
                if tr < h and tc < w and chars[tr][tc] == " ":
                    chars[tr][tc] = _SHADOW
                    colors[tr][tc] = "gray"


def render() -> List[List[Run]]:
    """Return the banner as rows of ``(text, color)`` runs."""
    height = 1 + 5 + 1 + 5 + 1  # blank, ENTRA, gap, BOT, blank
    width = _MARGIN + 5 * 6 + 2
    chars, colors = _blank_grid(height, width)

    _stamp(chars, colors, "ENTRA", top=1, left=_MARGIN, bright="bright_blue", body="blue")
    _stamp(chars, colors, "BOT", top=7, left=_MARGIN, bright="bright_magenta", body="magenta")
    _stamp_shadow(chars, colors)

    rows: List[List[Run]] = []
    for r in range(height):
        runs: List[Run] = []
        cur_color = colors[r][0]
        buf = []
        for c in range(width):
            if colors[r][c] != cur_color:
                runs.append(("".join(buf), cur_color))
                buf = []
                cur_color = colors[r][c]
            buf.append(chars[r][c])
        runs.append(("".join(buf), cur_color))
        rows.append(runs)
    return rows


def print_banner() -> None:
    if not ansi.ENABLED:
        print("ENTRABOT")
        return
    for row in render():
        line = []
        for text, color in row:
            if color and color in ansi._CODES:
                line.append(f"\x1b[{ansi._CODES[color]}m{text}\x1b[0m")
            else:
                line.append(text)
        print("".join(line))
