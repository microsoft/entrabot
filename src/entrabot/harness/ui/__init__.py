"""UI contract (port of Ui/IUi.cs) shared by the console and Textual implementations."""

from __future__ import annotations

import enum
from typing import Awaitable, Callable, List, Protocol, Tuple, runtime_checkable


class UiStyle(enum.Enum):
    NORMAL = "normal"
    USER = "user"
    TOOL = "tool"
    ERROR = "error"
    WARN = "warn"
    DIM = "dim"
    SUCCESS = "success"
    INFO = "info"
    ASSISTANT = "assistant"
    REASONING = "reasoning"
    ACCENT = "accent"


# A run of styled text.
Seg = Tuple[str, UiStyle]
BannerRows = List[List[Tuple[str, str]]]


@runtime_checkable
class UI(Protocol):
    def banner(self, rows: BannerRows) -> None: ...
    def set_identity(self, name: str) -> None: ...
    def set_status(self, left: str, right: str) -> None: ...
    def set_working(self, working: bool) -> None: ...
    def begin_assistant(self) -> None: ...
    def append_inline(self, text: str) -> None: ...
    def append_line(self, text: str, style: UiStyle = UiStyle.NORMAL) -> None: ...
    def set_commands(self, names: List[str]) -> None: ...
    def clear(self) -> None: ...
    async def confirm(self, title: str, message: str) -> bool: ...
    async def run(self, on_submit: "Callable[[str], Awaitable[None]]") -> None: ...
    def request_stop(self) -> None: ...
