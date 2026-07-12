# Teams and email

## A Teams tool says `chat_id` is required

Entrabot has no default chat. Pass an explicit `chat_id` from:

- `create_chat`
- an inbound channel notification's metadata
- the local `ENTRABOT_DATA_DIR/watched_chats` registry
- Agent User chat auto-discovery

The Teams poll runs every 5 seconds once at least one watched chat exists.
Agent User mode also discovers `/me/chats` every 120 seconds. Newly discovered
chats are saved locally and join the next polling cycle.

## A message was sent but the reply did not appear

Delivery depends on the MCP host:

- **Claude Code with channel push enabled** returns from
  `send_teams_message` immediately. Start it with:

  ```bash
  claude --dangerously-load-development-channels server:entrabot
  ```

  The reply arrives later through `notifications/claude/channel`.
- **Copilot CLI and other non-channel hosts** auto-wait after
  `send_teams_message` and return a verified sponsor reply inline as
  `sponsor_reply`. Use the reply's returned `chat_id`; it can differ from the
  chat that was just messaged.

Do not poll `watch_teams_replies` in a loop. `wait_for_sponsor_dm` is only for
an operator's explicit request to block until a reply arrives.

See [Teams and Chat Workflows](../guides/teams-and-chat-workflows.md) and
[Clients](../clients/overview.md).

## Sending is rejected for a missing thinking placeholder

Substantive Teams DMs require a recent `post_thinking_placeholder` for the same
`chat_id`. Post the placeholder, perform the work, then update or resolve it.
This is a delivery discipline check, not an authentication failure.

## Graph returns 401, 403, 404, or 429

| Status | Meaning | Safe recovery |
|---|---|---|
| 401 | Token expired, revoked, or wrong for the resource | Entrabot refreshes and retries once. If it repeats, inspect `whoami`, consent, and the named auth hop. |
| 403 | Missing delegated permission, missing Teams/Exchange license, or the current identity is not a member/author | Check Agent User licensing, per-principal consent, chat membership, and whether the operation targets the agent's own message. |
| 404 | Chat or message ID does not exist for this identity, or is inaccessible | Copy the exact current `chat_id` or Graph message ID from tool output; confirm the active identity belongs to the resource. |
| 429 | Microsoft Graph throttled the request | Honor `Retry-After`; do not immediately loop. Entrabot surfaces the retry delay. |

The message body limit is **28,000 characters**. Split longer content into
multiple messages or put the content in a file and share it.

## A newly added chat is not being polled

Auto-discovery runs only in Agent User mode and can take up to 120 seconds.
Delegated sessions do not run the discovery task. Register the chat by using
`create_chat`, or add its ID to the normal watched-chat flow.

The registry is always local:

```text
ENTRABOT_DATA_DIR/watched_chats
```

It is not read from Blob Storage. If multiple machines use the same Agent User,
each machine needs its own local watched-chat registry or must discover the
chat independently.

## Messages repeat, disappear, or stop after a cursor error

Per-chat delivery cursors use `MemoryBackend`; they can be local or Blob-backed.
Entrabot uses a timestamp, a bounded message-ID tail, and ETag compare-and-swap
claims so concurrent pollers do not deliver the same message twice.

Cursor reads and claims fail closed. If a cursor is corrupt, unavailable, or
cannot be updated after bounded ETag retries, Entrabot pushes nothing rather
than risk replaying messages. Check storage health before deleting cursor
state. For upgrade-related replay prevention, see
[Migrations and upgrades](migrations-and-upgrades.md).

## Email notifications do not arrive

The background email poll:

- Runs every 60 seconds.
- Runs only in Agent User mode.
- Reads `/me/messages`.
- Initializes a missing cursor to the current time, so first startup does not
  replay historical mail.
- Stores its cursor locally at `ENTRABOT_DATA_DIR/email_cursor.txt`.

Check `whoami` for `auth_mode: agent_user`, then confirm the Agent User has a
mailbox, mail permissions, and completed license provisioning.

The poll filters Teams/Microsoft 365 notification domains, common no-reply
senders, and messages sent by the Agent User from Sent Items. Filtered mail
still advances the cursor.

## A Purview-protected email has no readable body

Entrabot detects a `message.rpmsg` attachment and marks the message as
encrypted. Decrypting Microsoft Purview-protected RPMSG content is not
supported. Open the message in an authorized Microsoft 365 client or ask the
sender for an accessible copy.

## `read_email` cannot find a message

`message_id` is the Microsoft Graph resource ID returned by the poll, channel
notification, or another Graph listing. It is not the RFC `Message-ID` header.
Use it with the same mailbox in which it was obtained. Moving or deleting the
message can invalidate a previously returned ID because Entrabot does not
request immutable Graph IDs.

For replies, pass the original Graph ID as `reply_to_message_id`; Graph then
preserves thread headers and takes the subject from the original message.
