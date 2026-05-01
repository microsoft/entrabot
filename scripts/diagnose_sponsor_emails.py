"""Diagnose sponsor email allowlist gaps. Read-only.

Run from project root with venv active:
    .\\.venv\\Scripts\\python.exe scripts\\diagnose_sponsor_emails.py
    ./.venv/bin/python scripts/diagnose_sponsor_emails.py

Probes 8 things to pinpoint why the sponsor's email fields come back null:
1. /sponsors raw nav-collection projection
2. /sponsors with $select
3. /users/{sid} via Agent Identity FIC token
4. /users/{sid} via Agent User token (with $select)
5. /users/{sid} via Agent User token (no $select)
6. /users (search) with $filter=id eq '{sid}' via Agent User token
7. Agent User /me — what does the token see itself as
8. Decode the Agent User token to show its scopes (no signature verify)
"""

from __future__ import annotations

import base64
import json
import sys

import httpx

from entraclaw.config import get_config
from entraclaw.tools.teams import acquire_agent_identity_token, acquire_agent_user_token

GRAPH = "https://graph.microsoft.com/v1.0"
SELECT = "$select=id,userPrincipalName,mail,otherMails,proxyAddresses,identities"


def _b64url_decode(seg: str) -> bytes:
    pad = 4 - (len(seg) % 4)
    if pad < 4:
        seg = seg + "=" * pad
    return base64.urlsafe_b64decode(seg.encode("ascii"))


def _jwt_payload(tok: str) -> dict:
    parts = tok.split(".")
    if len(parts) < 2:
        return {"_error": "not a JWT"}
    try:
        return json.loads(_b64url_decode(parts[1]))
    except Exception as exc:  # noqa: BLE001
        return {"_error": f"decode failed: {exc}"}


def _show(label: str, status: int, body: str, *, max_len: int = 1500) -> None:
    print("=" * 78)
    print(label)
    print("=" * 78)
    print(f"status: {status}")
    print(f"body:   {body[:max_len]}")
    print()


def main() -> int:
    cfg = get_config()
    if not cfg.agent_object_id:
        print("ERROR: agent_object_id is not configured", file=sys.stderr)
        return 2

    print(f"agent_object_id: {cfg.agent_object_id}")
    print(f"agent_user_upn:  {cfg.agent_user_upn or '<unset>'}")
    print()

    print("Acquiring Agent Identity FIC token (Hop 2)...")
    ai = acquire_agent_identity_token(cfg)
    print("Acquiring Agent User token (Hop 3)...")
    au = acquire_agent_user_token(cfg)
    print()

    sponsors_url = (
        f"{GRAPH}/servicePrincipals/{cfg.agent_object_id}"
        "/microsoft.graph.agentIdentity/sponsors"
    )

    r0 = httpx.get(sponsors_url, headers={"Authorization": f"Bearer {ai}"})
    _show("[1] /sponsors WITHOUT $select", r0.status_code, r0.text)

    r1 = httpx.get(f"{sponsors_url}?{SELECT}", headers={"Authorization": f"Bearer {ai}"})
    _show("[2] /sponsors WITH $select", r1.status_code, r1.text)

    sponsor_ids: list[str] = []
    if r1.status_code == 200:
        for item in r1.json().get("value", []):
            if isinstance(item, dict) and item.get("id"):
                sponsor_ids.append(str(item["id"]))

    if not sponsor_ids:
        print("No sponsor ids found.")
        return 1

    sid = sponsor_ids[0]

    r2 = httpx.get(f"{GRAPH}/users/{sid}?{SELECT}", headers={"Authorization": f"Bearer {ai}"})
    _show(f"[3] /users/{sid} via AI token", r2.status_code, r2.text)

    r3 = httpx.get(f"{GRAPH}/users/{sid}?{SELECT}", headers={"Authorization": f"Bearer {au}"})
    _show(f"[4] /users/{sid} via AU token (with $select)", r3.status_code, r3.text)

    r4 = httpx.get(f"{GRAPH}/users/{sid}", headers={"Authorization": f"Bearer {au}"})
    _show(f"[5] /users/{sid} via AU token (no $select)", r4.status_code, r4.text)

    # /users with $filter often has different permission semantics than direct GET
    r5 = httpx.get(
        f"{GRAPH}/users?$filter=id eq '{sid}'&{SELECT}",
        headers={"Authorization": f"Bearer {au}"},
    )
    _show(f"[6] /users?$filter=id eq '{sid}' via AU token", r5.status_code, r5.text)

    r6 = httpx.get(f"{GRAPH}/me?{SELECT}", headers={"Authorization": f"Bearer {au}"})
    _show("[7] /me via AU token", r6.status_code, r6.text)

    print("=" * 78)
    print("[8] Agent User token JWT payload (scope/roles only)")
    print("=" * 78)
    payload = _jwt_payload(au)
    interesting = {
        k: payload.get(k)
        for k in (
            "aud",
            "iss",
            "appid",
            "tid",
            "oid",
            "upn",
            "scp",
            "roles",
            "idtyp",
            "amr",
        )
    }
    print(json.dumps(interesting, indent=2))
    print()

    print("=" * 78)
    print("[9] Agent Identity FIC token JWT payload (scope/roles only)")
    print("=" * 78)
    ai_payload = _jwt_payload(ai)
    ai_interesting = {
        k: ai_payload.get(k)
        for k in (
            "aud",
            "iss",
            "appid",
            "tid",
            "oid",
            "upn",
            "scp",
            "roles",
            "idtyp",
        )
    }
    print(json.dumps(ai_interesting, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
