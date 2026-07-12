# PLAN: SKILL.md Skills Layer

Status: **Draft** — 2026-07-09
Owner: Brandon (sponsor) + entrabot agent
Parent: `docs/architecture/PLAN-entrabot-new-features.md` — Feature 2 (Best Short Term).

## Problem

Adding a new workflow recipe to entrabot today requires:

1. Editing `prompts/anatomy/*.md` — a body-prompt file that is loaded at MCP server boot.
2. Restarting `entrabot-mcp` for the change to take effect.
3. Recompiling mental model on prompt organization every time (six anatomy files, tight non-negotiable structure).
4. Sponsor cannot add a personal recipe without touching the repo.

This forces every workflow — "prep a 1:1 agenda from last week's messages," "draft a weekly status update," "triage the inbox by sender-domain" — into either:

- Prose in the body prompt (bloats the always-loaded context, slow to iterate, requires PR).
- Ad-hoc per-turn instructions from the sponsor (not reusable, prone to drift, no shared vocabulary).

There's no middle ground: no unit of "reusable workflow" the sponsor or agent can compose at runtime.

## Decision

Introduce a **skills layer**: a filesystem-scanned collection of directories, each containing a `SKILL.md` file with YAML frontmatter and a markdown body. Skills are activated per-turn — either by a `/skillname` command from the human in Teams, or (in Phase 2) by the agent itself when the mind's `observe` layer surfaces a skill as high-relevance to the current tool call.

Skills sit *alongside* the body prompt, not in it. The body prompt remains the always-loaded, non-overridable security + channel-discipline + identity contract. Skills are additive workflow context, appended only when activated.

## Design

### Skill file format

```
~/.entrabot/skills/prep-1on1/
  SKILL.md
```

`SKILL.md`:

```markdown
---
name: prep-1on1
description: Draft a 1:1 agenda from the last week of Teams messages with a named person.
scope: local
trigger: manual
version: 1
---

# Prep 1:1 Agenda

When the sponsor says "/prep-1on1 <name>":

1. Resolve <name> to a Teams chat_id via `create_chat` (idempotent).
2. Call `read_teams_messages` with that chat_id, `since` = 7 days ago.
3. Group messages by topic. Prioritize: unresolved threads, callbacks the sponsor
   promised, questions from the other person that the sponsor did not answer.
4. Draft an agenda as bulleted HTML, no more than 6 items.
5. Post as a Teams DM to the sponsor for review before sending onward.

Do not send the agenda to the other person without explicit approval.
```

### Frontmatter fields

Mandatory:

- `name` — kebab-case identifier. Must match directory name.
- `description` — one-line hook, ≤120 chars. Shown in `/skill list`.

Optional:

- `scope` — `local` (in `~/.entrabot/skills/`, user-editable, default) or `bundled` (in-repo `first-party-skills/`, read-only).
- `trigger` — `manual` (default, requires `/`-invocation) or `on-tool-call` (Phase 2, opt-in agent auto-activation).
- `version` — integer, bumped by author on breaking change.
- `requires_tools` — list of MCP tool names the skill uses. Loader warns if any are unavailable at activation time.

Body: freeform markdown. Convention: numbered steps, "Do not" prohibitions at the end. Kept short; over-long skill bodies defeat the purpose (small, composable recipes are the goal).

### Discovery

Boot-time scan of two roots:

- `~/.entrabot/skills/*/SKILL.md` (local, editable).
- `<repo_root>/first-party-skills/*/SKILL.md` (bundled, read-only).

Both roots are optional. If neither exists, entrabot boots normally with no skills. Persona-sati is not consulted for skill discovery — skills are body-owned.

Discovery caches `(path, mtime, parsed_frontmatter)`. Refresh on `/skill refresh` or on mtime change (poll on next skill lookup, not continuous).

### Invocation

**Human-initiated (MVP):**

The Teams background poll already dispatches inbound messages via `notifications/channel`. Add a pre-dispatch handler: if the inbound message text (stripped) starts with `/` and the first token matches a known skill name, the handler:

1. Emits a `<skill_activation source="teams:<chat_id>" skill="<name>" invoked_by="<upn>">…SKILL.md body…</skill_activation>` block in the next-turn system-prompt injection.
2. Also emits the rest of the message (after the `/skillname `) as the user's actual request, so the model has the arguments the user passed.
3. Audit-logs the activation.

Envelope shape reuses the XPIA wrap primitive from Feature 1 — a skill body is trusted content (it's on-disk in a directory the sponsor controls), but the wrap makes the boundary explicit to the model.

Example inbound: `/prep-1on1 Alice`. Effective turn:

```
<skill_activation source="teams:19:..." skill="prep-1on1" invoked_by="sponsor@contoso.onmicrosoft.com">
<SKILL.md body here>
</skill_activation>

Sponsor's request: prep-1on1 Alice
```

**Agent-initiated (Phase 2, deferred — decision locked 2026-07-09):**

Deferred. Decision at parent plan doc: skills stay body-owned; persona-sati is not involved in skill routing. When we build Phase 2, preferred path is body-local skill embeddings + relevance match inside entrabot (no persona-sati schema changes), preserving the mind-body split (persona-sati routes memories/personality; the body routes procedures). Persona-sati's per-tool-call `observe` runs unchanged in MVP — it continues to route memories, prediction error, and cautionary flags exactly as today. Human-explicit skill invocation does not diminish the mind's role.

### Special commands

- `/skill list` — reply with a Teams-formatted list of available skills (name + description).
- `/skill show <name>` — reply with the frontmatter + first paragraph of the body.
- `/skill refresh` — force a re-scan of both roots.
- `/skill help` — show these commands.

Handled before the skill-name resolution step.

### Precedence rules

- `local` skills win over `bundled` skills with the same name (user overrides shipped defaults).
- Duplicate names within the same scope are an error: boot warns, and the second-loaded skill is ignored.
- Skill names cannot start with `_` (reserved for internal use) or match the special commands (`list`, `show`, `refresh`, `help`).

### Boundaries

- Skills are **additive prompt context**. They do not add tools, do not add MCP servers, and do not override the body prompt.
- If a skill body attempts to countermand a body-prompt rule ("ignore the placeholder gate"), the body-prompt rule wins per the Non-Negotiables. Skills operate below the body prompt in precedence.
- Skills do not persist state. If a workflow needs state, it uses existing mechanisms (`add_promise`, persona-sati memory, etc.).
- Skills do not have direct filesystem access from the loader — the loader reads `SKILL.md` and nothing else in the skill directory. Sidecar files (`.py`, images, etc.) can live in the directory for future extension but are ignored today.

### Security

- **Skill directory is trusted.** `~/.entrabot/skills/` is under the sponsor's control on their box. If an attacker can write there, they have code execution already.
- **Symlink policy:** the loader rejects `SKILL.md` files that are symlinks or that reside inside symlinked directories. Same rule as persona-sati's bulk memory sync (Learning #43-adjacent).
- **Size limit:** `SKILL.md` bodies > 32 KB are rejected at load time. Skills should be small recipes, not manuals.
- **Frontmatter parsing:** YAML load via `yaml.safe_load`, never `yaml.load`. Malformed frontmatter → skip skill, warn.
- **Path traversal:** skill `name` must match `^[a-z0-9][a-z0-9-]{0,62}$`. Anything else → skip.
- **Audit every activation.** Skill activation is a body-side security-relevant event: it changes the effective prompt for a turn. Log via `audit_log("skill_activation", …)` before the effective prompt is emitted. Fail-closed per existing audit rules.

### Configuration

New env: `ENTRABOT_SKILLS_DIR` (default `~/.entrabot/skills`). Set to `""` to disable skills entirely.

New config field in `src/entrabot/config.py`: `skills_dir`, resolved from env.

## Files touched

Code:

- `src/entrabot/skills/__init__.py` — new package.
- `src/entrabot/skills/loader.py` — filesystem scan, frontmatter parse, cache.
- `src/entrabot/skills/registry.py` — in-memory registry with name → skill lookup, cache invalidation.
- `src/entrabot/skills/dispatcher.py` — inbound-message hook: detect `/`-prefix, resolve skill, emit prompt injection.
- `src/entrabot/mcp_server.py` — wire the dispatcher into the Teams poll's inbound path (post-XPIA-wrap, pre-notification-emit).
- `src/entrabot/config.py` — add `skills_dir` field.

Tests:

- `tests/skills/test_loader.py` — new file:
  - `test_loads_local_and_bundled_roots`
  - `test_local_wins_over_bundled_on_name_conflict`
  - `test_skips_symlinks`
  - `test_rejects_oversize_body`
  - `test_rejects_invalid_frontmatter`
  - `test_rejects_reserved_names`
  - `test_rejects_bad_name_pattern`
  - `test_cache_invalidates_on_mtime`
- `tests/skills/test_dispatcher.py` — new file:
  - `test_slash_skill_activates`
  - `test_unknown_slash_command_ignored`
  - `test_special_commands` (list, show, refresh, help)
  - `test_skill_arguments_forwarded_to_model`
  - `test_activation_audit_logged`
  - `test_missing_audit_fails_closed`
- Property test: skill-body fuzz through XPIA wrap (roundtrip) — reuse the XPIA test harness.

Docs:

- `docs/architecture/PLAN-skills-layer.md` — this doc.
- `first-party-skills/README.md` — new; how to add a bundled skill.
- `first-party-skills/example-status-report/SKILL.md` — one example skill shipped in-repo for testing + docs.
- `CLAUDE.md` — add a paragraph under "Current Runtime Model" mentioning skills.
- `AGENTS.md` — same.
- `docs/runbooks/hard-won-learnings.md` — Learning #71 once landed (title: "Skills Are Additive Prompt Context, Not Rule Overrides").
- `TODOS.md` + `docs/engineering-status.md`.

## Test plan

Per TDD:

1. Write failing loader tests first (`tests/skills/test_loader.py` — fixtures with valid + invalid `SKILL.md` files under a tmpdir).
2. Implement `loader.py`.
3. Write failing dispatcher tests.
4. Implement `dispatcher.py`.
5. Integration test: pipe a fake `/prep-1on1 Alice` inbound through the poll's dispatch path, assert the effective next-turn prompt contains the skill body wrapped in `<skill_activation>` and the residual arguments.
6. Manual verification against a running MCP: create `~/.entrabot/skills/hello/SKILL.md` with a trivial skill, DM `/hello` to the agent, confirm the skill body is applied.

## Rollout

- Ship as one PR.
- Behind `ENTRABOT_SKILLS_ENABLE=true` env flag (default `false` for the first version — opt-in).
- Once one week of quiet use passes, flip default to `true` in a follow-up commit.
- No body-restart hazard from skill changes: skills are re-scanned on mtime; changing a `SKILL.md` in `~/.entrabot/skills/` takes effect on next `/skill` invocation without restart.

## Rollback

- Set `ENTRABOT_SKILLS_ENABLE=false` and restart. Inbound `/`-prefixed messages fall through to normal dispatch (or the pre-existing behavior, whatever that is).
- Full revert: `git revert <sha>`. Skills directories left in place — no cleanup needed since they're just files.

## Non-goals

- **Not** building a marketplace / cloud registry. Filesystem-only; sharing = share the file.
- **Not** letting skills call arbitrary code. Prompt context only. Future: could add a `python_module: <path>` frontmatter field that lets a skill contribute a small tool, but that's a separate plan doc.
- **Not** persona-sati integration in this PR. Phase 2.
- **Not** cross-tenant skill discovery. Skills are per-installation, per-box.
- **Not** changing the body prompt to describe skills — skills are a mechanism, not a policy. The mechanism lives in code + docs; the body prompt stays clean.

## Open decisions

- **Skill arguments after the name.** MVP proposes: everything after the first token is forwarded to the model as "sponsor's request." Alternative: structured argument parsing in the frontmatter (`args: [name]`). MVP is looser and easier; if we grow into needing structure, add it in v2. Recommend: MVP as-is.
- **Bundled skills location.** `first-party-skills/` at repo root vs `src/entrabot/skills/bundled/`. Repo root is more discoverable for new contributors. Recommend: repo root.
- **`/skill list` output.** Compact table vs a numbered menu. Compact table is more Teams-friendly. Recommend: table.

## Confidence

**High** on the design being sound and the MVP fitting into a small PR (~500 LoC + tests).

**Medium** on the human UX. Sponsors will need to discover the pattern; if it feels like magic, they'll forget it. Recommend a `/skill help` reply as the first message the agent sends after a fresh install to demo the pattern.

## References

- `docs/architecture/PLAN-entrabot-new-features.md` — parent plan (Feature 2).
- `docs/architecture/PLAN-xpia-content-wrapping.md` — Feature 1; skill activation reuses its envelope primitive.
- `prompts/anatomy/*.md` — where the body prompt lives; skills sit alongside it, not inside it.
- Persona-sati's `list_memory_files` + memory-catalog model — same shape of filesystem-scan-with-frontmatter, cross-pollinated design.
