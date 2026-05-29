#!/usr/bin/env python3
"""List sponsors for the Agent Identity.

Queries the Microsoft Graph beta API to show all sponsors assigned
to the configured (or specified) Agent Identity.

Usage::

    python scripts/list_sponsors.py
    python scripts/list_sponsors.py --agent-object-id OID
    python scripts/list_sponsors.py --json
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
from entrabot.graph_helpers import GRAPH_BETA, graph_request  # noqa: E402

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="List sponsors for the Agent Identity.",
    )
    parser.add_argument(
        "--agent-object-id", metavar="OID",
        help="Agent Identity Object ID (default: from state).",
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

    agent_oid = args.agent_object_id or get_state("AGENT_OBJECT_ID")
    if not agent_oid:
        print("ERROR: No Agent Object ID available.")
        print("  Pass --agent-object-id or run setup.sh first.")
        return 1

    path = (
        f"/servicePrincipals/{agent_oid}"
        "/microsoft.graph.agentIdentity/sponsors"
        "?$select=id,displayName,userPrincipalName,mail"
    )
    resp = graph_request("GET", path, token, base_url=GRAPH_BETA)
    if resp.status_code != 200:
        print(f"ERROR: Failed to list sponsors: {resp.status_code} {resp.text}")
        return 1

    sponsors = resp.json().get("value", [])

    if args.json:
        print(json.dumps(sponsors, indent=2))
        return 0

    if not sponsors:
        print(f"No sponsors found for Agent Identity {agent_oid}")
        return 0

    print(f"\nSponsors for Agent Identity {agent_oid}:\n")
    print(f"  {'Display Name':<30} {'UPN':<40} {'Mail':<35} {'Object ID'}")
    print(f"  {'-' * 30} {'-' * 40} {'-' * 35} {'-' * 36}")
    for sp in sponsors:
        print(
            f"  {sp.get('displayName', 'N/A'):<30}"
            f" {sp.get('userPrincipalName', 'N/A'):<40}"
            f" {sp.get('mail') or 'N/A':<35}"
            f" {sp.get('id', 'N/A')}"
        )
    print(f"\n  Total: {len(sponsors)} sponsor(s)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
