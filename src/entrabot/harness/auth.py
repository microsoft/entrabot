"""Token provider for the Teams bridge.

The Teams ingress/egress needs an Agent-User Graph token. Production should hand the
harness entrabot's three-hop token (the crown-jewel auth stack in :mod:`entrabot.auth`).
For bring-up/testing, an ``ENTRABOT_GRAPH_TOKEN`` env var is honored so the harness can
run end-to-end without standing up the full auth chain.

If no token source is available, returns ``None`` and the harness runs console-only
(you can still chat with the agent; it just won't listen to or post on Teams).
"""

from __future__ import annotations

import os
from typing import Optional

from .teams_comms import TokenProvider


def make_token_provider() -> Optional[TokenProvider]:
    env_token = os.environ.get("ENTRABOT_GRAPH_TOKEN")
    if env_token:
        async def _provider() -> str:
            # Re-read each call so a refreshed token in the env is picked up.
            return os.environ.get("ENTRABOT_GRAPH_TOKEN", env_token)

        return _provider

    # INTEGRATION POINT: wire entrabot's three-hop here, e.g.
    #   from entrabot.auth import acquire_agent_user_token
    #   async def _provider() -> str: return await acquire_agent_user_token()
    #   return _provider
    return None
