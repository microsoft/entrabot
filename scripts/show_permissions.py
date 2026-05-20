#!/usr/bin/env python3
"""Show delegated permission grants for the Agent Identity.

Queries Microsoft Graph for ``oauth2PermissionGrants`` scoped to the
Agent Identity's service principal and Agent User.

Usage::

    python scripts/show_permissions.py
    python scripts/show_permissions.py --json
"""

from __future__ import annotations

import argparse
import json
import sys

# fmt: off
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from entra_provisioning import (  # noqa: E402
    ProvisionerBootstrapError,
    get_existing_graph_token,
    get_state,
)

# fmt: on
from entraclaw.graph_helpers import GRAPH_V1, graph_request  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_resource_names(
    resource_ids: set[str], token: str,
) -> dict[str, str]:
    """Map service-principal IDs to display names (best-effort)."""
    names: dict[str, str] = {}
    for rid in resource_ids:
        resp = graph_request(
            "GET", f"/servicePrincipals/{rid}",
            token, base_url=GRAPH_V1,
        )
        if resp.status_code == 200:
            names[rid] = resp.json().get("displayName", rid)
    return names


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show delegated permission grants.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output machine-readable JSON.",
    )
    args = parser.parse_args(argv)

    try:
        token = get_existing_graph_token()
    except ProvisionerBootstrapError as exc:
        print(f"ERROR: {exc}")
        return 1

    agent_oid = get_state("AGENT_OBJECT_ID")
    agent_user_id = get_state("AGENT_USER_ID")

    if not agent_oid or not agent_user_id:
        print("ERROR: Missing AGENT_OBJECT_ID or AGENT_USER_ID in state.")
        print("  Run setup.sh first to provision the Agent Identity.")
        return 1

    path = (
        "/oauth2PermissionGrants"
        f"?$filter=clientId eq '{agent_oid}'"
        f" and principalId eq '{agent_user_id}'"
    )
    resp = graph_request("GET", path, token, base_url=GRAPH_V1)
    if resp.status_code != 200:
        print(f"ERROR: {resp.status_code} {resp.text}")
        return 1

    grants = resp.json().get("value", [])

    # Resolve resource service-principal IDs → display names
    resource_ids = {g.get("resourceId") for g in grants if g.get("resourceId")}
    names = _resolve_resource_names(resource_ids, token) if resource_ids else {}

    if args.json:
        out = []
        for g in grants:
            rid = g.get("resourceId", "")
            entry: dict[str, object] = {
                "id": g.get("id"),
                "resourceId": rid,
                "scopes": g.get("scope", "").split(),
                "consentType": g.get("consentType"),
            }
            if rid in names:
                entry["resourceName"] = names[rid]
            out.append(entry)
        print(json.dumps(out, indent=2))
        return 0

    if not grants:
        print("No permission grants found for this Agent Identity.")
        return 0

    print(f"\nPermission grants for Agent Identity {agent_oid}:\n")
    for g in grants:
        scopes = g.get("scope", "").strip()
        rid = g.get("resourceId", "N/A")
        consent = g.get("consentType", "N/A")
        name = names.get(rid)
        if name:
            print(f"  Resource:     {name} ({rid})")
        else:
            print(f"  Resource ID:  {rid}")
        print(f"  Consent:      {consent}")
        print(f"  Scopes:       {scopes}")
        print()

    print(f"  Total: {len(grants)} grant(s)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
