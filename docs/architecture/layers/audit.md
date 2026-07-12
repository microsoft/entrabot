# Audit Layer

## Purpose

Every agent action that touches a resource needs a record that proves *who* did it — the Agent User, a delegated human, or nobody resolvable. `src/entrabot/tools/audit.py` is that record: a single `log_event()` call, appended to a daily JSONL file.

## Design principles

1. **Fail closed on attribution.** If an action is meant to be agent-attributed and no agent identity can be resolved — from the active identity session, config, or the credential store — `log_event()` raises `AuditAttributionError` instead of silently logging `"unknown"`. Bootstrap and preflight code paths that genuinely have no identity yet must opt in explicitly with `attribution_type="none"`.
2. **Append-only.** Events are written as one JSON line per event to `<audit_dir>/<YYYY-MM-DD>.jsonl`; nothing is edited or deleted after the fact.
3. **Audit before (and after) execute.** Security-sensitive call sites — Files Graph calls, Teams member adds, file shares — write a `pending` event before the underlying call runs, then a `success` or `failure` event once it resolves. Other tools audit once, immediately before the side effect.

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
) -> dict
```

| Field | Meaning |
|---|---|
| `event_id` | A fresh UUID per event. |
| `timestamp` | UTC, ISO 8601. |
| `agent_id` | Resolved from the active identity session, then config, then the credential store, in that order — or supplied directly. |
| `action` | What was attempted, e.g. `"teams.send"`, `"files.read_file"`. |
| `resource` | What it targeted. |
| `outcome` | `"pending"`, `"success"`, or `"failure"`. |
| `attribution_type` | `"agent"`, `"delegated-human"`, or `"none"` — see below. |
| `metadata` | Caller-supplied, action-specific JSON-serializable detail. |

## Attribution types

- **`agent`** — the action ran as the Agent User identity. This is the default, and the one that fails closed if no identity resolves.
- **`delegated-human`** — the action ran using the human's own delegated token (delegated auth mode). This is an explicit, distinct attribution — delegated-mode actions are never recorded as agent-attributed, because they aren't.
- **`none`** — no identity is available yet (bootstrap, preflight checks before any auth has happened).

## Audit-before-execute in practice

Files tools route every Graph call through `_audit_graph_call()`, an async context manager that emits the `pending` event, runs the body, and emits `success` or `failure` based on whether it raised — one code path instead of a hand-written pair at every call site. Teams tools that gate on sponsor authorization (`add_member`, `share_file`) call `log_event()` directly so that a *rejected* request — one that never reaches Graph because a gate failed — still produces a `failure` audit record, not silence.

See [Security Boundaries](../security-boundaries.md) for how audit fits into the wider fail-closed model, and [MCP Tools](../../reference/mcp-tools.md) for the tools that emit these events.
