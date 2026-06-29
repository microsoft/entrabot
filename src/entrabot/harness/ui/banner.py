"""The ENTRABOT splash wordmark (port of Cli/Banner.cs).

A single-line block-font wordmark with shading + a drop shadow, like the C# COPILOT TEAMMATE
logo: each letter is bright across its top two rows and a deeper tone in the body, with a
dark offset shadow behind it. ``ENTRA`` is blue, ``BOT`` is pink. ``render()`` returns
UI-agnostic ``(text, color-key)`` runs that each UI maps to its own palette.
"""

from __future__ import annotations

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

_GLYPH_HEIGHT = 5  # rows per block glyph
_GLYPH_ADVANCE = 6  # 5 glyph cells + 1 inter-letter gap
_TOP_BLANK_ROWS = 1  # one blank row above the glyphs
_BOTTOM_SHADOW_ROWS = 1  # one row below for the drop shadow

Run = tuple[str, str]  # (text, color-key in ansi._CODES / the TUI banner palette)


def _render_glyphs(chars: list[list[str]], colors: list[list[str]]) -> None:
    """Stamp each letter of ``_WORD`` into the ``chars``/``colors`` grids."""
    for letter_index, character in enumerate(_WORD):
        glyph = _FONT.get(character)
        if not glyph:
            continue
        section = "entra" if letter_index < _ENTRA_LEN else "bot"
        left = _MARGIN + letter_index * _GLYPH_ADVANCE
        for glyph_row, line in enumerate(glyph):
            # bright top two rows, deeper body
            color = f"{section}_hi" if glyph_row < 2 else section
            for glyph_col, cell in enumerate(line):
                if cell != " ":
                    chars[glyph_row + _TOP_BLANK_ROWS][left + glyph_col] = cell
                    colors[glyph_row + _TOP_BLANK_ROWS][left + glyph_col] = color


def _render_shadow(chars: list[list[str]], colors: list[list[str]]) -> None:
    """Add a dark offset copy down-and-right, only where no glyph cell lands."""
    height = len(chars)
    width = len(chars[0])
    solid_cells = [
        (row_index, col_index)
        for row_index in range(height)
        for col_index in range(width)
        if chars[row_index][col_index] != " "
    ]
    for row_index, col_index in solid_cells:
        shadow_row, shadow_col = row_index + 1, col_index + 1
        if shadow_row < height and shadow_col < width and chars[shadow_row][shadow_col] == " ":
            chars[shadow_row][shadow_col] = _SHADOW
            colors[shadow_row][shadow_col] = "shadow"


def _compress_runs(row_chars: list[str], row_colors: list[str]) -> list[Run]:
    """Collapse a row of cells into ``(text, color-key)`` runs by adjacent color."""
    runs: list[Run] = []
    current_color = row_colors[0]
    text_buffer: list[str] = []
    for col_index in range(len(row_chars)):
        if row_colors[col_index] != current_color:
            runs.append(("".join(text_buffer), current_color))
            text_buffer = []
            current_color = row_colors[col_index]
        text_buffer.append(row_chars[col_index])
    runs.append(("".join(text_buffer), current_color))
    return runs


def render() -> list[list[Run]]:
    """Return the banner as rows of ``(text, color-key)`` runs (shaded + drop-shadowed)."""
    height = _GLYPH_HEIGHT + _TOP_BLANK_ROWS + _BOTTOM_SHADOW_ROWS
    width = _MARGIN + len(_WORD) * _GLYPH_ADVANCE + 1  # +1 for the shadow's right offset
    chars = [[" "] * width for _ in range(height)]
    colors = [[""] * width for _ in range(height)]

    _render_glyphs(chars, colors)
    _render_shadow(chars, colors)

    return [_compress_runs(chars[row_index], colors[row_index]) for row_index in range(height)]
