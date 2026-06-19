"""The ENTRABOT splash wordmark (port of Cli/Banner.cs).

A single-line block-font wordmark with shading + a drop shadow, like the C# COPILOT TEAMMATE
logo: each letter is bright across its top two rows and a deeper tone in the body, with a
dark offset shadow behind it. ``ENTRA`` is blue, ``BOT`` is pink. ``render()`` returns
UI-agnostic colored runs; ``print_banner()`` writes it with ANSI codes.
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
_WORD = "ENTRABOT"
_ENTRA_LEN = 5  # first 5 letters ("ENTRA") are blue; the rest ("BOT") are pink
_SHADOW = "░"

Run = Tuple[str, str]  # (text, color-key in ansi._CODES / the TUI banner palette)


def render() -> List[List[Run]]:
    """Return the banner as rows of ``(text, color-key)`` runs (shaded + drop-shadowed)."""
    height = 5 + 2  # blank top row, 5 glyph rows, 1 bottom row for the shadow
    width = _MARGIN + len(_WORD) * 6 + 1  # +1 for the shadow's right offset
    chars = [[" "] * width for _ in range(height)]
    colors = [[""] * width for _ in range(height)]

    for i, ch in enumerate(_WORD):
        glyph = _FONT.get(ch)
        if not glyph:
            continue
        section = "entra" if i < _ENTRA_LEN else "bot"
        base = _MARGIN + i * 6
        for r, line in enumerate(glyph):
            color = f"{section}_hi" if r < 2 else section  # bright top two rows, deeper body
            for c, cell in enumerate(line):
                if cell != " ":
                    chars[r + 1][base + c] = cell  # +1 for the top blank row
                    colors[r + 1][base + c] = color

    # Drop shadow: a dark offset copy down-and-right, only where no glyph cell lands.
    solid = [(r, c) for r in range(height) for c in range(width) if chars[r][c] != " "]
    for r, c in solid:
        tr, tc = r + 1, c + 1
        if tr < height and tc < width and chars[tr][tc] == " ":
            chars[tr][tc] = _SHADOW
            colors[tr][tc] = "shadow"

    rows: List[List[Run]] = []
    for r in range(height):
        runs: List[Run] = []
        cur = colors[r][0]
        buf: List[str] = []
        for c in range(width):
            if colors[r][c] != cur:
                runs.append(("".join(buf), cur))
                buf = []
                cur = colors[r][c]
            buf.append(chars[r][c])
        runs.append(("".join(buf), cur))
        rows.append(runs)
    return rows


def print_banner() -> None:
    if not ansi.ENABLED:
        print("ENTRABOT")
        return
    for row in render():
        line = []
        for text, color in row:
            code = ansi._CODES.get(color) if color else None
            line.append(f"\x1b[{code}m{text}\x1b[0m" if code else text)
        print("".join(line))
