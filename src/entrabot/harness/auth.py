"""Token provider for the Teams bridge.

Production path: entrabot's three-hop Agent-User flow
(:func:`entrabot.tools.teams.acquire_agent_user_token`). The token is cached and
re-acquired shortly before it expires (``exp`` decoded from the JWT).

Bring-up/testing path: an ``ENTRABOT_GRAPH_TOKEN`` env var, honored first so the harness
can run end-to-end without standing up cert/TPM auth.

If neither is available (no env token and three-hop creds incomplete), returns ``None``
and the harness runs console-only (you can still chat with the agent; it just won't
listen to or post on Teams).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

from .teams_comms import TokenProvider

# refresh this many seconds before the token's exp
_REFRESH_SKEW = 120


def _env_provider() -> Optional[TokenProvider]:
    if not os.environ.get("ENTRABOT_GRAPH_TOKEN"):
        return None

    async def _provider() -> str:
        token = os.environ.get("ENTRABOT_GRAPH_TOKEN")
        if not token:
            raise RuntimeError("ENTRABOT_GRAPH_TOKEN is no longer set")
        return token

    return _provider


def _three_hop_provider() -> Optional[TokenProvider]:
    try:
        import jwt  # PyJWT, already an entrabot dependency
        from entrabot.config import get_config
        from entrabot.tools.teams import acquire_agent_user_token
    except Exception:
        return None

    try:
        config = get_config()
    except Exception:
        return None

    # Only offer this provider if the three-hop creds are actually configured; otherwise
    # every poll would raise. These are the same fields acquire_agent_user_token checks.
    if not all(
        [
            getattr(config, "blueprint_app_id", None),
            getattr(config, "blueprint_cert_thumbprint", None),
            getattr(config, "tenant_id", None),
            getattr(config, "agent_id", None),
        ]
    ):
        return None

    cache: dict = {"token": None, "exp": 0.0}

    async def _provider() -> str:
        now = time.time()
        if cache["token"] and now < cache["exp"] - _REFRESH_SKEW:
            return cache["token"]
        # acquire is sync (cert signing + network); run off the event loop
        token = await asyncio.to_thread(acquire_agent_user_token, config)
        cache["token"] = token
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
            cache["exp"] = float(claims.get("exp", now + 3000))
        except Exception:
            cache["exp"] = now + 3000
        return token

    return _provider


def _delegated_provider() -> Optional[TokenProvider]:
    """MSAL-delegated fallback (the human's token), mirroring entrabot's _init_auth.

    Uses the shared MSAL token cache: ``try_silent`` returns a cached token with no prompt;
    if there's no cached login it does one interactive sign-in (browser/device code), same as
    entrabot's first run.
    """
    try:
        from entrabot.auth.delegated import MsalDelegatedAuth
        from entrabot.config import get_config
    except Exception:
        return None
    try:
        cfg = get_config()
    except Exception:
        return None
    client_id = getattr(cfg, "client_id", None)
    if not client_id:
        return None

    auth = MsalDelegatedAuth(client_id=client_id, tenant_id=getattr(cfg, "tenant_id", None) or "common")

    async def _provider() -> str:
        res = await asyncio.to_thread(auth.try_silent)
        if not (res and "access_token" in res):
            res = await asyncio.to_thread(auth.authenticate)  # interactive sign-in (first time only)
        if not res or "access_token" not in res:
            raise RuntimeError("MSAL delegated auth did not return a token")
        return res["access_token"]

    return _provider


def make_token_provider() -> Optional[TokenProvider]:
    return _env_provider() or _three_hop_provider() or _delegated_provider()
