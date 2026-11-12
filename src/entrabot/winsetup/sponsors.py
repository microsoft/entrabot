"""Who the agent answers to (its sponsors), and how they are written to ``.env``.

Port of ``scripts/setup.sh`` lines 644-734. A sponsor who is a B2B guest in the tenant needs
their home tenant ID so the agent can open a federated chat with them; it is looked up from
the guest's home domain through OpenID discovery.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from entrabot.winsetup.azcli import AmbiguousUser, AzCli, DirectoryUser

SPONSOR_KEYS = (
    "ENTRABOT_HUMAN_USER_ID",
    "ENTRABOT_HUMAN_UPN",
    "ENTRABOT_HUMAN_USER_IDS",
    "ENTRABOT_HUMAN_UPNS",
    "ENTRABOT_HUMAN_USER_TENANT_IDS",
    "ENTRABOT_HUMAN_USER_MAILS",
    "ENTRABOT_HUMAN_USER_TYPES",
)
# Any one of these in .env means "a sponsor is already named".
SPONSOR_NAMING_KEYS = SPONSOR_KEYS[:4]

# Every character RFC 5322 allows in an unquoted local part, apostrophe and "&" included:
# o'brien@contoso.com is a real address. None of them can change the meaning of a lookup,
# because the value is quoted for OData and percent-encoded for the URL where it is used.
_IDENTIFIER = re.compile(
    r"^([A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}"
    r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
_OBJECT_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_DOMAIN = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")

HttpGet = Callable[[str], dict[str, Any] | None]


class SponsorError(Exception):
    """A sponsor could not be resolved."""


@dataclass(frozen=True)
class Sponsor:
    object_id: str
    upn: str  # as the person typed it, which is what setup.sh records
    mail: str
    user_type: str  # "Member" or "Guest"
    home_tenant_id: str  # only for guests; "" otherwise


def validate_sponsor(value: str) -> str | None:
    if _IDENTIFIER.match(value.strip()):
        return None
    return "Enter a work email address (or an Entra object ID)."


def home_domain_from_guest_upn(upn: str) -> str:
    """``sam_fabrikam.com#EXT#@contoso.onmicrosoft.com`` -> ``fabrikam.com``."""
    if "#EXT#" not in upn:
        return ""
    local = upn.split("#EXT#", 1)[0]
    parts = local.rsplit("_", 1)
    return parts[1] if len(parts) > 1 else ""


def tenant_id_from_discovery(document: dict[str, Any] | None) -> str:
    """The tenant GUID from an OpenID configuration document's issuer."""
    issuer = str((document or {}).get("issuer") or "").rstrip("/")
    parts = issuer.split("/")
    if len(parts) <= 3:
        return ""
    # v2.0 issuers end in /<tenant>/v2.0; v1 issuers end in /<tenant>.
    return parts[-2] if parts[-1] == "v2.0" else parts[-1]


def http_get_json(url: str) -> dict[str, Any] | None:
    """GET a public JSON document. None on any failure: a guest still resolves without it."""
    try:
        response = httpx.get(url, timeout=10.0)
        if response.status_code != 200:
            return None
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def sponsor_from_user(user: DirectoryUser, *, entered: str, http_get: HttpGet) -> Sponsor:
    """A sponsor from a directory user. Type and home tenant come from the directory; the
    recorded address is what was typed, as ``setup.sh`` does (an object ID is replaced by the
    person's own address, so ``.env`` never names a sponsor by a bare GUID)."""
    user_type = user.user_type or ("Guest" if "#EXT#" in user.upn else "Member")
    home_tenant = ""
    if user_type == "Guest":
        domain = home_domain_from_guest_upn(user.upn)
        if domain and _DOMAIN.match(domain):
            home_tenant = tenant_id_from_discovery(
                http_get(
                    f"https://login.microsoftonline.com/{domain}/.well-known/openid-configuration"
                )
            )
    recorded = entered
    if _OBJECT_ID.match(entered):
        recorded = user.mail or user.upn or entered
    return Sponsor(
        object_id=user.object_id,
        upn=recorded,
        mail=user.mail,
        user_type=user_type,
        home_tenant_id=home_tenant,
    )


def _not_found(entered: str, tried: Sequence[str]) -> str:
    if _OBJECT_ID.match(entered):
        return (
            f"No user in this tenant has the object ID '{entered}'. Check it in the Microsoft "
            "Entra admin center (Users > the person > Object ID)."
        )
    looked = "; ".join(tried)
    return (
        f"Could not find '{entered}' in this tenant. Setup looked for a user by: {looked}. "
        "If the person is in the tenant, give their object ID instead (Microsoft Entra admin "
        "center > Users > the person > Object ID); that always works."
    )


def _ambiguous(error: AmbiguousUser) -> str:
    people = "; ".join(
        f"{user.display_name or '(no name)'} <{user.upn}>" for user in error.candidates
    )
    return (
        f"More than one user in this tenant matches '{error.identifier}' (by {error.how}): "
        f"{people}. Give the exact sign-in name of the one you mean, or their object ID."
    )


def resolve_sponsors(
    identifiers: Sequence[str], az: AzCli, *, http_get: HttpGet = http_get_json
) -> list[Sponsor]:
    """Look each sponsor up in the directory. Raises :class:`SponsorError` that says what was
    tried, or who the candidates are when the answer is not unique."""
    sponsors: list[Sponsor] = []
    for raw in identifiers:
        entered = raw.strip()
        if validate_sponsor(entered) is not None:
            raise SponsorError(f"'{entered}' is not an email address or an Entra object ID.")
        try:
            search = az.search_user(entered)
        except AmbiguousUser as exc:
            raise SponsorError(_ambiguous(exc)) from exc
        if search.user is None:
            raise SponsorError(_not_found(entered, search.tried))
        sponsors.append(sponsor_from_user(search.user, entered=entered, http_get=http_get))
    return sponsors


def sponsor_env(sponsors: Sequence[Sponsor]) -> dict[str, str]:
    """The seven sponsor lines. The lists are index-aligned; the first sponsor is primary."""
    if not sponsors:
        return {}

    def joined(values: list[str]) -> str:
        return ",".join(values)

    return {
        "ENTRABOT_HUMAN_USER_ID": sponsors[0].object_id,
        "ENTRABOT_HUMAN_UPN": sponsors[0].upn,
        "ENTRABOT_HUMAN_USER_IDS": joined([s.object_id for s in sponsors]),
        "ENTRABOT_HUMAN_UPNS": joined([s.upn for s in sponsors]),
        "ENTRABOT_HUMAN_USER_TENANT_IDS": joined([s.home_tenant_id for s in sponsors]),
        "ENTRABOT_HUMAN_USER_MAILS": joined([s.mail or s.upn for s in sponsors]),
        "ENTRABOT_HUMAN_USER_TYPES": joined([s.user_type for s in sponsors]),
    }
