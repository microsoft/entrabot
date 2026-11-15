#!/usr/bin/env python3
"""Register this agent in the Microsoft Agent 365 registry (Agent Registry API).

Microsoft Learn ("Agent 365 observability concepts"): "Creating a Microsoft Entra identity
alone doesn't register the instance." Learn ("Create an agent identity blueprint", "Register
agents in the Agent 365 registry"): a Blueprint created through the Graph API, as Entrabot's
is, registers with an explicit call to the Agent Registry API. That is
``POST https://graph.microsoft.com/beta/copilot/agentRegistrations``, which needs
``AgentRegistration.ReadWrite.All`` (delegated or application).

Opt-in and separate from setup, because:

- The setup (Provisioner) app does not hold ``AgentRegistration.ReadWrite.All``. An
  administrator has to add and consent to it first; this script checks the token and stops
  with what to do when it is missing. It never adds a permission itself.
- The API is beta and documents create, get, update and delete only (no list). The
  registration's ID is kept in ``.entrabot-state.json`` (``AGENT_REGISTRATION_ID``) and is
  how a later run finds it, so a second registration is never created on the strength of a
  lookup that failed.
- Learn does not say whether ``agentIdentityId`` and ``agentIdentityBlueprintId`` take app
  IDs or object IDs. Its observability glossary uses "agent id" and "blueprint id" for app
  IDs, so this sends app IDs. Not verified against a tenant.

The Agent 365 CLI's ``a365 setup all`` also registers, but it creates its own Blueprint with
a client secret and an Azure App Service; do not run it against an Entrabot agent.

A dry run is the default: it prints the request and writes nothing. ``--apply`` writes.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from entra_provisioning import (  # noqa: E402
    ProvisionerBootstrapError,
    get_existing_graph_token,
    get_state,
    set_state,
)

REGISTRATIONS_URL = "https://graph.microsoft.com/beta/copilot/agentRegistrations"
REQUIRED_ROLE = "AgentRegistration.ReadWrite.All"
STATE_KEY = "AGENT_REGISTRATION_ID"
TIMEOUT_SECONDS = 20
DESCRIPTION = (
    "Entrabot device agent: an Entra Agent ID with its own Agent User, working in Teams, "
    "Outlook and Microsoft 365 files as itself."
)


def _request(method: str, url: str, **kwargs: Any) -> requests.Response:
    return requests.request(method, url, timeout=TIMEOUT_SECONDS, **kwargs)


def _roles(token: str) -> list[str]:
    """The ``roles`` claim, read without checking the signature (only to see what Microsoft
    put in it). The token never leaves this function."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return []
    roles = claims.get("roles") if isinstance(claims, dict) else None
    return [str(role) for role in roles] if isinstance(roles, list) else []


def build_registration(*, owner_ids: list[str], now: datetime) -> dict[str, Any]:
    """The agentRegistration body for this machine's agent, from the state file."""
    agent_id = (get_state("AGENT_ID") or "").strip()
    blueprint_app_id = (get_state("BLUEPRINT_APP_ID") or "").strip()
    upn = (get_state("AGENT_USER_UPN") or "").strip()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    name = upn.partition("@")[0] or "Entrabot agent"
    return {
        "displayName": name,
        "description": DESCRIPTION,
        "ownerIds": owner_ids,
        "createdBy": (get_state("PROVISIONER_CLIENT_ID") or "").strip(),
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


def register(token: str, body: dict[str, Any]) -> str:
    """Create or update the registration. Returns its ID. Raises RuntimeError with a reason
    that holds no token."""
    recorded = (get_state(STATE_KEY) or "").strip()
    if recorded:
        found = _request("GET", f"{REGISTRATIONS_URL}/{recorded}", headers=_headers(token))
        if found.status_code == 200:
            update = {
                key: value
                for key, value in body.items()
                if key not in ("sourceCreatedDateTime", "createdBy")
            }
            patched = _request(
                "PATCH", f"{REGISTRATIONS_URL}/{recorded}", headers=_headers(token), json=update
            )
            if patched.status_code not in (200, 204):
                raise RuntimeError(
                    f"updating registration {recorded} failed ({patched.status_code}): "
                    f"{patched.text[:300]}"
                )
            return recorded
        if found.status_code != 404:
            raise RuntimeError(
                f"could not check registration {recorded} ({found.status_code}); nothing was "
                "created. Try again later."
            )
        print(f"  Registration {recorded} is gone; creating a new one.")
    created = _request("POST", REGISTRATIONS_URL, headers=_headers(token), json=body)
    if created.status_code not in (200, 201):
        raise RuntimeError(
            f"creating the registration failed ({created.status_code}): {created.text[:300]}"
        )
    registration_id = str((created.json() or {}).get("id") or "")
    if not registration_id:
        raise RuntimeError("the registration was created but Graph returned no ID")
    set_state(STATE_KEY, registration_id)
    return registration_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="Write (default: dry run).")
    parser.add_argument(
        "--owner-id",
        action="append",
        default=[],
        help="Entra object ID of a user who owns the registration (repeatable). Default: "
        "ENTRABOT_HUMAN_USER_ID from .env.",
    )
    args = parser.parse_args(argv)

    owners = [item.strip() for item in args.owner_id if item.strip()]
    if not owners and os.environ.get("ENTRABOT_HUMAN_USER_ID", "").strip():
        owners = [os.environ["ENTRABOT_HUMAN_USER_ID"].strip()]
    if not owners:
        print(
            "ERROR: name an owner with --owner-id (a user's object ID). App-only registrations "
            "need user owners.",
            file=sys.stderr,
        )
        return 2
    if not (get_state("AGENT_ID") and get_state("BLUEPRINT_APP_ID")):
        print("ERROR: this machine has no agent recorded yet; run setup first.", file=sys.stderr)
        return 2

    body = build_registration(owner_ids=owners, now=datetime.now(UTC))
    if not args.apply:
        print("Dry run: this is the registration that --apply would write. Nothing was sent.")
        print(json.dumps(body, indent=2))
        return 0

    try:
        token = get_existing_graph_token()
    except ProvisionerBootstrapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if REQUIRED_ROLE not in _roles(token):
        print(
            f"ERROR: the setup (Provisioner) app does not hold {REQUIRED_ROLE}. An "
            "administrator must add that Microsoft Graph application permission to it and "
            "grant admin consent; nothing was sent.",
            file=sys.stderr,
        )
        return 1
    try:
        registration_id = register(token, body)
    except (RuntimeError, requests.RequestException) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Registered in the Agent 365 registry: {registration_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
