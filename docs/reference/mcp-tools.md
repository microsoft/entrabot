# MCP tools reference

The EntraClaw MCP server exposes the following tools. Every tool that targets a Teams chat requires an explicit `chat_id` — there is no default chat.

## Teams messaging

### `send_teams_message`

Send a message to a Teams chat.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `message` | string | yes | The text/HTML body to send. |
| `chat_id` | string | **yes** | Target chat ID. Get one from `create_chat`, from the `meta.chat_id` of a channel notification, or from your own records. |
| `content_type` | `"text"` \| `"html"` | no (default `text`) | `html` is required for URLs, lists, code, or any structured content. |
| `mentions` | list[dict] | no | `@mention` payload. Each dict needs `id` (int matching an `<at id="N">` tag), `name`, and `user_id` (Entra GUID). |

Returns JSON with `message_id` and `sent_at`. In `bot` mode, writes to `outbound.jsonl` instead of calling Graph and returns a placeholder `bot-outbound-<id>`.

Error modes: missing `chat_id` returns `{"error": "chat_id is required — …"}` without calling Graph.

### `send_card`

Send an Adaptive Card to a Teams chat. Uses the same authentication as `send_teams_message` but ships a card attachment instead of a plain body.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `card_type` | `"tool_activity"` \| `"task_status"` \| `"build_result"` | yes | Which card template to render. |
| `chat_id` | string | **yes** | Target chat. |
| `title` | string | conditional | Tool name (for `tool_activity`) or task name (for `task_status`). |
| `status` | string | conditional | `running` \| `complete` \| `error` \| `in_progress` — for `tool_activity` / `task_status`. |
| `detail` | string | optional | Short description (for `tool_activity`). |
| `duration` | string | optional | Human-readable duration (for `task_status`). |
| `passed` | bool | conditional | `true`/`false` for `build_result`. |
| `summary` | string | optional | One-line summary (for `build_result`). |
| `details_text` | string | optional | Longer detail body (for `build_result`). |
| `extra` | dict | optional | Key/value metadata block for `task_status`. |

Cards are sent with no `[EntraClaw]` prefix — the card itself identifies the agent.

### `read_teams_messages`

Read recent messages from a Teams chat.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `chat_id` | string | **yes** | Chat to read from. |
| `count` | int | no (default 5, max ~50) | Number of messages. |

Returns a JSON array, newest first. Each message has `message_id`, `from`, `content`, `sent_at`, `reply_to_ids`.

### `list_chat_members`

List members of a Teams chat. Useful for resolving display names to user GUIDs for `@mentions`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `chat_id` | string | **yes** | Chat to list. |

Returns JSON array of `{user_id, name, email, roles}`.

### `add_teams_member`

Add a user to a Teams chat. Cross-tenant users are auto-resolved from the email domain.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `email` | string | yes | User email. |
| `chat_id` | string | **yes** | Chat to add them to. |
| `tenant_id` | string | optional | Override. Usually auto-resolved from the email domain; only pass this if resolution fails. |

Returns `{member_id, display_name, roles}`.

### `watch_teams_replies`

Block-and-poll a single chat for new human replies. Usually unnecessary — the background poll pushes replies automatically via `notifications/claude/channel`. Use this when you want to explicitly wait before returning a tool result.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `chat_id` | string | **yes** | Chat to watch. |
| `timeout` | int | no (default 30) | Max seconds to wait. |
| `interval` | int | no (default 5) | Seconds between poll iterations. |

Returns `{messages, timed_out, poll_count}`.

### `create_chat`

Create (or find) a 1:1 DM with a user by email. Idempotent on the Graph side — calling twice with the same email returns the existing chat.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `target_email` | string | yes | Email of the user you want to DM. |
| `target_tenant_id` | string | optional | Home tenant GUID override. Usually auto-resolved from the email domain. |

Returns `{chat_id, created_at}`. The new chat is auto-registered for background polling and persisted to the `watched_chats` file so it survives MCP restarts.

## Identity & audit

### `whoami`

Show the agent's Entra identity and connection status.

Returns a JSON object with `agent_type`, `blueprint_app_id`, `agent_id`, `tenant_id`, `human_sponsor`, `status`, `teams_chat_id` (usually `not_connected` — there is no default chat), `identity_state`, `attribution_type`, and `auth_mode`.

### `audit_log`

Record an action before performing it.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `action` | string | yes | Short name of the action (e.g. `send_teams_message`). |
| `resource` | string | yes | What the action touches (chat ID, email, URL, etc.). |
| `summary` | string | yes | Short human-readable summary. |
| `metadata` | dict | optional | Structured details. |

Audit entries are written through the same `MemoryBackend` as interaction logs. If the backend can't write, the action does not proceed (security rule #18).

## Summary / maintenance

### `run_daily_summary`

Force the daily summary generator to produce today's (or a specified day's) summary email. Runs automatically via a scheduler at 5pm PDT; this tool lets you trigger it on demand.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `target_day` | string (YYYY-MM-DD) | optional | Day to summarize. Defaults to the current local date. |

### `view_image`

Read an image from the local filesystem and return its bytes so the LLM can see it.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | yes | Absolute path to the image file. |

## Background tasks (not MCP tools — run at boot)

The MCP server also starts several background coroutines when authenticated as an Agent User:

| Task | Interval | What it does |
|------|----------|--------------|
| Teams chat poll | 5s | For every chat in `watched_chats`, fetches new messages and pushes them as `notifications/claude/channel` notifications. Per-chat resilience — a 403 on one chat doesn't starve the rest. |
| Email poll | 60s | `/me/messages`, filters Teams/M365 noise, detects Purview-encrypted mail, logs + pushes substantive inbound. |
| Chat auto-discovery | 120s | `GET /me/chats`, registers any chat that's not already watched. |
| Daily summary | 1/day (5pm PDT) | Generates a digest email of the day's interactions and sends it. |

## Failure modes

- **Missing `chat_id`** on any Teams tool → explicit error, no Graph call.
- **401 from Graph** → one retry with a freshly minted token (the state machine handles token refresh for `agent_user`, `delegated`, and `bot` modes).
- **403 from Graph** → logged as a per-chat warning; background poll moves on to the next chat, next cycle retries.
- **`notifications/claude/channel` write stream unavailable** → interaction is still logged, but no push. The tool returns success; you just won't see the inbound until you `read_teams_messages`.
