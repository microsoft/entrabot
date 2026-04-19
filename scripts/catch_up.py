"""Pull recent messages from all watched chats + inbox.

Run as Agent User to see what landed while we weren't polling.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

from entraclaw.config import get_config
from entraclaw.tools.teams import acquire_agent_user_token


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


async def main() -> None:
    _hours = int(sys.argv[1]) if len(sys.argv) > 1 else 6  # noqa: F841 — reserved for future filtering
    token = acquire_agent_user_token(get_config())

    watched = Path.home() / ".entraclaw" / "data" / "watched_chats"
    chat_ids: list[str] = []
    if watched.exists():
        chat_ids = [
            ln.strip() for ln in watched.read_text().splitlines() if ln.strip()
        ]
    default_chat = Path.home() / ".entraclaw" / "data" / "chat_id"
    if default_chat.exists():
        cid = default_chat.read_text().strip()
        if cid and cid not in chat_ids:
            chat_ids.insert(0, cid)

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
                who = (m.get("from") or {}).get("user", {}).get(
                    "displayName", "system/unknown"
                )
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
                sender = (
                    (m.get("from") or {}).get("emailAddress", {}).get("address", "?")
                )
                subj = m.get("subject", "?")
                ts = m.get("receivedDateTime", "")
                prev = (m.get("bodyPreview", "") or "").replace("\n", " ")[:200]
                att = " 📎" if m.get("hasAttachments") else ""
                print(f"  [{ts}] {sender}{att}: {subj}")
                if prev:
                    print(f"     {prev}")


if __name__ == "__main__":
    asyncio.run(main())
