"""Agent identity queries — no bootstrap, no device-code flows.

All identity setup happens in ``scripts/setup.sh`` BEFORE the MCP server
starts.  This module only reads the pre-configured state.
"""

from __future__ import annotations

import logging

from entraclaw.config import get_config

logger = logging.getLogger("entraclaw.tools.identity")


async def whoami(*, token: str | None = None) -> dict:
    """Return current agent identity info from the environment.

    *token* is optionally passed from the MCP server state to report
    authentication status.
    """
    config = get_config()
    return {
        "agent_type": "Entra Agent Identity",
        "blueprint_app_id": config.blueprint_app_id or "not_configured",
        "agent_id": config.agent_id or "not_configured",
        "tenant_id": config.tenant_id or "not_configured",
        "human_sponsor": config.human_upn or "not_configured",
        "status": "authenticated" if token else "not_authenticated",
    }
