#!/usr/bin/env python3
"""Grant delegated consent (oauth2PermissionGrant) for the Agent Identity.

Creates or updates an oAuth2PermissionGrant so the Agent Identity can
acquire delegated tokens with the specified scopes as the Agent User.

This is the generalised CLI form of the consent logic embedded in
``create_entra_agent_ids.py``.  ``grant_files_consent.py`` is now a thin
backward-compat wrapper around this script.

Usage::

    # Grant specific scopes against Microsoft Graph
    python scripts/grant_consent.py --scopes "Chat.Create,Mail.Read"

    # Grant against a different resource (e.g. Azure Storage)
    python scripts/grant_consent.py \\
        --scopes "user_impersonation" \\
        --resource-app-id "e406a681-f3d4-42a8-90b6-c2b029497af1"
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime

import requests

# fmt: off
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from entra_provisioning import (  # noqa: E402
    ProvisionerBootstrapError,
    get_existing_graph_token,
    get_state,
)

# fmt: on


MS_GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_sp_object_id(token: str, app_id: str) -> str | None:
    """Resolve a well-known appId to its service-principal object ID."""
    url = (
        f"https://graph.microsoft.com/v1.0/servicePrincipals"
        f"?$filter=appId eq '{app_id}'"
    )
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if resp.status_code == 200:
        values = resp.json().get("value", [])
        if values:
            return values[0]["id"]
    return None


def _grant_consent(
    token: str,
    agent_identity_obj_id: str,
    agent_user_obj_id: str,
    resource_sp_id: str,
    scopes: set[str],
) -> int:
    """Create or update an oAuth2PermissionGrant. Returns exit code."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Check for existing grant
    check_url = (
        "https://graph.microsoft.com/v1.0/oauth2PermissionGrants"
        f"?$filter=clientId eq '{agent_identity_obj_id}'"
        f" and principalId eq '{agent_user_obj_id}'"
    )
    resp = requests.get(check_url, headers=headers, timeout=15)
    if resp.status_code == 200:
        existing = resp.json().get("value", [])
        # Filter to the right resource
        for grant in existing:
            if grant.get("resourceId") == resource_sp_id:
                existing_scopes = set((grant.get("scope") or "").split())
                missing = scopes - existing_scopes
                if not missing:
                    print("  [skip] All requested scopes already granted.")
                    return 0
                # PATCH to add missing scopes
                merged = sorted(existing_scopes | scopes)
                patch_url = (
                    f"https://graph.microsoft.com/v1.0"
                    f"/oauth2PermissionGrants/{grant['id']}"
                )
                patch_resp = requests.patch(
                    patch_url,
                    headers=headers,
                    json={"scope": " ".join(merged)},
                    timeout=15,
                )
                if patch_resp.status_code in (200, 204):
                    print(f"  [updated] Added scopes: {sorted(missing)}")
                    return 0
                print(
                    f"  ERROR: Failed to patch consent ({patch_resp.status_code})"
                )
                return 1

    # No existing grant — create new
    body = {
        "clientId": agent_identity_obj_id,
        "consentType": "Principal",
        "principalId": agent_user_obj_id,
        "resourceId": resource_sp_id,
        "scope": " ".join(sorted(scopes)),
        "startTime": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    for attempt in range(4):
        resp = requests.post(
            "https://graph.microsoft.com/v1.0/oauth2PermissionGrants",
            headers=headers,
            json=body,
            timeout=15,
        )
        if resp.status_code in (200, 201):
            print(f"  [new] Consent granted: {' '.join(sorted(scopes))}")
            return 0

        resp_text = resp.text
        is_propagation = (
            "Principal was not found" in resp_text
            or "does not exist" in resp_text
            or resp.status_code == 404
        )
        if is_propagation and attempt < 3:
            wait = 15 * (attempt + 1)
            print(f"  Not yet propagated — waiting {wait}s (attempt {attempt + 1}/4)...")
            time.sleep(wait)
            continue

        print(f"  ERROR: Consent grant failed ({resp.status_code})")
        print(f"  Response: {resp_text[:400]}")
        return 1

    return 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Grant delegated consent for the Agent Identity.",
    )
    parser.add_argument(
        "--scopes", required=True,
        help="Comma-separated list of scopes (e.g. 'Chat.Create,Mail.Read').",
    )
    parser.add_argument(
        "--resource-app-id", default=MS_GRAPH_APP_ID,
        help=(
            "App ID of the resource to consent against "
            f"(default: Microsoft Graph {MS_GRAPH_APP_ID})."
        ),
    )
    args = parser.parse_args(argv)

    try:
        token = get_existing_graph_token()
    except ProvisionerBootstrapError as exc:
        print(f"ERROR: {exc}")
        return 1

    # Resolve identities from state
    agent_oid = get_state("AGENT_OBJECT_ID")
    agent_user_id = get_state("AGENT_USER_ID")
    if not agent_oid or not agent_user_id:
        print("ERROR: AGENT_OBJECT_ID and/or AGENT_USER_ID not found in state.")
        print("  Run setup.sh first to provision the Agent Identity + User.")
        return 1

    # Resolve resource SP
    resource_sp_id = _resolve_sp_object_id(token, args.resource_app_id)
    if not resource_sp_id:
        print(f"ERROR: Could not resolve service principal for {args.resource_app_id}")
        return 1

    scopes = set(s.strip() for s in args.scopes.split(",") if s.strip())
    if not scopes:
        print("ERROR: No valid scopes provided.")
        return 1

    print(f"\n--- Granting consent for {len(scopes)} scope(s) ---\n")
    print(f"  Resource: {args.resource_app_id}")
    print(f"  Scopes:   {', '.join(sorted(scopes))}")
    print()

    return _grant_consent(token, agent_oid, agent_user_id, resource_sp_id, scopes)


if __name__ == "__main__":
    sys.exit(main())
