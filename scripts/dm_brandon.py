"""Send a DM to Brandon. Used for progress updates while working.

Usage:
    python scripts/dm_brandon.py "Your message here"
"""
from __future__ import annotations

import asyncio
import sys

from entraclaw.config import get_config
from entraclaw.tools.teams import acquire_agent_user_token, send

BRANDON_DM = (
    "19:44444444-4444-4444-4444-444444444444_"
    "4d4a65ef-e9b3-4ec2-a1e2-b430a5855118@unq.gbl.spaces"
)


async def main() -> None:
    if len(sys.argv) < 2:
        print("usage: dm_brandon.py <message>", file=sys.stderr)
        sys.exit(2)
    msg = sys.argv[1]
    token = acquire_agent_user_token(get_config())
    result = await send(chat_id=BRANDON_DM, message=msg, token=token)
    print(result.get("id", "ok"))


if __name__ == "__main__":
    asyncio.run(main())
