"""Diagnose why the sponsor email allowlist is empty.

Run from the project root with the venv active:

    .\\.venv\\Scripts\\python.exe scripts\\diagnose_sponsor_emails.py
    ./.venv/bin/python scripts/diagnose_sponsor_emails.py

Prints the raw Graph response from three calls so we can see exactly
which projection fields are missing and which token is unauthorized:

1. ``/users/{sponsor_id}`` with the Agent Identity FIC token
2. ``/users/{sponsor_id}`` with the Agent User token
3. ``/servicePrincipals/{agent_object_id}/microsoft.graph.agentIdentity/sponsors``
   with the Agent Identity FIC token

This is a no-op diagnostic: it does NOT mutate anything. Safe to run on
any tenant. Tokens are not printed.
"""

from __future__ import annotations

import sys

import httpx

from entraclaw.config import get_config
from entraclaw.identity.sponsors import fetch_agent_identity_sponsors
from entraclaw.tools.teams import acquire_agent_identity_token, acquire_agent_user_token

GRAPH = "https://graph.microsoft.com/v1.0"
SELECT = "$select=id,userPrincipalName,mail,otherMails,proxyAddresses,identities"


def main() -> int:
    cfg = get_config()
    if not cfg.agent_object_id:
        print("ERROR: agent_object_id is not configured in .env", file=sys.stderr)
        return 2

    print(f"agent_object_id: {cfg.agent_object_id}")
    print(f"agent_user_upn:  {cfg.agent_user_upn or '<unset>'}")
    print()

    print("Acquiring Agent Identity FIC token (Hop 2)...")
    ai_token = acquire_agent_identity_token(cfg)
    print("Acquiring Agent User token (Hop 3)...")
    au_token = acquire_agent_user_token(cfg)
    print()

    sponsors_url = (
        f"{GRAPH}/servicePrincipals/{cfg.agent_object_id}"
        "/microsoft.graph.agentIdentity/sponsors"
    )
    sponsors_url_with_select = f"{sponsors_url}?{SELECT}"

    print("=" * 78)
    print("[1] /sponsors WITHOUT $select (raw nav-collection projection)")
    print("=" * 78)
    r0 = httpx.get(sponsors_url, headers={"Authorization": f"Bearer {ai_token}"})
    print(f"status: {r0.status_code}")
    print(f"body:   {r0.text[:1500]}")
    print()

    print("=" * 78)
    print("[2] /sponsors WITH $select=id,userPrincipalName,mail,...")
    print("=" * 78)
    r1 = httpx.get(sponsors_url_with_select, headers={"Authorization": f"Bearer {ai_token}"})
    print(f"status: {r1.status_code}")
    print(f"body:   {r1.text[:1500]}")
    print()

    sponsor_ids: list[str] = []
    if r1.status_code == 200:
        for item in r1.json().get("value", []):
            if isinstance(item, dict) and item.get("id"):
                sponsor_ids.append(str(item["id"]))

    if not sponsor_ids:
        print("No sponsor ids parsed from /sponsors. Cannot continue with /users probes.")
        return 1

    print(f"Sponsor ids found: {sponsor_ids}")
    print()

    for sid in sponsor_ids:
        users_url = f"{GRAPH}/users/{sid}?{SELECT}"

        print("=" * 78)
        print(f"[3] /users/{sid} via Agent Identity FIC token")
        print("=" * 78)
        ru1 = httpx.get(users_url, headers={"Authorization": f"Bearer {ai_token}"})
        print(f"status: {ru1.status_code}")
        print(f"body:   {ru1.text[:1500]}")
        print()

        print("=" * 78)
        print(f"[4] /users/{sid} via Agent User token")
        print("=" * 78)
        ru2 = httpx.get(users_url, headers={"Authorization": f"Bearer {au_token}"})
        print(f"status: {ru2.status_code}")
        print(f"body:   {ru2.text[:1500]}")
        print()

        # Also try without $select in case Graph drops fields when $select
        # is applied to a guest / federated user.
        users_url_no_select = f"{GRAPH}/users/{sid}"

        print("=" * 78)
        print(f"[5] /users/{sid} via Agent User token (NO $select)")
        print("=" * 78)
        ru3 = httpx.get(users_url_no_select, headers={"Authorization": f"Bearer {au_token}"})
        print(f"status: {ru3.status_code}")
        print(f"body:   {ru3.text[:1500]}")
        print()

    print("=" * 78)
    print("[6] fetch_agent_identity_sponsors() result with user_token_provider")
    print("=" * 78)
    try:
        sponsors = fetch_agent_identity_sponsors(
            cfg, user_token_provider=acquire_agent_user_token
        )
        for s in sponsors:
            print(
                f"  user_id={s.user_id} upn={s.user_principal_name} mail={s.mail}"
                f" other_mails={s.other_mails} proxy={s.proxy_addresses}"
                f" federated={s.federated_emails}"
                f" -> identifiers={sorted(s.email_identifiers())}"
            )
    except Exception as exc:
        print(f"  ERROR: {type(exc).__name__}: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
