"""Send a Teams message to a chat. Uses the Agent User (three-hop) token.

Usage:
    python scripts/dm.py "Your message here"                   # DMs Brandon (default)
    python scripts/dm.py "Your message here" --chat <chat_id>  # any chat (DM or group)
    python scripts/dm.py "Your message here" --chat brandon    # alias

Known aliases: brandon
"""

from __future__ import annotations

import argparse
import asyncio

from entraclaw.config import get_config
from entraclaw.tools.teams import acquire_agent_user_token, send

CHAT_ALIASES: dict[str, str] = {
    "brandon": (
        "19:44444444-4444-4444-4444-444444444444_"
        "4d4a65ef-e9b3-4ec2-a1e2-b430a5855118@unq.gbl.spaces"
    ),
}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("message")
    ap.add_argument(
        "--chat",
        default="brandon",
        help="chat_id or alias (default: 'brandon')",
    )
    args = ap.parse_args()

    chat_id = CHAT_ALIASES.get(args.chat, args.chat)
    token = acquire_agent_user_token(get_config())
    result = await send(chat_id=chat_id, message=args.message, token=token)
    print(result.get("id", "ok"))


if __name__ == "__main__":
    asyncio.run(main())
