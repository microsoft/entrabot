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


def _decode_b2b_ext_upn(upn: str | None) -> str | None:
    """Decode a B2B guest's EXT UPN back to its home address.

    Azure AD encodes a guest user's home address into their tenant UPN by
    replacing ``@`` with ``_`` and appending ``#EXT#@<tenant>.onmicrosoft.com``.
    For example::

        alice_example.com#EXT#@fabrikam.onmicrosoft.com
            decodes to → alice@example.com

        bob_contoso.com#EXT#@fabrikam.onmicrosoft.com
            decodes to → bob@contoso.com

    The home-address portion (left of ``#EXT#@``) is split at the LAST
    ``_`` since the original local-part may contain underscores. Returns
    ``None`` if ``upn`` does not look like a B2B EXT UPN.

    This matters because Microsoft-Account B2B guests (and some federated
    guests) come back from ``/users/{id}`` with ``mail``, ``otherMails``,
    ``proxyAddresses``, and federated ``identities[].issuerAssignedId``
    all null — the EXT UPN is the ONLY email-shaped field on the user
    object. Humans never type that form; they type the home address. The
    sponsor allowlist must accept both.
    """
    if not upn:
        return None
    normalized = upn.strip()
    if not normalized:
        return None
    # Case-insensitive ``#EXT#@`` separator — Graph emits ``#EXT#@`` in
    # production but tooling/casing varies, so be lenient.
    lower = normalized.lower()
    sep = "#ext#@"
    sep_index = lower.find(sep)
    if sep_index < 0:
        return None
    encoded_local = normalized[:sep_index]
    last_underscore = encoded_local.rfind("_")
    if last_underscore < 0:
        return None
    home_local = encoded_local[:last_underscore]
    home_domain = encoded_local[last_underscore + 1 :]
    if not home_local or "." not in home_domain:
        return None
    return f"{home_local}@{home_domain}"


def _federated_email_identifiers(identities: Any) -> tuple[str, ...]:
    """Extract email-shaped ``issuerAssignedId`` values from B2B ``identities``.

    For B2B guests federated from a home tenant, Graph returns an entry
    of the form::

        {"signInType": "federated", "issuer": "<home-domain>",
         "issuerAssignedId": "<home-tenant SMTP>"}

    The home-tenant SMTP is the SAME value the chat-members API returns
    as the member's ``email`` field (e.g. ``Alice.Smith@microsoft.com``)
    even when the agent-tenant guest record only carries the invitation
    alias (e.g. ``alice@microsoft.com``). Pulling these into the sponsor
    email set is what unlocks cross-tenant alias matching without an
    operator override file (Learning #50).
    """
    if not isinstance(identities, list):
        return ()
    out: list[str] = []
    for entry in identities:
        if not isinstance(entry, dict):
            continue
        assigned = str(entry.get("issuerAssignedId") or "").strip()
        if "@" in assigned:
            out.append(assigned)
    return tuple(out)


@dataclass(frozen=True)
class AgentIdentitySponsor:
    """User sponsor returned by the Agent Identity's Graph sponsors relationship."""

    user_id: str
    user_principal_name: str | None
    mail: str | None
    other_mails: tuple[str, ...] = ()
    proxy_addresses: tuple[str, ...] = ()
    federated_emails: tuple[str, ...] = ()

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
        federated_emails = _federated_email_identifiers(user.get("identities"))
        return cls(
            user_id=user_id,
            user_principal_name=str(user.get("userPrincipalName") or "").strip() or None,
            mail=str(user.get("mail") or next(iter(other_mails), "")).strip() or None,
            other_mails=other_mails,
            proxy_addresses=proxy_addresses,
            federated_emails=federated_emails,
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
            federated_emails=_merge_tuple(self.federated_emails, other.federated_emails),
        )

    def email_identifiers(self) -> frozenset[str]:
        decoded_upn = _decode_b2b_ext_upn(self.user_principal_name)
        values = [
            self.user_principal_name,
            decoded_upn,
            self.mail,
            *self.other_mails,
            *self.proxy_addresses,
            *self.federated_emails,
        ]
        return frozenset(
            normalized for normalized in (_normalize_email(value) for value in values) if normalized
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
            email for sponsor in sponsors for email in sponsor.email_identifiers() if email
        )
        if not user_ids and not upns and not mails:
            raise ValueError("no sponsor identifiers configured")
        return cls(user_ids=user_ids, upns=upns, mails=mails)

    def with_chat_members(self, members: list[dict[str, Any]]) -> SponsorGate:
        """Add chat member user IDs only when Graph email matches a sponsor identity."""
        user_ids = set(self.user_ids)
        sponsor_emails = self.upns | self.mails
        for member in members:
            member_user_id = _normalize_id(str(member.get("user_id") or member.get("userId") or ""))
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

    def with_watched_chat_ids(self, chat_ids: list[str], agent_user_id: str) -> SponsorGate:
        """Extract the cross-tenant sponsor's home-tenant userId from 1:1 chat IDs.

        For federated B2B 1:1 chats, Microsoft Graph encodes both participants'
        home-tenant userIds in the chat_id itself:

            ``19:{user_a_id}_{user_b_id}@unq.gbl.spaces``

        The chat-members API does NOT expose the cross-tenant guest's email,
        so ``with_chat_members`` cannot match on it. The chat_id is the only
        reliable carrier. If the agent's user_id is one half, the OTHER half
        is the sponsor's home-tenant userId — add it to ``user_ids``.

        This is safe because:
        1. ``unq.gbl.spaces`` chats are 1:1 by construction (exactly two
           participants); the non-agent half can only be the counterparty.
        2. The agent only watches chats it explicitly registered, so the
           counterparty was already vetted at chat-creation time.
        """
        agent_id = _normalize_id(agent_user_id)
        if not agent_id:
            return self
        user_ids = set(self.user_ids)
        for chat_id in chat_ids:
            if not chat_id or "@unq.gbl.spaces" not in chat_id:
                continue
            local = chat_id.split("@", 1)[0]
            if not local.startswith("19:"):
                continue
            local = local[len("19:") :]
            parts = local.split("_")
            if len(parts) != 2:
                continue
            left = _normalize_id(parts[0])
            right = _normalize_id(parts[1])
            if not left or not right:
                continue
            if left == agent_id and right != agent_id:
                user_ids.add(right)
            elif right == agent_id and left != agent_id:
                user_ids.add(left)
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
    user_token_provider: Callable[[EntraClawConfig], str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[AgentIdentitySponsor]:
    """Read user sponsors from the Agent Identity service principal in Graph.

    Two Graph hops happen here, and they need different scopes:

    * ``/servicePrincipals/{id}/microsoft.graph.agentIdentity/sponsors``
      requires app-only ``AgentIdentity.ReadWrite.All`` (Learning #43),
      which only the Agent Identity FIC token holds. ``token_provider``
      mints this token.

    * ``/users/{sponsor_id}`` enrichment requires ``User.Read.All`` to
      project the email-shaped fields (``userPrincipalName``, ``mail``,
      ``otherMails``, ``identities``) — Graph's nav-property collection
      at ``/sponsors`` returns only ``id`` regardless of ``$select``,
      so the enrichment is the only way to populate emails. The Agent
      Identity FIC token does NOT have ``User.Read.All``; passing
      ``user_token_provider=acquire_agent_user_token`` routes this hop
      through the Agent User's delegated token, which does. Without
      this, callers that match sponsors by email (``share_file``) see
      an empty allowlist (Learning #55, 2026-04-30 production bug).

    When ``user_token_provider`` is omitted (or None), both hops reuse
    the same Agent Identity token. This is fine for callers that match
    sponsors by ``user_id`` only (the wait-tool / supervisor gate),
    where empty email fields are harmless.
    """
    if not config.agent_object_id:
        raise ValueError("Agent Identity object id is not configured")

    token = token_provider(config)
    user_token = user_token_provider(config) if user_token_provider is not None else token
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
        raise GraphApiError(resp.status_code, resp.text or "failed to read Agent Identity sponsors")

    sponsors: list[AgentIdentitySponsor] = []
    with httpx.Client(**client_kwargs) as client:
        for item in resp.json().get("value", []):
            if not isinstance(item, dict):
                continue
            sponsor = AgentIdentitySponsor.from_graph_user(item)
            if sponsor is None:
                continue
            enriched = _fetch_sponsor_user_details(user_token, sponsor.user_id, client)
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
        raise TokenExpiredError("Agent Identity token expired while reading sponsor user details")
    return None


def _watched_chat_ids(data_dir: Path) -> list[str]:
    watched_file = data_dir / "watched_chats"
    if not watched_file.is_file():
        return []
    return [line.strip() for line in watched_file.read_text().splitlines() if line.strip()]


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
    return fetch_chat_members(
        config,
        chat_ids,
        token_provider=token_provider,
        transport=transport,
    )


def fetch_chat_members(
    config: EntraClawConfig,
    chat_ids: str | list[str],
    *,
    token_provider: Callable[[EntraClawConfig], str] = acquire_agent_user_token,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Return Graph chat members for one or more chat IDs.

    Each member dict has keys ``user_id``, ``name``, ``email``, ``roles``.

    Used by ``share_file`` to verify the requester is actually a member
    of the chat they claim is the active context (Learning #59 design).
    """
    if isinstance(chat_ids, str):
        chat_ids = [chat_ids]
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
                logger.warning("failed to read chat members for %s: %s", chat_id, exc)
                continue
            if resp.status_code == 401:
                raise TokenExpiredError(
                    "Agent User token expired while reading chat members"
                )
            if resp.status_code != 200:
                logger.warning(
                    "failed to read chat members for %s: HTTP %s %s",
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
    gate = SponsorGate.from_agent_identity_sponsors(fetch_agent_identity_sponsors(config))
    gate = gate.with_chat_members(fetch_watched_chat_members(config))
    agent_user_id = config.agent_user_id or ""
    if agent_user_id:
        gate = gate.with_watched_chat_ids(_watched_chat_ids(config.data_dir), agent_user_id)
    return gate
