# Teams and Chat Workflows

Entrabot has no default group chat. Every operation on an existing Teams
conversation — sending, reading, watching for replies — requires an explicit
`chat_id`. This guide covers where that ID comes from, how messages flow in
both directions, and how outstanding commitments and delivery state are
tracked across restarts.

## Chat identity: no default group chat

A `chat_id` reaches the agent one of four ways:

1. **`create_chat`** — creates (or finds) a 1:1 DM with a target user and
   registers it as a watched chat. The user must pass the recipient policy (a
   sponsor, a member of the requesting chat, the home tenant, or
   `ENTRABOT_OUTBOUND_ALLOW`), and the audit event is written before the chat
   is created.
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

## Reactions

`read_teams_messages` gives each message's `reaction_count` and lists its
newest 20 reactions: the emoji, the ID of whoever reacted, and when. A custom
reaction also has its name, wrapped as external content.

The background poll also notices when someone reacts to one of the agent's
own messages, and pushes a `[teams reaction]` notification saying which
message, which reaction and who. It looks at the ten newest messages in each
chat and at every reaction on them, announces additions only, and never
announces the agent's own reactions. Only a reaction made within the last
hour is announced, so turning notifications back on, or restarting after a
long break, doesn't release a backlog. The notification is always wrapped as
external content, a sponsor's reaction included: it is information, not an
instruction, and needs no reply. Set `ENTRABOT_REACTION_NOTIFICATIONS=false`
to turn these notifications off. They need an Agent User session and a host
that shows channel pushes.

`add_teams_reaction` and `remove_teams_reaction` let the agent react to a
message, or take its own reaction back, with exactly one Unicode emoji or one
of `like`, `heart`, `laugh`, `surprised`, `sad`, `angry`. Country flags,
keycap digits, arrows and letter-like symbols are refused, because a reaction
must not be able to carry text. The agent may react to other people's
messages, including in group chats where it otherwise only watches. The body
prompt asks it to do so sparingly, and never in place of a reply it owes. In
a delegated session both tools refuse: the reaction would be the signed-in
human's, with nothing to mark it as the agent's.

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
