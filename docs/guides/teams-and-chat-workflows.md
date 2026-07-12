# Teams and Chat Workflows

EntraBot has no default group chat. Every operation on an existing Teams
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
   written to the configured storage backend and reloaded at boot.
4. **`/me/chats` auto-discovery** — a background sweep, running every 120
   seconds in Agent User mode, finds chats the Agent User was added to by
   someone else and registers any that aren't already watched.

Newly discovered chats (from `create_chat` or auto-discovery) are persisted
immediately and picked up by the 5-second background poll on the next cycle
— there's no separate "activation" step.

## Sending messages

`send_teams_message` sends as the Agent User in `agent_user` mode; in
`delegated` mode it sends as the human sponsor with a `[EntraBot]` prefix so
recipients can tell agent-originated messages apart from the human's own.

Substantive outbound DMs require a recent `post_thinking_placeholder` call
for that `chat_id`. Skipping it returns `MissingPlaceholderError` with a
remediation hint (`post_thinking_placeholder + retry`) rather than silently
sending.

What happens after the message is sent depends on the host:

- On Claude Code and other channel-push hosts, `send_teams_message` returns
  immediately. The sponsor's reply, when it arrives, is delivered on the next
  turn via `notifications/claude/channel` — there's nothing further to poll.
- On Copilot CLI and other hosts without a channel-push mechanism,
  `send_teams_message` automatically waits for a sponsor-gated reply in any
  watched chat, not necessarily the chat just messaged, and returns it inline
  as `sponsor_reply`. The caller is expected to continue the conversation by
  replying to the `chat_id` returned with `sponsor_reply`.

`wait_for_sponsor_dm` is a separate tool reserved for the case where an
operator explicitly asks to block until a reply arrives mid-task. It is not
part of routine message sending and should never be used for polling in a
loop. See [Sponsor DM Wait Pattern](../clients/overview.md#sponsor-dm-wait-pattern).

## Receiving messages

A background poll runs every 5 seconds across all watched chats. Each chat is
polled independently, so a failure fetching one chat's messages doesn't
starve delivery for the others.

`watch_teams_replies` is a separate, tool-result-based fallback for polling a
single chat with a timeout. It intentionally maintains its own delivery and
deduplication state, distinct from the background poll's channel-push
delivery — the two are not meant to be reconciled into a single cursor.

## Promises: tracking commitments

Outstanding commitments the agent makes in conversation ("I'll get back to
you on this") are tracked durably in an append-only JSONL store, so they
survive process restarts. Adding a promise appends an "open" entry; resolving
it appends a new entry with the same ID and a "resolved" status — the store
never rewrites history, it folds by ID and keeps the last entry per ID.
Writes to a cloud backend use ETag-based optimistic concurrency.

Record a promise before using deferred-commitment language in a reply
("I'll follow up," "let me check and get back to you") — that's the point at
which the commitment should become durable state rather than something only
implied in a chat transcript.

## Cursors and deduplication

Each watched chat tracks its own cursor: a last-seen timestamp plus a bounded
set of recently seen message IDs, persisted through the configured Local or
Blob storage backend. The background poll and `watch_teams_replies` keep
separate cursor/dedup state by design, and a shared blob ETag claim prevents
the same message from being delivered twice when more than one process is
watching the same chat.

## See also

- [MCP Tools Reference](../reference/mcp-tools.md)
- [Messaging and Delivery](../architecture/messaging-and-delivery.md)
- [Teams Graph API](../platform-docs/teams-graph-api.md)
- [Troubleshooting: Teams and Email](../troubleshooting/teams-and-email.md)
