# Teams Graph API

Entrabot talks to Microsoft Teams through Microsoft Graph, acting as its Agent
User. This page is the platform reference for the chat, message, member, and
presence surfaces Entrabot uses, and the Graph behaviors that shape how those
calls must be made.

The base URL for all endpoints is `https://graph.microsoft.com/v1.0` (stable) or
`https://graph.microsoft.com/beta` (preview).

## Identity model

Teams chat messages cannot be sent with application (app-only) permissions.
`ChatMessage.Send` requires a delegated token, because Microsoft ties message
authorship to a user identity.

Entrabot satisfies this in two ways depending on mode:

- **Agent User mode** — the delegated token comes from the autonomous three-hop
  `user_fic` flow. Its `idtyp=user` means Graph attributes every message to the
  Agent User. Messages appear in Teams as sent by the agent's own account.
- **Delegated mode** — the token belongs to the signed-in human. Graph attributes
  messages to the human, so Entrabot prefixes outbound messages with `[EntraBot]`
  to distinguish agent-sent messages. See
  [Delegated Authentication with MSAL](delegated-auth.md).

There is no default group chat. Every Teams operation requires an explicit
`chat_id`; where it comes from is covered under
[Message delivery](#message-delivery).

## Chat operations

### Create a chat

```http
POST https://graph.microsoft.com/v1.0/chats
```

1:1 request body:

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

**Permission:** `Chat.Create` (delegated).

**Idempotency differs by chat type:**

- **1:1 (`oneOnOne`) is idempotent.** If a 1:1 chat between the same two users
  already exists, `POST /chats` returns the existing chat rather than creating a
  duplicate. This is safe to call on every agent startup.
- **Group (`group`) is not idempotent.** Creating a group chat with the same
  members produces a new, distinct chat each time. Track and reuse group
  `chat_id`s rather than re-creating them.

### Cross-tenant (federated) membership

To add a user from a different home tenant as an owner, include their home
tenant GUID as `tenantId` and reference the user by email in `user@odata.bind`
so Graph can resolve the federated identity:

```json
{
  "@odata.type": "#microsoft.graph.aadUserConversationMember",
  "roles": ["owner"],
  "user@odata.bind": "https://graph.microsoft.com/v1.0/users('user@partner.com')",
  "tenantId": "{partner-home-tenant-guid}"
}
```

For a B2B guest, the member is added by email plus their home `tenantId`,
which resolves their real identity rather than the guest object, with role
`owner`. Whether the resulting chat is `oneOnOne` or `group` depends on the
number of humans being added, not on guest status: adding a single guest
produces a `oneOnOne` chat, while adding multiple humans (guest or otherwise)
produces a `group` chat.

### Send a message

```http
POST https://graph.microsoft.com/v1.0/chats/{chat-id}/messages

{
  "body": {
    "contentType": "html",
    "content": "<b>Status:</b> task complete."
  }
}
```

**Permission:** `ChatMessage.Send` (delegated; app-only cannot send).

### List and get messages

```http
GET https://graph.microsoft.com/v1.0/chats/{chat-id}/messages
GET https://graph.microsoft.com/v1.0/chats/{chat-id}/messages/{message-id}
```

**Permissions:** delegated `Chat.Read` or `Chat.ReadWrite`; application
`Chat.Read.All` (admin consent).

### Members

```http
GET  https://graph.microsoft.com/v1.0/chats/{chat-id}/members
POST https://graph.microsoft.com/v1.0/chats/{chat-id}/members
```

**Permissions:** delegated `ChatMember.Read` / `ChatMember.ReadWrite`.

## Query behavior and client-side filtering

Chat-message queries have load-bearing constraints that must be handled in
client code:

- **`$filter` is unreliable for chat messages.** Filtering messages by identity
  through Graph `$filter` does not behave reliably. Fetch messages and filter
  **client-side** instead — matching on the sender's UPN (falling back to the
  sender's object ID), never on the mutable display name.
- **`/me/chats` rejects `$orderby`.** The chat-discovery endpoint returns HTTP
  400 when `$orderby` is supplied. Fetch without it and sort client-side if
  ordering is needed.
- **Deduplication is client-side.** Telling human messages apart from the
  agent's own, and from already-seen messages, is done in client code by
  matching canonical sender identity (`sender_upn`/`sender_id` against the
  agent's own identity), tracking in-process sent-message IDs, and persisting
  a per-chat cursor of the last-seen message. The `[EntraBot]` prefix used in
  delegated mode is not restart-safe on its own: inbound content is
  XPIA-wrapped before the literal-prefix check runs, so a wrapped copy of the
  agent's own message no longer starts with `[EntraBot]` by the time the
  filter inspects it.

`$top`, cursor pagination via `@odata.nextLink`, and the messages `delta` query
(`GET /chats/{chat-id}/messages/delta`) are supported and reliable.

## Message delivery

A `chat_id` reaches the agent from `create_chat`, inbound channel-push metadata,
the persisted `watched_chats` list, or the `/me/chats` auto-discovery sweep (Agent
User mode, every 120 seconds). Inbound messages are delivered by background
polling; high-frequency polling of Teams resources risks throttling, so poll
sparingly. Change-notification subscriptions on `/chats/{id}/messages` are an
alternative, but chat-message subscriptions expire after at most 60 minutes and
require an HTTPS webhook endpoint and renewal logic. For Entrabot's delivery
model, see [Messaging and Delivery](../architecture/messaging-and-delivery.md)
and the [Teams and Chat Workflows guide](../guides/teams-and-chat-workflows.md).

## Presence

Teams presence is readable and settable through Graph, though Entrabot does not
set presence in the current runtime:

```http
GET  https://graph.microsoft.com/v1.0/users/{user-id}/presence
POST https://graph.microsoft.com/v1.0/users/{user-id}/presence/setPresence
POST https://graph.microsoft.com/v1.0/users/{user-id}/presence/clearPresence
```

`setPresence` requires a `sessionId` equal to the app registration's client ID
and an `expirationDuration` (ISO 8601, 5 minutes to 4 hours); presence reverts
when the duration expires. Calendar-derived statuses (in a meeting, out of
office) cannot be overridden. Permissions: delegated `Presence.ReadWrite` (own),
application `Presence.ReadWrite.All` (others).

## Permissions

| Permission | Type | Purpose | Admin consent |
|---|---|---|---|
| `Chat.Create` | Delegated | Create 1:1 or group chats | No |
| `Chat.Read` | Delegated | Read chats the user is in | No |
| `Chat.ReadWrite` | Delegated | Read/write chats | No |
| `ChatMessage.Send` | Delegated | Send chat messages | Yes |
| `ChatMember.Read` | Delegated | List chat members | No |
| `ChatMember.ReadWrite` | Delegated | Add/remove chat members | No |
| `Presence.Read` | Delegated | Read own presence | No |
| `Presence.ReadWrite` | Delegated | Set own presence | No |
| `Chat.Read.All` | Application | Read all chats (compliance) | Yes |

## Message content and Adaptive Cards

Teams chat message HTML supports a limited subset of tags — `<b>`, `<i>`,
`<em>`, `<strong>`, `<a>`, `<br>`, `<p>`, `<ul>`, `<ol>`, `<li>`, `<h1>`–`<h3>`,
`<pre>`, `<code>`, `<blockquote>`, and hosted `<img>`. `<table>`, `<div>`,
`<span>` styling, custom CSS, and `<iframe>` are not supported; use Adaptive
Cards for rich formatting.

Graph supports sending messages with Adaptive Card attachments, but only
`Action.OpenUrl` works. Interactive actions such as `Action.Submit` require a bot
to handle the callback and are not available through Graph alone.

## Rate limits and throttling

| Scope | Limit |
|---|---|
| Per app across all tenants | 130,000 requests / 10 seconds |
| Chat/channel message throughput | ~10 messages / 10 seconds per thread |
| Presence API | 10,000 requests / 30 seconds / app / tenant |
| Subscription management | 500 requests / 20 seconds / app / tenant |

Per-thread (per-chat) limits are undocumented but real, and batched requests can
still trigger per-resource throttling. On HTTP 429, honor the `Retry-After`
header and back off with exponential backoff and jitter; `Retry-After` is not
always present, so implement a fallback delay.

## References

- [Teams API overview](https://learn.microsoft.com/en-us/graph/api/resources/teams-api-overview?view=graph-rest-1.0)
- [Teams messaging APIs overview](https://learn.microsoft.com/en-us/graph/teams-messaging-overview)
- [Send chatMessage](https://learn.microsoft.com/en-us/graph/api/chatmessage-post?view=graph-rest-1.0)
- [Create chat](https://learn.microsoft.com/en-us/graph/api/chat-post?view=graph-rest-1.0)
- [presence: setPresence](https://learn.microsoft.com/en-us/graph/api/presence-setpresence?view=graph-rest-1.0)
- [Change notifications for Teams](https://learn.microsoft.com/en-us/graph/teams-change-notification-in-microsoft-teams-overview)
- [Throttling limits](https://learn.microsoft.com/en-us/graph/throttling-limits)
- [Microsoft Graph permissions reference](https://learn.microsoft.com/en-us/graph/permissions-reference)
