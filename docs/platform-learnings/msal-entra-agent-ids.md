# MSAL & Entra Agent IDs

> **Last updated:** 2025-07-14
> **Status:** Living reference document for Entraclaw identity architecture

## Overview

MSAL (Microsoft Authentication Library) and Microsoft Entra Agent IDs form the **foundational auth layer** for Entraclaw. Every Entraclaw agent — whether running on Mac, Linux, or Windows — needs:

1. **A distinct identity** in the enterprise directory (Entra Agent ID)
2. **Token acquisition** to call APIs on behalf of humans or autonomously (MSAL)
3. **On-behalf-of (OBO) delegation** so agents act with the human's permissions, not their own blanket access

**Why this combination matters for Entraclaw:**

- **Agent IDs** give each Entraclaw agent instance a unique, auditable, governable identity in the customer's Entra tenant — separate from the human who deployed it.
- **MSAL** handles the complex OAuth 2.0 token acquisition, caching, and refresh flows that make this work in production.
- **OBO flow** is the critical bridge: a human authenticates once (e.g., via device code flow on a CLI), and the agent exchanges that token to call downstream APIs *as the human* — never exceeding the human's own permissions.

---

## MSAL Python SDK

### Installation

```bash
pip install msal
pip install msal-extensions  # for persistent token cache
```

Current stable version: `msal >= 1.31.x` (check PyPI for latest).

### Two Application Classes

MSAL Python has two primary classes, corresponding to OAuth 2.0 client types:

#### `PublicClientApplication`

For applications that **cannot securely store a secret** — desktop apps, CLI tools, mobile apps.

```python
from msal import PublicClientApplication

app = PublicClientApplication(
    client_id="YOUR_CLIENT_ID",
    authority="https://login.microsoftonline.com/YOUR_TENANT_ID",
    # token_cache=cache  # optional: provide a persistent cache
)
```

**Supported flows:**
- Device code flow (headless/CLI — our primary bootstrap flow)
- Interactive browser flow
- Username/password (ROPC — not recommended)

**Key constraint:** No `client_credential` parameter. Cannot do OBO or client credentials flows.

#### `ConfidentialClientApplication`

For applications that **can securely store credentials** — web APIs, backend services, daemons.

```python
from msal import ConfidentialClientApplication

app = ConfidentialClientApplication(
    client_id="YOUR_API_CLIENT_ID",
    authority="https://login.microsoftonline.com/YOUR_TENANT_ID",
    client_credential="YOUR_CLIENT_SECRET",  # or certificate dict
    # token_cache=cache  # optional
)
```

**Supported flows:**
- Client credentials (app-only tokens)
- On-behalf-of (OBO) — exchanging a user token for a downstream token
- Authorization code redemption

**Certificate authentication** (preferred over secrets for production):

```python
app = ConfidentialClientApplication(
    client_id="YOUR_CLIENT_ID",
    authority="https://login.microsoftonline.com/YOUR_TENANT_ID",
    client_credential={
        "thumbprint": "CERT_THUMBPRINT",
        "private_key": open("private_key.pem").read(),
    },
)
```

### Key Methods Reference

| Method | Class | Purpose |
|--------|-------|---------|
| `initiate_device_flow(scopes)` | Public | Start device code flow |
| `acquire_token_by_device_flow(flow)` | Public | Complete device code flow |
| `acquire_token_interactive(scopes)` | Public | Browser-based interactive auth |
| `acquire_token_silent(scopes, account)` | Both | Get token from cache or refresh |
| `acquire_token_for_client(scopes)` | Confidential | Client credentials (app-only) |
| `acquire_token_on_behalf_of(user_assertion, scopes)` | Confidential | OBO flow |
| `get_accounts()` | Both | List cached accounts |
| `remove_account(account)` | Both | Clear cached account tokens |

### Silent Acquisition Pattern (Critical)

Always attempt silent acquisition first. Only fall back to interactive methods if silent fails:

```python
accounts = app.get_accounts()
result = None

if accounts:
    result = app.acquire_token_silent(
        scopes=["https://graph.microsoft.com/.default"],
        account=accounts[0]
    )

if not result:
    # Fall back to interactive / device code / OBO
    result = app.acquire_token_interactive(scopes=["User.Read"])

if "access_token" in result:
    print("Token acquired")
else:
    print(f"Error: {result.get('error_description')}")
```

---

## On-Behalf-Of (OBO) Flow

### Concept

The OBO flow enables **delegated identity chaining**: when a middle-tier API receives a user's access token, it can exchange that token for a new access token to call a downstream API — preserving the user's identity and permissions throughout the chain.

**This is Entraclaw's core auth pattern:**
```
Human → (device code auth) → Entraclaw Agent → (OBO) → Microsoft Graph / other APIs
```

The agent never gets blanket permissions. It always acts within the bounds of what the human is authorized to do.

### Protocol-Level Detail

**HTTP Request:**

```http
POST /oauth2/v2.0/token HTTP/1.1
Host: login.microsoftonline.com/{tenant}
Content-Type: application/x-www-form-urlencoded

grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer
&client_id={middle-tier-app-client-id}
&client_secret={middle-tier-app-secret}
&assertion={incoming-user-access-token}
&scope=https://graph.microsoft.com/User.Read offline_access
&requested_token_use=on_behalf_of
```

**Required parameters:**

| Parameter | Description |
|-----------|-------------|
| `grant_type` | Must be `urn:ietf:params:oauth:grant-type:jwt-bearer` |
| `client_id` | Client ID of the middle-tier API app registration |
| `client_secret` | Secret of the middle-tier API (or use `client_assertion` for certs) |
| `assertion` | The incoming user access token (JWT). Must have `aud` matching this API |
| `scope` | Space-separated scopes for the downstream API |
| `requested_token_use` | Must be `on_behalf_of` |

**Success Response:**

```json
{
    "token_type": "Bearer",
    "scope": "https://graph.microsoft.com/user.read",
    "expires_in": 3269,
    "ext_expires_in": 0,
    "access_token": "eyJhbGciO...",
    "refresh_token": "OAQABAAAAAABnfiG..."
}
```

**Error Response (Conditional Access / MFA required):**

```json
{
    "error": "interaction_required",
    "error_description": "AADSTS50079: Due to a configuration change...",
    "error_codes": [50079],
    "claims": "{\"access_token\":{\"polids\":{\"essential\":true,\"values\":[\"policy-id\"]}}}"
}
```

### Python Implementation

Complete Flask example of an API that receives a user token and calls Microsoft Graph via OBO:

```python
import msal
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

TENANT_ID = "YOUR_TENANT_ID"
CLIENT_ID = "YOUR_API_CLIENT_ID"
CLIENT_SECRET = "YOUR_API_CLIENT_SECRET"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
DOWNSTREAM_SCOPES = ["https://graph.microsoft.com/User.Read"]

@app.route("/api/call-graph", methods=["GET"])
def call_graph_obo():
    # Extract the incoming bearer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "Missing or invalid Authorization header"}), 401

    incoming_token = auth_header.split(" ", 1)[1]

    # Create MSAL confidential client
    cca = msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET,
    )

    # Perform OBO exchange
    result = cca.acquire_token_on_behalf_of(
        user_assertion=incoming_token,
        scopes=DOWNSTREAM_SCOPES,
    )

    if "access_token" in result:
        # Call downstream API with the OBO token
        graph_response = requests.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {result['access_token']}"},
        )
        return jsonify(graph_response.json())
    else:
        # Handle errors — surface claims challenge if present
        error_info = {
            "error": result.get("error"),
            "description": result.get("error_description"),
            "claims": result.get("claims_challenge"),
        }
        return jsonify(error_info), 401

if __name__ == "__main__":
    app.run(port=5001)
```

### `acquire_token_on_behalf_of` Method Signature

```python
result = cca.acquire_token_on_behalf_of(
    user_assertion: str,       # The incoming JWT access token
    scopes: list[str],         # Permissions for downstream API
    claims_challenge: str = None,  # Claims challenge from conditional access
    **kwargs
)
```

**Returns** a dict with either:
- `"access_token"`, `"token_type"`, `"expires_in"` on success
- `"error"`, `"error_description"`, `"error_codes"` on failure

### Token Claims in OBO Tokens

When an OBO token is issued, it contains claims identifying both the **user** and the **calling application**:

```json
{
    "aud": "https://graph.microsoft.com",
    "iss": "https://sts.windows.net/{tenant-id}/",
    "iat": 1700000000,
    "exp": 1700003600,
    "oid": "user-object-id-in-entra",
    "sub": "user-subject-unique-per-app",
    "tid": "tenant-id",
    "azp": "middle-tier-api-client-id",
    "azpacr": "1",
    "scp": "User.Read",
    "name": "Jane Developer",
    "preferred_username": "jane@contoso.com"
}
```

**Key claims for Entraclaw:**

| Claim | What It Identifies | Entraclaw Use |
|-------|--------------------|--------------|
| `oid` | User's Object ID in the tenant | Identify the human operator |
| `sub` | Subject — unique per (user, app, tenant) | Stable user identifier for your app |
| `azp` | Authorized party — the client app that requested OBO | Identifies the Entraclaw agent's app registration |
| `tid` | Tenant ID | Multi-tenant routing |
| `scp` | Scopes (delegated permissions) | Verify what the agent is allowed to do |
| `azpacr` | Auth method of the calling app (0=public, 1=secret, 2=cert) | Security posture verification |
| `idtyp` | Token type identifier (`app` vs `user`) | Distinguish app-only vs delegated tokens |

### OBO Constraints

- OBO **only works with user (delegated) tokens**. You cannot OBO an app-only token.
- The middle-tier app must be a **ConfidentialClientApplication**.
- The incoming token's `aud` claim must match the middle-tier app's client ID.
- OBO uses **delegated scopes**, not application roles.
- Apps with **custom signing keys** cannot be used as middle-tier in OBO.
- SPAs using implicit flow cannot use `id_token` for OBO if they have wildcard redirect URIs.

---

## Agent IDs / Workload Identities

### What Are Entra Agent IDs?

**Microsoft Entra Agent ID** (public preview since May 2025, announced at Build 2025) is a dedicated identity solution for AI agents. It gives each agent a **unique, manageable digital identity** in the Entra directory — alongside human users and traditional applications.

Agent IDs extend Zero Trust principles to autonomous AI workloads: every agent gets the same governance, visibility, and security controls traditionally reserved for human identities.

### Architecture: Blueprints and Identities

Agent ID uses a two-level model:

#### Agent Identity Blueprint
A **reusable template** that defines a "kind" of agent. Think of it as the app registration equivalent for agents.

- Defines the agent's name, publisher, roles, and permissions
- Holds credentials (secrets, certs, federated identity credentials)
- Created once per agent type (e.g., "Entraclaw Code Assistant")

#### Agent Identity
An **instance** created from a blueprint. Each deployed agent gets its own identity.

- Has a unique Object ID (`id`) in the Entra tenant
- Has **no credentials of its own** — relies on the blueprint
- Has a display name, sponsor (accountable human), and lifecycle metadata
- Appears in sign-in logs, conditional access policies, and audit trails

```
Blueprint: "Entraclaw Agent"
  ├── Agent Identity: "Entraclaw-NorthAm-Jane" (sponsor: jane@contoso.com)
  ├── Agent Identity: "Entraclaw-EMEA-Bob" (sponsor: bob@contoso.com)
  └── Agent Identity: "Entraclaw-Dev-Test" (sponsor: devteam@contoso.com)
```

### How to Register Agent Identities

#### Prerequisites
- **Licensing:** Microsoft 365 Copilot with "Frontier" program enabled, or Entra Workload Identities Premium
- **Roles:** `Agent ID Developer` or `Agent ID Administrator`
- **Permissions:** Microsoft Graph beta API scopes: `AgentIdentityBlueprint.Create`, `AgentIdentityBlueprint.ReadWrite.All`

#### Step 1: Create an Agent Identity Blueprint

```http
POST https://graph.microsoft.com/beta/agentIdentityBlueprints
Content-Type: application/json

{
    "displayName": "Entraclaw Agent",
    "description": "Autonomous coding agent for Entraclaw platform",
    "identifierUris": ["api://entraclaw-agent-blueprint"],
    "appRoles": [
        {
            "displayName": "Code Assistant",
            "value": "CodeAssistant",
            "allowedMemberTypes": ["Application"]
        }
    ],
    "owners": ["{owner-object-id}"],
    "sponsors": ["{sponsor-object-id}"]
}
```

#### Step 2: Create an Agent Identity from the Blueprint

```http
POST https://graph.microsoft.com/beta/agentIdentities
Content-Type: application/json

{
    "agentIdentityBlueprintId": "{blueprint-id}",
    "displayName": "Entraclaw-Dev-Jane",
    "owner": "{owner-object-id}",
    "sponsor": "{sponsor-object-id}"
}
```

#### Step 3: Register to the Agent Registry (Optional)

```http
POST https://graph.microsoft.com/beta/agentRegistry/agentInstances
Content-Type: application/json

{
    "displayName": "Entraclaw-Dev-Jane",
    "endpointUrl": "https://entraclaw-agent.contoso.com",
    "identityId": "{agent-identity-id}",
    "skills": ["code-review", "code-generation"],
    "metadata": {
        "platform": "entraclaw",
        "version": "0.1.0"
    }
}
```

### Agent ID vs Service Principal vs Managed Identity

| Feature | Service Principal | Managed Identity | Agent Identity |
|---------|------------------|-----------------|----------------|
| **Credential management** | Manual (secrets/certs) | Automatic by Azure | Blueprint-driven, none of its own |
| **Works outside Azure?** | Yes | No | Designed for agent platforms |
| **Secret rotation** | Required | Not required | Handled by blueprint |
| **RBAC support** | Yes | Yes | Yes |
| **Multi-tenant** | Yes (configurable) | No | Blueprint model, single-tenant |
| **Purpose-built for AI?** | No | No | **Yes** |
| **Audit/sponsor** | Limited | N/A | Built-in sponsor + audit |
| **Conditional Access** | Yes (with premium) | No | Yes |
| **Identity type** | `servicePrincipal` | `managedIdentity` | Special `servicePrincipal` subtype |

### Key Characteristics of Agent Identities

1. **No credentials of their own** — the blueprint acquires tokens on their behalf
2. **Sponsor accountability** — a human or group is always associated
3. **Appear in sign-in logs** — full audit trail for compliance
4. **Blueprint-based governance** — disable/revoke at the blueprint level affects all instances
5. **Conditional Access support** — apply policies per-agent or per-blueprint
6. **Single-tenant only** — agent identities exist only in their home tenant

### Token Acquisition for Agent Identities

Agent identities support two token patterns:

1. **App tokens (autonomous agents):** The blueprint acquires tokens where the subject is the agent identity itself
2. **User tokens (interactive agents):** Called with a user token, the blueprint acquires user tokens on behalf of the agent identity (similar to OBO)

The **Microsoft Entra SDK for Agent Identities** provides simplified token acquisition in containerized environments via HTTP APIs.

### Current Limitations (Preview)

- **Public preview only** — APIs and behavior may change before GA
- **Microsoft Graph beta API required** — not yet in v1.0
- **Limited platform support** — initially Azure AI Foundry, Copilot Studio; third-party support expanding in late 2025
- **Single-tenant only** — agents can't access cross-tenant resources
- **No MSAL native support yet** — must use Graph API for identity management; token acquisition uses the Entra SDK or standard MSAL with the blueprint credentials
- **Licensing requirements** — requires specific Microsoft 365/Entra licensing

---

## Token Lifecycle

### Token Acquisition Flow

```
1. First-time auth: Device Code Flow → Access Token + Refresh Token
2. Subsequent calls: acquire_token_silent() → Token from cache or refreshed
3. Agent operations: OBO exchange → Downstream Access Token
4. Token expired, refresh failed: Re-authenticate via Device Code Flow
```

### Default Token Lifetimes

| Token Type | Default Lifetime | Notes |
|-----------|-----------------|-------|
| Access Token | ~60-90 minutes | Non-configurable for most scenarios |
| Refresh Token | Up to 90 days | Sliding window, revoked on password change |
| ID Token | ~60 minutes | For user identity claims only |
| OBO Access Token | ~60-90 minutes | Same as regular access tokens |

### MSAL Token Cache

By default, MSAL uses an **in-memory cache** — tokens are lost when the process exits.

#### File-Based Cache (Simple)

```python
import os
import atexit
import msal

CACHE_FILE = os.path.expanduser("~/.entraclaw/token_cache.bin")

cache = msal.SerializableTokenCache()

# Load existing cache
if os.path.exists(CACHE_FILE):
    cache.deserialize(open(CACHE_FILE, "r").read())

# Register save-on-exit
def save_cache():
    if cache.has_state_changed:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        open(CACHE_FILE, "w").write(cache.serialize())

atexit.register(save_cache)

app = msal.PublicClientApplication(
    client_id="YOUR_CLIENT_ID",
    authority="https://login.microsoftonline.com/YOUR_TENANT_ID",
    token_cache=cache,
)
```

#### Secure Platform-Native Cache (Production)

The `msal-extensions` package provides **encrypted, platform-native storage**:

| Platform | Backend | Protection |
|----------|---------|-----------|
| macOS | Keychain | Hardware-backed encryption |
| Windows | DPAPI | User-scoped encryption |
| Linux | libsecret (GNOME Keyring) | Desktop keyring integration |

```python
from msal_extensions import (
    PersistedTokenCache,
    FilePersistence,
    FilePersistenceWithDataProtection,  # Windows
    KeychainPersistence,                # macOS
    LibsecretPersistence,               # Linux
)
import sys
import msal

CACHE_LOCATION = os.path.expanduser("~/.entraclaw/token_cache.bin")

if sys.platform == "darwin":
    persistence = KeychainPersistence(
        CACHE_LOCATION, "OpencalwTokenCache", "com.entraclaw.agent"
    )
elif sys.platform == "win32":
    persistence = FilePersistenceWithDataProtection(CACHE_LOCATION)
else:
    persistence = LibsecretPersistence(
        CACHE_LOCATION,
        schema_name="com.entraclaw.tokencache",
        attributes={"app": "entraclaw"},
    )

cache = PersistedTokenCache(persistence)
app = msal.PublicClientApplication(client_id="...", token_cache=cache)
```

#### Cache Architecture Notes

- The cache stores **access tokens, refresh tokens, ID tokens, and account metadata** in a single JSON blob
- `SerializableTokenCache.has_state_changed` flag tells you if the cache needs saving
- For multi-process scenarios (e.g., multiple agent instances), use `msal-extensions` which provides **file locking**
- For web apps or distributed agents, consider Redis or database-backed caches
- Cache is **per-application** (keyed by client_id + authority)

### Token Refresh

MSAL handles refresh automatically in `acquire_token_silent()`:

1. Check in-memory cache for valid access token → return if found
2. If expired, use cached refresh token to get new access token
3. If refresh token is also expired/revoked → return `None` (caller must re-auth)

**Important:** Refresh tokens are revoked when:
- User changes password
- Admin revokes sessions
- Conditional Access policy changes
- Refresh token is unused for > 90 days (configurable)

---

## Device Code Flow

The device code flow is Entraclaw's **primary bootstrap authentication method** for CLI/headless scenarios where the agent runs in a terminal without a browser.

### How It Works

1. Agent calls `initiate_device_flow()` → gets a user code and URL
2. Agent displays: "Go to https://microsoft.com/devicelogin and enter code ABCD1234"
3. Human opens browser on any device, enters the code, authenticates
4. Agent polls until authentication completes → receives tokens

### Complete Implementation

```python
import sys
import json
import msal

CLIENT_ID = "YOUR_CLIENT_ID"
TENANT_ID = "YOUR_TENANT_ID"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["User.Read", "api://your-api/.default"]

# Set up persistent token cache
cache = msal.SerializableTokenCache()
CACHE_FILE = "token_cache.json"

try:
    with open(CACHE_FILE, "r") as f:
        cache.deserialize(f.read())
except FileNotFoundError:
    pass

app = msal.PublicClientApplication(
    CLIENT_ID,
    authority=AUTHORITY,
    token_cache=cache,
)

def save_cache():
    if cache.has_state_changed:
        with open(CACHE_FILE, "w") as f:
            f.write(cache.serialize())

def authenticate():
    """Authenticate via device code flow with silent-first pattern."""
    # 1. Try silent acquisition first
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            print(f"✓ Authenticated silently as {accounts[0]['username']}")
            save_cache()
            return result

    # 2. Fall back to device code flow
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(
            f"Failed to initiate device flow: {flow.get('error_description')}"
        )

    # Display instructions to human
    print(f"\n🔐 Authentication required:")
    print(f"   1. Open: {flow['verification_uri']}")
    print(f"   2. Enter code: {flow['user_code']}")
    print(f"   (Code expires in {flow.get('expires_in', 900)} seconds)\n")

    # 3. Block until user completes auth (or timeout)
    result = app.acquire_token_by_device_flow(flow)

    if "access_token" in result:
        print(f"✓ Authenticated as {result.get('id_token_claims', {}).get('preferred_username', 'unknown')}")
        save_cache()
        return result
    else:
        print(f"✗ Authentication failed: {result.get('error_description')}", file=sys.stderr)
        return None

if __name__ == "__main__":
    token_result = authenticate()
    if token_result:
        print(f"Access token (first 20 chars): {token_result['access_token'][:20]}...")
```

### App Registration Requirements

For device code flow to work, you must enable it in the app registration:

1. Go to **Microsoft Entra admin center → App registrations → Your App**
2. Under **Authentication**, enable **"Allow public client flows"** (set to "Yes")
3. Under **API permissions**, add the required delegated permissions
4. Platform: Add "Mobile and desktop applications" with `https://login.microsoftonline.com/common/oauth2/nativeclient` as redirect URI

### Device Code Flow Gotchas

- The flow **blocks** on `acquire_token_by_device_flow()` — it polls until success/timeout
- Default timeout is ~15 minutes (900 seconds)
- Each code is single-use; if it expires, call `initiate_device_flow()` again
- The flow requires network access to `login.microsoftonline.com`
- If MFA is enabled for the user, the MFA challenge happens in the browser, not the CLI

---

## Conditional Access & Policies

### Conditional Access for Workload Identities

Conditional Access policies can now be applied to **service principals** (including Agent IDs), providing Zero Trust controls for non-human identities.

#### Requirements
- **Entra Workload Identities Premium** license (~$3/workload identity/month)
- Policies must target service principals **directly** (not via group membership)

#### Available Controls

| Control | Description | Applicability |
|---------|-------------|---------------|
| **Location-based** | Block access unless from known IP ranges | ✅ Primary control |
| **Risk-based** | Block based on Entra ID Protection risk signals | ✅ When risk data available |
| **Authentication context** | Granular controls for sensitive operations | ✅ Advanced scenarios |
| **MFA** | Multi-factor authentication | ❌ Not applicable to workloads |
| **Device compliance** | Require compliant device | ❌ Not applicable to workloads |

#### Entraclaw Implications

- Agent IDs can be restricted to only authenticate from known networks
- Conditional Access can enforce that Entraclaw agents only operate from approved IP ranges
- Risk signals (anomalous sign-in patterns) can trigger automatic blocking
- Blueprint-level policies apply to **all** agent identities from that blueprint

### Implementing Conditional Access for Agents

1. Assign Entra Workload Identities Premium license
2. Create a Conditional Access policy targeting the agent's service principal
3. Define location conditions (trusted IP ranges)
4. Set grant controls (block or allow)
5. Test thoroughly — misconfiguration can break all agent operations

### Claims Challenges in OBO

When a Conditional Access policy triggers during OBO:

1. The token endpoint returns an `interaction_required` error with a `claims` field
2. The middle-tier must surface this to the client via `WWW-Authenticate` header (HTTP 401)
3. The client must re-authenticate with the claims challenge
4. MSAL's `acquire_token_on_behalf_of` accepts a `claims_challenge` parameter for this

```python
result = cca.acquire_token_on_behalf_of(
    user_assertion=incoming_token,
    scopes=downstream_scopes,
    claims_challenge=claims_from_error_response,  # Pass claims challenge
)
```

---

## Error Handling

### Error Response Structure

MSAL Python returns errors as dictionaries (not exceptions) from `acquire_token_*` methods:

```python
result = app.acquire_token_silent(scopes, account)

if "access_token" not in result:
    error = result.get("error")
    description = result.get("error_description")
    codes = result.get("error_codes", [])
    correlation_id = result.get("correlation_id")
    claims = result.get("claims_challenge")

    print(f"Error: {error}")
    print(f"Description: {description}")
    print(f"Codes: {codes}")
    print(f"Correlation ID: {correlation_id}")  # Useful for support tickets
```

### Common AADSTS Error Reference

| Error Code | Name | Cause | Recovery |
|-----------|------|-------|----------|
| `AADSTS50076` | MFA Required | Conditional Access requires MFA, but current flow doesn't support it | Switch to interactive or device code flow that supports MFA prompts |
| `AADSTS50079` | MFA Enrollment Required | User must enroll in MFA | Direct user to enroll at aka.ms/mfasetup, then retry |
| `AADSTS50058` | Silent Sign-in Failed | No active user session found | Fall back to interactive authentication |
| `AADSTS50105` | User Not Assigned | User exists but not assigned to the app | Admin must assign user/group to the app in Entra |
| `AADSTS50011` | Redirect URI Mismatch | Reply URL in code doesn't match app registration | Fix redirect URI in portal or code |
| `AADSTS65001` | Consent Not Granted | User/admin hasn't consented to required permissions | Trigger interactive consent flow, or admin grants consent |
| `AADSTS700016` | App Not Found | Application not found in the directory | Verify client_id and tenant_id are correct |
| `AADSTS700024` | Client Assertion Time Error | JWT assertion timestamp is invalid (clock skew) | Sync system clock; regenerate assertion |
| `AADSTS7000215` | Invalid Client Secret | Client secret doesn't match what's registered | Rotate and update secret in both portal and code |
| `AADSTS7000222` | Client Secret Expired | Client secret has expired | Generate new secret in portal; update code |
| `AADSTS90002` | Tenant Not Found | Tenant ID/name is invalid or doesn't exist | Verify tenant identifier |
| `AADSTS530003` | Blocked by CA Policy | Conditional Access policy blocks this sign-in | Review and adjust CA policy, or authenticate from allowed location/device |
| `interaction_required` | User Interaction Needed | Silent auth failed; user must re-authenticate | Catch this and fall back to interactive auth |
| `invalid_grant` | Token Exchange Failed | OBO assertion invalid, expired, or wrong audience | Verify incoming token's `aud` matches your app; check consent |

### Error Handling Pattern for Entraclaw

```python
def acquire_token_with_retry(app, scopes, account=None, max_retries=2):
    """Acquire token with structured error handling."""
    for attempt in range(max_retries + 1):
        if account:
            result = app.acquire_token_silent(scopes, account=account)
        else:
            result = None

        if result and "access_token" in result:
            return result

        # Analyze error
        error = result.get("error", "") if result else ""
        error_codes = result.get("error_codes", []) if result else []

        # Transient errors — retry
        if any(code in error_codes for code in []):
            if attempt < max_retries:
                import time
                time.sleep(2 ** attempt)  # exponential backoff
                continue

        # Consent required — needs user interaction
        if 65001 in error_codes or error == "interaction_required":
            return {"error": "consent_required", "action": "interactive_auth"}

        # MFA required — needs interactive flow
        if any(code in error_codes for code in [50076, 50079]):
            return {"error": "mfa_required", "action": "device_code_flow"}

        # Invalid credentials — configuration error
        if any(code in error_codes for code in [7000215, 7000222, 700016]):
            return {"error": "config_error", "action": "check_app_registration"}

        # Unrecoverable
        return result

    return {"error": "max_retries_exceeded"}
```

### Diagnostic Tools

- **Error lookup portal:** https://login.microsoftonline.com/error — enter AADSTS code for details
- **Entra sign-in logs:** Microsoft Entra admin center → Sign-in logs → filter by correlation ID
- **Token decoder:** https://jwt.ms — paste a token to inspect claims (never use production tokens!)

---

## Community Learnings & Gotchas

### OBO Flow Pitfalls

1. **Audience mismatch is the #1 cause of OBO failures.** The incoming token's `aud` must exactly match the middle-tier app's client ID. If the frontend requests a token for `https://graph.microsoft.com` instead of your API, OBO will fail with `invalid_grant`.

2. **Every API hop needs its own app registration.** Frontend App → Middle-Tier API → Downstream API. Each needs a separate registration. The middle-tier must "Expose an API" with at least one custom scope (e.g., `access_as_user`).

3. **Admin consent is required for downstream permissions.** The middle-tier app needs admin-consented delegated permissions for the downstream API. Without this, you get cryptic "insufficient permissions" errors.

4. **OBO tokens cannot chain infinitely.** Microsoft limits the depth of OBO chains. In practice, keep it to 2-3 hops maximum.

5. **OBO is ONLY for delegated (user) tokens.** If you have an app-only token (from client credentials flow), you cannot use OBO. Use client credentials to call the downstream API directly.

### Token Cache Issues

6. **Multi-process cache corruption.** If running multiple agent instances (e.g., behind Gunicorn), in-memory caches will desync. Use `msal-extensions` with file locking, or a centralized cache (Redis).

7. **Cache file format is JSON.** The serialized cache is plain JSON. On shared systems, ensure proper file permissions (600) to prevent token theft.

8. **`has_state_changed` must be checked.** Only write the cache when `cache.has_state_changed` is True. Writing on every call wastes I/O and can cause lock contention.

### MSAL Python Quirks

9. **MSAL returns dicts, not exceptions.** Unlike many auth libraries, MSAL Python returns error information in the result dictionary rather than raising exceptions. Always check for `"access_token"` in the result.

10. **Scopes use a list, not a string.** `scopes=["User.Read"]` not `scopes="User.Read"`. Passing a string will silently break scope parsing.

11. **The `.default` scope.** For client credentials and some OBO scenarios, use `api://app-id/.default` to request all statically configured permissions. Do not mix `.default` with individual scopes.

12. **Authority URL matters.** Using `https://login.microsoftonline.com/common` works for multi-tenant apps but will fail if you need tenant-specific policies. Use `/{tenant-id}` for single-tenant scenarios.

### Agent ID Considerations

13. **Agent IDs are in preview.** APIs are beta-only and may change. Don't build production dependencies on current API shapes without a migration plan.

14. **Agent IDs are single-tenant.** They can't access resources in other tenants. For multi-tenant Entraclaw deployments, each tenant needs its own blueprint and agent identities.

15. **Blueprint credentials control everything.** If the blueprint's credentials are compromised, ALL agent identities from that blueprint are compromised. Treat blueprint credentials with the same rigor as root certificates.

---

## Open Questions

### For Entraclaw Architecture

1. **Can we use Agent IDs with OBO?** When a human authenticates, can the Entraclaw agent (with its Agent ID) use OBO to call downstream APIs? Or does the Agent ID's blueprint credential perform its own separate token acquisition?

2. **Blueprint-per-tenant vs. shared blueprint?** For Entraclaw's multi-tenant model, should each customer tenant have its own blueprint, or can one blueprint span tenants (likely no, given single-tenant constraint)?

3. **Agent ID + Device Code Flow interaction:** Can a human bootstrap an Agent ID via device code flow? Or is device code strictly for the human's identity, with the agent identity being separate?

4. **Token cache isolation between agents:** If multiple Entraclaw agents run on the same machine, how do we isolate their token caches? Separate cache files? Separate keychains?

5. **Graceful degradation:** If a customer's tenant doesn't have Agent ID licensing, can Entraclaw fall back to standard service principals? What's the feature-detection mechanism?

6. **Refresh token behavior for OBO:** OBO access tokens have short lifetimes. Does MSAL cache the OBO refresh token? Can `acquire_token_silent` silently refresh an OBO token?

7. **Agent ID GA timeline:** When will Agent ID move from preview to GA? Should we build against the beta API now or wait?

8. **Rate limiting:** What are Microsoft Graph's rate limits for agent identity management APIs? Can we create/delete agent identities programmatically at scale?

9. **Cross-platform Entra SDK:** The Entra SDK for Agent Identities targets containerized environments. Does it work on bare-metal Mac/Linux/Windows (our target)?

10. **Conditional Access interaction:** If a CA policy blocks an Agent ID, does the error propagate cleanly through MSAL, or do we need to handle it at the Graph API level?

---

## Sources

### Official Microsoft Documentation
- [MSAL Python Documentation (Microsoft Learn)](https://learn.microsoft.com/en-us/entra/msal/python/) — Primary MSAL Python reference
- [MSAL Python API Reference — ConfidentialClientApplication](https://learn.microsoft.com/en-us/python/api/msal/msal.application.confidentialclientapplication?view=msal-py-latest) — Class reference with all methods
- [MSAL Python API Reference — PublicClientApplication](https://learn.microsoft.com/en-us/python/api/msal/msal.application.publicclientapplication?view=msal-py-latest) — Class reference for public client
- [OAuth 2.0 On-Behalf-Of Flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow) — Protocol specification and examples
- [Access Token Claims Reference](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference) — Complete claim definitions
- [AADSTS Error Codes Reference](https://learn.microsoft.com/en-us/entra/identity-platform/reference-error-codes) — All error codes with descriptions
- [Token Cache Serialization (MSAL Python)](https://learn.microsoft.com/en-us/entra/msal/python/advanced/msal-python-token-cache-serialization) — Cache persistence patterns
- [Conditional Access for Workload Identities](https://learn.microsoft.com/en-us/entra/identity/conditional-access/workload-identity) — Policy configuration for service principals
- [Claims Validation](https://learn.microsoft.com/en-us/entra/identity-platform/claims-validation) — How to validate token claims securely

### Entra Agent ID
- [Announcing Microsoft Entra Agent ID (Tech Community Blog)](https://techcommunity.microsoft.com/blog/microsoft-entra-blog/announcing-microsoft-entra-agent-id-secure-and-manage-your-ai-agents/3827392) — Official announcement from Build 2025
- [Overview of Agent Identities in Microsoft Entra](https://learn.microsoft.com/en-us/entra/agent-id/identity-platform/agent-identities) — Core concepts: blueprints, identities, sponsors
- [Agent Identity Blueprints](https://learn.microsoft.com/en-us/entra/agent-id/identity-platform/agent-blueprint) — Blueprint schema and configuration
- [Create an Agent Identity Blueprint](https://learn.microsoft.com/en-us/entra/agent-id/identity-platform/create-blueprint) — Step-by-step creation guide
- [Agent Identities, Service Principals, and Applications](https://learn.microsoft.com/en-us/entra/agent-id/identity-platform/agent-service-principals) — Comparison of identity types
- [Microsoft Entra Agent ID APIs (Graph Beta)](https://learn.microsoft.com/en-us/graph/api/resources/agentid-platform-overview?view=graph-rest-beta) — API reference for managing agent identities
- [Register Agents to the Agent Registry](https://learn.microsoft.com/en-us/entra/agent-id/identity-platform/publish-agents-to-registry) — Making agents discoverable
- [Microsoft Entra SDK for Agent Identities](https://github.com/MicrosoftDocs/entra-docs/blob/main/docs/agent-id/identity-platform/microsoft-entra-sdk-for-agent-identities.md) — SDK for containerized token acquisition

### Code Samples & Libraries
- [MSAL Python GitHub Repository](https://github.com/AzureAD/microsoft-authentication-library-for-python) — Source code, wiki, and issues
- [ms-identity-python-on-behalf-of (Azure Samples)](https://github.com/Azure-Samples/ms-identity-python-on-behalf-of) — End-to-end OBO sample with Django/Flask
- [ms-identity-python-devicecodeflow (Azure Samples)](https://github.com/Azure-Samples/ms-identity-python-devicecodeflow) — Device code flow sample
- [msal-extensions (PyPI)](https://pypi.org/project/msal-extensions/) — Persistent token cache with platform-native encryption
- [msal-extensions GitHub](https://github.com/AzureAD/microsoft-authentication-extensions-for-python) — Source for cache extension library

### Community & Blog Posts
- [Creating Entra Agent ID Blueprints with PowerShell (DEV.to)](https://dev.to/willvelida/creating-entra-agent-id-blueprints-and-identities-with-powershell-and-net-56pg) — Practical walkthrough of Agent ID creation
- [How to Create an Agent Identity with Microsoft Graph PowerShell](https://ourcloudnetwork.com/how-to-create-an-agent-identity-with-microsoft-graph-powershell/) — PowerShell-based guide
- [How Agent ID Secures AI Agents (LazyAdmin)](https://lazyadmin.nl/office-365/microsoft-entra-agent-id/) — Security-focused overview
- [Entra Agent ID: A New Era (Schneider.im)](https://www.schneider.im/microsoft-entra-agent-id-a-new-era-of-identity-for-ai-agents/) — Industry perspective
- [Exploring Entra Agent ID (EZCloudInfo)](https://ezcloudinfo.com/2025/12/07/exploring-microsoft-entra-agent-id-preview-identity-governance-zero-trust-for-ai-agents/) — Deep dive with governance focus
- [OBO Flow with Python and Entra ID (Zenn.dev)](https://zenn.dev/naokky/articles/202508-onbehalfof-flow?locale=en) — Minimal OBO implementation with diagrams
- [Stack Overflow: OBO Flow Failing with Downstream APIs](https://stackoverflow.com/questions/76411391/on-behalf-of-flow-failing-with-downstream-apis-using-microsoft-identity-web) — Common misconfiguration issues
- [Stack Overflow: Device Code Flow Authentication](https://stackoverflow.com/questions/77045877/device-code-flow-microsoft-azure-authentication) — Token refresh and silent auth patterns
- [OID vs SUB in Microsoft Identity Platform](https://0x8.in/blog/2021/04/30/mip-oid-sub/) — Understanding user identifiers in tokens
