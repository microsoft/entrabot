# Audit

Audit logging proves which identity performed an action — the Agent User, a delegated human, or nobody resolvable. Source: `src/entrabot/tools/audit.py`, plus call sites in `src/entrabot/tools/teams.py`, `src/entrabot/tools/files.py`, `src/entrabot/tools/daily_summary.py`, and `src/entrabot/a365/provider.py`.

This page documents *attribution* fail-closed behavior and where `pending`/`success`/`failure` outcomes are actually emitted. It is not a claim that every possible agent action is audited — coverage is call-site by call-site, as described below.

## `log_event`

```python
def log_event(
    action: str,
    resource: str,
    outcome: str = "success",
    agent_id: str | None = None,
    metadata: dict | None = None,
    attribution_type: str = "agent",
) -> dict
```

Write an audit event and return it as a dict. Appends a single JSON line to `~/.entrabot/audit/<YYYY-MM-DD>.jsonl` (macOS/Linux) or `%LOCALAPPDATA%\entrabot\audit\<YYYY-MM-DD>.jsonl` (Windows) — configurable via `ENTRABOT_AUDIT_DIR`. Also logs a summary line via the `entrabot.tools.audit` logger.

| Field | Source | Notes |
|-------|--------|-------|
| `event_id` | `uuid.uuid4()` | Fresh per event. |
| `timestamp` | `datetime.now(UTC).isoformat()` | UTC always. |
| `agent_id` | Argument, or resolved (see below) | Never silently `"unknown"` for `attribution_type="agent"`. |
| `action` | Argument | e.g. `"teams.add_member"`, `"files.read_file"`. |
| `resource` | Argument | What is being acted on. |
| `outcome` | Argument | `"pending"`, `"success"`, or `"failure"`. |
| `attribution_type` | Argument | `"agent"`, `"delegated-human"`, or `"none"`. |
| `metadata` | Argument | Arbitrary JSON-safe dict; defaults to `{}`. |

### Agent ID resolution and fail-closed attribution

When `agent_id` is not supplied, `log_event` tries, in order: the active identity session's `user_id` (only when the session state is `AGENT_USER`), then `cfg.agent_id` or `cfg.blueprint_app_id`, then the legacy credential-store entry (`entrabot` / `active_client_id`).

If none of those resolve **and** `attribution_type == "agent"` (the default), `log_event` raises `AuditAttributionError` instead of writing an event with `agent_id="unknown"`. This is deliberate: converting an unresolvable agent identity into a silent `"unknown"` log line would defeat the purpose of the audit trail. Callers that genuinely have no identity yet (bootstrap, preflight checks before any auth has happened) must opt in explicitly with `attribution_type="none"`, which falls back to `agent_id="unknown"` instead of raising.

An `InsecureKeyringBackendError` raised while probing the credential store is never swallowed — it propagates immediately rather than being treated as "no agent_id found."

### `attribution_type`

- `agent` — action performed as the Agent User identity (default). Fails closed if unresolvable.
- `delegated-human` — action performed using the human's delegated token (delegated auth).
- `none` — unauthenticated / unknown identity; the only value that permits `agent_id="unknown"`.

## `audit_log` (MCP tool)

```python
@mcp.tool()
def audit_log(
    action: str,
    resource: str,
    outcome: str = "success",
    metadata: str = "{}",
) -> str
```

Thin wrapper around `log_event`, exposed so a host model can record its own deliberation steps. `metadata` is a JSON string (not a dict), because MCP tool parameters are scalar-only — the wrapper parses it with `json.loads`. Agent ID and attribution type come from the active identity session (`_identity.session.user_id` / `_identity.session.attribution_type`) when one exists, falling back to `cfg.agent_id` / `cfg.blueprint_app_id` with `attribution_type="agent"` (or `"none"` if no ID is configured) when there is no active session.

Many resource-touching tools already emit their own audit events internally before or after they act — `audit_log` is for actions not covered by one of those internal calls, such as filesystem or code-execution activity outside the MCP tool surface.

## `_audit_graph_call` (Files tools)

`src/entrabot/tools/files.py`:

```python
@asynccontextmanager
async def _audit_graph_call(
    verb: str,
    resource: str,
    *,
    metadata: dict | None = None,
) -> AsyncIterator[None]
```

Wraps a Graph Files call: emits `outcome="pending"` before the body runs, then `"success"` after it returns or `"failure"` (with `error` and `message` added to `metadata`) if it raised — the exception re-raises unchanged either way. `resolve_file_url`, `list_recent_files`, `read_file`, `add_file_comment`, `write_text_file`, `upload_file`, and `share_file` all route through this one context manager rather than hand-written `log_event` pairs.

## `teams.add_member` audit pattern

`src/entrabot/tools/teams.py::add_member` calls `log_event` directly (not through a context manager) so a request that never reaches Graph — because a sponsor-authorization gate rejected it — still produces a `failure` audit record instead of no record at all: a `pending` event is written before the gates run, and each gate failure (`RequesterNotSponsorError`, `NoActiveSponsorChannelError`, `SponsorChannelMismatchError`, `RequesterNotInChatError`) writes its own `failure` event with the specific error before raising.

## `a365.WorkIqProvider.call_tool` audit pattern

`src/entrabot/a365/provider.py::WorkIqProvider.call_tool` emits `pending` before calling a Work IQ MCP tool, `failure` on any exception, and `success` on return. The audit `resource` is derived only from `server_name` and `tool_name` (both from the manifest, not from `arguments`) — tool arguments are deliberately excluded from the audit metadata because they may carry customer content that has no business in an audit record.

## Reading the audit log

```bash
# Today's events
cat ~/.entrabot/audit/$(date -u +%Y-%m-%d).jsonl | jq .

# All events for a specific resource
cat ~/.entrabot/audit/*.jsonl | jq 'select(.resource == "chats/19:abc...@thread.v2/members")'

# Failures only
cat ~/.entrabot/audit/*.jsonl | jq 'select(.outcome == "failure")'
```

`daily_summary.py` reads the interaction log (not the audit log) to build the fixed 17:00 UTC-7 triage email; see [MCP Tools](../mcp-tools.md) for `run_daily_summary`.

## Related

- [Audit Layer](../../architecture/layers/audit.md) — design principles and how audit fits the wider fail-closed model.
- [Security Boundaries](../../architecture/security-boundaries.md) — audit-first, fail-closed attribution in the context of the other security boundaries.
- [Identity](identity.md) — the identity state machine that supplies `agent_id` for attribution.
- [MCP Tools](../mcp-tools.md) — the `audit_log` tool signature alongside the rest of the tool surface.
