# Next: Tenant & Identity Setup

> Everything that needs to happen in Entra and M365 before the code works.

## Checklist

- [ ] M365 license assigned to your user (E3, E5, or Business Basic — needs Teams)
- [ ] Teams enabled in tenant admin settings
- [ ] Entra app registration created for Openclaw agent
- [ ] Graph API permissions granted + admin consent
- [ ] Client secret (or certificate) generated for the app
- [ ] Agent ID blueprint registered via Entra GA API
- [ ] Optional: Agent User account created with M365 license (for distinct Teams identity)

## Entra App Registration — Step by Step

### 1. Create the App

Portal: https://entra.microsoft.com → App registrations → New registration

| Field | Value |
|-------|-------|
| Name | `Openclaw Agent` |
| Supported account types | Accounts in this organizational directory only |
| Redirect URI | (leave blank for now — device code flow doesn't need one) |

### 2. Expose an API (required for OBO)

Go to App registration → Expose an API:

1. Click "Set" next to Application ID URI → accept the default `api://<client-id>`
2. Click "Add a scope":
   - Scope name: `access_as_user`
   - Who can consent: Admins and users
   - Admin consent display name: "Access Openclaw as user"
   - Admin consent description: "Allows the Openclaw agent to act on behalf of the signed-in user"
   - State: Enabled

This creates the scope `api://<client-id>/access_as_user`. The device code flow must request THIS scope (not Graph scopes directly) so the resulting token has `aud=<client-id>`, which is required for the OBO exchange.

### 3. Add API Permissions

Go to API permissions → Add a permission → Microsoft Graph → Delegated permissions:

| Permission | Why |
|------------|-----|
| `User.Read` | Read the signed-in user's profile |
| `Chat.Create` | Create 1:1 chats between agent and human |
| `Chat.ReadWrite` | Read and send messages in chats |
| `ChatMessage.Send` | Send messages in chats |
| `Presence.ReadWrite` | Set and read presence status |

Then click **Grant admin consent for [tenant]**.

### 4. Create Client Secret

Go to Certificates & secrets → New client secret:

| Field | Value |
|-------|-------|
| Description | `Openclaw MVP` |
| Expires | 6 months (for dev) |

**Copy the secret value immediately** — you won't see it again.

### 5. Note the IDs

You'll need these in your code and MCP server config:

| Value | Where to Find |
|-------|---------------|
| **Application (client) ID** | App registration → Overview |
| **Directory (tenant) ID** | App registration → Overview |
| **Client secret** | Certificates & secrets (copied above) |
| **Object ID** | App registration → Overview (for Agent ID registration) |

### 6. Register Agent ID Blueprint

> ⚠️ **Agent IDs require the beta API** (`/beta/agentIdentityBlueprints`, not `/v1.0`).
> Verify your tenant has Frontier/Workload Identities Premium licensing before proceeding.
> If Agent IDs aren't available, skip this step — the OBO flow still works without Agent IDs
> (the `azp` claim still identifies the agent app in sign-in logs).

```http
POST https://graph.microsoft.com/beta/agentIdentityBlueprints
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "displayName": "Openclaw Code Agent",
  "description": "Autonomous coding agent with OBO identity and Teams integration",
  "appId": "<application-client-id>"
}
```

Save the blueprint ID from the response — you'll use it to create agent instances.

## Verify Setup

Quick smoke test from the command line:

```bash
# Get a token using device code flow
# NOTE: Request YOUR APP's custom scope, not Graph scopes directly.
# This ensures aud=<your-client-id>, which is required for OBO exchange.
python -c "
from msal import PublicClientApplication
app = PublicClientApplication('<client-id>', authority='https://login.microsoftonline.com/<tenant-id>')
flow = app.initiate_device_flow(scopes=['api://<client-id>/access_as_user'])
print(f\"Go to {flow['verification_uri']} and enter code {flow['user_code']}\")
result = app.acquire_token_by_device_flow(flow)
if 'access_token' in result:
    print('SUCCESS — got token')
    print(f\"User: {result.get('id_token_claims', {}).get('preferred_username', 'unknown')}\")
else:
    print(f\"FAILED: {result.get('error_description', 'unknown error')}\")
"
```

If this prints "SUCCESS," your app registration and permissions are correct.

## M365 / Teams Verification

```bash
# After getting a token with Chat.Create scope, verify Teams works:
curl -s -H "Authorization: Bearer <token>" \
  "https://graph.microsoft.com/v1.0/me/chats?$top=5" | python -m json.tool
```

If this returns a JSON list of chats (even empty), Teams Graph API is working.
