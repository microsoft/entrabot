# PLAN: Sponsor-DM Wait Tool for Copilot CLI (and Claude Code)

> Generated 2026-04-28 on branch `probe/copilot-instructions`.
> **Supersedes** the prior plan at `eafb184` (PR #42 / `feature/copilot-cli-inbox`)
> and the prior spec at `441573e` (`docs/architecture/NEXT-copilot-cli-watcher.md`).
> Both prior documents proposed a headless-`copilot -p` daemon. That approach is
> abandoned. See "Why the old plan was wrong" below.

## Problem Statement

The primary user experience is one sentence:

> *"I launch a CLI (Copilot or Claude Code), tell it 'I'm going to lunch — ping me
> when the build is done,' and get a Teams DM when it's done. I can ask follow-up
> questions from my phone. The agent only responds to messages from me, the Sponsor."*

Today this works in **Claude Code** (entraclaw's MCP server pushes inbound DMs via
the proprietary `notifications/claude/channel`, which wakes the LLM mid-turn).
It does **not** work in **Copilot CLI**, which has no equivalent push mechanism.
Microsoft's internal audience runs Copilot CLI; the experience needs to work there
without forcing them to install Claude Code.

## Why the old plan was wrong

The prior plan (`PLAN-copilot-cli-watcher.md @ eafb184`) tried to bolt a separate
`entraclaw-respond` daemon onto the system that would tail the interaction log
and spawn `copilot -p "<prompt>"` per inbound DM. This was wrong on two axes:

1. **It abandoned the running session.** The user's primary use case is "the
   agent that's already working on the task replies to me from inside that
   session." A spawned headless responder is a *different* agent with no
   knowledge of the in-flight task. The user-visible behavior would diverge from
   the Claude Code experience (which keeps the same session).
2. **It built an entire daemon to work around a missing wake mechanism that
   Copilot already supplies through MCP.** Copilot CLI honors blocking MCP tool
   calls cleanly (verified via probe; see below). A long-blocking tool *is* the
   wake mechanism.

Two `feature/copilot-cli-inbox` attempts — the headless responder and the PTY
supervisor (`supervise.py`) — both failed for the same root reason: they tried to
inject content into an Ink TUI from outside. The probe results below show the
right move is to inject *via the MCP tool surface*, which the TUI is built to
consume.

## Probe results (2026-04-28, branch `probe/copilot-instructions`)

Three minimal probes ran against a live Copilot CLI session. Probe code lives
only in branch history; reverted before this plan was committed.

| Probe | Question | Result |
|---|---|---|
| 1 | Does FastMCP `instructions=` reach Copilot's system prompt? | **No.** Same as Claude Code. `instructions=` is debug-UI-only on both clients. |
| 2 | Do MCP tool descriptions reach the LLM's prompt? | **Yes.** Sentinel `OCELOT_2026B` was visible to the model. |
| 3 | Does Ctrl+C cancel a long-blocking tool call cleanly? | **Yes.** `await asyncio.sleep(30)` was cancelled at ~5s with `"Operation aborted by user"`. No screen blank. CLI immediately usable. |

Result 3 is the load-bearing finding. The screen-blank symptoms from the four
PTY-supervisor attempts (Learnings #50–#53) were specific to the PTY wrapper, not
to Copilot's MCP transport. **Long-blocking MCP tool calls are safe in Copilot.**

## Architecture

```
  Copilot CLI / Claude Code session  (the agent the user is working with)
                │
                │ user: "I'm going to lunch, ping me when build done"
                ▼
  Agent calls send_teams_message("Build queued, will ping when done")
                │
                │ build runs in same session
                ▼
  Agent calls send_teams_message("Build done.")
                │
                │ user is at lunch — wants to be able to reply from phone
                ▼
  Agent calls wait_for_sponsor_dm()        ◄── BLOCKS, holds the turn open
                │
                │  (the user's terminal sits at "tool running"; they can Ctrl+C
                │   any time to retake control)
                │
                │  Inside the tool: short-poll Graph every 5s for new DMs in
                │  any watched chat from a Sponsor. Send progress heartbeats.
                ▼
  Sponsor DMs back from phone: "What about the lint warnings?"
                │
                ▼
  Tool returns: {chat_id, sender, content_text}
                │
                ▼
  Agent reads result as the next user-visible turn input, replies via
  send_teams_message, then calls wait_for_sponsor_dm again to re-arm.
```

**Key properties:**

- Same session. The agent that started the task is the agent that answers from
  Teams. No spawned `copilot -p`. No headless responder. Memory is preserved.
- Sponsor gating. The tool only returns when a Sponsor message arrives. Other
  inbound traffic (group chat, non-sponsors, the agent's own outbound) is
  discarded by the existing `_dedupe_for_injection` / sponsor-fetch helpers
  ported from `supervise.py`.
- Graceful Ctrl+C. The user can always retake control by hitting Ctrl+C; the
  asyncio cancellation propagates cleanly (probe-verified).
- Progress heartbeats. The tool sends `notifications/progress` every 30 seconds
  to keep the MCP client from timing out a multi-hour wait.
- Both clients work. Claude Code keeps `notifications/claude/channel` push as
  its primary path (no input lock during DMs while the user is at the desk);
  the wait tool is *additive* there, opt-in for the "I'm gone" pattern. Copilot
  uses the wait tool as its primary path. Same code, two surface behaviors,
  decided by the agent reading prompt context.
- Re-arm by tool call, not by daemon. No new processes. No new files watched.
  Anything the existing background poll already does (interaction log, daily
  summary) keeps working unchanged.

## Implementation: Three PRs (test-first)

### PR #1 — `wait_for_sponsor_dm` tool (Phase 1: core wake mechanism)

Delivers the long-blocking tool with sponsor gating, progress heartbeats, and
Ctrl+C semantics. Nothing else changes. Both CLIs gain it; agent prompt
guidance is in PR #2.

**Tests first** (`tests/tools/test_wait_for_sponsor_dm.py`, new):

- `test_returns_on_first_sponsor_dm`: simulate one Sponsor DM mid-poll → tool
  returns within one poll interval with `{chat_id, sender, content_text,
  message_id}`.
- `test_ignores_non_sponsor_traffic`: group-chat messages from non-sponsors,
  bot/system messages, the agent's own outbound — none of these wake the tool.
- `test_ignores_agent_own_outbound`: when the agent calls `send_teams_message`
  during the wait, that round-trip does not satisfy the wait.
- `test_dedupes_against_already_seen`: a DM that was already returned to the
  agent in a prior `wait_for_sponsor_dm` call (same `message_id`) is not
  returned again. Uses the existing `_dedupe_for_injection` set semantics.
- `test_progress_heartbeats`: with a fake MCP context, advance time by 90s →
  three `notifications/progress` calls fired with monotonically increasing
  `progress` values.
- `test_cancellation_propagates_cleanly`: `asyncio.CancelledError` raised
  mid-poll bubbles out without leaking the polling task and without writing a
  spurious "tool succeeded" audit event.
- `test_per_chat_rate_limit_respected`: existing rate limit on Graph reads is
  honored; the tool does not bypass it.
- `test_sponsor_set_refreshed_periodically`: after N polls, the tool re-fetches
  the sponsor list from `_state["agent_identity_sponsors"]`. Default refresh
  every 60s.
- `test_returns_dataclass_not_dict`: result is a `WaitForSponsorDmResult`
  dataclass, not a raw dict (per repo convention).
- `test_audit_event_emitted`: a single audit event of kind
  `wait_for_sponsor_dm.return` is recorded with the message_id.

**Implementation files:**

```
src/entraclaw/
  tools/
    wait_tool.py             — new module; defines @mcp.tool() and the
                                WaitForSponsorDmResult dataclass
  models.py                  — add WaitForSponsorDmResult dataclass + audit-
                                event kind constant
  mcp_server.py              — register the tool; share existing Graph
                                client and _state["agent_identity_sponsors"]
                                with it (no module-level globals)
```

**What gets ported from `supervise.py`** (in worktree
`feature/copilot-cli-inbox`, file slated for deletion in PR #3):

- `fetch_agent_identity_sponsors` → keep as-is, move into
  `src/entraclaw/identity/sponsors.py` (small new module so both the wait
  tool and the existing channel-push path import from one place).
- `load_agent_identity_sponsor_gate` → ported to `identity/sponsors.py`.
- `_dedupe_for_injection` → ported to `tools/wait_tool.py`. Same semantics:
  bounded LRU set keyed on `message_id`, capped at 1000 entries.

**Polling loop shape (skeleton, not final code):**

```python
async def wait_for_sponsor_dm(ctx: Context) -> WaitForSponsorDmResult:
    seen: deque[str] = deque(maxlen=1000)  # dedupe across re-arms in same proc
    sponsors = await _refresh_sponsors()
    last_sponsor_refresh = time.monotonic()
    last_heartbeat = time.monotonic()
    while True:
        await asyncio.sleep(WAIT_TOOL_POLL_INTERVAL)  # default 5s
        now = time.monotonic()
        if now - last_sponsor_refresh > SPONSOR_REFRESH_S:
            sponsors = await _refresh_sponsors()
            last_sponsor_refresh = now
        if now - last_heartbeat > HEARTBEAT_S:
            await ctx.report_progress(progress=int(now), total=None)
            last_heartbeat = now
        msg = await _next_unseen_sponsor_dm(sponsors, seen)
        if msg is not None:
            seen.append(msg.message_id)
            _audit("wait_for_sponsor_dm.return", message_id=msg.message_id)
            return WaitForSponsorDmResult.from_graph(msg)
```

`_next_unseen_sponsor_dm` reuses the existing background poll's Graph reads (or
calls them itself with the same rate limiter — TBD during implementation; the
two paths must not double-fetch). The existing `notifications/claude/channel`
push path is unchanged; both paths can coexist because dedup is keyed on
`message_id`.

**Risk addressed:** The Sponsor list could change mid-wait (rare but possible
during a multi-hour wait). The 60s refresh interval bounds the staleness
window. Test `test_sponsor_set_refreshed_periodically` enforces it.

**Decision points to surface during implementation:**

- Default `WAIT_TOOL_POLL_INTERVAL`: 5s mirrors the existing background poll.
  No reason to differ.
- Default `HEARTBEAT_S`: 30s. MCP spec doesn't mandate a maximum but Claude
  Code is known to time out at ~10 minutes of silence; 30s gives plenty of
  margin and is cheap.
- Should the tool accept an optional `timeout_s` arg? **No for v1.** The user
  retakes control with Ctrl+C. A timeout would require deciding what to return
  on expiry, which is a UX choice we don't need to make yet.

### PR #2 — Prompt doctrine (Phase 2: teach the agent when to call the tool)

Delivers the prompt changes that make the agent call `wait_for_sponsor_dm` at
the right moment without explicit user instruction every time.

**Inputs to the agent's context, in order of reliability proven by probe:**

1. **Tool description.** The `wait_for_sponsor_dm` description (in PR #1
   already) tells the model *when* to call it: "Call this tool when the user
   has explicitly stepped away (said they're going to lunch, leaving the desk,
   etc.) and asked to be pinged or paged when something completes. Do not call
   it just because you're idle." — proven to reach both Copilot and Claude Code.
2. **`prompts/anatomy/communication.md`** (existing body anatomy file; gets a
   new "Sponsor DM wait state" subsection). Body-first prompt loads via
   `_load_body_prompt()` and is part of every entraclaw session via the
   composed system prompt — proven via the persona-sati integration. This is
   the canonical place; it cannot be overridden by persona.
3. **`AGENTS.md` and `.github/copilot-instructions.md`** at the repo root.
   Auto-injected by Copilot CLI on every session (visible in the system
   messages of this very session). One-paragraph summary that points at the
   tool description and `prompts/anatomy/communication.md`. Redundant by
   design; redundancy is cheap and the cost of the agent forgetting the rule
   is high.
4. **`CLAUDE.md`** at the repo root. Same one-paragraph addition for symmetry.
   Claude Code already has the channel-push UX, so this is opt-in: "if the
   user explicitly says they're stepping away, prefer `wait_for_sponsor_dm`
   over leaving them with channel push." Avoids the small annoyance of a DM
   wake while the user is actively at the desk in a non-Sponsor chat.

**Tests first** (`tests/test_prompt_doctrine.py`, new):

- `test_communication_anatomy_mentions_wait_tool`: assert the rendered body
  prompt (via `_load_body_prompt`) contains the exact string
  `wait_for_sponsor_dm` in a "stepping away" context.
- `test_agents_md_mentions_wait_tool`: assert `AGENTS.md` contains the
  rule. (Plain string match. Linter-style guard.)
- `test_copilot_instructions_mentions_wait_tool`: same for
  `.github/copilot-instructions.md`.
- `test_claude_md_mentions_wait_tool`: same for `CLAUDE.md`.
- `test_tool_description_mentions_when_to_call`: introspect the registered
  MCP tool's description string and assert it contains "stepped away" or
  equivalent semantic gate.

The string-match tests are intentional belt-and-braces. They keep the four
documents from drifting out of sync silently (the agent only needs *one* of
them to fire correctly, but if all four agree, drift is detectable in CI).

**Files modified:**

```
prompts/anatomy/communication.md           — add "Sponsor DM wait state" subsection
AGENTS.md                                  — one paragraph
.github/copilot-instructions.md            — one paragraph
CLAUDE.md                                  — one paragraph (Claude-specific framing)
docs/runbooks/hard-won-learnings.md        — Learning #54 (probe results)
```

**Decision point:** the prompt rule must be written once, then the four files
either all reference the canonical text or all repeat it. Recommendation:
canonical text lives in `prompts/anatomy/communication.md`; `AGENTS.md` /
`copilot-instructions.md` / `CLAUDE.md` repeat the rule verbatim with a
back-reference. Three lines of duplication is fine; a fork in semantics is not.

### PR #3 — Removal (Phase 3: delete the failed approaches)

Once PR #1 + PR #2 are landed and verified, clean up.

**Files removed:**

```
src/entraclaw/supervise.py                         — PTY supervisor (failed approach)
src/entraclaw/watch.py                             — terminal-banner watcher (replaced)
console_scripts: entraclaw-supervise, entraclaw-watch  — removed from pyproject.toml
docs/architecture/NEXT-copilot-cli-watcher.md      — superseded
docs/architecture/PLAN-copilot-cli-watcher.md      — superseded by THIS file
                                                     (yes, this file replaces itself
                                                     once PR #3 lands; Git history
                                                     preserves the lineage)
.copilot/skills/* (worktree-only)                  — any PTY-helper scripts under
                                                     feature/copilot-cli-inbox
tests/test_supervise.py                            — removed
tests/test_watch.py                                — removed (the entries that test
                                                     the human-DM polling path; keep
                                                     any that exercise unrelated
                                                     interaction-log shape)
```

**Files surgically reduced (not removed):**

- `src/entraclaw/respond.py` was never written into `main`; nothing to remove
  there.
- `src/entraclaw/mcp_server.py`: leave the existing background poll +
  `notifications/claude/channel` push intact. They are still the right
  primary path for Claude Code. Only the wait tool is *new*.

**Tests first** (this PR is pure deletion + retargeting; tests are about
ensuring nothing accidentally still depends on the removed modules):

- `tests/test_no_dead_imports.py` (new, small): grep the source tree for
  `from entraclaw.supervise` / `from entraclaw.watch` — must yield zero
  matches outside `tests/` and `docs/`.
- Existing `pytest -v --tb=short && ruff check .` must pass after deletion.
  Ruff catches dead imports cheaply; if it complains, fix the dependent
  module.

**PR #42 disposition:** close as superseded with a reference to this plan
and the `probe/copilot-instructions` branch. Do not merge it.

## Decisions made (so far)

1. Long-blocking MCP tool, not a daemon. (Probe-verified Ctrl+C is clean.)
2. Same session, not spawned `copilot -p`. (The user's task agent answers, not
   a fresh agent with no context.)
3. Sponsor gating *inside* the tool, not at the prompt layer. (The model can't
   be relied on to filter; the gate must be mechanical.)
4. Heartbeats every 30s via `notifications/progress`. (Prevents client-side
   timeout on multi-hour waits.)
5. No `timeout_s` arg in v1. (Ctrl+C is the user's escape hatch.)
6. Tool description + `prompts/anatomy/communication.md` + AGENTS.md + Copilot
   instructions + CLAUDE.md — four redundant places, one canonical source.
   Skip FastMCP `instructions=` entirely (probe-verified neither client
   honors it).
7. Claude Code keeps `notifications/claude/channel` push as primary; wait tool
   is opt-in there. Copilot uses wait tool as primary. Same code, agent
   chooses based on context.
8. Learning #54 (probe results) lands with PR #2.

## What already exists and is reused

- `_state["agent_identity_sponsors"]` and the populating Graph call. Source of
  truth for sponsor identity. (Already in `mcp_server.py`.)
- The background interaction-log poll. Untouched. The wait tool is a *parallel*
  consumer of Graph; both paths dedupe on `message_id`, so they coexist.
- `_load_body_prompt()` and the `prompts/anatomy/*.md` `@include` machinery
  proven by the mind-body split (PR #14).
- `notifications/claude/channel` push for Claude Code. Untouched.
- Audit-log infrastructure. New event kind `wait_for_sponsor_dm.return`
  registered in `models.py`, otherwise unchanged.
- `fetch_agent_identity_sponsors`, `load_agent_identity_sponsor_gate`, and
  `_dedupe_for_injection` ported from `supervise.py` into the new
  `identity/sponsors.py` and `tools/wait_tool.py` modules. Code keeps its
  shape; only the import paths change.

## NOT in scope (v1)

- A `wait_for_anyone_at_all` variant for non-Sponsor traffic. (Trivial to add
  in v2 by widening the gate.)
- Multi-watch (different wait-tool instances on different chat-ID filters).
  (Single global Sponsor watch is sufficient; group-chat @-mentions can be
  v2.)
- Token-bucket rate limiting on outbound replies. (Existing per-chat rate
  limiter on `send_teams_message` already covers this.)
- A wrapper tool `reply_to_trigger(text)` that auto-targets the chat the wake
  came from. (Nice-to-have; v2.)
- Browser/desktop-notification fallbacks for users on neither Copilot CLI nor
  Claude Code. (Out of repo scope.)
- Persistent state across MCP server restarts mid-wait. (The CLI session is
  the unit of durability — if the MCP dies, the wait dies with it; user
  retakes control. This is correct; longer-lived persistence is a different
  product.)

## Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Model fails to call `wait_for_sponsor_dm` despite four prompt-injection vectors | Low (model has read this paragraph in this very session) | Belt-and-braces docs; first run with verbose logging; iterate on the wording in PR #2 if observed in practice |
| Heartbeat interval too short / spammy in MCP logs | Low | 30s default is conservative; expose `WAIT_TOOL_HEARTBEAT_S` env var for ops tuning |
| Heartbeat interval too long, client times out a long wait | Low (Claude Code's known timeout is ~10min) | 30s gives 20× margin |
| Ctrl+C blanks the screen on some Copilot version we haven't tested | Probe says no; risk that future Copilot regresses | Document the probe procedure in `docs/runbooks/`; rerun on each Copilot major version |
| Dedup state lost across MCP restart, same DM returned twice | Medium (in-process `deque`) | The agent will deduplicate at the conversation layer (it remembers what it just replied to); add note in tool description; revisit in v2 with a small persistent set if it bites |
| User on phone sends a DM during the agent's "in-flight build" *before* the agent calls `wait_for_sponsor_dm` | Medium | The interaction log captures it; first call to the tool returns it immediately (it's an unseen Sponsor message) |
| Sponsor list changes mid-wait (rare) | Low | 60s sponsor-refresh inside the tool |
| Multiple Sponsor DMs arrive while the tool is processing the first one | Medium | Tool returns one at a time. Agent re-arms by calling `wait_for_sponsor_dm` again. Order is preserved by the dedup queue. |
| Headless responder code in PR #42 still references shared modules after PR #3 deletion | Low | PR #42 will be closed before PR #3, not merged. `tests/test_no_dead_imports.py` catches any regression. |

## Test plan summary

Each PR ships test-first per repo non-negotiables.

- **PR #1**: 10 new tests in `tests/tools/test_wait_for_sponsor_dm.py`.
  Covers polling, sponsor gating, dedup, heartbeats, cancellation, audit.
- **PR #2**: 5 new tests in `tests/test_prompt_doctrine.py`. String-match
  guards across four documents + one tool-description introspection test.
- **PR #3**: 1 new test in `tests/test_no_dead_imports.py`. Pure deletion
  hygiene.

Total new: 16 tests. None should be quarantined. Repo baseline (484) → 500.

`pytest -v --tb=short && ruff check .` must pass on each PR.

## Operator runbook (lands with PR #2)

Single user-visible workflow:

```
$ copilot                            # or `claude`
> kick off the build, then ping me when it's done.
  i'm going for a walk.
  ...
  agent: "Build queued. I'll DM you in Teams when it's done."
  agent: [calls wait_for_sponsor_dm — terminal sits at "tool running"]
  ...
  (15 minutes later, Teams DM arrives on phone)
  agent: "Build finished. 3 lint warnings."
  user (from phone): "fix them"
  agent: [tool returns, agent reads the DM as next turn]
  agent: "On it."
  agent: [does the work, sends "Done." via send_teams_message]
  agent: [calls wait_for_sponsor_dm again to re-arm]
  ...
  user returns to laptop, Ctrl+C
  > ok thanks
```

`docs/runbooks/copilot-cli-walkaway.md` (new, lands with PR #2) captures the
full flow with screenshots / asciicasts.

## Relationship to other open work

- **MCP-disconnect investigation** (`docs/runbooks/mcp-disconnect-investigation.md`):
  unrelated. The wait tool does not change the MCP transport, message size, or
  payload shape.
- **Mind-body split / persona-sati** (PR #14): the wait tool is a body-layer
  capability. Persona-sati's voice contract may add personality to *how* the
  agent announces "I'll DM you when done," but the *when* and *how* of the wait
  itself are body rules.
- **ADR-005 cloud memory**: unrelated. The wait tool is in-process state only.
- **Three-hop Agent User flow**: unchanged. The wait tool reuses the existing
  Graph client and tokens.

## Open questions for review

1. **Is the same-session UX really better than spawned-responder?** Yes, per
   user statement 2026-04-28: *"the agent that's already working on the task
   replies to me from inside that session."* If this assumption is wrong, this
   plan is also wrong.
2. **Should `wait_for_sponsor_dm` accept an optional `chat_id` to scope the
   wait to one chat?** Defer to v2. v1 watches all Sponsor traffic across
   watched chats — the simpler default.
3. **Should we ship a `cancel_wait` tool, or rely on Ctrl+C?** Ctrl+C is
   sufficient and proven. A `cancel_wait` tool would require the model to be
   able to call it, which by construction it cannot do while the wait is
   blocking the agent's turn. Skip.
