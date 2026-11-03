# Teams and Chat Workflows

Entrabot has no default group chat. Every operation on an existing Teams
conversation — sending, reading, watching for replies — requires an explicit
`chat_id`. This guide covers where that ID comes from, how messages flow in
both directions, and how outstanding commitments and delivery state are
tracked across restarts.

## Chat identity: no default group chat

A `chat_id` reaches the agent one of four ways:

1. **`create_chat`** — creates (or finds) a 1:1 DM with a target user and
   registers it as a watched chat.
2. **Channel notification metadata** — every inbound message pushed through
   `notifications/claude/channel` carries the originating chat's ID.
3. **Persisted `watched_chats`** — chats registered in a previous session are
   kept in the configured storage backend and reloaded at boot. With Blob
   storage, instances sharing a container share this list.
4. **`/me/chats` auto-discovery** — a background sweep, running every 120
   seconds in Agent User mode, reads every page of the Agent User's chats and
   registers any that aren't already watched.

Newly discovered chats (from `create_chat` or auto-discovery) are persisted
right away, and become active automatically: the 5-second background poll picks
them up on its next cycle. If the write fails, polling isn't affected and the
write is retried. A chat first found by discovery surfaces its newest message
only if it is under 24 hours old. Read older history with `read_teams_messages`.

## Sending messages

`send_teams_message` sends as the Agent User in `agent_user` mode; in
`delegated` mode it sends as the human sponsor with a `[EntraBot]` prefix so
recipients can tell agent-originated messages apart from the human's own.

Substantive outbound DMs require a recent `post_thinking_placeholder` call
for that `chat_id`. Skipping it causes the send to be rejected, with
remediation guidance pointing at calling `post_thinking_placeholder` and
retrying, rather than silently sending.

What happens after the message is sent depends on the host:

- On Claude Code and other channel-push hosts, `send_teams_message` returns
  immediately. The sponsor's reply, when it arrives, is delivered on the next
  turn via `notifications/claude/channel` — there's nothing further to poll.
- On Copilot CLI and other hosts without a channel-push mechanism,
  `send_teams_message` blocks until a verified sponsor reply arrives in any
  watched chat, then returns it inline as `sponsor_reply`, including the
  originating `chat_id`. This means the reply may come from a different chat
  than the one just messaged, so follow-up messages should use the `chat_id`
  returned with `sponsor_reply` rather than assuming it matches the chat just
  messaged.

`wait_for_sponsor_dm` is a separate tool reserved for the case where an
operator explicitly asks to block until a reply arrives mid-task. It is not
part of routine message sending and should never be used for polling in a
loop. See [Sponsor DM Wait Pattern](../clients/overview.md#sponsor-dm-wait-pattern).

## Receiving messages

A background poll runs every 5 seconds across all watched chats. Each chat is
polled independently, so a failure fetching one chat's messages doesn't
starve delivery for the others.

`watch_teams_replies` is a tool-result fallback for polling a single chat
with a timeout. It maintains its own delivery state, separate from the
background poll's channel-push delivery.

## Promises: tracking commitments

Promise tools durably store deferred commitments the agent makes in
conversation ("I'll get back to you on this") and their resolution history,
so they survive process restarts. The store is append-only: adding a promise
appends an "open" entry, and resolving it appends a new entry with the same
ID and a "resolved" status, keeping the full history rather than overwriting
it. Writes to a cloud backend use ETag-based optimistic concurrency.

## Delivery state and deduplication

Each watched chat tracks its own cursor: a last-seen timestamp plus a bounded
set of recently seen message IDs, persisted through the configured Local or
Blob storage backend. The background poll's channel push and
`watch_teams_replies` use separate delivery state, so one does not consume
the other's messages. A shared blob ETag claim prevents the same message
from being delivered twice when more than one process is watching the same
chat.

## See also

- [MCP Tools Reference](../reference/mcp-tools.md)
- [Messaging and Delivery](../architecture/messaging-and-delivery.md)
- [Teams Graph API](../platform-docs/teams-graph-api.md)
- [Troubleshooting: Teams and Email](../troubleshooting/teams-and-email.md)
