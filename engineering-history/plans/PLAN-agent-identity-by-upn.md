# PLAN: Identify Self-Authored Messages by UPN, Not Display Name

**Status:** Shipped on `main` (2026-07)

Status: **In progress** — 2026-07-09
Owner: Brandon (sponsor) + entrabot agent
Trigger: 2026-07-09 cursor-replay incident — 6-week-old self-authored Teams messages replayed as fresh channel notifications after agent rename.

## Problem

The background Teams poll uses a **display-name string match** to identify the agent's own outbound messages before pushing them as channel notifications:

- `src/entrabot/mcp_server.py:1495` — hard-coded `"EntraBot Agent"`
- `src/entrabot/mcp_server.py:3324` — same hard-coded string
- `src/entrabot/tools/teams.py:1411` — `filter_human_messages` predicate compares `sender_display_name` against the hard-coded string

Graph now returns `"EntraClaw Agent"` in `message.from.user.displayName` because the agent was renamed. The self-authored filter therefore no-ops. Every outbound the agent has sent since the rename is eligible to be re-read as inbound. On 61 of 62 watched chats the cursor sits just before a self-authored message from the pre-rename window, so a normal poll pass picks those up as "new," pushes them via the channel notification path, and advances the cursor past them — one replay burst per chat.

**Root shape:** identifying an AAD principal by its display name is unsafe. Display names are user-mutable and change with rename. UPN and object-id are stable.

## Decision

Use **UPN as the canonical self-identity** in config and in the filter predicate. Use **AAD object-id as fallback** when the Graph message payload does not surface UPN.

- **Config canonical:** `ENTRABOT_AGENT_UPN` (e.g. `entra-agent@contoso.onmicrosoft.com`) — human-readable, matches the Entra directory, easy to grep.
- **Runtime match on the message payload:** whichever of `sender_upn` or `sender_id` (object-id) is present, in that order. Never `sender_display_name`.
- **Never trust display names for identity anywhere.** Displayed names are for humans, not for authorization or de-duplication.

### Why UPN and object-id, in that order

- UPN is stable in practice for machine identities under an owned domain (`contoso.onmicrosoft.com`). Renaming a UPN in production is a deliberate migration, not an incidental change.
- Object-id is guaranteed stable and is present on every Graph message. It's the correct fallback when UPN is absent from the payload.
- Display name is user-mutable, unindexed, and localizable. It has no business being an identity predicate.

## Scope

In scope for this PR:

1. **Doc rule** — "AGENT NAMES CHANGE — USE UPN" added to CLAUDE.md and AGENTS.md as a Non-Negotiable, mirrored as Learning #69 in `docs/runbooks/hard-won-learnings.md`.
2. **Code change** — `filter_human_messages` predicate switches to UPN-first, object-id-fallback. Callers at `mcp_server.py:1495` and `:3324` updated to pass config-sourced UPN + object-id instead of a hard-coded string.
3. **Config** — new env `ENTRABOT_AGENT_UPN` in `src/entrabot/config.py`; documented in `.env.example`; wired through `whoami` for observability.
4. **Cursor migration** — one-shot script that (a) migrates cursor blob keys to UPN-based schema if they are currently keyed by anything mutable, and (b) bumps `last_ts` + populates `seen_ids_tail` with recent self-authored message IDs to suppress the in-flight replay flood on next poll.
5. **Tests** — failing tests written first per the TDD non-negotiable. New tests cover: UPN-first match, object-id fallback when UPN absent, display name is never used, cursor migration idempotency.

Out of scope:

- Re-keying interaction log, promises, or daily-summary blobs (those are already keyed on chat_id / agent identity attribution, not display name).
- Extending the rule to email-sender identity (the email poll uses `mail` / `userPrincipalName` already; this is a Teams-poll bug).

## Files touched

Code:

- `src/entrabot/config.py` — add `AGENT_UPN` config field.
- `src/entrabot/tools/teams.py` — `filter_human_messages` predicate (currently line ~1411), sender extraction (currently line ~1400). Emit `sender_upn` alongside `sender_id`.
- `src/entrabot/mcp_server.py` — replace hard-coded string usages at ~1495 and ~3324 with config-sourced UPN + object-id lookup.
- `src/entrabot/tools/whoami.py` (or wherever whoami lives) — surface `agent_upn` for observability.

Tests:

- `tests/tools/test_teams.py::TestFilterHumanMessages` — new tests for UPN-first / object-id-fallback / display-name-never.
- `tests/test_cursor_migration.py` (new) — migration idempotency + last_ts bump behavior.

Docs:

- `CLAUDE.md` — add Non-Negotiable.
- `AGENTS.md` — mirror.
- `docs/runbooks/hard-won-learnings.md` — Learning #69.
- `.env.example` — document `ENTRABOT_AGENT_UPN`.
- `TODOS.md` and `docs/engineering-status.md` — reflect fix landing.

Migration:

- `scripts/migrate_cursors_to_upn.py` — one-shot, idempotent, dry-run-first flag.

## Test plan

Per TDD:

1. Write failing tests first:
   - `test_filter_matches_agent_upn` — inbound message with `sender_upn == config.AGENT_UPN` is filtered out.
   - `test_filter_matches_agent_object_id_when_upn_absent` — same, with only `sender_id`.
   - `test_filter_ignores_display_name_match` — inbound message with matching display name but non-matching UPN/object-id is NOT filtered.
   - `test_migration_is_idempotent` — running twice produces the same state as running once.
   - `test_migration_bumps_last_ts_and_populates_seen_ids_tail` — cursors advance past today.
2. Implement code.
3. Run `pytest -v --tb=short && ruff check .` — must be green.
4. Run migration script in `--dry-run` mode against the 62 real cursor blobs; inspect diff.
5. Run migration script live.
6. Restart entrabot MCP; verify next poll pass does not replay old self-authored messages (check interaction log for a clean poll cycle).

## Migration approach

The 62 cursor blobs live under the operational blob prefix (owned by the agent-user token). Investigation step during implementation:

- Read a sample cursor blob to determine current schema. Confirm whether the key includes any agent-identity component or is purely per-chat_id.
- If keyed by agent identity (name, id, or otherwise), plan a rename to UPN-based key.
- Regardless of key: bump `last_ts` to `now()` and add the last N self-authored message IDs to `seen_ids_tail` to prevent the fleet-safe idempotency layer from having to catch replays one-by-one.

Script contract:

```
scripts/migrate_cursors_to_upn.py --dry-run          # prints planned changes, no writes
scripts/migrate_cursors_to_upn.py                    # performs the migration
scripts/migrate_cursors_to_upn.py --verify           # confirms all cursors are on new schema
```

Idempotent: running against already-migrated cursors is a no-op.

## Rollback

- Code fix is a pure predicate change plus config addition. Reverting the PR restores the previous behavior (and the replay bug).
- Cursor migration is one-way in the sense that `last_ts` bumps forward; there's no automatic revert. If the migration mis-bumps a cursor, the next poll pass simply doesn't push messages before the new `last_ts` — no data loss, only a small delay for legitimately-new inbound (bounded by poll interval, 5s). Manual override: patch the specific cursor blob's `last_ts` backward.

## Non-goals

- We are not changing how the agent authenticates (three-hop flow is untouched).
- We are not changing cursor cloud storage (blob endpoint / container / RBAC) — only the schema.
- We are not adding a general-purpose "identity change detector." This PR fixes the specific bug and adds a rule so future filters use UPN by default.

## References

- Cursor-replay incident: 2026-07-09 conversation with Brandon, root cause traced by sub-agent in the same session.
- Adjacent fix: PR #97 (commit `77b6d49`) — fleet-safe channel poll + per-message cloud idempotency. That layer is working as designed; it just never saw these message IDs before, so it can't help.
- Related learning: Learning #16 (Graph `$filter`/`$orderby` unreliable for chat messages — always filter client-side). Same shape: don't trust Graph-returned fields for identity/filtering unless they're stable primitives.
