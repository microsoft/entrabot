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

### 2. Add API Permissions

Go to API permissions → Add a permission → Microsoft Graph → Delegated permissions:

| Permission | Why |
|------------|-----|
| `User.Read` | Read the signed-in user's profile |
| `Chat.Create` | Create 1:1 chats between agent and human |
| `Chat.ReadWrite` | Read and send messages in chats |
| `ChatMessage.Send` | Send messages in chats |
| `Presence.ReadWrite` | Set and read presence status |

Then click **Grant admin consent for [tenant]**.

### 3. Create Client Secret

Go to Certificates & secrets → New client secret:

| Field | Value |
|-------|-------|
| Description | `Openclaw MVP` |
| Expires | 6 months (for dev) |

**Copy the secret value immediately** — you won't see it again.

### 4. Note the IDs

You'll need these in your code and MCP server config:

| Value | Where to Find |
|-------|---------------|
| **Application (client) ID** | App registration → Overview |
| **Directory (tenant) ID** | App registration → Overview |
| **Client secret** | Certificates & secrets (copied above) |
| **Object ID** | App registration → Overview (for Agent ID registration) |

### 5. Register Agent ID Blueprint

```http
POST https://graph.microsoft.com/v1.0/agentIdentityBlueprints
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
python -c "
from msal import PublicClientApplication
app = PublicClientApplication('<client-id>', authority='https://login.microsoftonline.com/<tenant-id>')
flow = app.initiate_device_flow(scopes=['User.Read'])
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
