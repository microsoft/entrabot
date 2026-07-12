# Teams Integration Layer

## Purpose

Gives the agent a Teams presence the human can talk to directly, and gives Entrabot the tools to read and act on that conversation. `src/entrabot/tools/teams.py` holds the Graph API calls; how a message is attributed depends on the active auth mode.

## Sending as the agent, or as the human

In **Agent User mode**, the token from the three-hop flow (see [Auth](auth.md)) has its own `idtyp=user` identity, so `send()` posts messages that appear in Teams as coming from the Agent User itself — a distinct identity from the human, with its own display name and (if licensed) AI-agent badge.

In **delegated mode**, the token belongs to the human, so outbound messages are prefixed `[EntraBot]` (via the `prefix` argument to `send()`) so the human can tell which messages the agent sent versus typed themselves. Graph still sees the human's identity — there is no separate Agent User attribution in this mode.

## No default chat

Every Teams tool requires an explicit `chat_id`; Entrabot has no default or fallback chat. See [Messaging and Delivery](../messaging-and-delivery.md) for chat sources and persistence.

## Chat creation

`create_or_find_chat()` builds the Graph `POST /chats` payload for three shapes:

- **1:1 chat** (one human) — `chatType: oneOnOne`. Graph's create-chat endpoint is idempotent for one-on-one chats: calling it again with the same two members returns the existing chat rather than creating a duplicate.
- **Group chat** (multiple humans) — `chatType: group` with a fixed topic. Group chat creation is **not** idempotent; each call creates a new chat.
- **Cross-tenant / guest members** — when a home-tenant ID and email are supplied, the payload includes `tenantId` and an email-based `user@odata.bind` so Graph can resolve the federated identity. The member role remains `owner`, and chat type still depends on the human count: one human produces `oneOnOne`; multiple humans produce `group`.

`add_member()` layers sponsor-gated authorization on top of a plain Graph member-add: the requester must match a known Agent Identity sponsor, and that sponsor must currently be bound to `chat_id` through the active-channel mechanism, before the invite is attempted.

## Reading messages

`read()` fetches `GET /chats/{chat_id}/messages` with `$top` and `$orderby=createdDateTime desc`, then wraps every message body in the XPIA `<external_content>` envelope before returning it (see [Audit](audit.md) and [Security Boundaries](../security-boundaries.md) for why). Identity and dedup filtering — telling human messages apart from the agent's own, and from already-seen messages — happens client-side in `filter_human_messages()`, matching on `sender_upn` (falling back to the sender's object ID), never on the mutable display name or Graph `$filter`. The separate `/me/chats` discovery sweep omits `$orderby` because that endpoint rejects it.

## Background polling

Two tasks keep conversations current without the agent needing to ask:

- **Teams chat poll (5s)** — polls every chat in `watched_chats` for new messages. Starts as soon as at least one chat is watched; not gated on auth mode.
- **Chat auto-discovery (120s)** — Agent User mode only. Sweeps `/me/chats` and registers any chat not already in `watched_chats`, persisting the updated list so it survives a restart.

## Delivery: channel push vs. auto-wait

How a new message reaches the LLM depends on the connected host. Claude Code subscribes to the `notifications/claude/channel` push extension, so inbound messages surface as a next-turn system reminder with no tool call needed. Hosts that don't implement the extension (GitHub Copilot CLI and others) instead get the reply delivered inline: `send_teams_message` auto-blocks after sending and returns the sponsor's reply as `sponsor_reply`. See the [Clients Overview](../../clients/overview.md) and [Messaging and Delivery](../messaging-and-delivery.md) for the full per-host behavior and the sponsor-DM wait pattern.

## Prerequisites

- The Agent User must exist and hold a Teams-capable license; Teams provisioning takes a few minutes after license assignment.
- The Agent Identity/Agent User must have consented delegated scopes for chat creation, read/write, and message send.
- For cross-tenant chats, the target user's home tenant ID must be available so Graph can route the invite.

See [MCP Tools](../../reference/api/mcp-tools.md) for the full tool signatures, and [Token Flows](../../reference/token-flows.md) for the wire-level request shapes behind every call in this layer.
