#!/usr/bin/env python3
"""
revoke_consent.py
=================
Revoke (or pare-down) the oauth2PermissionGrant that lets the Agent Identity
act as the Agent User.

Inverse of ``grant_files_consent.py`` / ``create_entra_agent_ids.py``'s
``grant_agent_user_consent()``.

Usage::

    # Remove specific scopes
    python3 scripts/revoke_consent.py --scopes "Mail.Read,Files.ReadWrite"

    # Remove all scopes (delete the grant entirely)
    python3 scripts/revoke_consent.py --all

    # The grant on another resource (Azure Storage, or any API registered in the tenant)
    python3 scripts/revoke_consent.py --all --resource-app-id <resource-app-id>

The Agent Identity holds one grant per resource (Microsoft Graph, Azure Storage, and any
other API it was given). The script changes only the grant on one resource: Microsoft
Graph unless ``--resource-app-id`` names another, the same default as ``grant_consent.py``.

The script:
  1. Reads AGENT_OBJECT_ID + AGENT_USER_ID from ``.entrabot-state.json``.
  2. Finds the ``oauth2PermissionGrant`` on that resource via Graph v1.0.
  3a. If ``--scopes`` is partial: PATCHes the grant to remove those scopes.
  3b. If ``--all`` or remaining scopes are empty: DELETEs the grant.
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from entra_provisioning import (  # noqa: E402, I001
    ProvisionerBootstrapError,
    get_existing_graph_token,
    get_state,
)
from script_output import make_output_safe  # noqa: E402


GRAPH_V1 = "https://graph.microsoft.com/v1.0"
MS_GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"


def _print_usage() -> None:
    print(
        "usage: revoke_consent.py (--scopes 'Scope1,Scope2' | --all) [--resource-app-id ID]"
    )
    print("")
    print("Options:")
    print("  --scopes SCOPES        Comma- or space-separated scopes to remove")
    print("  --all                  Delete the entire Agent Identity -> Agent User grant")
    print("  --resource-app-id ID   The resource whose grant to change")
    print(f"                         (default: Microsoft Graph {MS_GRAPH_APP_ID})")
    print("  --help, -h             Show this help")


def _resolve_sp_object_id(token: str, app_id: str) -> str | None:
    """Return the object ID of the service principal for *app_id*, or None."""
    resp = requests.get(
        f"{GRAPH_V1}/servicePrincipals?$filter=appId eq '{app_id}'",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to look up the service principal: {resp.status_code} {resp.text}"
        )
    values = resp.json().get("value", [])
    return values[0]["id"] if values else None


def _find_consent_grant(
    token: str,
    agent_identity_object_id: str,
    agent_user_id: str,
    resource_sp_id: str,
) -> dict | None:
    """Find the oauth2PermissionGrant for agent identity → agent user on one resource."""
    url = (
        f"{GRAPH_V1}/oauth2PermissionGrants"
        f"?$filter=clientId eq '{agent_identity_object_id}'"
        f" and principalId eq '{agent_user_id}'"
    )
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to list consent grants: {resp.status_code} {resp.text}"
        )
    for grant in resp.json().get("value", []):
        if grant.get("resourceId") == resource_sp_id:
            return grant
    return None


def main(argv: list[str]) -> int:
    # ---- parse args ----
    make_output_safe()
    if any(arg in ("--help", "-h") for arg in argv[1:]):
        _print_usage()
        return 0

    scopes_to_remove: set[str] = set()
    revoke_all = False
    resource_app_id = MS_GRAPH_APP_ID

    i = 1
    while i < len(argv):
        if argv[i] == "--scopes" and i + 1 < len(argv):
            raw = argv[i + 1].replace(",", " ")
            scopes_to_remove = {s for s in raw.split() if s}
            i += 2
        elif argv[i] == "--all":
            revoke_all = True
            i += 1
        elif argv[i] == "--resource-app-id" and i + 1 < len(argv):
            resource_app_id = argv[i + 1].strip()
            i += 2
        else:
            i += 1

    if not scopes_to_remove and not revoke_all:
        _print_usage()
        print("ERROR: provide --scopes 'Scope1,Scope2' or --all", file=sys.stderr)
        return 2

    # ---- state ----
    agent_obj_id = get_state("AGENT_OBJECT_ID")
    agent_user_id = get_state("AGENT_USER_ID")
    if not agent_obj_id or not agent_user_id:
        print(
            "ERROR: AGENT_OBJECT_ID and/or AGENT_USER_ID missing from state.",
            file=sys.stderr,
        )
        return 1

    try:
        token = get_existing_graph_token()
    except ProvisionerBootstrapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # ---- find grant ----
    resource_sp_id = _resolve_sp_object_id(token, resource_app_id)
    if not resource_sp_id:
        print(f"ERROR: No service principal found for resource {resource_app_id}.")
        return 1
    grant = _find_consent_grant(token, agent_obj_id, agent_user_id, resource_sp_id)
    if not grant:
        print(
            "No consent grant found for this agent identity / agent user pair "
            f"on resource {resource_app_id}."
        )
        return 1

    grant_id = grant["id"]
    current_scopes = set(grant.get("scope", "").split())
    print(f"Current scopes: {' '.join(sorted(current_scopes))}")

    if revoke_all:
        scopes_to_remove = current_scopes.copy()

    # ---- calculate new scopes ----
    actually_removing = current_scopes & scopes_to_remove
    if not actually_removing:
        print("No matching scopes to revoke — not present in the grant.")
        return 0

    remaining = current_scopes - actually_removing
    print(f"Removing: {' '.join(sorted(actually_removing))}")

    if remaining:
        # PATCH to shrink
        print(f"Remaining: {' '.join(sorted(remaining))}")
        resp = requests.patch(
            f"{GRAPH_V1}/oauth2PermissionGrants/{grant_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"scope": " ".join(sorted(remaining))},
            timeout=15,
        )
        if resp.status_code not in (200, 204):
            print(f"PATCH failed: {resp.status_code} {resp.text}", file=sys.stderr)
            return 1
        print("✓ Grant updated (scopes removed).")
    else:
        # DELETE entire grant
        print("All scopes removed — deleting the grant entirely.")
        resp = requests.delete(
            f"{GRAPH_V1}/oauth2PermissionGrants/{grant_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if resp.status_code not in (200, 204):
            print(f"DELETE failed: {resp.status_code} {resp.text}", file=sys.stderr)
            return 1
        print("✓ Grant deleted.")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
