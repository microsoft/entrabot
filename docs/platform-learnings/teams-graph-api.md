# Teams Graph API

> **Last updated:** 2025-07-17
> **Context:** Entraclaw identity research — autonomous agents communicating with humans via Microsoft Teams

## Overview

The Microsoft Graph API provides a comprehensive REST interface for interacting with Microsoft Teams programmatically. For Entraclaw, Graph API is the primary pathway for an autonomous agent to:

- **Send messages** (status updates, results, alerts) to a human operator in Teams
- **Receive commands** from the human via webhook-driven notifications
- **Set presence** to indicate the agent's operational status (Available, Busy, Away)
- **Create and manage chats** to establish dedicated agent↔human communication channels

The base URL for all endpoints is `https://graph.microsoft.com/v1.0` (stable) or `https://graph.microsoft.com/beta` (preview).

### Why This Matters for Entraclaw

Our agent architecture has agents running on Mac/Linux/Windows with Agent IDs, using OBO (On-Behalf-Of) token flows. The agent connects to Teams as an "Agent User" — a real Entra ID user account — for bidirectional communication. Understanding Graph API's capabilities, permissions model, and limitations is critical to designing this integration correctly.

---

## Key APIs / Interfaces

### 1. Chat API

The Chat API enables 1:1 and group chat creation and messaging.

#### Create a Chat

```
POST https://graph.microsoft.com/v1.0/chats
```

**Request body (1:1 chat):**
```json
{
  "chatType": "oneOnOne",
  "members": [
    {
      "@odata.type": "#microsoft.graph.aadUserConversationMember",
      "roles": ["owner"],
      "user@odata.bind": "https://graph.microsoft.com/v1.0/users('{agent-user-id}')"
    },
    {
      "@odata.type": "#microsoft.graph.aadUserConversationMember",
      "roles": ["owner"],
      "user@odata.bind": "https://graph.microsoft.com/v1.0/users('{human-user-id}')"
    }
  ]
}
```

**Permissions:** `Chat.Create` (delegated)

**Entraclaw note:** If a 1:1 chat between two users already exists, this endpoint returns the existing chat rather than creating a duplicate. This is idempotent and safe for agent startup flows.

#### Send a Message in a Chat

```
POST https://graph.microsoft.com/v1.0/chats/{chat-id}/messages
```

**Request body:**
```json
{
  "body": {
    "contentType": "html",
    "content": "<b>Agent Status:</b> Task XYZ completed successfully. <br/>Duration: 45s"
  }
}
```

**Permissions:** `ChatMessage.Send` (delegated only for normal use)

**⚠️ CRITICAL:** Application permissions CANNOT send regular chat messages. Only delegated permissions (user context) can send messages. This is the single most important constraint for Entraclaw — the agent MUST have a user identity and use delegated auth (OBO flow) to send messages.

#### List Messages in a Chat

```
GET https://graph.microsoft.com/v1.0/chats/{chat-id}/messages
```

**Permissions:**
- Delegated: `Chat.Read`, `Chat.ReadWrite`
- Application: `Chat.Read.All` (requires admin consent)

**Supports:** `$top`, `$filter`, `$orderby`, pagination via `@odata.nextLink`

#### Get a Specific Message

```
GET https://graph.microsoft.com/v1.0/chats/{chat-id}/messages/{message-id}
```

### 2. Channel Messaging API

For team-based communication (channels within a Team).

#### Send a Message to a Channel

```
POST https://graph.microsoft.com/v1.0/teams/{team-id}/channels/{channel-id}/messages
```

**Permissions:** `ChannelMessage.Send` (delegated)

#### Reply to a Channel Message

```
POST https://graph.microsoft.com/v1.0/teams/{team-id}/channels/{channel-id}/messages/{message-id}/replies
```

#### List Channel Messages

```
GET https://graph.microsoft.com/v1.0/teams/{team-id}/channels/{channel-id}/messages
```

**Permissions:**
- Delegated: `ChannelMessage.Read.All`
- Application: `ChannelMessage.Read.All` (admin consent required)

### 3. Presence API

The Presence API allows reading and setting a user's Teams presence status.

#### Get User Presence

```
GET https://graph.microsoft.com/v1.0/users/{userId}/presence
```

or for the signed-in user:

```
GET https://graph.microsoft.com/v1.0/me/presence
```

**Response:**
```json
{
  "id": "fa8bf3dc-eca7-46b7-bad1-db199b62afc3",
  "availability": "Available",
  "activity": "Available"
}
```

**Availability values:** `Available`, `AvailableIdle`, `Away`, `BeRightBack`, `Busy`, `BusyIdle`, `DoNotDisturb`, `Offline`, `PresenceUnknown`

**Activity values:** `Available`, `InACall`, `InAConferenceCall`, `Inactive`, `InAMeeting`, `Offline`, `OffWork`, `OutOfOffice`, `PresenceUnknown`, `Presenting`, `UrgentInterruptionsOnly`

**Permissions:**
- Delegated: `Presence.Read` (own), `Presence.Read.All` (others)
- Application: `Presence.Read.All`

#### Set User Presence (Agent Status)

```
POST https://graph.microsoft.com/v1.0/users/{userId}/presence/setPresence
```

**Request body:**
```json
{
  "sessionId": "{your-application-client-id}",
  "availability": "Busy",
  "activity": "InACall",
  "expirationDuration": "PT1H"
}
```

**Key details:**
- `sessionId` MUST be your app registration's client/application ID
- `expirationDuration` uses ISO 8601 format: min 5 minutes, max 4 hours
- The presence automatically reverts when the duration expires
- You must periodically re-set presence to keep it active
- Calendar-derived statuses ("In a meeting", "Out of office") cannot be overridden

**Permissions:** `Presence.ReadWrite.All` (application permission, admin consent required)

**Entraclaw pattern:** Agent sets presence to `Available` on startup, `Busy` when processing a task, and `Away` or `Offline` on shutdown. A background timer re-sets presence every 55 minutes to prevent expiration.

#### Clear Presence

```
POST https://graph.microsoft.com/v1.0/users/{userId}/presence/clearPresence
```

**Request body:**
```json
{
  "sessionId": "{your-application-client-id}"
}
```

### 4. Subscriptions / Webhooks API

For real-time notifications when new messages arrive.

#### Create a Subscription

```
POST https://graph.microsoft.com/v1.0/subscriptions
```

**Request body (subscribe to chat messages):**
```json
{
  "changeType": "created,updated",
  "notificationUrl": "https://your-agent-endpoint.example.com/api/webhook",
  "resource": "/chats/{chat-id}/messages",
  "expirationDateTime": "2025-07-17T15:00:00.000Z",
  "clientState": "secretClientValue",
  "lifecycleNotificationUrl": "https://your-agent-endpoint.example.com/api/lifecycle"
}
```

**Subscribable resources:**
| Resource | Path | Max Expiration |
|---|---|---|
| Chat messages (specific chat) | `/chats/{id}/messages` | **60 minutes** |
| Channel messages (specific channel) | `/teams/{id}/channels/{id}/messages` | **60 minutes** |
| All chat messages (tenant-wide) | `/chats/getAllMessages` | **60 minutes** |
| All channel messages (tenant-wide) | `/teams/getAllMessages` | **60 minutes** |
| Presence changes | `/communications/presences/{id}` | 60 minutes |

**Permissions for chat message subscriptions:**
- Delegated: `Chat.Read` or `Chat.ReadWrite`
- Application: `Chat.Read.All` (admin consent required)

**⚠️ CRITICAL — 60-minute max subscription lifetime:** Chat message subscriptions expire after at most 60 minutes. Your agent MUST implement automated renewal logic (PATCH the subscription every ~50 minutes). Failure to renew = silent message loss.

#### Renew a Subscription

```
PATCH https://graph.microsoft.com/v1.0/subscriptions/{subscription-id}
```

```json
{
  "expirationDateTime": "2025-07-17T16:00:00.000Z"
}
```

#### Webhook Endpoint Requirements

1. **HTTPS required** — your endpoint must be publicly accessible via HTTPS
2. **Validation handshake** — on subscription creation, Microsoft sends a validation token; respond with the token in the body within 10 seconds
3. **Fast response** — return HTTP 200/202 within 3 seconds; queue heavy processing
4. **Encrypted payloads** — if you include `encryptionCertificate`, message content arrives encrypted and must be decrypted
5. **Lifecycle notifications** — provide a `lifecycleNotificationUrl` to receive reauthorization and subscription-removal warnings

#### Webhook Notification Payload

```json
{
  "value": [
    {
      "subscriptionId": "subscription-id",
      "changeType": "created",
      "clientState": "secretClientValue",
      "resource": "chats/{chat-id}/messages/{message-id}",
      "resourceData": {
        "@odata.type": "#microsoft.graph.chatMessage",
        "@odata.id": "chats/{chat-id}/messages/{message-id}",
        "id": "{message-id}"
      },
      "tenantId": "tenant-id"
    }
  ]
}
```

**Note:** The notification typically contains only metadata. You may need to make a follow-up GET request to retrieve the full message content, unless you opt into receiving rich notifications with resource data (which requires encryption setup).

### 5. Activity Feed Notifications

An alternative to chat messages for sending notifications to a user's Teams activity feed.

```
POST https://graph.microsoft.com/v1.0/users/{userId}/teamwork/sendActivityNotification
```

This requires a Teams app manifest and is typically used for bots/apps installed in Teams, not for user-to-user messaging. Less relevant for the Entraclaw "Agent User" pattern but worth noting.

---

## Auth & Identity Model

### Permission Types

| Type | Context | Consent | Use Case |
|---|---|---|---|
| **Delegated** | Acts on behalf of a signed-in user | User or Admin | Send messages, read own chats, set own presence |
| **Application** | Acts as the app itself (no user) | Admin only | Read all messages (compliance), tenant-wide subscriptions |

### Key Permission Scopes for Entraclaw

| Permission | Type | Purpose | Admin Consent? |
|---|---|---|---|
| `Chat.Create` | Delegated | Create 1:1 or group chats | No |
| `Chat.Read` | Delegated | Read chats the user is in | No |
| `Chat.ReadWrite` | Delegated | Read/write chats | No |
| `ChatMessage.Send` | Delegated | Send messages in chats | Yes |
| `ChannelMessage.Send` | Delegated | Send messages in channels | No |
| `ChannelMessage.Read.All` | Delegated | Read channel messages | Yes |
| `Presence.Read` | Delegated | Read own presence | No |
| `Presence.Read.All` | Delegated/App | Read any user's presence | Yes (for app) |
| `Presence.ReadWrite.All` | Application | Set any user's presence | Yes |
| `Chat.Read.All` | Application | Read all chats (compliance) | Yes |
| `Chat.ReadWrite.All` | Application | Read/write all chats | Yes |
| `TeamsAppInstallation.ReadWriteSelfForUser.All` | Application | Install bot app for users | Yes |

### OBO (On-Behalf-Of) Flow for Entraclaw Agents

The OBO flow is the recommended pattern for Entraclaw's "Agent User" scenario:

1. **Agent authenticates as itself** to the Entraclaw identity service
2. **Entraclaw service** holds a user token (or refresh token) for the Agent User account
3. **OBO exchange:** The service exchanges the agent's token for a Microsoft Graph token scoped to the Agent User's delegated permissions
4. **Agent calls Graph API** with the resulting delegated token — messages appear as sent by the Agent User

**Architecture:**
```
Agent Process → Entraclaw Identity Service → (OBO Token Exchange) → Microsoft Graph API
                                                                          ↓
                                                               Teams Chat (as Agent User)
```

**Key insight:** With OBO + delegated permissions, the agent sends messages *as the Agent User*. The message appears in Teams as coming from that user account. This is the only way to send chat messages via Graph API without a bot framework.

### Can an Agent Send Messages "As Itself"?

**No, not via Graph API alone.** Graph API does not support application permissions for sending chat messages (except for data migration). To send messages "as" a distinct identity:

- **Option A (Recommended for Entraclaw):** Create a dedicated Entra ID user account (the "Agent User") and use OBO/delegated flow. Messages show the Agent User's display name.
- **Option B:** Build a Teams Bot using Bot Framework. The bot sends messages as the app, but requires Bot Framework SDK + Azure Bot Service, not just Graph API.
- **Option C:** Use a combination — Graph API for chat operations + Bot Framework for proactive messaging.

---

## Integration Patterns

### Pattern 1: Agent Sends Messages to a Human

**Flow:**
1. Agent obtains OBO token with `Chat.Create` + `ChatMessage.Send` scopes
2. `POST /chats` — create (or retrieve existing) 1:1 chat with the human
3. `POST /chats/{chat-id}/messages` — send status update / result

**Example — sending a rich status update:**
```json
POST https://graph.microsoft.com/v1.0/chats/{chat-id}/messages
Authorization: Bearer {obo-token}
Content-Type: application/json

{
  "body": {
    "contentType": "html",
    "content": "🤖 <b>Agent Task Complete</b><br/><br/>Task: <i>Data pipeline refresh</i><br/>Status: ✅ Success<br/>Duration: 2m 34s<br/>Records processed: 15,420"
  }
}
```

**Adaptive Card (rich formatting):**
```json
{
  "body": {
    "contentType": "html",
    "content": ""
  },
  "attachments": [
    {
      "id": "card1",
      "contentType": "application/vnd.microsoft.card.adaptive",
      "content": "{\"type\":\"AdaptiveCard\",\"version\":\"1.4\",\"body\":[{\"type\":\"TextBlock\",\"text\":\"Agent Status Report\",\"weight\":\"bolder\",\"size\":\"large\"},{\"type\":\"FactSet\",\"facts\":[{\"title\":\"Task\",\"value\":\"Data Pipeline\"},{\"title\":\"Status\",\"value\":\"Complete\"},{\"title\":\"Duration\",\"value\":\"2m 34s\"}]}]}"
    }
  ]
}
```

**Note on Adaptive Cards via Graph:** Graph supports sending messages with Adaptive Card attachments. However, only `Action.OpenUrl` is supported for actions — interactive actions like `Action.Submit` require a bot to handle the callback.

### Pattern 2: Agent Receives Commands from a Human

**Option A: Webhook Subscriptions (Recommended)**

1. Agent creates a subscription on `/chats/{chat-id}/messages` with `changeType: "created"`
2. Agent exposes an HTTPS webhook endpoint
3. When human sends a message, Microsoft Graph POSTs a notification
4. Agent fetches the full message via `GET /chats/{chat-id}/messages/{message-id}`
5. Agent parses the command and acts

**Renewal loop (required):**
```
Every 50 minutes:
  PATCH /subscriptions/{id}  →  extend expirationDateTime by 60 minutes
```

**Option B: Polling (Fallback)**

Use delta queries if webhooks aren't feasible (e.g., agent behind a firewall):

```
GET https://graph.microsoft.com/v1.0/chats/{chat-id}/messages/delta
```

**⚠️ Warning:** Microsoft's Terms of Use restrict polling to once per day for most Teams resources. Delta queries for chat messages are the exception but should still be used sparingly. Polling at high frequency risks throttling or account suspension.

### Pattern 3: Agent Sets Its Own Presence

1. On startup: Set presence to `Available`
2. On task start: Set to `Busy` / `InACall`
3. On idle: Set to `Away`
4. On shutdown: `clearPresence` or set to `Offline`

```
POST /users/{agent-user-id}/presence/setPresence
{
  "sessionId": "{app-client-id}",
  "availability": "Available",
  "activity": "Available",
  "expirationDuration": "PT1H"
}
```

**Background renewal:** Re-set presence every ~55 minutes to prevent expiration.

### Pattern 4: Subscribe to Presence Changes

Monitor whether the human operator is available before sending non-urgent updates:

```
POST /subscriptions
{
  "changeType": "updated",
  "notificationUrl": "https://agent.example.com/api/presence-webhook",
  "resource": "/communications/presences/{human-user-id}",
  "expirationDateTime": "2025-07-17T16:00:00Z"
}
```

---

## Rate Limits & Throttling

### Global Limits

| Scope | Limit |
|---|---|
| Per app across all tenants | 130,000 requests / 10 seconds |

### Teams-Specific Limits

| Resource / Operation | Limit |
|---|---|
| **Chat/Channel messages** — send to channel | ~3,000 messages / app / day / channel (observed) |
| **Chat/Channel messages** — general throughput | ~10 messages / 10 seconds (per thread) |
| **Presence API** | 10,000 requests / 30 seconds / app / tenant |
| **Calls (Cloud Communication)** | 50,000 requests / 15 seconds / app / tenant |
| **Call Records** | 1,500 requests / 20 seconds / app / tenant |
| **Virtual Events** | 750 GET requests / 30 seconds / app (all tenants) |
| **Meetings** | 2,000 meetings scheduled / user / month |
| **Subscription management** (POST/PATCH/DELETE) | 500 requests / 20 seconds / app / tenant |

### Throttling Response

When throttled, the API returns:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 30
```

**Best practices:**
1. Always check for and respect the `Retry-After` header
2. Implement exponential backoff with jitter
3. Cache responses where possible (e.g., chat IDs, user IDs)
4. Use `$select` to reduce response payload size
5. Batch requests using `POST /$batch` where applicable (but note: batch requests can still trigger per-resource throttling)
6. The `Retry-After` header is NOT always present — implement fallback backoff

### Entraclaw Implications

For a single agent sending periodic status updates (a few messages per hour), throttling is unlikely. However:
- Multiple agents per tenant could hit per-tenant limits
- Subscription renewal every 60 minutes adds baseline load
- Presence re-set every hour adds baseline load
- Plan for ~5-10 API calls per agent per hour at minimum

---

## Known API Gaps

### What Teams UI Can Do That Graph API Cannot

| Capability | Teams UI | Graph API |
|---|---|---|
| **Send messages as an app** (non-user) | Bots can do this | ❌ Requires Bot Framework, not Graph alone |
| **Interactive Adaptive Card actions** (Action.Submit) | Full support | ❌ Only `Action.OpenUrl` supported via Graph |
| **Edit sent messages** | ✅ | ✅ (PATCH on chatMessage, delegated only) |
| **Delete messages** | ✅ | ⚠️ Soft-delete only, limited scenarios |
| **Reactions** | Full emoji set | ⚠️ Can list reactions; adding via API is limited |
| **Message read receipts** | ✅ Shows "Seen by" | ⚠️ Beta only (`/readReceipts`) |
| **Urgent/important messages** | Priority flag | ❌ Not exposed in standard Graph; requires workaround |
| **Pin messages** | ✅ | ⚠️ Beta only |
| **Forward messages** | ✅ | ❌ No direct API; must copy content to new message |
| **Schedule messages** | ✅ (delayed send) | ❌ Not supported |
| **Meeting live events** | ✅ | ⚠️ Deprecated (Live Events API retired Sep 2024) |
| **Channel ordering/pinning** | ✅ | ❌ Not exposed |
| **Full message history export** | ✅ Scroll back | ⚠️ Paginated, may age out older messages |
| **Status message** (custom text) | ✅ "In a meeting until 3pm" | ⚠️ `statusMessage` property exists but limited |
| **Calendar-based presence** (In a meeting) | Automatic | ❌ Cannot set programmatically |

### Beta-Only APIs (Not Yet GA)

These are available under `/beta` but not stable for production:
- Read receipts for chat messages
- Pinned messages
- Some presence subscription scenarios
- Enhanced activity notifications

---

## Community Learnings & Gotchas

### 1. "Application permissions can't send chat messages" (Most Common Trap)

**The #1 surprise for developers:** You cannot use client_credentials (application-only) flow to send chat messages. `ChatMessage.Send` requires delegated permissions. This is explicitly by design — Microsoft considers message authorship tied to user identity.

**Workarounds:**
- Use OBO flow with a service account (Entraclaw's approach)
- Use Bot Framework for app-identity messaging
- For channels only: some limited app-permission sending exists but is throttled to migration rates

> *Source: Multiple Stack Overflow threads, Microsoft Q&A*

### 2. Webhook Subscriptions Are Unreliable

Community reports frequent issues:
- Subscriptions appear active but notifications silently stop arriving
- The 60-minute expiration means any renewal failure = immediate message loss
- Lifecycle notifications (`lifecycleNotificationUrl`) can warn you of issues but add complexity
- Some notifications arrive out of order or with significant delay (seconds to minutes)

**Mitigation:** Implement a hybrid approach — webhooks as primary, with periodic delta-query polling as a safety net to catch missed messages.

> *Source: Microsoft Q&A issue reports, Reddit r/MicrosoftTeams*

### 3. Permission Consent Is the Leading Cause of Production Failures

Per community analysis, **>60% of Graph API issues in production are permissions-related**, not code bugs:
- Permissions that work in dev tenant fail in production (missing admin consent)
- Delegated vs application permission confusion
- Tenant admin policies can block specific Graph permissions
- Conditional Access policies may block automated sign-in flows

**Mitigation:** Test with a production-like tenant early. Document all required admin consents. Build a permissions verification endpoint.

> *Source: dev.to, Stack Overflow*

### 4. Rate Limits Are More Aggressive Than Documented

- Batch API calls can trigger HTTP 429 even when individual request rates seem within limits
- Per-thread (per-chat, per-channel) limits are undocumented but exist
- The `Retry-After` header is sometimes missing from 429 responses
- Throttling behavior can vary by tenant size and Microsoft's backend load

> *Source: Stack Overflow, MSEndpointMgr.com*

### 5. Message Content HTML Is Severely Limited

Teams Graph API only supports a small subset of HTML:
- Supported: `<b>`, `<i>`, `<em>`, `<strong>`, `<a>`, `<br>`, `<p>`, `<ul>`, `<ol>`, `<li>`, `<h1>`-`<h3>`, `<pre>`, `<code>`, `<blockquote>`, `<img>` (hosted content)
- NOT supported: `<table>`, `<div>`, `<span>` with styles, custom CSS, `<iframe>`
- For rich formatting, use Adaptive Cards instead

> *Source: Microsoft Learn documentation*

### 6. Unannounced Deprecations Happen

Microsoft retired the Teams Live Events API in September 2024 with incomplete replacement APIs (Town Hall). Developers relying on these APIs had to scramble.

**Mitigation:** Subscribe to the [Microsoft Graph changelog](https://developer.microsoft.com/en-us/graph/changelog) and [Microsoft 365 Developer Blog](https://devblogs.microsoft.com/microsoft365dev/).

### 7. Billing Changes for Metered APIs

As of late 2025, Microsoft ceased charging for some previously "metered" Teams Graph APIs (exports, transcripts). These had required separate Azure billing subscriptions. The billing model for Teams Graph APIs is still evolving.

### 8. SharePoint Limits Affect Teams Files

Files shared in Teams are stored in SharePoint. SharePoint's rate limits and storage quotas apply independently and can cause failures when working with Teams file attachments.

---

## Open Questions for Entraclaw

### Critical Path Questions

1. **Agent User Licensing:** Does the Agent User account need a Microsoft 365 / Teams license? (Almost certainly yes for sending messages — unlicensed accounts cannot use Teams.)

2. **OBO Token Lifetime:** How long do OBO-obtained tokens last? Can we use refresh tokens to maintain long-lived sessions, or must the Agent User re-authenticate periodically?

3. **Webhook Endpoint for Headless Agents:** Agents on Mac/Linux may not have a public HTTPS endpoint. Options:
   - Use Azure Relay or ngrok-like tunnel
   - Use Azure Event Grid with Graph change notifications
   - Fall back to delta-query polling
   - Run a shared webhook receiver service that dispatches to agents

4. **Multi-Agent Fan-Out:** If 50 agents share one tenant, do their API calls aggregate against per-tenant limits? (Yes — this needs capacity planning.)

5. **Agent Display Name:** Can we set the Agent User's display name to something like "🤖 Entraclaw Agent — Pipeline Runner" so humans easily distinguish agents from people?

6. **Conditional Access:** Will tenant Conditional Access policies (MFA, device compliance, IP restrictions) block the OBO flow for agent accounts? May need CA policy exclusions for agent accounts.

### Design Decision Questions

7. **Chat vs Channel:** Should agents communicate via 1:1 chats (private, focused) or a dedicated channel (visible to team, searchable)? Recommendation: 1:1 chats for commands, channel for broadcast status.

8. **Bot Framework Hybrid:** Should we use Bot Framework for proactive messaging and Graph API for everything else? This adds complexity but gives app-identity messaging and interactive cards.

9. **Presence Semantics:** What presence states map to agent states?
   - `Available` → Agent idle, ready for commands
   - `Busy` → Agent executing a task
   - `Away` → Agent paused / low priority
   - `DoNotDisturb` → Agent in critical section
   - `Offline` → Agent shut down

10. **Message Format Standard:** Should agents use a structured message format (e.g., always start with an emoji + bold header) so humans can quickly scan agent messages?

---

## Appendix: Complete API Endpoint Reference

### Chat Operations

| Operation | Method | Endpoint | Delegated Perm | App Perm |
|---|---|---|---|---|
| Create chat | POST | `/chats` | `Chat.Create` | `Chat.Create` (beta) |
| Get chat | GET | `/chats/{id}` | `Chat.Read` | `Chat.Read.All` |
| List user's chats | GET | `/me/chats` | `Chat.Read` | — |
| Update chat | PATCH | `/chats/{id}` | `Chat.ReadWrite` | `Chat.ReadWrite.All` |
| List members | GET | `/chats/{id}/members` | `ChatMember.Read` | `ChatMember.Read.All` |
| Add member | POST | `/chats/{id}/members` | `ChatMember.ReadWrite` | `ChatMember.ReadWrite.All` |

### Message Operations

| Operation | Method | Endpoint | Delegated Perm | App Perm |
|---|---|---|---|---|
| Send chat message | POST | `/chats/{id}/messages` | `ChatMessage.Send` | ❌ (migration only) |
| Send channel message | POST | `/teams/{id}/channels/{id}/messages` | `ChannelMessage.Send` | ❌ (migration only) |
| Reply to channel msg | POST | `/teams/{id}/channels/{id}/messages/{id}/replies` | `ChannelMessage.Send` | ❌ |
| List chat messages | GET | `/chats/{id}/messages` | `Chat.Read` | `Chat.Read.All` |
| List channel messages | GET | `/teams/{id}/channels/{id}/messages` | `ChannelMessage.Read.All` | `ChannelMessage.Read.All` |
| Get message | GET | `/chats/{id}/messages/{id}` | `Chat.Read` | `Chat.Read.All` |
| Update message | PATCH | `/chats/{id}/messages/{id}` | `Chat.ReadWrite` | ❌ |
| Delta (new messages) | GET | `/chats/{id}/messages/delta` | `Chat.Read` | `Chat.Read.All` |

### Presence Operations

| Operation | Method | Endpoint | Delegated Perm | App Perm |
|---|---|---|---|---|
| Get my presence | GET | `/me/presence` | `Presence.Read` | — |
| Get user presence | GET | `/users/{id}/presence` | `Presence.Read.All` | `Presence.Read.All` |
| Set presence | POST | `/users/{id}/presence/setPresence` | — | `Presence.ReadWrite.All` |
| Clear presence | POST | `/users/{id}/presence/clearPresence` | — | `Presence.ReadWrite.All` |
| Get multiple users | POST | `/communications/getPresencesByUserId` | `Presence.Read.All` | `Presence.Read.All` |

### Subscription Operations

| Operation | Method | Endpoint | Delegated Perm | App Perm |
|---|---|---|---|---|
| Create subscription | POST | `/subscriptions` | Per resource | Per resource |
| Get subscription | GET | `/subscriptions/{id}` | Per resource | Per resource |
| Update subscription | PATCH | `/subscriptions/{id}` | Per resource | Per resource |
| Delete subscription | DELETE | `/subscriptions/{id}` | Per resource | Per resource |
| List subscriptions | GET | `/subscriptions` | Per resource | Per resource |

---

## Sources

1. **[Teams API Overview — Microsoft Learn](https://learn.microsoft.com/en-us/graph/api/resources/teams-api-overview?view=graph-rest-1.0)**
   Canonical reference for all Teams Graph API capabilities, resources, and use cases.

2. **[Teams Messaging APIs Overview — Microsoft Learn](https://learn.microsoft.com/en-us/graph/teams-messaging-overview)**
   Detailed chatMessage schema, attachment types (cards, files, Loop components), and messaging patterns.

3. **[Send chatMessage — Microsoft Learn](https://learn.microsoft.com/en-us/graph/api/chatmessage-post?view=graph-rest-1.0)**
   Endpoint reference for sending messages to chats and channels, including permission requirements.

4. **[Create Chat — Microsoft Learn](https://learn.microsoft.com/en-us/graph/api/chat-post?view=graph-rest-1.0)**
   Creating 1:1 and group chats programmatically.

5. **[Presence: setPresence — Microsoft Learn](https://learn.microsoft.com/en-us/graph/api/presence-setpresence?view=graph-rest-1.0)**
   Setting user presence programmatically with session IDs and expiration.

6. **[Manage Presence State — Microsoft Learn](https://learn.microsoft.com/en-us/graph/cloud-communications-manage-presence-state)**
   Conceptual guide to presence state management, session model, and aggregation.

7. **[Change Notifications via Webhooks — Microsoft Learn](https://learn.microsoft.com/en-us/graph/change-notifications-delivery-webhooks)**
   Webhook setup, validation handshake, and notification payload format.

8. **[Change Notifications for Teams — Microsoft Learn](https://learn.microsoft.com/en-us/graph/teams-change-notification-in-microsoft-teams-overview)**
   Teams-specific subscription resources, supported change types, and encrypted payloads.

9. **[Subscription Resource Type — Microsoft Learn](https://learn.microsoft.com/en-us/graph/api/resources/subscription?view=graph-rest-1.0)**
   Maximum expiration durations per resource type (60 minutes for chatMessage).

10. **[Throttling Limits — Microsoft Learn](https://learn.microsoft.com/en-us/graph/throttling-limits)**
    Service-specific rate limits including Teams, Presence, Cloud Communications.

11. **[Throttling Guidance — Microsoft Learn](https://learn.microsoft.com/en-us/graph/throttling)**
    Best practices for handling 429 responses, Retry-After headers, and exponential backoff.

12. **[Microsoft Graph Permissions Overview — Microsoft Learn](https://learn.microsoft.com/en-us/graph/permissions-overview)**
    Delegated vs application permissions model, consent types.

13. **[OBO Flow — Microsoft Learn](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow)**
    On-Behalf-Of OAuth 2.0 flow for middle-tier services calling Graph API.

14. **[Proactive Bot Messaging — Microsoft Learn](https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/proactive-bots-and-messages/graph-proactive-bots-and-messages)**
    Using Graph API to install bot apps and enable proactive messaging.

15. **[Microsoft Q&A: Webhook Subscription Interruptions](https://learn.microsoft.com/en-us/answers/questions/2101043/frequent-interruptions-in-graph-api-subscriptions)**
    Community reports of unreliable webhook delivery for Teams chat subscriptions.

16. **[Stack Overflow: Graph Teams REST APIs](https://stackoverflow.com/questions/42728492/microsoft-teams-rest-apis)**
    Long-running community thread on common Graph Teams API issues and limitations.

17. **[Stack Overflow: Send chatMessage from app](https://stackoverflow.com/questions/78621639/send-a-chatmessage-from-an-app-to-a-user-using-graphapi)**
    Discussion of application permission limitations for sending messages.

18. **[Permissions Gotchas — dev.to](https://dev.to/howdataworks/why-microsoft-graph-permissions-keep-tripping-you-up-and-how-to-outsmart-the-consent-maze-44ae)**
    Deep dive into why Graph permissions cause 60%+ of production failures.

19. **[Microsoft Ends Metered API Charges — Empowering Cloud](https://empowering.cloud/microsoft-ends-charges-for-select-teams-metered-graph-apis/)**
    Billing model changes for Teams Graph API metered endpoints.

20. **[Graph Permissions Reference — Microsoft Learn](https://learn.microsoft.com/en-us/graph/permissions-reference)**
    Complete listing of all Microsoft Graph permission scopes.
