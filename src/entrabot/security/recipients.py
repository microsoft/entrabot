"""Server-side recipient policy for every tool that reaches someone new.

The model decides whom to write to; this decides whether it may. It runs in the server,
before Microsoft Graph is called, so no prompt, persona, injected message or tool argument
can widen it.

Allowed by default:

- the agent's sponsors, as the directory lists them on its Agent Identity (a cross-tenant
  sponsor included: sponsor chats across tenants are a shipped feature);
- the members of the chat the request came from (the chat of the last message pushed to the
  model, while it is recent);
- anyone in the agent's home tenant: an address in one of its domains, or one whose domain
  Microsoft's sign-in discovery resolves to the home tenant ID.

Anyone else needs an entry in ``ENTRABOT_OUTBOUND_ALLOW``: a comma-separated list of exact
addresses, or domains (``fabrikam.com`` or ``@fabrikam.com``; a domain entry covers that
domain only, not its subdomains). Anything that cannot be checked counts as external.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

ALLOWLIST_ENV = "ENTRABOT_OUTBOUND_ALLOW"
HOME_DOMAINS_ENV = "ENTRABOT_HOME_DOMAINS"

# One local part, one domain, no spaces: enough to refuse what is not an address.
_ADDRESS = re.compile(r"^[^@\s]+@([A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+)$")


@dataclass(frozen=True)
class RecipientPolicy:
    """What configuration says: the home tenant and the explicit allowlist."""

    home_tenant_id: str
    home_domains: frozenset[str]
    allow_addresses: frozenset[str] = field(default_factory=frozenset)
    allow_domains: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class RecipientContext:
    """What the server knows about this request. Addresses are lowercased.
    ``tenant_of_domain`` maps a domain to the tenant ID sign-in discovery returned for it
    (None: it returned none, or failed)."""

    sponsors: frozenset[str] = field(default_factory=frozenset)
    chat_members: frozenset[str] = field(default_factory=frozenset)
    tenant_of_domain: Mapping[str, str | None] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    address: str
    allowed: bool
    reason: str


def parse_allowlist(raw: str | None) -> tuple[frozenset[str], frozenset[str]]:
    """``(addresses, domains)`` from ``ENTRABOT_OUTBOUND_ALLOW``."""
    addresses: set[str] = set()
    domains: set[str] = set()
    for item in (raw or "").split(","):
        entry = item.strip().lower()
        if not entry:
            continue
        if entry.startswith("@"):
            domains.add(entry[1:])
        elif "@" in entry:
            addresses.add(entry)
        else:
            domains.add(entry)
    return frozenset(addresses), frozenset(domains)


def domain_of(address: str) -> str | None:
    """The domain of a well-formed address, lowercased, or None."""
    match = _ADDRESS.match(address.strip())
    return match.group(1).lower() if match else None


def decide(address: str, policy: RecipientPolicy, context: RecipientContext) -> Decision:
    """May the agent reach *address*?"""
    normalized = address.strip().lower()
    domain = domain_of(normalized)
    if domain is None:
        return Decision(address, False, "not an email address")
    if normalized in context.sponsors:
        return Decision(address, True, "sponsor")
    if normalized in context.chat_members:
        return Decision(address, True, "member of the requesting chat")
    if domain in policy.home_domains:
        return Decision(address, True, "home tenant")
    resolved = context.tenant_of_domain.get(domain)
    if resolved and policy.home_tenant_id and resolved.lower() == policy.home_tenant_id.lower():
        return Decision(address, True, "home tenant")
    if normalized in policy.allow_addresses or domain in policy.allow_domains:
        return Decision(address, True, "allowlisted")
    return Decision(
        address,
        False,
        f"outside the agent's tenant and not a sponsor or a member of the requesting chat; "
        f"an administrator can allow it in {ALLOWLIST_ENV}",
    )
