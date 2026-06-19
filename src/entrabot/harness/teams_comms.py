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
_DISCOVER_SECONDS = 120  # re-sweep GET /me/chats this often to pick up new chats


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
        on_note: Optional[Callable[[str], None]] = None,
    ):
        self._token = token_provider
        self._watched: List[str] = list(watched_chats)
        self._inject = inject
        self._self_id = self_id
        self._poll_seconds = poll_seconds
        self._on_note = on_note
        self._noted: Set[str] = set()  # de-dupe repeated status notes
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

    async def discover_chats(self) -> int:
        """Register the agent's Teams chats via GET /me/chats (mirrors entrabot).

        Without this, the agent would never see messages in chats it didn't explicitly
        /watch — including ones a human starts by adding the agent. Newly found chats are
        primed (existing messages marked seen) so they don't flood with history.
        """
        import httpx

        try:
            token = await self._token()
        except Exception as e:
            self._note("teams-token", f"Teams: could not acquire a token — {type(e).__name__}: {e}")
            return 0
        new: List[str] = []
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(
                    "https://graph.microsoft.com/v1.0/me/chats",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"$top": "50"},
                )
            if r.status_code != 200:
                hint = ""
                if r.status_code == 403:
                    hint = " — the token is missing Chat.Read/Chat.ReadWrite. Complete the entrabot agent provisioning (it issues a token with Teams scopes)."
                elif r.status_code == 401:
                    hint = " — token expired/invalid."
                self._note(f"discover-{r.status_code}", f"Teams: GET /me/chats returned {r.status_code}{hint}")
                return 0
            for ch in r.json().get("value", []):
                cid = ch.get("id")
                if cid and cid not in self._watched:
                    self._watched.append(cid)
                    self._seen.setdefault(cid, set())
                    new.append(cid)
        except Exception as e:
            self._note("discover-err", f"Teams: chat discovery failed — {type(e).__name__}: {e}")
            return 0
        for cid in new:
            try:
                for m in await self.read(cid, count=20):
                    self._seen[cid].add(_field(m, "message_id", "id"))
            except Exception:
                pass
        if self._watched:
            self._note("watching", f"Teams: watching {len(self._watched)} chat(s)")
        return len(new)

    def _note(self, key: str, msg: str) -> None:
        if self._on_note and key not in self._noted:
            self._noted.add(key)
            self._on_note(msg)

    async def _prime(self) -> None:
        """Mark existing messages as seen so we only inject genuinely new ones."""
        for chat in self._watched:
            try:
                for m in await self.read(chat, count=20):
                    self._seen[chat].add(_field(m, "message_id", "id"))
            except Exception:
                pass

    async def _poll(self) -> None:
        await self.discover_chats()  # find chats the agent is in before priming
        await self._prime()
        elapsed = 0
        while not self._stop.is_set():
            await asyncio.sleep(self._poll_seconds)
            elapsed += self._poll_seconds
            if elapsed >= _DISCOVER_SECONDS:  # periodically pick up newly-created chats
                elapsed = 0
                await self.discover_chats()
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
