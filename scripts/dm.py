"""Send a Teams message to a chat. Uses the Agent User (three-hop) token.

Usage:
    python scripts/dm.py "Your message here" --chat <chat_id>   # any chat (DM or group)
    python scripts/dm.py "Your message here" --chat myalias    # alias defined in CHAT_ALIASES

Known aliases: (none by default — populate CHAT_ALIASES below with your own chat IDs)
"""

from __future__ import annotations

import argparse
import asyncio

from entrabot.config import get_config
from entrabot.tools.teams import acquire_agent_user_token, send

# Populate this dict with your own chat aliases for convenience.
# Example: "alice": "19:xxxxxxxx...@unq.gbl.spaces"
CHAT_ALIASES: dict[str, str] = {}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("message")
    ap.add_argument(
        "--chat",
        required=True,
        help="chat_id or alias key from CHAT_ALIASES",
    )
    args = ap.parse_args()

    chat_id = CHAT_ALIASES.get(args.chat, args.chat)
    token = acquire_agent_user_token(get_config())
    result = await send(chat_id=chat_id, message=args.message, token=token)
    print(result.get("id", "ok"))


if __name__ == "__main__":
    asyncio.run(main())
