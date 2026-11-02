"""Pull recent messages from all watched chats + inbox.

Run as Agent User to see what landed while we weren't polling. The chat list comes
from the configured storage backend, the same one the MCP server uses. Importing
``entrabot.config`` loads ``.env``, which is usually where the blob settings that
select that backend live. The pre-explicit-chats ``chat_id`` key is read from a
local backend only, since a cloud container may hold a stale copy of it.
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from entrabot.config import get_config
from entrabot.storage.backend import LocalBackend, get_backend
from entrabot.tools.poll_state import (
    LEGACY_DEFAULT_CHAT_KEY,
    PollStateOutcome,
    is_chat_id,
    read_watched_chats,
)
from entrabot.tools.teams import acquire_agent_user_token


async def fetch(
    client: httpx.AsyncClient,
    url: str,
    token: str,
    params: dict | None = None,
) -> dict:
    r = await client.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    if r.status_code >= 400:
        return {"_error": r.status_code, "_body": r.text[:300]}
    return r.json()


def _legacy_default_chat() -> str:
    """Return the single-chat key from before chats were explicit, or ``""``.

    Only a local backend is asked. Nothing writes this key any more, but a cloud
    container may still hold a copy from an earlier migration, and that copy could
    be another agent's chat. A failed read is reported and treated as absent, since
    the key is optional.
    """
    try:
        backend = get_backend()
        if not isinstance(backend, LocalBackend):
            return ""
        return (backend.read_text(LEGACY_DEFAULT_CHAT_KEY) or "").strip()
    except Exception as exc:  # noqa: BLE001 — optional key; the list is what matters.
        print(
            f"WARNING: could not read {LEGACY_DEFAULT_CHAT_KEY}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return ""


def load_chat_ids() -> tuple[list[str], bool]:
    """Return ``(chat_ids, resolved)`` from the configured backend.

    ``resolved`` is False when the list couldn't be read, which is reported instead
    of looking like "no chats". There is no fallback to a local copy.
    """
    listing = read_watched_chats()
    if listing.outcome is PollStateOutcome.UNRESOLVED:
        print(
            "ERROR: could not read the watched-chat list from the configured "
            "storage backend; showing the inbox only.",
            file=sys.stderr,
        )
        return [], False

    chat_ids = list(listing.chat_ids)
    legacy = _legacy_default_chat()
    if is_chat_id(legacy) and legacy not in chat_ids:
        chat_ids.insert(0, legacy)
    return chat_ids, True


async def main() -> None:
    _hours = int(sys.argv[1]) if len(sys.argv) > 1 else 6  # noqa: F841 — reserved for future filtering
    token = acquire_agent_user_token(get_config())

    chat_ids, resolved = await asyncio.to_thread(load_chat_ids)

    async with httpx.AsyncClient() as client:
        for cid in chat_ids:
            print(f"\n========= CHAT {cid} =========")
            data = await fetch(
                client,
                f"https://graph.microsoft.com/v1.0/chats/{cid}/messages",
                token,
                params={"$top": "15", "$orderby": "createdDateTime desc"},
            )
            if "_error" in data:
                print(f"  ERROR {data['_error']}: {data['_body'][:200]}")
                continue
            for m in data.get("value", [])[:15]:
                who = (m.get("from") or {}).get("user", {}).get("displayName", "system/unknown")
                ts = m.get("createdDateTime", "")
                body = (m.get("body") or {}).get("content", "")
                body = body.replace("\n", " ")[:300]
                print(f"  [{ts}] {who}: {body}")

        print("\n========= INBOX (top 15) =========")
        mail = await fetch(
            client,
            "https://graph.microsoft.com/v1.0/me/messages",
            token,
            params={
                "$top": "15",
                "$orderby": "receivedDateTime desc",
                "$select": "subject,from,receivedDateTime,bodyPreview,hasAttachments",
            },
        )
        if "_error" in mail:
            print(f"  ERROR {mail['_error']}: {mail['_body'][:200]}")
        else:
            for m in mail.get("value", []):
                sender = (m.get("from") or {}).get("emailAddress", {}).get("address", "?")
                subj = m.get("subject", "?")
                ts = m.get("receivedDateTime", "")
                prev = (m.get("bodyPreview", "") or "").replace("\n", " ")[:200]
                att = " 📎" if m.get("hasAttachments") else ""
                print(f"  [{ts}] {sender}{att}: {subj}")
                if prev:
                    print(f"     {prev}")

    if not resolved:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
