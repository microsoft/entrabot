#!/usr/bin/env python3
"""PATCH the Agent User's oauth2PermissionGrant to add the Files / Sites scopes.

Use when ``MissingPermissionError`` fires on a Files MCP tool call —
typically because the Agent User was provisioned before
``Files.Read.All`` / ``Sites.Read.All`` / ``Sites.ReadWrite.All`` were
added to ``grant_agent_user_consent``'s required-scope list.

Why this exists separately from ``create_entra_agent_ids.py``:
running the full provisioning script does a lot of unrelated work
(Blueprint create, Agent Identity create, Agent User create, license
assign). This wrapper invokes only ``grant_agent_user_consent``, which
is the one operation that actually fixes the missing-scope problem
idempotently — its PATCH path detects "consent exists, missing N
scopes" and adds only the missing ones.

No browser flow: the provisioner certificate (Keychain) authenticates
to Graph; the consent grant is a per-Principal write into
``oauth2PermissionGrants``. Architectural note: AgentIdentity service
principals are created via ``POST /servicePrincipals`` extending
``Microsoft.Graph.AgentIdentity`` and have no associated app
registration → no ``requiredResourceAccess`` manifest → admin-consent
URL flows do not apply here.

Usage:
    python scripts/grant_files_consent.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running from anywhere; resolve sibling modules under scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent))

from create_entra_agent_ids import grant_agent_user_consent
from entra_provisioning import ProvisionerBootstrapError, get_graph_token, get_state


def main() -> int:
    print("=" * 60)
    print("EntraClaw — PATCH Agent User Files / Sites consent")
    print("=" * 60)

    agent_obj_id = get_state("AGENT_OBJECT_ID")
    agent_user_id = get_state("AGENT_USER_ID")
    if not agent_obj_id or not agent_user_id:
        print("ERROR: AGENT_OBJECT_ID and AGENT_USER_ID must be present in")
        print("       .entraclaw-state.json. Run scripts/setup.sh first.")
        return 1

    print(f"  Agent Identity object ID: {agent_obj_id}")
    print(f"  Agent User object ID:     {agent_user_id}")
    print("")

    try:
        token = get_graph_token(wait_for_propagation=False)
    except ProvisionerBootstrapError as exc:
        print(f"ERROR: provisioner token: {exc}")
        return 2

    grant_agent_user_consent(token, agent_obj_id, agent_user_id)

    print("")
    print("Done. The next three-hop Agent User token mint will pick up the")
    print("updated scopes — restart entraclaw-mcp (e.g., via /mcp Reconnect)")
    print("so the new token is acquired with the patched grant.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
