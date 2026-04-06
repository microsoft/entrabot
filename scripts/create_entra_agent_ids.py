#!/usr/bin/env python3
"""
create_entra_agent_ids.py
=========================
Creates an Agent Identity Blueprint and a per-device Agent Identity in
Microsoft Entra ID via the Graph beta API.  Stores the resulting IDs in
the local provision state file (.openclaw-state.json).

Uses the dedicated provisioner app from entra_provisioning.py — never
Azure CLI tokens (which include Directory.AccessAsUser.All and get
rejected by Agent Identity APIs).

Usage:
    python3 scripts/create_entra_agent_ids.py

Prerequisites:
    - az login has been run
    - pip install azure-identity requests
"""

import platform
import socket
import sys
import time

import requests

# entra_provisioning.py lives in the same directory
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from entra_provisioning import (
    ProvisionerBootstrapError,
    build_sponsors_bind,
    get_graph_token,
    get_signed_in_user_id,
    get_state,
    set_state,
)

GRAPH_BASE = "https://graph.microsoft.com/beta"

BLUEPRINT_DISPLAY_NAME = "Openclaw Code Agent"


def odata_escape(value: str) -> str:
    """Escape single quotes for OData filter strings."""
    return value.replace("'", "''")


def graph_request(
    method: str,
    path: str,
    token: str,
    json_body: dict | None = None,
    retry: bool = True,
) -> requests.Response:
    """Make a request to the Microsoft Graph beta API."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    url = f"{GRAPH_BASE}{path}"
    resp = requests.request(method, url, headers=headers, json=json_body)

    # Retry once on 429 (throttling) or 5xx
    if retry and resp.status_code in (429, 500, 502, 503, 504):
        wait = int(resp.headers.get("Retry-After", "10"))
        print(f"  Graph API returned {resp.status_code}, retrying in {wait}s...")
        time.sleep(wait)
        resp = requests.request(method, url, headers=headers, json=json_body)

    return resp


# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------


def find_existing_blueprint(token: str) -> dict | None:
    """Find an existing Blueprint by stored IDs, then by display name."""
    # Try stored object ID first
    stored_obj_id = get_state("BLUEPRINT_OBJECT_ID")
    if stored_obj_id:
        resp = graph_request("GET", f"/applications/{stored_obj_id}", token, retry=False)
        if resp.status_code == 200:
            return resp.json()
        print(f"  [warn] Stored BLUEPRINT_OBJECT_ID not found: {stored_obj_id}")

    # Try stored app ID
    stored_app_id = get_state("BLUEPRINT_APP_ID")
    if stored_app_id:
        resp = graph_request(
            "GET",
            f"/applications?$filter=appId eq '{odata_escape(stored_app_id)}'",
            token,
        )
        if resp.status_code == 200:
            values = resp.json().get("value", [])
            if values:
                return values[0]
        print(f"  [warn] Stored BLUEPRINT_APP_ID not found: {stored_app_id}")

    # Fall back to display name search
    resp = graph_request(
        "GET",
        f"/applications?$filter=displayName eq '{odata_escape(BLUEPRINT_DISPLAY_NAME)}'",
        token,
    )
    if resp.status_code != 200:
        return None

    for app in resp.json().get("value", []):
        if app.get("displayName") == BLUEPRINT_DISPLAY_NAME:
            return app
    return None


def ensure_blueprint_principal(token: str, app_id: str) -> None:
    """Ensure the BlueprintPrincipal (SP) exists — it is NOT auto-created."""
    resp = graph_request(
        "GET",
        f"/servicePrincipals?$filter=appId eq '{app_id}'",
        token,
    )
    if resp.status_code == 200:
        sps = resp.json().get("value", [])
        if sps:
            print(f"  Blueprint SP already exists: {sps[0].get('id')}")
            return

    print("  Creating BlueprintPrincipal...")
    sp_body = {
        "@odata.type": "Microsoft.Graph.AgentIdentityBlueprintPrincipal",
        "appId": app_id,
    }
    for attempt in range(4):
        sp_resp = graph_request("POST", "/servicePrincipals", token, json_body=sp_body)
        if sp_resp.status_code in (200, 201):
            sp_data = sp_resp.json()
            print(f"  BlueprintPrincipal created: {sp_data.get('id', 'unknown')}")
            return
        if attempt < 3:
            wait = (attempt + 1) * 10
            print(
                f"  SP creation returned {sp_resp.status_code}, "
                f"retrying in {wait}s (app may still be propagating)..."
            )
            time.sleep(wait)
    print(f"  WARNING: Failed to create BlueprintPrincipal after retries: {sp_resp.status_code}")
    print(f"  Response: {sp_resp.text[:300]}")


def create_blueprint(token: str) -> tuple[str, str]:
    """Create or find the Agent Identity Blueprint. Returns (app_id, object_id)."""
    print("\n--- Creating Agent Identity Blueprint ---\n")

    existing = find_existing_blueprint(token)
    if existing:
        app_id = existing["appId"]
        obj_id = existing["id"]
        name = existing.get("displayName", BLUEPRINT_DISPLAY_NAME)
        print(f"  [skip] Blueprint already exists: {name}")
        print(f"         App ID:    {app_id}")
        print(f"         Object ID: {obj_id}")
        set_state("BLUEPRINT_APP_ID", app_id)
        set_state("BLUEPRINT_OBJECT_ID", obj_id)
        # Always ensure BlueprintPrincipal exists — previous run may have crashed
        ensure_blueprint_principal(token, app_id)
        return app_id, obj_id

    body: dict = {
        "@odata.type": "Microsoft.Graph.AgentIdentityBlueprint",
        "displayName": BLUEPRINT_DISPLAY_NAME,
        "description": "Agent Identity Blueprint for Openclaw device agents",
    }
    sponsors_bind = build_sponsors_bind()
    if sponsors_bind:
        body["sponsors@odata.bind"] = sponsors_bind

    resp = graph_request("POST", "/applications", token, json_body=body)
    if resp.status_code not in (200, 201):
        resp_text = resp.text
        if "Directory.AccessAsUser.All" in resp_text:
            print("  ERROR: Agent APIs reject tokens with Directory.AccessAsUser.All")
            print("  The provisioner app token still has issues.")
            print(f"  Check admin consent for: {get_state('PROVISIONER_CLIENT_ID')}")
            sys.exit(1)
        print(f"  ERROR: Failed to create blueprint: {resp.status_code}")
        print(f"  Response: {resp_text[:300]}")
        sys.exit(1)

    data = resp.json()
    app_id = data["appId"]
    obj_id = data["id"]
    print(f"  [new] Blueprint created: {BLUEPRINT_DISPLAY_NAME}")
    print(f"        App ID:    {app_id}")
    print(f"        Object ID: {obj_id}")

    set_state("BLUEPRINT_APP_ID", app_id)
    set_state("BLUEPRINT_OBJECT_ID", obj_id)

    ensure_blueprint_principal(token, app_id)

    return app_id, obj_id


# ---------------------------------------------------------------------------
# Agent Identity
# ---------------------------------------------------------------------------


def _agent_display_name() -> str:
    """Generate a display name for the agent identity based on hostname."""
    try:
        hostname = socket.gethostname().split(".")[0]
    except Exception:
        hostname = platform.node() or "unknown"
    return f"Openclaw Agent - {hostname}"


def find_existing_agent_identity(
    token: str, display_name: str, stored_app_id: str | None = None
) -> dict | None:
    """Find an existing Agent Identity by stored appId or display name."""
    if stored_app_id:
        resp = graph_request(
            "GET",
            f"/servicePrincipals?$filter=appId eq '{odata_escape(stored_app_id)}'",
            token,
        )
        if resp.status_code == 200:
            values = resp.json().get("value", [])
            if values:
                return values[0]

    resp = graph_request(
        "GET",
        f"/servicePrincipals?$filter=displayName eq '{odata_escape(display_name)}'",
        token,
    )
    if resp.status_code != 200:
        return None

    for sp in resp.json().get("value", []):
        if sp.get("displayName") == display_name:
            return sp
    return None


def create_agent_identity(token: str, blueprint_app_id: str) -> tuple[str, str]:
    """Create or find the Agent Identity. Returns (agent_app_id, agent_object_id)."""
    print("\n--- Creating Agent Identity ---\n")

    display_name = _agent_display_name()
    stored_app_id = get_state("AGENT_ID")

    existing = find_existing_agent_identity(token, display_name, stored_app_id=stored_app_id)
    if existing:
        agent_id = existing.get("appId", "")
        agent_obj_id = existing.get("id", "")
        print(f"  [skip] Agent Identity already exists: {display_name} (appId={agent_id})")
        set_state("AGENT_ID", agent_id)
        set_state("AGENT_OBJECT_ID", agent_obj_id)
        return agent_id, agent_obj_id

    sponsor_id = get_signed_in_user_id()
    if sponsor_id:
        print(f"  Sponsor (current user): {sponsor_id}")
    else:
        print("  WARNING: Could not get current user for sponsorship")

    body: dict = {
        "@odata.type": "Microsoft.Graph.AgentIdentity",
        "displayName": display_name,
        "agentIdentityBlueprintId": blueprint_app_id,
    }
    if sponsor_id:
        body["sponsors@odata.bind"] = [
            f"https://graph.microsoft.com/beta/users/{sponsor_id}"
        ]

    for attempt in range(3):
        resp = graph_request("POST", "/servicePrincipals", token, json_body=body)
        if resp.status_code in (200, 201):
            data = resp.json()
            agent_id = data.get("appId", "")
            agent_obj_id = data.get("id", "")
            if not agent_id:
                print("  ERROR: Graph response did not include appId")
                sys.exit(1)
            print(f"  [new] Agent Identity created: {display_name} (appId={agent_id})")
            set_state("AGENT_ID", agent_id)
            set_state("AGENT_OBJECT_ID", agent_obj_id)
            return agent_id, agent_obj_id
        elif resp.status_code == 403:
            print("  ERROR: Permission denied creating Agent Identity")
            print(f"  Response: {resp.text[:300]}")
            sys.exit(1)
        elif attempt < 2:
            wait = 10 * (attempt + 1)
            print(f"  Returned {resp.status_code}, retrying in {wait}s...")
            time.sleep(wait)
        else:
            print(f"  ERROR: Failed after retries ({resp.status_code})")
            print(f"  Response: {resp.text[:300]}")
            sys.exit(1)

    # Unreachable, but satisfies type checker
    sys.exit(1)


# ---------------------------------------------------------------------------
# Agent User
# ---------------------------------------------------------------------------

# Microsoft Graph SP object ID — needed for oauth2PermissionGrant.
# Resolved at runtime because it varies per tenant.
MS_GRAPH_API_APP_ID = "00000003-0000-0000-c000-000000000000"


def _agent_user_upn() -> str:
    """Generate the UPN for the Agent User.

    Extracts the domain from the signed-in user's UPN (e.g., admin@werner.ac → werner.ac).
    This guarantees the domain is a verified domain in the tenant.
    """
    from entra_provisioning import run_az

    rc, out, _ = run_az([
        "ad", "signed-in-user", "show", "--query", "userPrincipalName", "-o", "tsv",
    ])
    if rc == 0 and out and "@" in out:
        domain = out.split("@", 1)[1]
    else:
        # Fallback: query tenant verified domains via az CLI
        rc, out, _ = run_az([
            "rest", "--method", "GET",
            "--url", "https://graph.microsoft.com/v1.0/domains?$select=id,isDefault",
            "--query", "value[?isDefault].id | [0]", "-o", "tsv",
        ])
        domain = out if rc == 0 and out else "unknown.onmicrosoft.com"
    return f"openclaw-agent@{domain}"


def _resolve_graph_sp_object_id(token: str) -> str | None:
    """Get the object ID of the Microsoft Graph service principal in this tenant."""
    resp = graph_request(
        "GET",
        f"/servicePrincipals?$filter=appId eq '{MS_GRAPH_API_APP_ID}'&$select=id",
        token,
    )
    if resp.status_code == 200:
        values = resp.json().get("value", [])
        if values:
            return values[0].get("id")
    return None


def find_existing_agent_user(token: str, agent_identity_obj_id: str) -> dict | None:
    """Find an existing Agent User linked to the given Agent Identity."""
    stored_user_id = get_state("AGENT_USER_ID")
    if stored_user_id:
        resp = graph_request("GET", f"/users/{stored_user_id}", token, retry=False)
        if resp.status_code == 200:
            return resp.json()
        print(f"  [warn] Stored AGENT_USER_ID not found: {stored_user_id}")

    # Search by identityParentId (the Agent Identity's object ID)
    resp = graph_request(
        "GET",
        f"/users?$filter=identityParentId eq '{agent_identity_obj_id}'",
        token,
    )
    if resp.status_code == 200:
        values = resp.json().get("value", [])
        if values:
            return values[0]
    return None


def create_agent_user(
    token: str,
    agent_identity_obj_id: str,
) -> tuple[str, str]:
    """Create or find the Agent User. Returns (user_object_id, user_upn)."""
    print("\n--- Creating Agent User ---\n")

    existing = find_existing_agent_user(token, agent_identity_obj_id)
    if existing:
        user_id = existing.get("id", "")
        upn = existing.get("userPrincipalName", "")
        print(f"  [skip] Agent User already exists: {upn} ({user_id})")
        set_state("AGENT_USER_ID", user_id)
        set_state("AGENT_USER_UPN", upn)
        return user_id, upn

    upn = _agent_user_upn()
    body = {
        "@odata.type": "microsoft.graph.agentUser",
        "displayName": "Openclaw Agent",
        "userPrincipalName": upn,
        "identityParentId": agent_identity_obj_id,
        "mailNickname": "openclaw-agent",
        "accountEnabled": True,
    }

    for attempt in range(3):
        resp = graph_request("POST", "/users", token, json_body=body)
        if resp.status_code in (200, 201):
            data = resp.json()
            user_id = data.get("id", "")
            actual_upn = data.get("userPrincipalName", upn)
            print(f"  [new] Agent User created: {actual_upn} ({user_id})")
            set_state("AGENT_USER_ID", user_id)
            set_state("AGENT_USER_UPN", actual_upn)
            return user_id, actual_upn
        elif resp.status_code == 403:
            print("  ERROR: Permission denied creating Agent User")
            print("  Ensure AgentIdUser.ReadWrite.IdentityParentedBy is granted")
            print(f"  Response: {resp.text[:300]}")
            sys.exit(1)
        elif attempt < 2:
            wait = 10 * (attempt + 1)
            print(f"  Returned {resp.status_code}, retrying in {wait}s...")
            time.sleep(wait)
        else:
            print(f"  ERROR: Failed after retries ({resp.status_code})")
            print(f"  Response: {resp.text[:300]}")
            sys.exit(1)

    sys.exit(1)


def grant_agent_user_consent(
    token: str,
    agent_identity_obj_id: str,
    agent_user_obj_id: str,
) -> None:
    """Grant the Agent Identity permission to act as the Agent User for Graph APIs.

    Creates an oAuth2PermissionGrant so the Agent Identity can get delegated
    tokens (Chat.Create, ChatMessage.Send, etc.) as the Agent User.
    """
    print("\n--- Granting Agent User consent ---\n")

    graph_sp_id = _resolve_graph_sp_object_id(token)
    if not graph_sp_id:
        print("  ERROR: Could not resolve Microsoft Graph SP object ID")
        print("  Consent grant will need to be done manually")
        return

    # Check if consent already exists
    resp = graph_request(
        "GET",
        "/oauth2PermissionGrants"
        f"?$filter=clientId eq '{agent_identity_obj_id}'"
        f" and principalId eq '{agent_user_obj_id}'"
        f" and resourceId eq '{graph_sp_id}'",
        token,
    )
    if resp.status_code == 200:
        existing = resp.json().get("value", [])
        if existing:
            print(f"  [skip] Consent already granted (scope: {existing[0].get('scope', '')})")
            return

    scopes = "Chat.Create Chat.ReadWrite ChatMessage.Send User.Read"
    body = {
        "clientId": agent_identity_obj_id,
        "consentType": "Principal",
        "principalId": agent_user_obj_id,
        "resourceId": graph_sp_id,
        "scope": scopes,
    }

    # Use v1.0 endpoint for oauth2PermissionGrants
    resp = graph_request("POST", "/oauth2PermissionGrants", token, json_body=body)
    if resp.status_code in (200, 201):
        print(f"  [new] Consent granted: {scopes}")
    else:
        print(f"  WARNING: Consent grant returned {resp.status_code}")
        print(f"  Response: {resp.text[:300]}")
        print("  You may need to grant consent manually via the Entra admin center")


# ---------------------------------------------------------------------------
# License assignment
# ---------------------------------------------------------------------------

# SKU part numbers that include Teams (any of these will work)
TEAMS_CAPABLE_SKUS = [
    "ENTERPRISEPREMIUM",     # M365 E5
    "SPE_E5",                # M365 E5 (alternate)
    "SPE_E3",                # M365 E3
    "ENTERPRISEPACK",        # Office 365 E3
    "TEAMS_EXPLORATORY",     # Teams Exploratory
    "Microsoft_Teams_Essentials",
    "TEAMS_PREMIUM",
    "M365_E5_SUITE_COMPONENTS",
    "MICROSOFT_365_COPILOT",  # M365 Copilot (includes Teams)
]


def _get_available_skus(token: str) -> list[dict]:
    """Get all subscribed SKUs with available licenses."""
    resp = graph_request("GET", "/subscribedSkus", token)
    if resp.status_code != 200:
        print(f"  WARNING: Could not list subscribed SKUs ({resp.status_code})")
        return []

    skus = resp.json().get("value", [])
    available = []
    for sku in skus:
        enabled = sku.get("prepaidUnits", {}).get("enabled", 0)
        consumed = sku.get("consumedUnits", 0)
        remaining = enabled - consumed
        if remaining > 0:
            available.append({
                "skuId": sku["skuId"],
                "skuPartNumber": sku.get("skuPartNumber", ""),
                "displayName": sku.get("skuPartNumber", sku["skuId"]),
                "remaining": remaining,
                "total": enabled,
            })
    return available


def _set_usage_location(token: str, user_id: str, location: str = "US") -> bool:
    """Set the usageLocation on a user (required before license assignment)."""
    resp = graph_request(
        "PATCH",
        f"/users/{user_id}",
        token,
        json_body={"usageLocation": location},
    )
    return resp.status_code in (200, 204)


def _check_existing_licenses(token: str, user_id: str) -> list[str]:
    """Check what licenses are already assigned to the user."""
    resp = graph_request("GET", f"/users/{user_id}?$select=assignedLicenses", token)
    if resp.status_code == 200:
        return [
            lic.get("skuId", "")
            for lic in resp.json().get("assignedLicenses", [])
        ]
    return []


def assign_license_to_agent_user(token: str, agent_user_id: str) -> None:
    """Assign a Teams-capable M365 license to the Agent User.

    Lists available SKUs, checks if agent already has one, and either
    auto-assigns a Teams-capable SKU or prompts the user to choose.
    """
    print("\n--- License Assignment ---\n")

    # Check if already licensed
    existing = _check_existing_licenses(token, agent_user_id)
    if existing:
        print(f"  [skip] Agent User already has {len(existing)} license(s) assigned")
        return

    # Get available SKUs
    all_skus = _get_available_skus(token)
    if not all_skus:
        print("  ERROR: No subscribed SKUs found in this tenant, or no available licenses.")
        print("  Purchase M365 licenses (E3/E5/Teams Enterprise) at https://admin.microsoft.com")
        print("  Then re-run setup.sh to assign a license to the Agent User.")
        return

    # Filter to Teams-capable SKUs
    teams_skus = [
        s for s in all_skus
        if s["skuPartNumber"] in TEAMS_CAPABLE_SKUS
    ]

    # If no Teams-capable SKUs, show all available and let user decide
    if not teams_skus:
        print("  No Teams-capable licenses found with available seats.")
        print("  Available SKUs in this tenant:")
        for i, sku in enumerate(all_skus, 1):
            print(f"    {i}. {sku['displayName']} ({sku['remaining']}/{sku['total']} available)")
        print("")
        print("  To assign a license to the Agent User, either:")
        print("  - Purchase a Teams-capable license (E3/E5/Teams Enterprise)")
        print("  - Or assign one manually in the Entra admin center")
        return

    # If exactly one Teams-capable SKU, auto-assign it
    if len(teams_skus) == 1:
        chosen = teams_skus[0]
        print(f"  Found 1 Teams-capable license: {chosen['displayName']}"
              f" ({chosen['remaining']}/{chosen['total']} available)")
    else:
        # Multiple options — ask the user
        print("  Teams-capable licenses available:")
        for i, sku in enumerate(teams_skus, 1):
            print(f"    {i}. {sku['displayName']}"
                  f" ({sku['remaining']}/{sku['total']} available)")
        print("")
        while True:
            try:
                choice = input(f"  Which license? [1-{len(teams_skus)}]: ").strip()
                idx = int(choice) - 1
                if 0 <= idx < len(teams_skus):
                    chosen = teams_skus[idx]
                    break
                print(f"  Please enter a number between 1 and {len(teams_skus)}")
            except (ValueError, EOFError):
                print("  Invalid input. Skipping license assignment.")
                print("  Assign manually in the Entra admin center.")
                return

    # Set usageLocation (required before license assignment)
    print("  Setting usageLocation=US on Agent User...")
    if not _set_usage_location(token, agent_user_id):
        print("  WARNING: Could not set usageLocation — license assignment may fail")

    # Assign the license
    print(f"  Assigning {chosen['displayName']} to Agent User...")
    resp = graph_request(
        "POST",
        f"/users/{agent_user_id}/assignLicense",
        token,
        json_body={
            "addLicenses": [{"skuId": chosen["skuId"]}],
            "removeLicenses": [],
        },
    )
    if resp.status_code in (200, 201):
        print(f"  [done] License assigned: {chosen['displayName']}")
        print("  Teams/mailbox provisioning will complete in 10-15 minutes")
        set_state("AGENT_USER_LICENSE_SKU", chosen["skuPartNumber"])
    else:
        print(f"  WARNING: License assignment returned {resp.status_code}")
        print(f"  Response: {resp.text[:300]}")
        print("  Assign manually in the Entra admin center")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("=" * 60)
    print("Openclaw — Entra Agent Identity Provisioning")
    print("=" * 60)

    try:
        token = get_graph_token()
    except ProvisionerBootstrapError as exc:
        print(f"ERROR: {exc}")
        return 1

    blueprint_app_id, blueprint_obj_id = create_blueprint(token)
    agent_id, agent_obj_id = create_agent_identity(token, blueprint_app_id)
    agent_user_id, agent_user_upn = create_agent_user(token, agent_obj_id)
    grant_agent_user_consent(token, agent_obj_id, agent_user_id)
    assign_license_to_agent_user(token, agent_user_id)

    print("\n--- Summary ---\n")
    print(f"  Blueprint App ID:    {blueprint_app_id}")
    print(f"  Blueprint Object ID: {blueprint_obj_id}")
    print(f"  Agent App ID:        {agent_id}")
    print(f"  Agent Object ID:     {agent_obj_id}")
    print(f"  Agent User ID:       {agent_user_id}")
    print(f"  Agent User UPN:      {agent_user_upn}")
    print(f"  Agent Display Name:  {_agent_display_name()}")
    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
