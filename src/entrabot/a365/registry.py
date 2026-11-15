"""The agent's entry in the Microsoft Agent 365 registry (Agent Registry API, Graph beta).

Microsoft Learn ("Agent 365 observability concepts"): "Creating a Microsoft Entra identity
alone doesn't register the instance." A Blueprint created through the Graph API, as
Entrabot's is, registers with an explicit call:
``POST https://graph.microsoft.com/beta/copilot/agentRegistrations``, which needs
``AgentRegistration.ReadWrite.All``. Microsoft lists registered agents in the Microsoft 365
admin center (Agents > All agents > Registry).

The API is beta and documents create, get, update and delete only: there is no list call, so
nothing can be looked up by agent. The registration's ID is kept in the state file
(``AGENT_REGISTRATION_ID``) and is the only way a later run finds it; a lookup that fails is
never taken to mean "there is none".

This module is the shared logic: the request body, and the four calls, each given a token and
a ``request(method, url, **kwargs)`` function (``requests.request`` in
``scripts/register_agent_instance.py``). It never gets a token, never reads ``.env`` into the
process, and never prints one. Setup runs the script in a child process
(:mod:`entrabot.winsetup.agentregistry`), as it runs every Graph call made with the setup
(Provisioner) app's token.

Not yet checked against a tenant: whether ``agentIdentityId`` and ``agentIdentityBlueprintId``
take application IDs or object IDs (this sends application IDs, as Learn's observability
glossary uses "agent id" and "blueprint id"), and which display name the admin center shows.
:func:`build_registration` is the one place to change either.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal, Protocol

REGISTRATIONS_URL = "https://graph.microsoft.com/beta/copilot/agentRegistrations"
REQUIRED_ROLE = "AgentRegistration.ReadWrite.All"
STATE_KEY = "AGENT_REGISTRATION_ID"
ADMIN_CENTER_PLACE = "Microsoft 365 admin center > Agents > All agents > Registry"
FALLBACK_NAME = "Entrabot agent"
DESCRIPTION = (
    "Entrabot device agent: an Entra Agent ID with its own Agent User, working in Teams, "
    "Outlook and Microsoft 365 files as itself."
)
# Set by Microsoft or by the first write, so never part of an update.
_NOT_UPDATABLE = ("sourceCreatedDateTime", "createdBy")
_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,199}$")
_REASON_LIMIT = 300

Outcome = Literal["refused", "could-not-check", "not-ours", "no-id", "bad-id"]


class ResponseLike(Protocol):
    status_code: int
    text: str

    def json(self) -> Any: ...


Request = Callable[..., ResponseLike]


class RegistryError(RuntimeError):
    """A registry call did not do what was asked. ``str(error)`` says why and holds no token;
    :attr:`outcome` says what kind of failure it was:

    * ``refused``: Graph answered the write with an error; nothing was written.
    * ``could-not-check``: the recorded registration could not be read, so nothing was created.
    * ``not-ours``: the recorded registration names another agent; it was left alone.
    * ``no-id``: Graph created it but returned no ID (it may exist without being recorded).
    * ``bad-id``: the recorded ID is not one Graph could have given.
    """

    def __init__(self, message: str, *, outcome: Outcome) -> None:
        super().__init__(message)
        self.outcome = outcome


def token_roles(token: str) -> list[str]:
    """The ``roles`` claim of an app-only token, read without checking the signature (only to
    see what Microsoft put in it). The token never leaves this function."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return []
    roles = claims.get("roles") if isinstance(claims, dict) else None
    return [str(role) for role in roles] if isinstance(roles, list) else []


def display_name(*, preferred: str, upn: str) -> str:
    """The name the registry shows: a human-readable one when there is one (``.env``'s
    ``ENTRABOT_AGENT_DISPLAY_NAME``), else the part of the agent's address before the @."""
    return preferred.strip() or upn.strip().partition("@")[0] or FALLBACK_NAME


def owner_ids(env_values: Mapping[str, str]) -> list[str]:
    """Who owns the registration: the agent's sponsors as ``.env`` records them. Members of
    the tenant first; guests only when no sponsor is a member. App-only registrations need
    user owners (Microsoft Learn, bulk onboarding)."""
    ids = [item.strip() for item in env_values.get("ENTRABOT_HUMAN_USER_IDS", "").split(",")]
    if not any(ids):
        ids = [env_values.get("ENTRABOT_HUMAN_USER_ID", "").strip()]
    types = [
        item.strip().lower() for item in env_values.get("ENTRABOT_HUMAN_USER_TYPES", "").split(",")
    ]
    members = [
        user for index, user in enumerate(ids)
        if user and (types[index] if index < len(types) else "") != "guest"
    ]  # fmt: skip
    chosen = members or [user for user in ids if user]
    return list(dict.fromkeys(chosen))


def build_registration(
    *,
    agent_id: str,
    blueprint_app_id: str,
    name: str,
    owner_ids: list[str],
    created_by: str,
    now: datetime,
) -> dict[str, Any]:
    """The agentRegistration request body. The one place its fields are decided."""
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "displayName": name,
        "description": DESCRIPTION,
        "ownerIds": owner_ids,
        "createdBy": created_by,
        "sourceCreatedDateTime": stamp,
        "sourceLastModifiedDateTime": stamp,
        "sourceAgentId": agent_id,
        "originatingStore": "Entrabot",
        "agentIdentityId": agent_id,
        "agentIdentityBlueprintId": blueprint_app_id,
        "agentCard": {
            "name": name,
            "description": DESCRIPTION,
            "provider": "Entrabot (open source)",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text"],
            "defaultOutputModes": ["text"],
        },
    }


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _url(registration_id: str) -> str:
    if not _ID.match(registration_id) or ".." in registration_id:
        raise RegistryError(
            f"the recorded registration ID {registration_id[:60]!r} is not one Graph gives",
            outcome="bad-id",
        )
    return f"{REGISTRATIONS_URL}/{registration_id}"


def _said(response: ResponseLike) -> str:
    return f"HTTP {response.status_code}: {(response.text or '').strip()[:_REASON_LIMIT]}"


def _json(response: ResponseLike) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def register(
    token: str, body: dict[str, Any], *, recorded_id: str, request: Request
) -> tuple[str, Literal["created", "updated", "recreated"]]:
    """Create the registration, or update the one recorded. Returns its ID and what was done.
    Raises :class:`RegistryError` otherwise; a recorded registration that cannot be read is
    never replaced by a new one."""
    how: Literal["created", "recreated"] = "created"
    if recorded_id:
        url = _url(recorded_id)
        found = request("GET", url, headers=_headers(token))
        if found.status_code == 200:
            theirs = str(_json(found).get("agentIdentityId") or "").strip().lower()
            ours = str(body.get("agentIdentityId") or "").strip().lower()
            if theirs and ours and theirs != ours:
                raise RegistryError(
                    f"registration {recorded_id} names another agent; it was left alone",
                    outcome="not-ours",
                )
            update = {key: value for key, value in body.items() if key not in _NOT_UPDATABLE}
            patched = request("PATCH", url, headers=_headers(token), json=update)
            if patched.status_code not in (200, 204):
                raise RegistryError(
                    f"updating registration {recorded_id} was refused ({_said(patched)})",
                    outcome="refused",
                )
            return recorded_id, "updated"
        if found.status_code != 404:
            raise RegistryError(
                f"registration {recorded_id} could not be read ({_said(found)}); nothing was "
                "created",
                outcome="could-not-check",
            )
        how = "recreated"
    created = request("POST", REGISTRATIONS_URL, headers=_headers(token), json=body)
    if created.status_code not in (200, 201):
        raise RegistryError(f"the registration was refused ({_said(created)})", outcome="refused")
    registration_id = str(_json(created).get("id") or "").strip()
    if not registration_id:
        raise RegistryError("Graph created the registration but returned no ID", outcome="no-id")
    return registration_id, how


def look_up(token: str, registration_id: str, *, request: Request) -> Literal["present", "gone"]:
    """Does Graph still have the registration? Raises when it cannot say."""
    found = request("GET", _url(registration_id), headers=_headers(token))
    if found.status_code == 200:
        return "present"
    if found.status_code == 404:
        return "gone"
    raise RegistryError(
        f"registration {registration_id} could not be read ({_said(found)})",
        outcome="could-not-check",
    )


def delete(token: str, registration_id: str, *, request: Request) -> Literal["deleted", "gone"]:
    """Delete the registration. One that is already gone counts as deleted ("gone")."""
    answer = request("DELETE", _url(registration_id), headers=_headers(token))
    if answer.status_code in (200, 204):
        return "deleted"
    if answer.status_code == 404:
        return "gone"
    raise RegistryError(
        f"deleting registration {registration_id} was refused ({_said(answer)})",
        outcome="refused",
    )
