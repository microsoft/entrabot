# Audit Layer

## Purpose

Every agent action that touches a resource needs a record that proves *who* did it — the Agent User, a delegated human, or nobody resolvable. `src/entrabot/tools/audit.py` is that record: a single `log_event()` call, appended to a daily JSONL file.

## Design principles

1. **Fail closed on attribution.** If an action is meant to be agent-attributed and no agent identity can be resolved — from the active identity session, config, or the credential store — `log_event()` raises `AuditAttributionError` instead of silently logging `"unknown"`. Bootstrap and preflight code paths that genuinely have no identity yet must opt in explicitly with `attribution_type="none"`.
2. **Append-only, local.** Events are written as one JSON line per event to `<audit_dir>/<YYYY-MM-DD>.jsonl` and flushed to disk (`fsync`); Entrabot never edits or deletes them. The file is not signed: anything running as the same OS user can edit it.
3. **Audit before execute, fail closed.** Every tool that sends, creates, shares or deletes (`send_email`, `send_teams_message`, `send_card`, `post_thinking_placeholder`, `create_chat`, `add_teams_member`, `share_file`, the Files and Work IQ writes, the daily summary) writes a `pending` event before it acts and does not act if that write fails; most then write `success` or `failure`. A recipient-policy refusal writes a `denied` event and makes no Graph call.

## Event schema

`log_event()` returns (and writes) a dict with these fields:

```python
def log_event(
    action: str,
    resource: str,
    outcome: str = "success",
    agent_id: str | None = None,
    metadata: dict | None = None,
    attribution_type: str = "agent",
    origin: str = "system",
) -> dict
```

| Field | Meaning |
|---|---|
| `event_id` | A fresh UUID per event. |
| `timestamp` | UTC, ISO 8601. |
| `agent_id` | Resolved from the active identity session, then config, then the credential store, in that order — or supplied directly. |
| `action` | What was attempted, e.g. `"teams.send"`, `"files.read_file"`. |
| `resource` | What it targeted. |
| `outcome` | `"pending"`, `"success"`, `"failure"`, or `"denied"` (refused by the recipient policy). |
| `attribution_type` | `"agent"`, `"delegated-human"`, or `"none"` — see below. |
| `metadata` | Caller-supplied, action-specific JSON-serializable detail. |
| `origin` | `"system"` for events the server writes itself; `"model"` for events the model writes through the `audit_log` tool. |

## Attribution types

- **`agent`** — the action ran as the Agent User identity. This is the default, and the one that fails closed if no identity resolves.
- **`delegated-human`** — the action ran using the human's own delegated token (delegated auth mode). This is an explicit, distinct attribution — delegated-mode actions are never recorded as agent-attributed, because they aren't.
- **`none`** — no identity is available yet (bootstrap, preflight checks before any auth has happened).

## Audit-before-execute in practice

Files tools route every Graph call through `_audit_graph_call()`, an async context manager that emits the `pending` event, runs the body, and emits `success` or `failure` based on whether it raised — one code path instead of a hand-written pair at every call site. Teams tools that gate on sponsor authorization (`add_member`, `share_file`) call `log_event()` directly so that a *rejected* request — one that never reaches Graph because a gate failed — still produces a `failure` audit record, not silence.

See [Security Boundaries](../security-boundaries.md) for how audit fits into the wider fail-closed model, and [MCP Tools](../../reference/mcp-tools.md) for the tools that emit these events.
