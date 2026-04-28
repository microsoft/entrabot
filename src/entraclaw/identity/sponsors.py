"""Agent Identity sponsor gate for inbound Teams messages.

The wait-for-sponsor-DM tool only returns when an inbound DM comes
from one of the Agent Identity's sponsors. Sponsors come from the
Graph relationship at::

    /servicePrincipals/{agent_object_id}/microsoft.graph.agentIdentity/sponsors

This module mirrors the (now-removed) supervise.py gate so the same
identifiers work in Copilot CLI's wait-tool path as in the prior
PTY-supervisor path. Background poll uses the same gate when present.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from entraclaw.config import EntraClawConfig
from entraclaw.errors import GraphApiError, TokenExpiredError
from entraclaw.tools.teams import (
    acquire_agent_identity_token,
    acquire_agent_user_token,
)

AGENT_IDENTITY_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
logger = logging.getLogger(__name__)


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _merge_tuple(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    merged: list[str] = []
    for value in (*left, *right):
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(value)
    return tuple(merged)


def _normalize_proxy_address(value: str) -> str:
    value = value.strip()
    if ":" not in value:
        return value
    prefix, address = value.split(":", 1)
    if prefix.lower() == "smtp":
        return address.strip()
    return value


def _normalize_email(value: str | None) -> str:
    if not value:
        return ""
    return _normalize_proxy_address(value).strip().lower()


def _normalize_id(value: str | None) -> str:
    return (value or "").strip().lower()


@dataclass(frozen=True)
class AgentIdentitySponsor:
    """User sponsor returned by the Agent Identity's Graph sponsors relationship."""

    user_id: str
    user_principal_name: str | None
    mail: str | None
    other_mails: tuple[str, ...] = ()
    proxy_addresses: tuple[str, ...] = ()

    @classmethod
    def from_graph_user(cls, user: dict[str, Any]) -> AgentIdentitySponsor | None:
        user_id = str(user.get("id") or "").strip()
        if not user_id:
            return None
        other_mails = _strings(user.get("otherMails"))
        proxy_addresses = tuple(
            normalized
            for normalized in (
                _normalize_proxy_address(item) for item in _strings(user.get("proxyAddresses"))
            )
            if normalized
        )
        return cls(
            user_id=user_id,
            user_principal_name=str(user.get("userPrincipalName") or "").strip() or None,
            mail=str(user.get("mail") or next(iter(other_mails), "")).strip() or None,
            other_mails=other_mails,
            proxy_addresses=proxy_addresses,
        )

    def merge(self, other: AgentIdentitySponsor) -> AgentIdentitySponsor:
        if self.user_id.lower() != other.user_id.lower():
            raise ValueError("cannot merge different sponsors")
        return AgentIdentitySponsor(
            user_id=self.user_id,
            user_principal_name=self.user_principal_name or other.user_principal_name,
            mail=self.mail or other.mail,
            other_mails=_merge_tuple(self.other_mails, other.other_mails),
            proxy_addresses=_merge_tuple(self.proxy_addresses, other.proxy_addresses),
        )

    def email_identifiers(self) -> frozenset[str]:
        values = [
            self.user_principal_name,
            self.mail,
            *self.other_mails,
            *self.proxy_addresses,
        ]
        return frozenset(
            normalized
            for normalized in (_normalize_email(value) for value in values)
            if normalized
        )


@dataclass(frozen=True)
class SponsorGate:
    """Allow inbound Teams messages only from the Agent Identity's user sponsors."""

    user_ids: frozenset[str]
    upns: frozenset[str]
    mails: frozenset[str]

    @classmethod
    def from_agent_identity_sponsors(
        cls,
        sponsors: list[AgentIdentitySponsor],
    ) -> SponsorGate:
        user_ids = frozenset(
            _normalize_id(sponsor.user_id) for sponsor in sponsors if sponsor.user_id
        )
        upns = frozenset(
            _normalize_email(sponsor.user_principal_name)
            for sponsor in sponsors
            if sponsor.user_principal_name
        )
        mails = frozenset(
            email
            for sponsor in sponsors
            for email in sponsor.email_identifiers()
            if email
        )
        if not user_ids and not upns and not mails:
            raise ValueError("no sponsor identifiers configured")
        return cls(user_ids=user_ids, upns=upns, mails=mails)

    def with_chat_members(self, members: list[dict[str, Any]]) -> SponsorGate:
        """Add chat member user IDs only when Graph email matches a sponsor identity."""
        user_ids = set(self.user_ids)
        sponsor_emails = self.upns | self.mails
        for member in members:
            member_user_id = _normalize_id(
                str(member.get("user_id") or member.get("userId") or "")
            )
            member_emails = {
                _normalize_email(str(member.get("email") or "")),
                _normalize_email(str(member.get("mail") or "")),
                _normalize_email(str(member.get("userPrincipalName") or "")),
            }
            if member_user_id and sponsor_emails.intersection(member_emails):
                user_ids.add(member_user_id)
        return SponsorGate(
            user_ids=frozenset(user_ids),
            upns=self.upns,
            mails=self.mails,
        )

    def accepts(self, message: dict[str, Any]) -> bool:
        sender_id = _normalize_id(str(message.get("sender_id") or ""))
        sender = _normalize_email(str(message.get("sender") or ""))
        return bool(
            (sender_id and sender_id in self.user_ids)
            or (sender and sender in self.upns)
            or (sender and sender in self.mails)
        )


def fetch_agent_identity_sponsors(
    config: EntraClawConfig,
    *,
    token_provider: Callable[[EntraClawConfig], str] = acquire_agent_identity_token,
    transport: httpx.BaseTransport | None = None,
) -> list[AgentIdentitySponsor]:
    """Read user sponsors from the Agent Identity service principal in Graph."""
    if not config.agent_object_id:
        raise ValueError("Agent Identity object id is not configured")

    token = token_provider(config)
    client_kwargs: dict[str, Any] = {"timeout": httpx.Timeout(15.0)}
    if transport is not None:
        client_kwargs["transport"] = transport
    url = (
        f"{AGENT_IDENTITY_GRAPH_BASE}/servicePrincipals/{config.agent_object_id}"
        "/microsoft.graph.agentIdentity/sponsors?"
        "$select=id,userPrincipalName,mail,otherMails,proxyAddresses,identities"
    )
    with httpx.Client(**client_kwargs) as client:
        resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code == 401:
        raise TokenExpiredError(
            "Agent Identity token expired while reading Agent Identity sponsors"
        )
    if resp.status_code != 200:
        raise GraphApiError(
            resp.status_code, resp.text or "failed to read Agent Identity sponsors"
        )

    sponsors: list[AgentIdentitySponsor] = []
    with httpx.Client(**client_kwargs) as client:
        for item in resp.json().get("value", []):
            if not isinstance(item, dict):
                continue
            sponsor = AgentIdentitySponsor.from_graph_user(item)
            if sponsor is None:
                continue
            enriched = _fetch_sponsor_user_details(token, sponsor.user_id, client)
            sponsors.append(sponsor.merge(enriched) if enriched is not None else sponsor)
    if not sponsors:
        raise ValueError("Agent Identity has no user sponsors")
    return sponsors


def _fetch_sponsor_user_details(
    token: str,
    user_id: str,
    client: httpx.Client,
) -> AgentIdentitySponsor | None:
    url = (
        f"{AGENT_IDENTITY_GRAPH_BASE}/users/{user_id}"
        "?$select=id,userPrincipalName,mail,otherMails,proxyAddresses,identities"
    )
    resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code == 200:
        return AgentIdentitySponsor.from_graph_user(resp.json())
    if resp.status_code == 401:
        raise TokenExpiredError(
            "Agent Identity token expired while reading sponsor user details"
        )
    return None


def _watched_chat_ids(data_dir: Path) -> list[str]:
    watched_file = data_dir / "watched_chats"
    if not watched_file.is_file():
        return []
    return [
        line.strip()
        for line in watched_file.read_text().splitlines()
        if line.strip()
    ]


def fetch_watched_chat_members(
    config: EntraClawConfig,
    *,
    token_provider: Callable[[EntraClawConfig], str] = acquire_agent_user_token,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Return Graph chat members for watched chats using the Agent User token."""
    chat_ids = _watched_chat_ids(config.data_dir)
    if not chat_ids:
        return []

    token = token_provider(config)
    client_kwargs: dict[str, Any] = {"timeout": httpx.Timeout(15.0)}
    if transport is not None:
        client_kwargs["transport"] = transport

    members: list[dict[str, Any]] = []
    with httpx.Client(**client_kwargs) as client:
        for chat_id in chat_ids:
            try:
                resp = client.get(
                    f"{AGENT_IDENTITY_GRAPH_BASE}/chats/{chat_id}/members",
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "failed to read watched chat members for %s: %s", chat_id, exc
                )
                continue
            if resp.status_code == 401:
                raise TokenExpiredError(
                    "Agent User token expired while reading watched chat members"
                )
            if resp.status_code != 200:
                logger.warning(
                    "failed to read watched chat members for %s: HTTP %s %s",
                    chat_id,
                    resp.status_code,
                    resp.text[:200],
                )
                continue
            for member in resp.json().get("value", []):
                if isinstance(member, dict):
                    members.append(
                        {
                            "user_id": member.get("userId", ""),
                            "name": member.get("displayName", ""),
                            "email": member.get("email", ""),
                            "roles": member.get("roles", []),
                        }
                    )
    return members


def load_agent_identity_sponsor_gate(config: EntraClawConfig) -> SponsorGate:
    """Build the sponsor gate from the Agent Identity's Graph sponsors."""
    gate = SponsorGate.from_agent_identity_sponsors(
        fetch_agent_identity_sponsors(config)
    )
    return gate.with_chat_members(fetch_watched_chat_members(config))
