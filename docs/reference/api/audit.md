# Audit

Audit logging proves the agent (not the human) performed each action. Events land in `~/.entrabot/audit/<YYYY-MM-DD>.jsonl`.

The body prompt's security section makes audit non-overridable: **if audit can't record, the action doesn't proceed.** Security-sensitive operations (cross-tenant Teams sends, chat member adds, memory mutations) must call `audit_log` before execution. Fail-closed is enforced by the call sites, not by the audit module itself.

## `log_event`

`src/entrabot/tools/audit.py`:

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

Write an audit event and return it. Writes a single JSON line to `~/.entrabot/audit/<YYYY-MM-DD>.jsonl`. Also logs via the standard `entrabot.tools.audit` logger.

| Field | Source | Notes |
|-------|--------|-------|
| `event_id` | `uuid.uuid4()` | Globally unique. |
| `timestamp` | `datetime.now(UTC).isoformat()` | UTC always. |
| `agent_id` | Argument or credential store | Falls back to `"unknown"` on any retrieval failure. |
| `action` | Argument | e.g. `"file_read"`, `"teams_send"`, `"identity_promote"`. |
| `resource` | Argument | What is being acted on. |
| `outcome` | Argument | `"success"`, `"pending"`, `"failure"`. |
| `attribution_type` | Argument | `"agent"`, `"delegated-human"`, `"none"`. |
| `metadata` | Argument | Arbitrary JSON-safe dict. |

### `attribution_type`

Distinguishes agent actions from delegated-human actions:

- `agent` — action performed as the Agent User identity (default).
- `delegated-human` — action performed using the human's delegated token (`delegated` mode).
- `none` — unauthenticated / unknown identity.

## `audit_log` (MCP tool)

The `audit_log` MCP tool is a thin wrapper around `log_event`, exposed so the LLM can record its own deliberation steps:

```python
@mcp.tool()
def audit_log(
    action: str,
    resource: str,
    outcome: str = "success",
    metadata: str = "{}",
) -> str
```

Call BEFORE performing any action on the user's behalf. The `metadata` argument is a JSON string (not a dict) because MCP-tool params are scalar-only. The wrapper parses it.

## `_audit_graph_call` middleware

`src/entrabot/tools/files.py` wraps every Graph Files call in an async context manager:

```python
@asynccontextmanager
async def _audit_graph_call(
    verb: str,
    resource: str,
    *,
    metadata: dict | None = None,
) -> AsyncIterator[None]
```

Emits `outcome="pending"` before the body runs and `"success"` or `"failure"` after. Replaces nine ad-hoc `log_event` blocks. Use the same pattern in new tool modules.

On exception, the metadata is enriched with `error` (class name) and `message` (str) before the failure event is written, then the exception re-raises unchanged.

## Fail-closed semantics

From `prompts/anatomy/security.md`:

> **Audit before acting.** Security-sensitive operations (adding a chat member, cross-tenant sends, changing memory state) must be logged via `audit_log` before execution. If audit writes fail, the action does not proceed.

`log_event` itself does not enforce this — it returns the event on success and only raises on actual write failure (disk full, permission denied). Call sites that need fail-closed must catch and abort. The Files tool wrapper above is the canonical pattern.

## Reading the audit log

```bash
# Today's events
cat ~/.entrabot/audit/$(date -u +%Y-%m-%d).jsonl | jq .

# All events for a specific resource
cat ~/.entrabot/audit/*.jsonl | jq 'select(.resource == "chat_19:abc...@unq.gbl.spaces")'

# Failures only
cat ~/.entrabot/audit/*.jsonl | jq 'select(.outcome == "failure")'
```

`run_daily_summary` reads the audit log and the interaction log to build the 17:00 (fixed UTC-7) triage email — see `src/entrabot/tools/daily_summary.py`.
