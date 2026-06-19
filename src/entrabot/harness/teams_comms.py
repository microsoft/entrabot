"""Teams ingress/egress bridge (replaces the MQTT Session/ChannelConnection.cs).

Polls the watched Teams chats via :func:`entrabot.tools.teams.read`, deduplicates, and
injects new human messages into the running Copilot session as steering. Outbound replies
go through :func:`entrabot.tools.teams.send`.

Crucially, it tracks the *active caller* (the sender of the message currently being
handled) so the permission layer can gate tools per caller.
"""

from __future__ import annotations

import asyncio
import html
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional, Set

# Token provider: returns a valid Agent-User Graph token (entrabot's three-hop auth).
TokenProvider = Callable[[], Awaitable[str]]
# Inject a steering prompt into the session: (prompt, caller_id, chat_id).
InjectFn = Callable[[str, Optional[str], Optional[str]], Awaitable[None]]

_POLL_SECONDS = 5


@dataclass
class TurnContext:
    """The caller + chat bound to the turn currently running.

    Owned by the session and read by the permission policy (who is this turn for?) and the
    reply tools (which chat do I answer in?). ``None`` for operator-typed/local input.
    """

    caller: Optional[str] = None
    chat: Optional[str] = None


def _field(msg: dict, *names: str, default: str = "") -> str:
    for n in names:
        v = msg.get(n)
        if v:
            return str(v)
    return default


class TeamsBridge:
    def __init__(
        self,
        token_provider: TokenProvider,
        watched_chats: List[str],
        inject: InjectFn,
        *,
        self_id: Optional[str] = None,
        poll_seconds: int = _POLL_SECONDS,
    ):
        self._token = token_provider
        self._watched: List[str] = list(watched_chats)
        self._inject = inject
        self._self_id = self_id
        self._poll_seconds = poll_seconds
        self._seen: Dict[str, Set[str]] = {c: set() for c in self._watched}
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # ---- watched chats ---------------------------------------------------------------
    def watched_chats(self) -> List[str]:
        return list(self._watched)

    def watch(self, chat_id: str) -> None:
        if chat_id not in self._watched:
            self._watched.append(chat_id)
            self._seen.setdefault(chat_id, set())

    # ---- egress ----------------------------------------------------------------------
    async def send(self, chat_id: str, message: str, content_type: str = "html") -> dict:
        from ..tools import teams  # lazy: entrabot auth need not be configured to import

        token = await self._token()
        return await teams.send(chat_id=chat_id, message=message, token=token, content_type=content_type)

    async def read(self, chat_id: str, count: int = 5) -> List[dict]:
        from ..tools import teams

        token = await self._token()
        return await teams.read(chat_id=chat_id, token=token, count=count)

    # ---- ingress loop ----------------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()

    async def _prime(self) -> None:
        """Mark existing messages as seen so we only inject genuinely new ones."""
        for chat in self._watched:
            try:
                for m in await self.read(chat, count=20):
                    self._seen[chat].add(_field(m, "message_id", "id"))
            except Exception:
                pass

    async def _poll(self) -> None:
        await self._prime()
        while not self._stop.is_set():
            await asyncio.sleep(self._poll_seconds)
            for chat in list(self._watched):
                try:
                    msgs = await self.read(chat, count=10)
                except Exception:
                    continue
                # oldest-first so injection order is chronological
                for m in reversed(msgs):
                    mid = _field(m, "message_id", "id")
                    if not mid or mid in self._seen[chat]:
                        continue
                    self._seen[chat].add(mid)
                    sender_id = _field(m, "sender_id", "from_id", "fromId")
                    if self._self_id and sender_id == self._self_id:
                        continue  # don't echo our own messages
                    await self._inject_message(chat, m)

    async def _inject_message(self, chat: str, msg: dict) -> None:
        sender = _field(msg, "from", "sender_name", "senderName", default="someone")
        sender_id = _field(msg, "sender_id", "from_id", "fromId", default=sender)
        body = _field(msg, "content", "body")
        framed = (
            "[teams] New message (this is a steering update — fold it into your work; "
            "reply in Teams with the entrabot_send tool, don't echo it here).\n"
            f"chat: {chat}\nfrom: {sender} ({sender_id})\nmessage: {html.unescape(body)}"
        )
        # carry the caller + chat so the session can bind them to this turn
        await self._inject(framed, sender_id, chat)
