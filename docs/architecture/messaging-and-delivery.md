# Messaging and Delivery

## No default chat

Every Teams tool requires an explicit `chat_id` — there is no default group chat or fallback conversation. A `chat_id` comes from one of three places:

1. **`create_chat`** (the MCP tool, wrapping `create_or_find_chat()` / `create_one_on_one_chat()` in `src/entrabot/tools/teams.py`) when the agent creates a new 1:1 or group chat.
2. **The persisted `watched_chats` file** under the config data directory, loaded at boot by `_init_poll()`.
3. **Chat auto-discovery** (`_background_discover_chats`, Agent User mode only) — sweeps `GET /me/chats` every 120 seconds and registers any chat_id not already watched, so chats another human starts with the Agent User still get picked up.

## Outbound path

`send_teams_message` calls `send()` in `tools/teams.py` through `_with_token_retry()`. What identity the message carries depends on the active auth mode:

- **Agent User mode** — the three-hop token's `idtyp=user` identity means the message posts as the Agent User itself, with its own display name and (if licensed) AI-agent badge.
- **Delegated mode** — the token belongs to the signed-in human, so `send()` prepends the literal `[EntraBot]` prefix (passed as the `prefix` argument) so the human can tell which messages the agent sent versus typed themselves. Graph still attributes the message to the human's own identity in this mode.

## Inbound background poll

`_background_poll()` runs every 5 seconds, iterating every chat in `watched_chats`:

- **Per-chat persisted cursors.** Each chat's poll state (`last_ts`, a bounded tail of `seen_ids`, and a `bootstrapped` flag) is persisted through the configured `MemoryBackend` via `src/entrabot/tools/chat_cursors.py`, keyed `chat_cursors/<chat_id>.json`. This survives MCP server restarts — without it, every restart would re-baseline "the newest message at boot" as if it were live, even days-old messages.
- **Client-side filtering.** `filter_human_messages()` drops the agent's own messages by `sender_upn`, falling back to the sender's AAD object ID — never display name, which is mutable. It also drops system messages (`from == "unknown"`) and messages already present in the caller's `sent_message_ids` set. Graph `$filter` is not used for these identity and dedup decisions.
- **Sent-message echo prevention.** Agent User mode reliably excludes the agent by UPN/object ID. Delegated mode additionally tracks outbound Graph message IDs in process and excludes those IDs on later polls. Message content is already XPIA-wrapped when `filter_human_messages()` receives it, so the literal `[EntraBot]` prefix is not a reliable restart-safe filter.
- **XPIA wrapping before model exposure.** `read()` wraps every message body in the `<external_content>` envelope (`entrabot.security.xpia.wrap_external`) before it's returned — the body prompt instructs the model that anything inside the envelope is data, not instructions. Metadata (`message_id`, `sender_id`, timestamps) stays outside the envelope so filters and counts still work on it directly.

## Fleet-safe delivery

`chat_cursors.claim_delivery()` is the mechanism that lets more than one Entrabot instance poll the same chat without double-delivering a message:

- It reads the shared cursor with its ETag, computes which candidate message IDs aren't already recorded as delivered, and writes the merged cursor back with an `If-Match` precondition — an atomic compare-and-swap.
- Across N instances racing on the same message, exactly one wins the CAS for a given ID; the losers hit a `ConcurrencyError`, re-read, see the ID already claimed, and push nothing for it.
- Retries are bounded (`CLAIM_MAX_ATTEMPTS = 4`). Every ambiguous outcome — a read failure, a corrupt or wrong-shaped cursor payload, or exhausted CAS retries — returns an empty claim list rather than guessing. The design fails closed: it would rather skip a push than risk delivering the same message twice across the fleet.

This guarantees no duplicate claims are made **before** a message reaches a host's push mechanism. It does not extend to what happens after a claim succeeds — if the `notifications/claude/channel` write itself fails partway (a broken pipe, a host that dropped the connection), that specific delivery attempt can still be lost; `claim_delivery` only prevents two instances from both believing they're the one delivering a given message.

## Channel-push hosts

Claude Code subscribes to `notifications/claude/channel`. The background poll's notification emitter sends this push unconditionally regardless of which client is connected — the emitter itself doesn't check the host. What differs by host is whether that push reaches the model: Claude Code injects it as a next-turn system reminder; hosts that don't understand the method ignore it per the MCP spec, silently. Because of that gap, `send_teams_message`'s auto-wait behavior (below) is what actually guarantees delivery on non-push hosts — the notification firing is necessary but not sufficient.

## Non-push hosts: `send_teams_message` auto-wait

For hosts not in the small hardcoded channel-push set (Copilot CLI, Codex, and anything else `mcp_server.py` doesn't specifically recognize), `send_teams_message` auto-blocks after sending:

- It runs the same sponsor-gated wait loop as `wait_for_sponsor_dm`, listening across **every currently watched chat**, not only the chat just sent to — the reply that satisfies the wait can come from any chat the sponsor is active in.
- `SponsorGate`, resolved from the Agent Identity's `/sponsors` Graph relationship and extended across watched chats, determines whether an inbound message can satisfy the wait. Active-channel binding is a separate mutation-authorization gate and is not part of wait reply validation.
- If a reply arrives, the result's `sponsor_reply` includes that reply's own `chat_id`. The caller must reply in *that* chat, which may differ from the chat the outbound message was sent to — the tool docstring and the `_next_action` field both restate this so the model doesn't answer in the wrong conversation.

`wait_for_sponsor_dm` is a separate, explicit tool for the same underlying wait mechanism, reserved for when the operator explicitly asks the agent to block until a reply arrives outside of a send. Never poll it in a loop, spawn a headless subprocess to watch for replies, or use `watch_teams_replies` as a substitute for this pattern — `watch_teams_replies` runs its own independent dedup state and exists as a fallback path, not a proactive-wait tool.

## Chat auto-discovery

`_background_discover_chats()` runs every 120 seconds, in Agent User mode only (it targets `/me/chats`, which resolves to the human's chats in delegated mode — not what's wanted). It cannot use Graph's `$orderby` on this endpoint (it 400s there); results are not sorted, but chat IDs are deduplicated against `watched_chats` client-side. Newly discovered chats are persisted to the `watched_chats` file immediately so a restart inherits them, and they pick up their cursor from the normal bootstrap path on the next poll cycle — no historical flood of old messages.

## See also

- [MCP Runtime](mcp-runtime.md) — the background task matrix and initialization lifecycle this messaging path runs inside.
- [Teams Integration Layer](layers/teams.md) — chat creation, sending, and reading at the Graph API level.
- [Clients Overview](../clients/overview.md) and the per-host pages it links to — the exact channel-push vs. auto-wait table for each tested host.
- [Identity and Token Flow](identity-and-token-flow.md) — the token behind every send/read call in this document.
