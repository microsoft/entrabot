# Teams Integration Layer

## Purpose

Enables bidirectional communication between the agent and the human user through Microsoft Teams. The agent connects as its **Agent User** identity — a real Entra user with a Teams license, mailbox, and org chart presence.

## How It Works

The Agent User token (from the three-hop flow, `idtyp=user`) is used to call the Teams Graph API. Since the token has user context, `/me` resolves to the Agent User, and the agent appears as its own identity in Teams conversations.

### Chat Creation

```
POST /v1.0/chats
```

Creates a 1:1 chat between the Agent User (`/me`) and the human user. Idempotent — if the chat already exists, it's returned unchanged.

### Send Message

```
POST /v1.0/chats/{chat-id}/messages
```

Sends a message FROM the Agent User. The human sees it as a message from a distinct Teams identity, not from themselves.

### Read Messages

```
GET /v1.0/chats/{chat-id}/messages?$top=N&$orderby=createdDateTime desc
```

Reads recent messages from the chat. The agent can check for human responses and act on them.

## Prerequisites

- Agent User must be created (via `create_entra_agent_ids.py`)
- Agent User must have a Teams-capable M365 license (E3/E5/Teams Enterprise)
- Teams provisioning must be complete (10-15 min after license assignment)
- `oAuth2PermissionGrant` must exist for `Chat.Create Chat.ReadWrite ChatMessage.Send User.Read`

## Key Files

- `src/entrabot/tools/teams.py` — `acquire_agent_user_token()`, `create_or_find_chat()`, `send()`, `read()`
- `tests/tools/test_teams.py` — 21 tests covering all three hops + Graph API calls
