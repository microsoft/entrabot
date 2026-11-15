"""Static rendering constants for the Textual UI: style→markup maps, the banner palette, the
spinner frames, and markup escaping. Imports no textual, so it's safe to load without it."""

from __future__ import annotations

from . import UiStyle

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

_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # braille spinner frames (matches the C# BootProgress)


def _escape(text: str) -> str:
    return text.replace("[", "\\[")
