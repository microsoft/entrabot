"""Fetch and print one or more emails by subject match."""

from __future__ import annotations

import asyncio
import sys

import httpx

from entrabot.config import get_config
from entrabot.tools.teams import acquire_agent_user_token


async def main() -> None:
    subject_contains = sys.argv[1] if len(sys.argv) > 1 else "Project Apollo"
    top = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    token = acquire_agent_user_token(get_config())
    async with httpx.AsyncClient() as client:
        r = await client.get(
            "https://graph.microsoft.com/v1.0/me/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "$top": str(top),
                "$orderby": "receivedDateTime desc",
                "$filter": f"contains(subject, '{subject_contains}')",
                "$select": "subject,from,receivedDateTime,body,bodyPreview,hasAttachments",
            },
        )
        data = r.json()
        for m in data.get("value", []):
            sender = (m.get("from") or {}).get("emailAddress", {}).get("address", "?")
            ts = m.get("receivedDateTime", "")
            subj = m.get("subject", "?")
            body = (m.get("body") or {}).get("content", "")
            print(f"\n\n======== [{ts}] {sender} ========")
            print(f"Subject: {subj}")
            import re

            text = re.sub(r"<[^>]+>", " ", body)
            text = re.sub(r"\s+", " ", text).strip()
            print(text[:3000])


if __name__ == "__main__":
    asyncio.run(main())
