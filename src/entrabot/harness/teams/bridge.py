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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

# Token provider: returns a valid Agent-User Graph token (entrabot's three-hop auth).
TokenProvider = Callable[[], Awaitable[str]]
# Inject a steering prompt into the session: (prompt, caller_id, chat_id).
InjectFn = Callable[[str, str | None, str | None], Awaitable[None]]

_POLL_SECONDS = 5
_DISCOVER_SECONDS = 120  # re-sweep GET /me/chats this often to pick up new chats
_ME_CHATS_URL = "https://graph.microsoft.com/v1.0/me/chats"


@dataclass
class TurnContext:
    """The caller + chat bound to the turn currently running.

    Owned by the session and read by the permission policy (who is this turn for?) and the
    reply tools (which chat do I answer in?). ``None`` for operator-typed/local input.
    """

    caller: str | None = None
    chat: str | None = None


def _field(msg: dict, *names: str, default: str = "") -> str:
    """First non-empty value among ``names`` in ``msg`` (Graph shapes vary), else ``default``."""
    for name in names:
        value = msg.get(name)
        if value:
            return str(value)
    return default


def _discovery_error_hint(status_code: int) -> str:
    """Human hint for a non-200 GET /me/chats response (empty for unrecognized codes)."""
    if status_code == 403:
        return (
            " — the token is missing Chat.Read/Chat.ReadWrite. Complete the entrabot agent "
            "provisioning (it issues a token with Teams scopes)."
        )
    if status_code == 401:
        return " — token expired/invalid."
    return ""


class TeamsBridge:
    def __init__(
        self,
        token_provider: TokenProvider,
        watched_chats: list[str],
        inject: InjectFn,
        *,
        self_id: str | None = None,
        poll_seconds: int = _POLL_SECONDS,
        on_note: Callable[[str], None] | None = None,
    ):
        self._token = token_provider
        self._watched: list[str] = list(watched_chats)
        self._inject = inject
        self._self_id = self_id
        self._poll_seconds = poll_seconds
        self._on_note = on_note
        self._noted: set[str] = set()  # de-dupe repeated status notes
        self._seen: dict[str, set[str]] = {chat: set() for chat in self._watched}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ---- watched chats ---------------------------------------------------------------
    def watched_chats(self) -> list[str]:
        return list(self._watched)

    def watch(self, chat_id: str) -> None:
        if chat_id not in self._watched:
            self._watched.append(chat_id)
            self._seen.setdefault(chat_id, set())

    # ---- egress ----------------------------------------------------------------------
    async def send(self, chat_id: str, message: str, content_type: str = "html") -> dict:
        from ...tools import teams  # lazy: entrabot auth need not be configured to import

        token = await self._token()
        return await teams.send(
            chat_id=chat_id, message=message, token=token, content_type=content_type
        )

    async def read(self, chat_id: str, count: int = 5) -> list[dict]:
        from ...tools import teams

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
        except Exception as error:
            self._note(
                "teams-token", f"Teams: could not acquire a token — {type(error).__name__}: {error}"
            )
            return 0

        new_chats: list[str] = []
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    _ME_CHATS_URL,
                    headers={"Authorization": f"Bearer {token}"},
                    params={"$top": "50"},
                )
            if response.status_code != 200:
                self._note(
                    f"discover-{response.status_code}",
                    f"Teams: GET /me/chats returned {response.status_code}"
                    f"{_discovery_error_hint(response.status_code)}",
                )
                return 0
            for chat in response.json().get("value", []):
                chat_id = chat.get("id")
                if chat_id and chat_id not in self._watched:
                    self._watched.append(chat_id)
                    self._seen.setdefault(chat_id, set())
                    new_chats.append(chat_id)
        except Exception as error:
            self._note(
                "discover-err", f"Teams: chat discovery failed — {type(error).__name__}: {error}"
            )
            return 0

        for chat_id in new_chats:
            await self._mark_seen(chat_id)
        if self._watched:
            self._note("watching", f"Teams: watching {len(self._watched)} chat(s)")
        return len(new_chats)

    def _note(self, key: str, msg: str) -> None:
        if self._on_note and key not in self._noted:
            self._noted.add(key)
            self._on_note(msg)

    async def _mark_seen(self, chat_id: str, count: int = 20) -> None:
        """Mark a chat's existing messages as seen so they aren't injected as 'new'."""
        try:
            for message in await self.read(chat_id, count=count):
                self._seen[chat_id].add(_field(message, "message_id", "id"))
        except Exception:
            pass

    async def _prime(self) -> None:
        for chat in self._watched:
            await self._mark_seen(chat)

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
                await self._inject_new_messages(chat)

    async def _inject_new_messages(self, chat: str) -> None:
        try:
            messages = await self.read(chat, count=10)
        except Exception:
            return
        # oldest-first so injection order is chronological
        for message in reversed(messages):
            message_id = _field(message, "message_id", "id")
            if not message_id or message_id in self._seen[chat]:
                continue
            self._seen[chat].add(message_id)
            sender_id = _field(message, "sender_id", "from_id", "fromId")
            if self._self_id and sender_id == self._self_id:
                continue  # don't echo our own messages
            await self._inject_message(chat, message)

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
