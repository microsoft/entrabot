#!/usr/bin/env python3
"""
add_agent_sponsor.py
====================
Add a user as a sponsor on the configured Agent Identity.

This is the surgical fix for the SponsorGate-rejects-B2B-guest bug:
when the existing sponsor list does not include the agent operator's
home-tenant identity, ``wait_for_sponsor_dm`` will silently reject
their inbound chat messages.  Adding the operator's resolved guest
user as a sponsor causes Graph to populate ``mail`` correctly and the
gate to start matching.

Usage::

    python3 scripts/add_agent_sponsor.py user@example.com

The script:
  1. Reads the agent's object id from ``.entraclaw-state.json``.
  2. Mints a Graph token via the dedicated provisioner cert (NEVER az
     CLI tokens — they are rejected by Agent Identity APIs).
  3. Resolves the email to a user object id in the agent's home tenant
     (works for both home-tenant users and B2B guests by mail / UPN /
     proxyAddresses).
  4. POSTs to
     ``/servicePrincipals/{agent}/microsoft.graph.agentIdentity/sponsors/$ref``
     to add the user as an additional sponsor (does not replace).
  5. Prints the resulting sponsor list for verification.
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from entra_provisioning import (  # noqa: E402
    ProvisionerBootstrapError,
    get_graph_token,
    get_state,
)

GRAPH_BASE = "https://graph.microsoft.com/beta"


def _resolve_user_id(token: str, email: str) -> tuple[str, str]:
    """Return (object_id, displayName) for *email* in the agent's tenant.

    Tries, in order: ``userPrincipalName eq``, ``mail eq``, then a
    ``proxyAddresses`` containment check.  The last is necessary for
    B2B guests whose UPN is the EXT-mangled form but whose ``mail``
    sometimes lives in proxyAddresses as ``smtp:<email>`` instead of
    on the top-level field.
    """
    headers = {"Authorization": f"Bearer {token}"}
    select = "id,displayName,userPrincipalName,mail,proxyAddresses"
    quoted = email.replace("'", "''")

    for filt in (
        f"userPrincipalName eq '{quoted}'",
        f"mail eq '{quoted}'",
        f"otherMails/any(m: m eq '{quoted}')",
        f"proxyAddresses/any(p: p eq 'smtp:{quoted}')",
        f"proxyAddresses/any(p: p eq 'SMTP:{quoted}')",
    ):
        url = f"{GRAPH_BASE}/users?$filter={filt}&$select={select}"
        resp = requests.get(url, headers=headers, params={"$count": "true"}, timeout=15)
        # ConsistencyLevel is required for some advanced queries
        if resp.status_code == 400:
            resp = requests.get(
                url,
                headers={**headers, "ConsistencyLevel": "eventual"},
                timeout=15,
            )
        if resp.status_code != 200:
            continue
        results = resp.json().get("value", [])
        if results:
            user = results[0]
            return user["id"], user.get("displayName") or email
    raise SystemExit(
        f"Could not resolve {email} to a user object in the agent's tenant. "
        "Is this person a guest in the tenant? Try inviting them via "
        "az ad user invite first."
    )


def _list_sponsors(token: str, agent_object_id: str) -> list[dict]:
    url = (
        f"{GRAPH_BASE}/servicePrincipals/{agent_object_id}"
        "/microsoft.graph.agentIdentity/sponsors"
        "?$select=id,displayName,userPrincipalName,mail"
    )
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    if resp.status_code != 200:
        raise SystemExit(f"Failed to read sponsors: {resp.status_code} {resp.text}")
    return resp.json().get("value", [])


def _add_sponsor(token: str, agent_object_id: str, user_id: str) -> None:
    url = (
        f"{GRAPH_BASE}/servicePrincipals/{agent_object_id}"
        "/microsoft.graph.agentIdentity/sponsors/$ref"
    )
    body = {"@odata.id": f"{GRAPH_BASE}/users/{user_id}"}
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=15,
    )
    if resp.status_code in (204, 200, 201):
        return
    if resp.status_code == 400 and "already exist" in resp.text.lower():
        print("  (already a sponsor — no change)")
        return
    raise SystemExit(f"Failed to add sponsor: {resp.status_code} {resp.text}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        print("\nERROR: exactly one email argument is required.", file=sys.stderr)
        return 2
    email = argv[1].strip()

    agent_object_id = get_state("AGENT_OBJECT_ID")
    if not agent_object_id:
        print(
            "ERROR: AGENT_OBJECT_ID missing from .entraclaw-state.json. "
            "Run scripts/create_entra_agent_ids.py first.",
            file=sys.stderr,
        )
        return 1

    print(f"Agent object id: {agent_object_id}")
    print(f"Adding sponsor:  {email}")
    print("")

    try:
        token = get_graph_token()
    except ProvisionerBootstrapError as exc:
        print(f"ERROR: provisioner bootstrap failed: {exc}", file=sys.stderr)
        return 1

    print("Resolving user object id...")
    user_id, display_name = _resolve_user_id(token, email)
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

    print(f"Adding {display_name} ({user_id}) as sponsor...")
    _add_sponsor(token, agent_object_id, user_id)
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
    print("Restart the entraclaw MCP server so the sponsor gate is reloaded with the new sponsor:")
    print("  killall -TERM Python 2>/dev/null; copilot")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
