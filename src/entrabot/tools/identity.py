"""Agent identity queries — no bootstrap, no device-code flows.

All identity setup happens in ``scripts/setup.sh`` BEFORE the MCP server
starts.  This module only reads the pre-configured state.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import httpx

from entrabot.config import get_config
from entrabot.errors import EntraBotError
from entrabot.identity import sponsors as agent_identity_sponsors
from entrabot.identity.sponsors import AgentIdentitySponsor
from entrabot.tools.teams import acquire_agent_user_token

logger = logging.getLogger("entrabot.tools.identity")


SPONSOR_SOURCE = "entra_agent_identity_sponsors"


def _dedupe(values: Iterable[str | None]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        normalized = (value or "").strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
    return out


def _sponsor_mail_values(sponsor: AgentIdentitySponsor) -> list[str | None]:
    return [
        sponsor.mail,
        *sponsor.other_mails,
        *sponsor.proxy_addresses,
        *sponsor.federated_emails,
    ]


def _sponsor_fields_from_entra(sponsors: list[AgentIdentitySponsor]) -> dict:
    sponsor_upns = _dedupe(sponsor.user_principal_name for sponsor in sponsors)
    sponsor_mails = _dedupe(
        value for sponsor in sponsors for value in _sponsor_mail_values(sponsor)
    )
    sponsor_user_ids = _dedupe(sponsor.user_id for sponsor in sponsors)
    human_sponsors = _dedupe([*sponsor_upns, *sponsor_mails])
    primary_sponsor = (
        human_sponsors[0]
        if human_sponsors
        else sponsor_user_ids[0]
        if sponsor_user_ids
        else "unavailable"
    )
    return {
        "human_sponsor": primary_sponsor,
        "human_sponsors": human_sponsors,
        "human_sponsor_upns": sponsor_upns,
        "human_sponsor_mails": sponsor_mails,
        "human_sponsor_user_ids": sponsor_user_ids,
        "human_sponsor_count": len(sponsors),
        "human_sponsor_source": SPONSOR_SOURCE,
        "human_sponsor_status": "loaded",
    }


def _sponsor_fields_unavailable(status: str, error: str | None = None) -> dict:
    fields = {
        "human_sponsor": "not_configured" if status == "not_configured" else "unavailable",
        "human_sponsors": [],
        "human_sponsor_upns": [],
        "human_sponsor_mails": [],
        "human_sponsor_user_ids": [],
        "human_sponsor_count": 0,
        "human_sponsor_source": SPONSOR_SOURCE,
        "human_sponsor_status": status,
    }
    if error:
        fields["human_sponsor_error"] = error
    return fields


async def whoami(*, token: str | None = None) -> dict:
    """Return current agent identity info from the environment.

    *token* is optionally passed from the MCP server state to report
    authentication status.
    """
    config = get_config()
    result = {
        "agent_type": "Entra Agent Identity",
        "blueprint_app_id": config.blueprint_app_id or "not_configured",
        "agent_id": config.agent_id or "not_configured",
        "tenant_id": config.tenant_id or "not_configured",
        "status": "authenticated" if token else "not_authenticated",
    }
    if not config.agent_object_id:
        result.update(
            _sponsor_fields_unavailable(
                "not_configured",
                "Agent Identity object id is not configured",
            )
        )
        return result

    try:
        sponsors = agent_identity_sponsors.fetch_agent_identity_sponsors(
            config,
            user_token_provider=acquire_agent_user_token,
        )
    except (EntraBotError, httpx.HTTPError, ValueError) as exc:
        logger.warning("Failed to read Agent Identity sponsors from Entra: %s", exc)
        result.update(_sponsor_fields_unavailable("error", str(exc)))
        return result

    result.update(_sponsor_fields_from_entra(sponsors))
    return result
