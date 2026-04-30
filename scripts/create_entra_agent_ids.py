#!/usr/bin/env python3
"""
create_entra_agent_ids.py
=========================
Creates an Agent Identity Blueprint and a per-device Agent Identity in
Microsoft Entra ID via the Graph beta API.  Stores the resulting IDs in
the local provision state file (.entraclaw-state.json).

Uses the dedicated provisioner app from entra_provisioning.py — never
Azure CLI tokens (which include Directory.AccessAsUser.All and get
rejected by Agent Identity APIs).

Usage:
    python3 scripts/create_entra_agent_ids.py

Prerequisites:
    - az login has been run
    - pip install azure-identity requests
"""

import os
import platform
import socket
import sys
import time

import requests

# When ENTRACLAW_NEW_CHAIN=1, skip all find_existing_* lookups and create fresh.
# Set by setup.sh --new to force a new identity chain.
_FORCE_NEW = os.environ.get("ENTRACLAW_NEW_CHAIN") == "1"

# entra_provisioning.py lives in the same directory
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from entra_provisioning import (  # noqa: E402 — sys.path insert precedes this import
    ProvisionerBootstrapError,
    build_sponsors_bind,
    get_graph_token,
    get_signed_in_user_id,
    get_state,
    set_state,
)

# The repo root is one directory up; src/ contains the entraclaw package.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src"))
from entraclaw.preflight import TEAMS_CAPABLE_SKUS  # noqa: E402

GRAPH_BASE = "https://graph.microsoft.com/beta"

BLUEPRINT_DISPLAY_NAME = "EntraClaw Code Agent"


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

    if _FORCE_NEW:
        print("  [--new] Skipping existing Blueprint lookup — creating fresh")
        existing = None
    else:
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
        "description": "Agent Identity Blueprint for EntraClaw device agents",
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
    return f"EntraClaw Agent - {hostname}"


def find_existing_agent_identity(
    token: str,
    display_name: str,
    blueprint_app_id: str,
    stored_app_id: str | None = None,
) -> dict | None:
    """Find an existing Agent Identity under ``blueprint_app_id``.

    Agent Identity display names include the machine hostname
    (``EntraClaw Agent - <host>``) — in a tenant that has multiple
    EntraClaw Blueprints, multiple Agent Identity SPs will share the
    same display name, each parented by a different Blueprint.

    Graph does not support ``$filter=agentIdentityBlueprintId eq ...``
    on service principals, so we filter the result set in Python. Any
    candidate whose ``agentIdentityBlueprintId`` doesn't match our
    target Blueprint is rejected — otherwise this function can cross-
    contaminate state and point the agent at the wrong Blueprint's
    identity, which is a latent source of "why is my three-hop flow
    trying to use the old Blueprint?" bugs.

    ``stored_app_id`` is still honoured as a fast path, but only when
    the fetched SP is also scoped to ``blueprint_app_id``.
    """
    if stored_app_id:
        resp = graph_request(
            "GET",
            f"/servicePrincipals?$filter=appId eq '{odata_escape(stored_app_id)}'",
            token,
        )
        if resp.status_code == 200:
            for sp in resp.json().get("value", []):
                if sp.get("agentIdentityBlueprintId") == blueprint_app_id:
                    return sp
            if resp.json().get("value"):
                print(
                    f"  [warn] stored AGENT_ID={stored_app_id} is parented by a "
                    f"different Blueprint; ignoring and re-discovering."
                )

    resp = graph_request(
        "GET",
        f"/servicePrincipals?$filter=displayName eq '{odata_escape(display_name)}'",
        token,
    )
    if resp.status_code != 200:
        return None

    for sp in resp.json().get("value", []):
        if (
            sp.get("displayName") == display_name
            and sp.get("agentIdentityBlueprintId") == blueprint_app_id
        ):
            return sp
    return None


def create_agent_identity(token: str, blueprint_app_id: str) -> tuple[str, str]:
    """Create or find the Agent Identity. Returns (agent_app_id, agent_object_id)."""
    print("\n--- Creating Agent Identity ---\n")

    display_name = _agent_display_name()
    stored_app_id = get_state("AGENT_ID")

    if _FORCE_NEW:
        print("  [--new] Skipping existing Agent Identity lookup — creating fresh")
        existing = None
    else:
        existing = find_existing_agent_identity(
            token,
            display_name,
            blueprint_app_id,
            stored_app_id=stored_app_id,
        )
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

# Azure Storage well-known app ID — the target SP for user_impersonation grants.
# Resolved at runtime per tenant like the Graph SP above.
AZURE_STORAGE_APP_ID = "e406a681-f3d4-42a8-90b6-c2b029497af1"
AZURE_STORAGE_SCOPE = "user_impersonation"


def _agent_user_upn(token: str) -> str:
    """Generate the UPN for the Agent User.

    Queries the tenant's verified domains via Graph API (using the Provisioner
    token) and picks the best domain. Prefers custom domains over .onmicrosoft.com.

    Previous approach (extracting from signed-in user UPN) failed for guest
    accounts like brandwe@outlook.com — the domain is outlook.com, not the
    tenant's verified domain.
    """
    # Query verified domains via Provisioner token (not az CLI — Learning #1)
    resp = requests.get(
        "https://graph.microsoft.com/v1.0/domains?$select=id,isDefault,isVerified",
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code == 200:
        domains = resp.json().get("value", [])
        # Prefer custom domain (not .onmicrosoft.com)
        custom = [
            d["id"]
            for d in domains
            if d.get("isVerified") and ".onmicrosoft.com" not in d["id"]
        ]
        # When --new, add a unique suffix to avoid UPN collision
        upn_suffix = os.environ.get("_ENTRACLAW_UPN_SUFFIX", "")
        agent_name = f"entraclaw-agent-{upn_suffix}" if upn_suffix else "entraclaw-agent"
        if custom:
            print(f"  Using custom verified domain: {custom[0]}")
            return f"{agent_name}@{custom[0]}"
        # Fall back to default domain (including .onmicrosoft.com)
        default = [d["id"] for d in domains if d.get("isDefault")]
        if default:
            print(f"  Using default domain: {default[0]}")
            return f"{agent_name}@{default[0]}"

    # Last resort
    print("  WARNING: Could not determine verified domain for Agent User UPN")
    print(f"  Graph API returned: {resp.status_code} {resp.text[:200]}")
    return "entraclaw-agent@unknown.onmicrosoft.com"


def _resolve_graph_sp_object_id(token: str) -> str | None:
    """Get the object ID of the Microsoft Graph service principal in this tenant."""
    return _resolve_sp_object_id_by_app_id(token, MS_GRAPH_API_APP_ID)


def _resolve_sp_object_id_by_app_id(token: str, app_id: str) -> str | None:
    """Get the object ID of a service principal by its appId in this tenant."""
    resp = graph_request(
        "GET",
        f"/servicePrincipals?$filter=appId eq '{app_id}'&$select=id",
        token,
    )
    if resp.status_code == 200:
        values = resp.json().get("value", [])
        if values:
            return values[0].get("id")
    return None


def find_existing_agent_user(token: str, agent_identity_obj_id: str) -> dict | None:
    """Find an existing Agent User linked to the given Agent Identity.

    The stored AGENT_USER_ID is only trusted if the fetched user's
    ``identityParentId`` matches ``agent_identity_obj_id`` — otherwise
    state from a previous Blueprint's chain can silently win and the
    caller ends up pointing at a user parented by a different Agent
    Identity. Seen in the wild on 2026-04-19 when a name-based lookup
    elsewhere mis-assigned AGENT_ID across Blueprints; defense in
    depth here keeps the damage from propagating.
    """
    stored_user_id = get_state("AGENT_USER_ID")
    if stored_user_id:
        resp = graph_request("GET", f"/users/{stored_user_id}", token, retry=False)
        if resp.status_code == 200:
            user = resp.json()
            if user.get("identityParentId") == agent_identity_obj_id:
                return user
            print(
                f"  [warn] stored AGENT_USER_ID={stored_user_id} is parented by a "
                f"different Agent Identity; ignoring and re-discovering."
            )
        else:
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


def find_existing_agent_user_by_upn(token: str, upn: str) -> dict | None:
    """Find an Agent User in the tenant by its UPN (tenant-wide).

    An Agent User's UPN is globally unique in Entra. We check BEFORE any
    creation so a re-run on a different machine (with a different
    hostname-suffixed Agent Identity display name) doesn't try to mint
    a second Agent Identity + Agent User and collide with the existing
    UPN — the failure mode that produced the orphan 966d16f3-... on
    2026-04-16.

    Returns the user dict (including ``identityParentId``) so the caller
    can derive the parent Agent Identity already in use.
    """
    resp = graph_request(
        "GET",
        (
            f"/users?$filter=userPrincipalName eq '{odata_escape(upn)}'"
            "&$select=id,userPrincipalName,displayName,identityParentId,"
            "mailNickname,accountEnabled"
        ),
        token,
    )
    if resp.status_code != 200:
        return None
    for u in resp.json().get("value", []):
        if u.get("userPrincipalName", "").lower() == upn.lower():
            return u
    return None


def _servicePrincipal_by_object_id(token: str, obj_id: str) -> dict | None:
    """Fetch a service principal by its object ID, or None on any error."""
    resp = graph_request("GET", f"/servicePrincipals/{obj_id}", token, retry=False)
    if resp.status_code == 200:
        return resp.json()
    return None


def create_agent_user(
    token: str,
    agent_identity_obj_id: str,
) -> tuple[str, str]:
    """Create or find the Agent User. Returns (user_object_id, user_upn)."""
    print("\n--- Creating Agent User ---\n")

    if _FORCE_NEW:
        print("  [--new] Skipping existing Agent User lookup — creating fresh")
        existing = None
    else:
        existing = find_existing_agent_user(token, agent_identity_obj_id)
    if existing:
        user_id = existing.get("id", "")
        upn = existing.get("userPrincipalName", "")
        print(f"  [skip] Agent User already exists: {upn} ({user_id})")
        set_state("AGENT_USER_ID", user_id)
        set_state("AGENT_USER_UPN", upn)
        return user_id, upn

    upn = _agent_user_upn(token)
    upn_suffix = os.environ.get("_ENTRACLAW_UPN_SUFFIX", "")
    mail_nick = f"entraclaw-agent-{upn_suffix}" if upn_suffix else "entraclaw-agent"
    display = f"EntraClaw Agent ({upn_suffix})" if upn_suffix else "EntraClaw Agent"
    body = {
        "@odata.type": "microsoft.graph.agentUser",
        "displayName": display,
        "userPrincipalName": upn,
        "identityParentId": agent_identity_obj_id,
        "mailNickname": mail_nick,
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
            # Wait for Entra propagation (Learning #8: 30-120s for new principals)
            print("  Waiting 30s for Agent User to propagate in Entra...")
            time.sleep(30)
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

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    from datetime import UTC, datetime

    # Scopes the Agent Identity can request when impersonating the Agent User:
    #   Chat.*, ChatMessage.Send  — Teams chat send/read/manage (the original use case)
    #   User.Read                 — basic profile lookup
    #   Files.ReadWrite           — OneDrive file ops (e.g., upload PDFs, share links)
    #   Mail.Read                 — read Agent User's mailbox (inbound email)
    #   Mail.Send                 — send email from Agent User (e.g., daily activity summary)
    scopes = (
        "Chat.Create Chat.ReadWrite ChatMessage.Send User.Read "
        "Files.ReadWrite Mail.Read Mail.Send"
    )
    required_scopes = set(scopes.split())

    # Check if consent already exists (v1.0 API)
    check_url = (
        "https://graph.microsoft.com/v1.0/oauth2PermissionGrants"
        f"?$filter=clientId eq '{agent_identity_obj_id}'"
        f" and principalId eq '{agent_user_obj_id}'"
    )
    resp = requests.get(check_url, headers=headers)
    if resp.status_code == 200:
        existing = resp.json().get("value", [])
        if existing:
            grant = existing[0]
            existing_scopes = set((grant.get("scope") or "").split())
            missing = required_scopes - existing_scopes
            if not missing:
                print(f"  [skip] Consent already granted (scope: {grant.get('scope', '')})")
                return
            # Existing consent is missing some required scopes — PATCH to add them.
            merged = sorted(existing_scopes | required_scopes)
            patch_url = f"https://graph.microsoft.com/v1.0/oauth2PermissionGrants/{grant['id']}"
            patch_resp = requests.patch(
                patch_url,
                headers=headers,
                json={"scope": " ".join(merged)},
            )
            if patch_resp.status_code in (200, 204):
                print(f"  [updated] Added missing scopes: {sorted(missing)}")
                print(f"           Full scope set: {' '.join(merged)}")
                return
            print(
                f"  ERROR: Failed to patch consent ({patch_resp.status_code}): "
                f"{patch_resp.text[:200]}"
            )
            print("  Falling through to create new grant...")


    body = {
        "clientId": agent_identity_obj_id,
        "consentType": "Principal",
        "principalId": agent_user_obj_id,
        "resourceId": graph_sp_id,
        "scope": scopes,
        "startTime": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    # oAuth2PermissionGrants is a v1.0 API — use the full URL, not graph_request()
    # which prepends the beta base URL.
    # Retry on "Principal not found" — Agent User may still be propagating (Learning #8)
    for attempt in range(4):
        resp = requests.post(
            "https://graph.microsoft.com/v1.0/oauth2PermissionGrants",
            headers=headers,
            json=body,
        )
        if resp.status_code in (200, 201):
            print(f"  [new] Consent granted: {scopes}")
            return

        # Retry on propagation-related errors
        resp_text = resp.text
        is_propagation = (
            "Principal was not found" in resp_text
            or "does not exist" in resp_text
            or resp.status_code == 404
        )
        if is_propagation and attempt < 3:
            wait = 15 * (attempt + 1)
            print(f"  Agent User not yet propagated — waiting {wait}s (attempt {attempt + 1}/4)...")
            time.sleep(wait)
            continue

        print(f"  ERROR: Consent grant failed ({resp.status_code})")
        print(f"  Response: {resp_text[:400]}")
        print("")
        print("  This is a BLOCKING error — hop 3 of the three-hop flow will fail")
        print("  without this consent grant. Check that the provisioner has")
        print("  DelegatedPermissionGrant.ReadWrite.All permission.")
        sys.exit(1)


def grant_agent_identity_app_permissions(
    token: str,
    agent_identity_obj_id: str,
) -> None:
    """Grant Application.Read.All to the Agent Identity service principal.

    The Agent Identity needs this app permission to call the sponsors API
    (GET /servicePrincipals/{id}/microsoft.graph.agentIdentity/sponsors)
    which is required for sponsor-gated flows like wait_for_sponsor_dm.

    This is an app role assignment (application permission), not a delegated
    permission grant — the Agent Identity calls Graph as itself, not on behalf
    of the Agent User.
    """
    print("\n--- Granting Agent Identity app permissions ---\n")

    graph_sp_id = _resolve_graph_sp_object_id(token)
    if not graph_sp_id:
        print("  ERROR: Could not resolve Microsoft Graph SP object ID")
        print("  Application.Read.All grant will need to be done manually")
        return

    # Application.Read.All app role ID (well-known across all tenants)
    APPLICATION_READ_ALL_ROLE_ID = "9a5d68dd-52b0-4cc2-bd40-abcf44ac3a30"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Check if already granted
    check_url = (
        f"https://graph.microsoft.com/v1.0/servicePrincipals/{agent_identity_obj_id}"
        "/appRoleAssignments"
    )
    resp = requests.get(check_url, headers=headers)
    if resp.status_code == 200:
        existing = resp.json().get("value", [])
        for assignment in existing:
            if assignment.get("appRoleId") == APPLICATION_READ_ALL_ROLE_ID:
                print("  [skip] Application.Read.All already granted")
                return

    # Grant the app role
    body = {
        "principalId": agent_identity_obj_id,
        "resourceId": graph_sp_id,
        "appRoleId": APPLICATION_READ_ALL_ROLE_ID,
    }
    resp = requests.post(
        f"https://graph.microsoft.com/v1.0/servicePrincipals/{agent_identity_obj_id}"
        "/appRoleAssignments",
        headers=headers,
        json=body,
    )
    if resp.status_code in (200, 201):
        print("  [ok] Application.Read.All granted to Agent Identity")
    elif resp.status_code == 409:
        print("  [skip] Application.Read.All already granted (409 conflict)")
    else:
        print(f"  WARNING: Failed to grant Application.Read.All ({resp.status_code})")
        print(f"  Response: {resp.text[:300]}")
        print("  The sponsors API (wait_for_sponsor_dm) will fail with 403.")
        print("  Grant manually: New-MgServicePrincipalAppRoleAssignment")


def grant_agent_user_storage_consent(
    token: str,
    agent_identity_obj_id: str,
    agent_user_obj_id: str,
) -> None:
    """Grant the Agent Identity permission to request Azure Storage tokens as the Agent User.

    Creates/updates an oAuth2PermissionGrant with `user_impersonation` on the
    Azure Storage service principal so the third hop of the three-hop flow can
    exchange a `user_fic` grant for a `idtyp=user` token with
    `https://storage.azure.com/.default` audience (ADR-005).
    """
    print("\n--- Granting Agent User storage consent ---\n")

    storage_sp_id = _resolve_sp_object_id_by_app_id(token, AZURE_STORAGE_APP_ID)
    if not storage_sp_id:
        print("  WARN: Azure Storage SP not found in tenant — cannot grant consent.")
        print("  This is non-fatal; cloud-hosted memory (ADR-005) will not work")
        print("  until the grant is added manually or --keep-memory-local is set.")
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    from datetime import UTC, datetime

    check_url = (
        "https://graph.microsoft.com/v1.0/oauth2PermissionGrants"
        f"?$filter=clientId eq '{agent_identity_obj_id}'"
        f" and principalId eq '{agent_user_obj_id}'"
        f" and resourceId eq '{storage_sp_id}'"
    )
    resp = requests.get(check_url, headers=headers)
    if resp.status_code == 200:
        existing = resp.json().get("value", [])
        if existing:
            grant = existing[0]
            existing_scopes = set((grant.get("scope") or "").split())
            if AZURE_STORAGE_SCOPE in existing_scopes:
                print(f"  [skip] Storage consent already granted ({AZURE_STORAGE_SCOPE})")
                return
            merged = sorted(existing_scopes | {AZURE_STORAGE_SCOPE})
            patch_url = f"https://graph.microsoft.com/v1.0/oauth2PermissionGrants/{grant['id']}"
            patch_resp = requests.patch(
                patch_url,
                headers=headers,
                json={"scope": " ".join(merged)},
            )
            if patch_resp.status_code in (200, 204):
                print(f"  [updated] Added storage scope: {AZURE_STORAGE_SCOPE}")
                return
            print(
                f"  WARN: Failed to patch storage consent ({patch_resp.status_code}): "
                f"{patch_resp.text[:200]}"
            )
            print("  Falling through to create new grant...")

    body = {
        "clientId": agent_identity_obj_id,
        "consentType": "Principal",
        "principalId": agent_user_obj_id,
        "resourceId": storage_sp_id,
        "scope": AZURE_STORAGE_SCOPE,
        "startTime": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    for attempt in range(4):
        resp = requests.post(
            "https://graph.microsoft.com/v1.0/oauth2PermissionGrants",
            headers=headers,
            json=body,
        )
        if resp.status_code in (200, 201):
            print(f"  [new] Storage consent granted: {AZURE_STORAGE_SCOPE}")
            return

        resp_text = resp.text
        is_propagation = (
            "Principal was not found" in resp_text
            or "does not exist" in resp_text
            or resp.status_code == 404
        )
        if is_propagation and attempt < 3:
            wait = 15 * (attempt + 1)
            print(
                f"  Storage SP/User not yet propagated — "
                f"waiting {wait}s (attempt {attempt + 1}/4)..."
            )
            time.sleep(wait)
            continue

        print(f"  WARN: Storage consent grant failed ({resp.status_code})")
        print(f"  Response: {resp_text[:400]}")
        print("  Cloud-hosted memory (ADR-005) will not work until this is resolved.")
        print("  Re-run this script or set ENTRACLAW_KEEP_MEMORY_LOCAL=true to bypass.")
        return


# ---------------------------------------------------------------------------
# License assignment
# ---------------------------------------------------------------------------
# TEAMS_CAPABLE_SKUS is imported from entraclaw.preflight — single source of
# truth so setup.sh's preflight check and the actual license assignment use
# the same SKU list.


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

    # Check if already licensed with a Teams-capable SKU
    existing_sku_ids = _check_existing_licenses(token, agent_user_id)
    if existing_sku_ids:
        # Resolve SKU IDs to part numbers to check if any are Teams-capable
        resp = graph_request("GET", "/subscribedSkus", token)
        sku_id_to_name = {}
        if resp.status_code == 200:
            for sku in resp.json().get("value", []):
                sku_id_to_name[sku["skuId"]] = sku.get("skuPartNumber", sku["skuId"])

        existing_names = [sku_id_to_name.get(sid, sid) for sid in existing_sku_ids]
        has_teams = any(name in TEAMS_CAPABLE_SKUS for name in existing_names)

        if has_teams:
            teams_name = next(n for n in existing_names if n in TEAMS_CAPABLE_SKUS)
            print(f"  [skip] Agent User already has Teams-capable license: {teams_name}")
            return
        else:
            print(f"  Agent User has {len(existing_sku_ids)} license(s) but none include Teams:")
            for name in existing_names:
                print(f"    - {name}")
            print("  Will assign a Teams-capable license...")

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

    # Set usageLocation (required before license assignment).
    # The Agent User may not have fully replicated yet — retry a few times.
    print("  Setting usageLocation on Agent User (waiting for Entra replication)...")
    location_set = False
    for attempt in range(5):
        if _set_usage_location(token, agent_user_id):
            location_set = True
            break
        wait = 5 * (attempt + 1)
        print(f"  Agent User not ready yet, retrying in {wait}s...")
        time.sleep(wait)

    if not location_set:
        print("  WARNING: Could not set usageLocation after retries")
        print("  The Agent User may not have replicated to M365 yet.")
        print("  Re-run setup.sh in a few minutes to assign the license.")
        return

    # Assign the license (also retry — replication can lag)
    print(f"  Assigning {chosen['displayName']} to Agent User...")
    assigned = False
    for attempt in range(3):
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
            assigned = True
            break
        if attempt < 2:
            wait = 10 * (attempt + 1)
            print(f"  License assignment returned {resp.status_code}, retrying in {wait}s...")
            time.sleep(wait)

    if assigned:
        print(f"  [done] License assigned: {chosen['displayName']}")
        print("  Teams/mailbox provisioning will complete in 10-15 minutes")
        set_state("AGENT_USER_LICENSE_SKU", chosen["skuPartNumber"])
    else:
        print(f"  WARNING: License assignment failed after retries ({resp.status_code})")
        print(f"  Response: {resp.text[:300]}")
        print("  Re-run setup.sh in a few minutes or assign manually in the Entra admin center")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("=" * 60)
    print("EntraClaw — Entra Agent Identity Provisioning")
    print("=" * 60)

    try:
        token = get_graph_token()
    except ProvisionerBootstrapError as exc:
        print(f"ERROR: {exc}")
        return 1

    blueprint_app_id, blueprint_obj_id = create_blueprint(token)

    # Check tenant-wide for an existing Agent User by UPN BEFORE provisioning
    # anything new. If the UPN already exists (from a prior run on another
    # machine, or a state-file loss), reuse the Agent User + its parent
    # Agent Identity rather than creating duplicates that would then
    # collide on the unique-UPN constraint.
    #
    # When _FORCE_NEW (setup.sh --new), skip this check entirely and use a
    # unique UPN suffix so the new Agent User doesn't collide.
    if _FORCE_NEW:
        existing_user = None
        _suffix = os.environ.get("_ENTRACLAW_UPN_SUFFIX", "")
        if _suffix:
            print(f"  [--new] Will use UPN suffix: {_suffix}")
        else:
            print("  [--new] WARNING: No UPN suffix set — may collide with existing Agent User")
    else:
        intended_upn = _agent_user_upn(token)
        existing_user = find_existing_agent_user_by_upn(token, intended_upn)
    if existing_user:
        agent_user_id = existing_user["id"]
        agent_user_upn = existing_user["userPrincipalName"]
        agent_obj_id = existing_user.get("identityParentId", "")
        sp = (
            _servicePrincipal_by_object_id(token, agent_obj_id)
            if agent_obj_id
            else None
        )
        agent_id = sp.get("appId", "") if sp else ""
        parent_name = sp.get("displayName", "?") if sp else "?"
        print("\n--- Reusing existing Agent User ---\n")
        print(f"  UPN:            {agent_user_upn}")
        print(f"  User Object ID: {agent_user_id}")
        print(f"  Parent Agent Identity: {parent_name}")
        print(f"    appId:        {agent_id}")
        print(f"    objId:        {agent_obj_id}")
        print("  (Skipping Agent Identity + Agent User creation — already provisioned.)")
        set_state("AGENT_ID", agent_id)
        set_state("AGENT_OBJECT_ID", agent_obj_id)
        set_state("AGENT_USER_ID", agent_user_id)
        set_state("AGENT_USER_UPN", agent_user_upn)
    else:
        agent_id, agent_obj_id = create_agent_identity(token, blueprint_app_id)
        agent_user_id, agent_user_upn = create_agent_user(token, agent_obj_id)

    grant_agent_identity_app_permissions(token, agent_obj_id)
    grant_agent_user_consent(token, agent_obj_id, agent_user_id)
    grant_agent_user_storage_consent(token, agent_obj_id, agent_user_id)
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
