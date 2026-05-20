#!/usr/bin/env python3
"""
remove_agent_sponsor.py
=======================
Remove a user from the sponsor list on the configured Agent Identity.

Inverse of ``add_agent_sponsor.py``.

Usage::

    python3 scripts/remove_agent_sponsor.py user@example.com
    python3 scripts/remove_agent_sponsor.py user@example.com --agent-object-id OID

The script:
  1. Reads the agent's object id from ``.entraclaw-state.json`` (or ``--agent-object-id``).
  2. Mints a Graph token via the dedicated provisioner cert.
  3. Resolves the email to a user object id.
  4. DELETEs
     ``/servicePrincipals/{agent}/microsoft.graph.agentIdentity/sponsors/{sponsor_id}/$ref``
  5. Prints the resulting sponsor list for verification.
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from entra_provisioning import (  # noqa: E402
    ProvisionerBootstrapError,
    get_existing_graph_token,
    get_state,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from entraclaw.graph_helpers import GRAPH_BETA as GRAPH_BASE  # noqa: E402
from entraclaw.graph_helpers import resolve_user_by_email  # noqa: E402


def _list_sponsors(token: str, agent_object_id: str) -> list[dict]:
    url = (
        f"{GRAPH_BASE}/servicePrincipals/{agent_object_id}"
        "/microsoft.graph.agentIdentity/sponsors"
        "?$select=id,displayName,userPrincipalName,mail"
    )
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to read sponsors: {resp.status_code} {resp.text}")
    return resp.json().get("value", [])


def _remove_sponsor(token: str, agent_object_id: str, sponsor_id: str) -> str:
    """DELETE the sponsor ref. Returns 'removed', 'not_found', or raises."""
    url = (
        f"{GRAPH_BASE}/servicePrincipals/{agent_object_id}"
        f"/microsoft.graph.agentIdentity/sponsors/{sponsor_id}/$ref"
    )
    resp = requests.delete(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if resp.status_code in (204, 200):
        return "removed"
    if resp.status_code == 404:
        return "not_found"
    raise RuntimeError(f"Failed to remove sponsor: {resp.status_code} {resp.text}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        print("\nERROR: email argument is required.", file=sys.stderr)
        return 2

    email = argv[1].strip()

    # Parse optional --agent-object-id
    agent_object_id = None
    i = 2
    while i < len(argv):
        if argv[i] == "--agent-object-id" and i + 1 < len(argv):
            agent_object_id = argv[i + 1]
            i += 2
        else:
            i += 1

    if not agent_object_id:
        agent_object_id = get_state("AGENT_OBJECT_ID")

    if not agent_object_id:
        print(
            "ERROR: AGENT_OBJECT_ID missing from .entraclaw-state.json. "
            "Run scripts/create_entra_agent_ids.py first, or pass --agent-object-id.",
            file=sys.stderr,
        )
        return 1

    print(f"Agent object id: {agent_object_id}")
    print(f"Removing sponsor: {email}")
    print("")

    try:
        token = get_existing_graph_token()
    except ProvisionerBootstrapError as exc:
        print(f"ERROR: provisioner bootstrap failed: {exc}", file=sys.stderr)
        return 1

    print("Resolving user object id...")
    try:
        user_id, display_name = resolve_user_by_email(token, email)
    except LookupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"  Resolved: {display_name} ({user_id})")
    print("")

    print("Current sponsors:")
    for sp in _list_sponsors(token, agent_object_id):
        print(
            f"  - {sp.get('displayName')} ({sp.get('id')}) "
            f"upn={sp.get('userPrincipalName')!r} "
            f"mail={sp.get('mail')!r}"
        )
    print("")

    print(f"Removing {display_name} ({user_id}) from sponsors...")
    try:
        result = _remove_sponsor(token, agent_object_id, user_id)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if result == "not_found":
        print("  (not a sponsor — already removed or never added)")
    else:
        print("  done")
    print("")

    print("Sponsors after update:")
    for sp in _list_sponsors(token, agent_object_id):
        print(
            f"  - {sp.get('displayName')} ({sp.get('id')}) "
            f"upn={sp.get('userPrincipalName')!r} "
            f"mail={sp.get('mail')!r}"
        )
    print("")
    print("Restart the entraclaw MCP server so the sponsor gate is reloaded:")
    print("  killall -TERM Python 2>/dev/null; copilot")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
