# MCP tools

The EntraBot MCP server (`src/entrabot/mcp_server.py`) exposes 37 tools across five domains. Every tool that targets a Teams chat requires an explicit `chat_id` — there is no default chat.

All Teams / Files / Email tools authenticate via the Agent User three-hop token (see [Auth](auth.md) and [Token Flows](../token-flows.md)). Tokens are minted on demand and cached — no credentials need to be supplied at tool-call time.

## Messaging

### `send_teams_message`

Send a message to a Teams chat, then listen for the reply.

```python
async def send_teams_message(
    message: str,
    content_type: str = "html",
    mentions: list[dict] | None = None,
    chat_id: str = "",
    ctx: Context | None = None,
) -> str
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `message` | `str` | yes | The text/HTML body. |
| `chat_id` | `str` | **yes** | Target chat. Get from `create_chat` or a channel notification's `meta.chat_id`. |
| `content_type` | `"text" \| "html"` | no (default `"html"`) | `html` is required for URLs, lists, code blocks, structured content. |
| `mentions` | `list[dict] \| None` | no | `@mention` payload. Each dict needs `id` (int matching `<at id="N">`), `name`, `user_id` (Entra GUID). |

Returns JSON with `message_id` and `sent_at`.

**Auto-wait behaviour**: on non-Claude-Code hosts (Copilot CLI, Codex, etc.), `send_teams_message` blocks after sending and returns the sponsor's reply inline as `sponsor_reply`. Claude Code ends the turn and gets the reply pushed via `notifications/claude/channel`. Server-side host detection — not a parameter.

### `post_thinking_placeholder`

Post a short placeholder so humans see the agent was triggered.

```python
async def post_thinking_placeholder(chat_id: str, text: str = "thinking…") -> str
```

Use when you need to ack a Teams chat and the real reply will take real work. Resolve with `resolve_placeholder` when the answer lands.

Returns JSON with `placeholder_id`.

### `update_placeholder`

Patch a thinking placeholder with a short italic progress note.

```python
async def update_placeholder(chat_id: str, placeholder_id: str, progress_text: str) -> str
```

Middle stage of the three-part placeholder flow. Use to surface what you're doing so the human sees work-in-progress, not a frozen placeholder.

### `resolve_placeholder`

Replace a thinking placeholder with the final message.

```python
async def resolve_placeholder(
    chat_id: str,
    placeholder_id: str,
    final_message: str,
    content_type: str = "html",
    mentions: list[dict] | None = None,
    mode: str = "edit",
) -> str
```

Modes:

- `edit` (default, quieter) — PATCH the placeholder in place.
- `delete_repost` — soft-delete the placeholder and send a fresh message. Use when a fresh ping matters (long sub-agent runs, multi-minute investigations).

On Graph failure, falls back to posting `final_message` as a new message.

### `delete_teams_message`

Soft-delete one of the agent's own Teams messages.

```python
async def delete_teams_message(message_id: str, chat_id: str = "") -> str
```

Graph replaces the body with a tombstone visible to chat participants. You can only delete messages the Agent User itself sent; Graph returns 403 on anyone else's.

### `send_email`

Send an email from the Agent User's mailbox.

```python
async def send_email(
    to: str,
    subject: str,
    body: str,
    content_type: str = "html",
    cc: str = "",
    bcc: str = "",
    reply_to_message_id: str = "",
) -> str
```

When replying to a known inbound, pass `reply_to_message_id` so Graph preserves the thread headers. Graph uses the original message's subject; any subject you pass here is informational only.

### `read_email`

Fetch the complete body, recipients, headers, and attachment flag for one Graph message.

```python
async def read_email(message_id: str, mailbox: str = "") -> str
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `message_id` | `str` | yes | Graph message ID from email channel push or another message listing. |
| `mailbox` | `str` | no | Shared mailbox UPN/address. Empty reads the Agent User's mailbox. |

Use this when the email-poll preview was truncated. A 401 refreshes and retries once; other Graph failures return structured error JSON. The model-facing body is wrapped as untrusted external content.

### `send_card`

Send an Adaptive Card to a Teams chat. Three card types:

```python
async def send_card(
    card_type: str,
    chat_id: str = "",
    title: str = "",
    status: str = "complete",
    detail: str = "",
    duration: str = "",
    passed: bool = True,
    summary: str = "",
    details_text: str = "",
    extra: str = "",
) -> str
```

| `card_type` | Use case |
|-------------|----------|
| `tool_activity` | Show a tool running / completing. Pass `title` (tool name), `status`, `detail`. |
| `task_status` | Show task progress with optional `duration` and `extra` key/value block. |
| `build_result` | Pass / fail summary with `summary`, `details_text`. |

Cards are sent without the `[EntraBot]` prefix — the card itself identifies the agent.

### `list_chat_members`

```python
async def list_chat_members(chat_id: str) -> str
```

Resolve display names to user GUIDs for `@mentions` in `send_teams_message`. Returns user ID, name, email, and roles for each member.

### `add_teams_member`

```python
async def add_teams_member(
    email: str,
    chat_id: str,
    requester_email: str,
    tenant_id: str = "",
) -> str
```

Add a member to a Teams chat. Authorization model: only sponsors can ask the agent to invite anyone. A sponsor may invite anyone they choose; the invitee is unrestricted.

### `create_chat`

```python
async def create_chat(target_email: str, target_tenant_id: str = "") -> str
```

Create a 1:1 private DM with a user by email. The new chat is automatically registered for background polling — replies push via channel notifications. Returns the `chat_id`.

### `read_teams_messages`

```python
async def read_teams_messages(chat_id: str, count: int = 5) -> str
```

Read recent messages from a Teams chat. Returns a JSON array, newest first. Each message has `message_id`, `from`, `content`, `sent_at`, `reply_to_ids`.

### `watch_teams_replies`

```python
async def watch_teams_replies(
    chat_id: str,
    timeout: int = 30,
    interval: int = 5,
    ctx: Context | None = None,
) -> str
```

Poll Teams for new replies. Returns when new messages arrive or after `timeout` seconds. Uses a server-side cursor — only returns genuinely new human messages.

### `wait_for_sponsor_dm`

```python
async def wait_for_sponsor_dm(
    timeout_seconds: int = 0,
    ctx: Context | None = None,
) -> str
```

Block until a sponsor sends a Teams DM, then return their message. Reserved for the rare case the operator explicitly says "block until they reply" mid-task. Never poll in a loop. See the sponsor DM wait pattern in `CLAUDE.md`.

### `view_image`

```python
async def view_image(url: str) -> str
```

Fetch and display an image from a Teams chat message. Pass the Graph API hosted content URL from a chat message's `<img src="...">` tag. Only accepts URLs under `graph.microsoft.com` — will not send the Bearer token to arbitrary hosts.

## Promises

Persistent commitment tracking. Survives restart. Persisted to the entrabot blob (or local backend) under `promises.jsonl`.

### `add_promise`

```python
async def add_promise(chat_id: str, description: str, due_by: str = "") -> str
```

Record an outstanding human-facing commitment. Use instead of `TaskCreate` for "I'll report back when X lands" shaped commitments. Returns `{id, ...}`.

### `list_promises`

```python
async def list_promises(open_only: bool = True) -> str
```

List outstanding promises. Returns JSON array of `{id, chat_id, description, created_at, due_by, status, resolved_at, resolution}`. Call at session start to see what you owe whom.

### `resolve_promise`

```python
async def resolve_promise(promise_id: str, resolution: str) -> str
```

Mark a promise resolved. Only call after the human-facing update has been posted in the correct chat — not when the internal signal (sub-agent completion, build finish) arrives.

## Files

SharePoint / OneDrive operations. All require the Agent User to be consented for `Files.Read.All` / `Sites.Read.All` / `Sites.ReadWrite.All`.

### `resolve_file_url`

```python
async def resolve_file_url(url: str) -> str
```

Resolve a SharePoint / OneDrive / shared-link URL to a stable `FileRef`. The returned handle carries `drive_id`, `item_id`, `site_id` (for SharePoint), and the file's `kind` (`sharepoint` / `onedrive_business` / `onedrive_personal`). Pass that handle to downstream Files tools — they do NOT re-resolve.

### `list_recent_files`

```python
async def list_recent_files(limit: int = 25) -> str
```

List files recently shared with the agent. Post-filtered by the operator site denylist (`ENTRABOT_FILES_DENIED_SITES`). The `denied_count` field reports how many files were filtered — surface that to the user.

### `read_file`

```python
async def read_file(
    drive_id: str,
    item_id: str,
    name: str,
    mime_type: str = "application/octet-stream",
    kind: str = "sharepoint",
    site_id: str = "",
    web_url: str = "",
    size_bytes: int = 0,
    as_format: str = "auto",
) -> str
```

Read a SharePoint / OneDrive file as text. Supported formats:

- `.md` / `.txt` / `.html` / `.htm` — fetched raw, decoded as UTF-8.
- `.docx` — converted to PDF via Graph (`?format=pdf`), text extracted via `pypdf`.
- `.pdf` — fetched raw, text extracted via `pypdf`.

Pass the `FileRef` fields returned from `resolve_file_url` or `list_recent_files`.

### `add_file_comment`

```python
async def add_file_comment(
    drive_id: str,
    item_id: str,
    name: str,
    content: str,
    mime_type: str = "application/octet-stream",
    kind: str = "sharepoint",
    site_id: str = "",
) -> str
```

Call the Graph drive-item comment surface for a `.docx` or `.xlsx` file. Personal OneDrive is rejected. Microsoft Graph does not expose native Word UI comments for `.docx` through this endpoint, so use `add_word_comment` and `reply_to_word_comment` for Word documents.

### `write_text_file`

```python
async def write_text_file(
    target_type: str,             # "onedrive" or "sharepoint"
    file_name: str,
    content: str,
    folder_path: str = "/",
    drive_id: str = "",
    site_id: str = "",
    conflict_behavior: str = "fail",
) -> str
```

Write text to OneDrive or SharePoint. `conflict_behavior`: `rename` / `replace` / `fail`.

### `upload_file`

```python
async def upload_file(
    target_type: str,
    file_name: str,
    content_base64: str,
    folder_path: str = "/",
    drive_id: str = "",
    site_id: str = "",
    conflict_behavior: str = "fail",
) -> str
```

Upload a binary file with automatic chunking for large files. Pass content as base64.

### `share_file`

```python
async def share_file(
    drive_id: str,
    item_id: str,
    name: str,
    recipient_email: str,
    requester_email: str,
    chat_id: str,
    role: str = "read",
    mime_type: str = "application/octet-stream",
    kind: str = "sharepoint",
    site_id: str = "",
) -> str
```

Share a file with another user. Authorization model: only sponsors can ask the agent to share. The recipient is unrestricted. `role`: `read` or `write`.

## Agent 365 Work IQ

Word document operations through the A365 Work IQ MCP. Use these for Word UI comments and document mutation.

### `read_word_document`

```python
async def read_word_document(url: str) -> str
```

Read a Word document and its comments through Agent 365 Work IQ. Use this instead of the Graph beta file-comment endpoints when the goal is to inspect Word UI comments or prepare a comment-thread reply.

### `create_word_document`

```python
async def create_word_document(file_name: str, content_html: str) -> str
```

Create a Word document through A365 Work IQ. HTML content is converted to native Word formatting.

### `add_word_comment`

```python
async def add_word_comment(drive_id: str, document_id: str, content: str) -> str
```

Create a top-level Word comment.

### `reply_to_word_comment`

```python
async def reply_to_word_comment(
    drive_id: str,
    document_id: str,
    comment_id: str,
    content: str,
) -> str
```

Reply inside an existing Word comment thread.

### `get_a365_file_metadata_by_url`

```python
async def get_a365_file_metadata_by_url(url: str) -> str
```

Read OneDrive / SharePoint file metadata by URL through Work IQ.

### `read_a365_text_file`

```python
async def read_a365_text_file(document_library_id: str, file_id: str) -> str
```

Read a small text file from OneDrive / SharePoint through Work IQ.

### `read_a365_binary_file`

```python
async def read_a365_binary_file(document_library_id: str, file_id: str) -> str
```

Read a small binary file from OneDrive / SharePoint through Work IQ.

## Identity and operations

### `whoami`

```python
async def whoami() -> str
```

Show the current agent identity, Teams connection status, and permissions. Verifies the agent is authenticated and connected.

### `audit_log`

```python
def audit_log(
    action: str,
    resource: str,
    outcome: str = "success",
    metadata: str = "{}",
) -> str
```

Record an audit event. Call BEFORE performing any action on the user's behalf. The audit trail proves the agent (not the human) performed the action. Events are written to `~/.entrabot/audit/` as daily JSONL files. See [Audit](audit.md).

### `read_interactions`

Query the body-side interaction log without re-reading Graph.

```python
async def read_interactions(
    chat_id: str = "",
    sender: str = "",
    action: str = "",
    direction: str = "",
    since: str = "",
    limit: int = 10,
) -> str
```

Filters are optional. `direction` is `inbound` or `outbound`; `since` is an ISO 8601 timestamp and can reach back up to seven days. Results are most-recent first. Use this before outbound sends to avoid duplicate replies.

### `bootstrap_body_state`

Return a single operational-continuity packet at session start.

```python
async def bootstrap_body_state() -> str
```

Returns counts for today's activity, top chats, open promises, watched-chat count, and cursor freshness. It is an index, not message content; use `read_interactions` for the underlying entries.

### `run_daily_summary`

```python
async def run_daily_summary(day: str = "", send: bool = True) -> str
```

Triage today's interactions and optionally email a summary. Reads the interaction log for `day` (UTC, `YYYY-MM-DD`; defaults to today). Sorts entries into `needs_you`, `handled`, `heads_up`; renders an HTML summary; archives to `<data_dir>/summaries/<day>.html`; emails it to the primary sponsor via Graph `/me/sendMail` when `send=True`.

## Related

- [Storage Backends](storage-backends.md) — where persistence lives.
- [Auth](auth.md) — how tokens are acquired.
- [Identity](identity.md) — sponsor gating, state machine.
- [Audit](audit.md) — fail-closed semantics.
- [Token Flows](../token-flows.md) — flow diagrams.
