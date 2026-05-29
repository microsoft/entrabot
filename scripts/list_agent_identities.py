#!/usr/bin/env python3
"""List Agent Identities under a Blueprint.

Queries the Microsoft Graph beta API for all Agent Identity service
principals, then filters to those belonging to the specified (or state-
stored) Blueprint.

Usage::

    # Use Blueprint from state
    python scripts/list_agent_identities.py

    # Explicit Blueprint
    python scripts/list_agent_identities.py --blueprint-app-id <APP_ID>
"""

from __future__ import annotations

import argparse
import sys

# fmt: off
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from entra_provisioning import (  # noqa: E402
    ProvisionerBootstrapError,
    get_existing_graph_token,
    get_state,
)

# fmt: on
from entrabot.graph_helpers import graph_collection_values  # noqa: E402

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="List Agent Identities under a Blueprint.",
    )
    parser.add_argument(
        "--blueprint-app-id", metavar="APP_ID",
        help="Blueprint App ID to filter on (default: from state).",
    )
    args = parser.parse_args(argv)

    try:
        token = get_existing_graph_token()
    except ProvisionerBootstrapError as exc:
        print(f"ERROR: {exc}")
        return 1

    blueprint_app_id = args.blueprint_app_id or get_state("BLUEPRINT_APP_ID")
    if not blueprint_app_id:
        print("ERROR: No Blueprint App ID available.")
        print("  Pass --blueprint-app-id or run setup.sh first.")
        return 1

    # Fetch all Agent Identity service principals
    path = (
        "/servicePrincipals/microsoft.graph.agentIdentity"
        "?$select=id,appId,displayName,agentIdentityBlueprintId"
        "&$top=999"
    )
    try:
        all_identities = graph_collection_values(
            path, token, action="List Agent Identities"
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1

    # Client-side filter by Blueprint (Graph doesn't support server-side filter here)
    filtered = [
        ai for ai in all_identities
        if ai.get("agentIdentityBlueprintId") == blueprint_app_id
    ]

    if not filtered:
        print(f"No Agent Identities found for Blueprint {blueprint_app_id}")
        return 0

    # Tabular output
    print(f"\nAgent Identities for Blueprint {blueprint_app_id}:\n")
    print(f"  {'Display Name':<35} {'App ID':<40} {'Object ID'}")
    print(f"  {'-' * 35} {'-' * 40} {'-' * 36}")
    for ai in filtered:
        print(
            f"  {ai.get('displayName', 'N/A'):<35}"
            f" {ai.get('appId', 'N/A'):<40}"
            f" {ai.get('id', 'N/A')}"
        )
    print(f"\n  Total: {len(filtered)} identity/ies\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
