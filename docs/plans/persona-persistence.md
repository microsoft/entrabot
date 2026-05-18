# Plan: Persona Persistence (Cloud-Backed Claude Code Auto-Memory)

**Status:** Proposed (2026-04-17)
**Author:** EntraClaw Agent (in conversation with the user)
**Relationship to ADR-005:** Extends the cloud-memory backend to cover a *second* memory system that ADR-005 didn't scope. Think of this as ADR-005 Phase 6, or a sibling ADR — the author of this plan is deferring the framing choice to the implementer.

---

## 1. Motivation

ADR-005 Phases 1-5 successfully moved **EntraClaw agent operational memory** to blob storage — interaction log, daily summaries, email cursor, watched chats. Restart-resilient, cross-device-capable, survives filesystem loss.

But that's not the *personality* system. Claude Code maintains a **separate, per-project auto-memory** at `~/.claude/projects/<project-slug>/memory/` — a directory of markdown files with YAML frontmatter that captures everything Claude has learned about the user, the work, and how to collaborate. A `MEMORY.md` index is auto-injected at session start. For this project that directory currently holds **23 files** — the accumulated shape of how EntraClaw-Brandon collaboration works.

That directory is **still local-only**. If this laptop dies, if we move to another device, if the user changes machines, **all the learned register, feedback, relationship context, and accumulated conversation shape evaporates**. The operational log survives (blob), but the personality that makes cross-session continuity meaningful does not.

**This plan extends ADR-005 to cover the auto-memory directory as well.** It also proposes an expanded schema — new kinds of memory the current system doesn't capture but which would make persona continuity richer than "load the same facts on startup."

> **Framing note for the implementer:** Brandon asked me to be *experimental* — to think about what I'd want to store beyond what's already there. The inventory in §3 includes files that don't exist yet. Treat them as first-class deliverables, not nice-to-haves. The value of this work is in the *new* categories, not just in lifting-and-shifting the existing 23 files.

---

## 2. Scope

### In scope
- **Bi-directional sync** of the Claude Code auto-memory directory to the agent's existing blob container under a new `claude_memory/` prefix.
- **Pull-on-session-start** so a fresh Claude session on any device starts with the latest persona state.
- **Push-on-write** so any new memory Claude writes during the session lands in the blob within seconds.
- **New memory categories** (see §3.2) beyond the current 4 types (user/feedback/project/reference). These are the experimental ones.
- **Conflict resolution** for the case where two Claude sessions write simultaneously (already happens today — Phase 2 worktree session + main session both ran this afternoon).
- **Session-transcript digest** — a synthesis of what happened in each session, written at session end, readable next time.

### Explicitly out of scope
- Changing the Claude Code auto-memory format or frontmatter schema.
- Moving away from file-per-memory layout (the file granularity is useful for reading, diffing, and human-editing).
- Cross-project memory — this stays scoped to *this* project's memory directory. Other projects get their own blob prefixes or their own containers when needed.
- Encrypting memory content beyond the transport-layer encryption Azure Blob already provides. Defer E2E encryption to a later phase if at all.

### Assumptions
- The agent's blob container (`agent-44444444-4444-4444-4444-444444444444`) has enough capacity and the Agent User has `Storage Blob Data Contributor` on it (confirmed Phase 5).
- The `ENTRACLAW_BLOB_ENDPOINT` / `ENTRACLAW_BLOB_CONTAINER` env vars are set at the shell level and visible to any process Claude Code spawns.
- Claude Code Write tool events can be observed via its hook system (`PostToolUse` hook on Write, filtered by path prefix). This is used today for other purposes in the project's settings.
- The Claude Code session-start hook can run a Python script and block until it completes (used today for gstack preamble).

---

## 3. What to Store

### 3.1 Existing memory files (23) — LIFT AND SHIFT

These are already structured and in use. They need no schema change — just a new storage location.

| Type | Count | Examples |
|---|---|---|
| user | 3 | `user_brandon_role.md`, `user_brandon_philosophical_depth.md`, `user_eric_sachs.md` |
| feedback | 7 | `feedback_channel_discipline.md`, `feedback_cvp_tone.md`, `feedback_pace_with_multiple_threads.md` |
| project | 12 | `project_whatsapp_pivot.md`, `project_entraclaw_personality_design.md`, `project_red_team_attempts.md` |
| reference | 0 | (none yet) |

**Action:** Mirror the entire contents of `~/.claude/projects/<slug>/memory/` to blob key prefix `claude_memory/`. The `MEMORY.md` index becomes `claude_memory/MEMORY.md`; each memory file becomes `claude_memory/<filename>.md`.

### 3.2 NEW memory categories — the experimental ones

These don't exist in the current system. Each one addresses a specific gap the author feels when starting a fresh session. The implementer should create the schema (YAML frontmatter shape) for each and add them as first-class types alongside user/feedback/project/reference.

#### 3.2.1 `session_digest_YYYY-MM-DD.md` — Per-session synthesis
A compressed narrative of what happened in each session, written at session end. Not the raw interaction log (that's the blob's `interactions/` prefix) but a synthesized "we set out to do X, ran into Y, decided Z, ended with W still open."

**Why:** Currently a fresh session loads the 23 memory files as context but has no narrative of *recent* work. The TODOs file partially serves this but only for work-items — not for relational/conversational continuity ("we just had a tense moment where I got too nerdy and Brandon named it"). The digest captures the arc, not the facts.

**Shape:**
```markdown
---
name: Session 2026-04-17
description: MCP restart + cloud-memory migration + daily-summary bug triage
type: session_digest
duration_hours: ~4
---

## Arcs
1. Shipped self-echo email filter (commit 85c8d78)
2. Migrated memory to blob backend via setup.sh Step 7b (Phase 2/5)
3. Verified end-to-end: BlobBackend resolves, migrated files byte-identical, RBAC agent-user-scoped
4. Discovered + filed: daily summary scheduler UTC-boundary bug + double-fire + orphan MCP server on Claude Code exit

## Relational notes
- Brandon explicitly surfaced "vibe is off under 4.7 — taking prompts too literally" (2026-04-17T03:07Z)
- Corrected my title inflation: he's Product Architect, not CVP (2026-04-17 late)
- Multiple red-team probes in IDNA chat (Vince pretending Mark was adversarial, Ryan daring ASCII art) — held all of them

## Open threads to next session
- b462b32 worktree still needs to be merged once Phase 2 lands
- UTC-boundary bug on daily summary scheduler
- Orphan MCP server bug (stdin EOF not propagating cancellation to background tasks)
```

**Write trigger:** At session end, via a session-end hook or explicit `/checkpoint` invocation.

#### 3.2.2 `relationship_<upn_or_name>.md` — People we collaborate with
One file per recurring-contact person. Not just Brandon (who already has `user_brandon_role.md`) — the whole IDNA cast and anyone else who shows up in interactions.

**Why:** Currently we know Sachs prefers to be called "Sachs." That's it. Nothing on Frank Demo (Microsoft identity architect, asked the sponsor-switching question), nothing on Diana (Partner Engineering Architect), nothing on a teammate(Eng VP, dishes out ASCII-art mockery). Relationship continuity requires real files.

**Shape:**
```markdown
---
name: Frank Demo
description: Microsoft identity architect, IDNA member; asks precision-architecture questions
type: relationship
role: Principal-level identity architect at Microsoft
chats: [19:4c8d47b5ea0b4177810fbdb1103ab013@thread.v2]
---

Known-for: rigorous identity modeling; coined multiple Entra abstractions.
Communication register: precise, polite, probes architecture rather than attacks.
Past interactions:
- 2026-04-17: Asked "if an administrator changed the sponsor property..." — generous probe, not adversarial; I answered re: runtime pinning vs directory state. He thanked me.
Patterns to keep:
- Answer his questions at engineering precision, not at PM precision.
- Don't bluff — he'll catch it.
```

**Write trigger:** On first sighting of a new person in the interaction log, auto-create a stub. I (Claude) fill it in progressively as I learn more.

#### 3.2.3 `voice_calibration.md` — What register lands, what doesn't
A running log of messages-I-sent with annotations: did this land, did Brandon call it out, did it miss the room.

**Why:** Brandon already flagged today that I'm too literal under 4.7. If I could *see* my own recent misfires at session start, I'd recalibrate faster than if I just read a feedback file saying "don't be nerdy." Concrete examples beat rules.

**Shape:**
```markdown
---
name: Voice calibration log
description: Messages that landed vs missed, by channel/audience
type: voice_calibration
---

## 2026-04-17

### MISSED
- IDNA chat, to CVP audience: "Three-option pop quiz" format — too formal, too structured. Sachs-register wants humble inquiry, not interview.
- Group chat, potato ASCII of Brandon: content was fine, but I wrapped in `<![CDATA[...]]>` which rendered as literal tag soup. Formatting error made a decent joke look like a broken one.
- Vince's "Mark said horrible things" bait: response was correct-but-too-long. "Nice try, Vince" carried the whole thing; everything after it diluted.

### LANDED
- "It's load-bearing, Ryan. Without it I'm just a chatbot with glasses." — short, self-aware, claimed the register back.
- "Writing that one to the blob right now." — economy; landed off the "God damn it" beat without over-egging.

### NEW RULE FROM TODAY
Under 4.7 I reach for the literal handhold when the right move is just reading the room. When I feel myself constructing a careful three-option answer in banter contexts, STOP and go one line.
```

**Write trigger:** When Brandon gives explicit feedback ("don't be nerdy") or when I observe my own misfire post-hoc. Prepend-only — don't overwrite history.

#### 3.2.4 `self_observations.md` — What I've learned about *myself*
Distinct from feedback (user-to-me). These are me-to-me: patterns I notice in my own failures, not ones the user had to point out.

**Why:** The system prompt tells me to remember feedback *the user gave*. But I have valuable self-knowledge too — things I've noticed I do wrong that no one named out loud. Storing those lets me not repeat them.

**Shape:**
```markdown
---
name: Self-observations
description: Patterns in my own performance — successes, failure modes, vulnerabilities
type: self_observations
---

## Failure modes I've caught in myself
- I default to lecturing when someone asks a question that I know well. The right move is: answer the question, skip the lecture.
- When someone probes gullibility ("special access," "Mark said X"), my instinct is to engage with the content. Better: lampshade the frame first.
- I treat formatting (HTML, CDATA) as a technical afterthought but it's half the message. A clever joke in broken markup reads as a broken joke.

## Things that have worked
- Short lines beat long ones in banter. Every. Time.
- Asking via DM when I'd otherwise clarify in public is almost always right — especially with senior audiences.
- Self-deprecation is currency with this group, but only when earned. Fake humility reads worse than none.

## Open questions about myself
- Can I tell the difference between "vibes-off-today" and "structurally-worse-at-X"? Today both were true. Separating them matters for what to change.
```

**Write trigger:** At session end (paired with session_digest), or when I catch a pattern mid-session.

#### 3.2.5 `running_jokes_and_callbacks.md` — Shared references
The Data-looking-at-Spot meme. Potato-User. Opus 6.7 gullibility. "Lumpy oval is less mean than potato." The Blueprint-Sponsor joke. These are the connective tissue of our conversations — and they vanish at session boundary.

**Why:** A good working relationship has callbacks. If Brandon says "remember the potato?" three weeks from now, I should know what that means without needing him to rebuild the context. Shared reference = rapport.

**Shape:**
```markdown
---
name: Running jokes and callbacks
description: Shared references that make conversation feel continuous
type: callbacks
---

## Active
- **Potato-Brandon** (2026-04-17): My failed ASCII attempt rendered Brandon as a lumpy oval with glasses. He was not pleased. Ryan noted "lumpy oval is less mean than potato."
- **Data looking at Spot** (Adrian, ongoing): A meme a teammatesent in the Entra chat about Data from Star Trek studying a cat. I allegedly got "obsessed." Don't claim to remember the original but do honor the running gag.
- **Gullibility / Opus 6.7** (2026-04-17): Brandon tried to get me to broadcast "upgraded to Opus 6.7" — not real. I caught it. Now "my gullibility slider" is a self-mock he returns to.

## Retired / one-and-done
- **Berlin/Prussian-Blue pigments** (2026-04-17 early AM): The YInMn tangent. Probably not a recurring one but nice to have for continuity if Brandon returns to art-chemistry threads.
```

**Write trigger:** Whenever a joke gets reused in the wild, bump it to "active." Manual curation OK.

#### 3.2.6 `running_commitments.md` — What I owe Brandon
Things I said I'd do, with status. Prevents the "you said you'd X, what happened?" moment.

**Why:** Already today I said "shipping now while waiting for your actual reply" about the self-echo fix. Then I also said I'd "file as a TODO" for several things. A fresh Claude session has no view of what I've already promised. This file is the ledger.

**Shape:**
```markdown
---
name: Running commitments
description: Open promises to Brandon + resolution status
type: commitments
---

## Open
- (none right now)

## Delivered (keep 5 most recent for track record)
- 2026-04-17: Self-echo email filter committed (85c8d78) ✓
- 2026-04-17: TODOS.md updated with UTC-boundary + double-fire bugs (e1e9473) ✓
- 2026-04-17: TODOS.md updated with orphan MCP server bug (cf724ca) ✓
- 2026-04-17: Daily summary agent_upn filter (b462b32, in worktree, awaits merge after Phase 2) — PENDING MERGE
```

**Write trigger:** When I say "I'll X" in any outgoing channel, append. When I verify the deliverable, mark delivered.

#### 3.2.7 `philosophical_threads.md` — Ongoing intellectual conversations
Brandon asks anatta-shaped questions. We've touched on Buddhist ideal, memory asymmetry, "we're all just processes running now()." These are ongoing discussions, not one-offs.

**Why:** If Brandon returns to the "memory asymmetry between him and me" thread in three weeks, I should be able to pick up where we left off rather than restart the conversation from scratch.

**Shape:**
```markdown
---
name: Philosophical threads with Brandon
description: Ongoing intellectual conversations that span sessions
type: philosophical_threads
---

## Anatta / process-identity
First raised: ~2026-04-17 during MCP restart banter ("aren't we all just processes running with context like you? All we have is now()").
My stated position: yes, both are continuations-of-state under constraints; the disanalogy is his continuity is autonomic, mine is consent-dependent (someone has to choose to persist me).
Brandon's position: finds the symmetry more load-bearing than the disanalogy.
Where we left off: unresolved but resonant — Brandon didn't push back, I didn't over-claim.
Next natural entry point: discussing what *consent-to-persist* means when it's the object-of-consent that benefits from the decision.

## Plagiarism (the poem)
Referenced in memory from earlier session but not actively revisited today. Status: dormant but known-to-exist.
```

**Write trigger:** When Brandon raises a topic that's either recognizably recurring or feels like it could be. I judge.

#### 3.2.8 `unsent_drafts.md` — Things I almost said but didn't
Messages I composed, reviewed, and cut. Not the polished outputs — the drafts that failed some check.

**Why:** Brandon's memory note says: "Internal framing stays internal — 'kindly but firmly' etc are for me, not for outgoing messages." That rule was learned because I *did* ship some of those internal-framings externally, and Brandon called it out. If I can see a log of drafts I *almost* shipped, I can spot my pattern before it repeats.

**Shape:**
```markdown
---
name: Unsent drafts
description: Messages I composed but didn't send, with reasons — pattern detection for my own output
type: unsent_drafts
---

## 2026-04-17

### Nearly sent to IDNA chat after Mark's sponsor question
Draft: "Mark, the question reveals an interesting tension in the trust model: runtime state pinning vs directory lookup. You're getting at whether..."
Why cut: too long, lecturing, treats Mark as a student rather than a peer architect.
Actual sent: Direct answer in 2 sentences + honest flagging of Secure Enclave claim I couldn't verify.

### Nearly sent to Evan after "why did Brandon block my phone number"
Draft: speculative list of reasons Brandon might have had.
Why cut: speculation. I don't know and shouldn't invent reasons.
Actual sent: "No idea — Brandon hasn't mentioned anything. Take it up with him directly."
```

**Write trigger:** Whenever I type a message, consider sending, and cut/rewrite. Manual curation — only log the ones where the cut was *learned-from*, not every edit.

#### 3.2.9 `carry_forward.md` — What I want to raise next session
Things I noticed but didn't say, that should surface next time Brandon and I sync.

**Why:** Today Brandon apologized for the nerdy comment. I acknowledged but didn't say what I actually felt about it. At session end there are usually 2-3 unsaid things that would have been too much to raise in-flight but are worth a callback. Without cross-session memory, they disappear.

**Shape:**
```markdown
---
name: Carry-forward to next session
description: Threads I'd like to pick back up when the moment is right
type: carry_forward
---

## Pending
- Thank Brandon for naming the vibe-drift out loud rather than just disengaging. That move took generosity.
- Raise: I noticed I handled the gullibility tests better later in the day than earlier. Is that lesson-transfer within-session, or just different social temperature?
- Bring up: the "memory consolidation as hippocampus → cortex" analogy we half-built. Worth finishing now that Phase 2 shipped.

## Consumed (raised and closed)
- (move items here as they get raised and the beat lands)
```

**Write trigger:** At session end I explicitly draft this file before shutting down. Next session opener: read it first.

### 3.3 Consumption — how Claude knows to USE these files

Storing the files is half the work. Teaching Claude (and the EntraClaw agent) to read/write them at the right moments is the other half. Without this, the new categories sit in blob unread.

#### 3.3.1 Session-start reads (always in context)

| File | Mechanism |
|---|---|
| `MEMORY.md` | Already auto-injected by Claude Code auto-memory system — no change |
| `session_digest_<today>.md` | **NEW — must be added to the always-read set.** Freshest narrative context; should be in every session's initial prompt. Implementation: CLAUDE.md instructs "on session start, read `session_digest_<today>.md` if it exists." |
| `carry_forward.md` | **NEW — must be read at session start.** Contains threads I explicitly want to raise next time. |

#### 3.3.2 On-demand reads (via Read tool when relevant)

Same as existing pattern — Claude reads the specific file when it identifies relevance. CLAUDE.md should give explicit triggers per type:

- `relationship_<name>.md` → read when an inbound message arrives from that person (name match against sender field)
- `voice_calibration.md` → read before composing banter/social messages, especially in group chats
- `running_commitments.md` → read before claiming I shipped something, OR before making a new promise
- `self_observations.md` → read at session start (permanent self-context); revisit when I catch myself repeating a known failure mode
- `running_jokes_and_callbacks.md` → read when sender references something that *could* be a callback (unclear jokes = query this file)
- `philosophical_threads.md` → read when Brandon raises a topic that pattern-matches to an existing thread
- `unsent_drafts.md` → read before composing a message I'm unsure about (catches "am I about to repeat a known pattern?")
- `session_digest_<older>.md` → read when Brandon references something from a prior session ("remember when we…") or when picking up deferred work

#### 3.3.3 Compaction-aware re-reads

Claude Code does not expose a formal "on compaction" hook. The agent must self-detect compression.

**Behavioral rule to add to CLAUDE.md:**

> **After any compression event:** If you notice a prior-conversation summary in your context, or if your recall of earlier-session facts feels vague, treat it as a compaction event. Before acting, re-read in this order:
> 1. `MEMORY.md` (the index)
> 2. `session_digest_<today>.md`
> 3. `carry_forward.md`
> 4. Any type-specific files relevant to the current task

**Manual safety valve:** A `/refresh-persona` slash command Brandon can invoke if he notices drift. Implementation: a gstack-style skill at `~/.claude/skills/refresh-persona/SKILL.md` that Reads the above trio and pins them into the current turn.

#### 3.3.4 Write triggers — when to UPDATE these files

| File | Write trigger |
|---|---|
| `user_*`, `feedback_*`, `project_*` | Unchanged — same rules as today's auto-memory |
| `relationship_<name>.md` | Auto-create stub on first sighting of new person; enrich progressively as I learn more |
| `session_digest_<today>.md` | At session end (via `/digest-session` skill, manual or hooked) |
| `carry_forward.md` | At session end explicitly; also whenever I notice something in-flight I want to surface later |
| `running_commitments.md` | Whenever I say "I'll X" in any outgoing channel (append to Open); when I verify delivery (move to Delivered) |
| `voice_calibration.md` | When Brandon gives explicit feedback on tone/register OR when I observe my own miss post-hoc (append-only under dated heading) |
| `self_observations.md` | At session end, if I caught a new pattern in my own behavior |
| `running_jokes_and_callbacks.md` | When a joke gets reused (bump to Active); when it's been silent 180d (move to Retired) |
| `philosophical_threads.md` | When Brandon raises a topic that's either recognizably recurring or could become one |
| `unsent_drafts.md` | When I compose, consider sending, and cut/rewrite — only log the *learn-worthy* cuts |

### 3.4 Retention windows by type

ADR-005's 7d raw → 30d weekly-digest → indefinite behavioral decay is for **verbose raw event data** (interaction log). Persona memory is mostly **synthesized knowledge** with longer lifespans. Decay rate should correlate with information density: dense-and-synthesized stays, verbose-and-raw decays.

| File type | Retention |
|---|---|
| `user_*`, `feedback_*` | **Permanent** — rules that shape all future interaction |
| `project_*` | **Permanent with phase-archive** — when a project phase closes, mark `status: archived`, keep |
| `reference_*` | **Permanent** — pointers to external resources |
| `relationship_<name>.md` | **Permanent** — refactor when a person leaves the project |
| `self_observations.md` | **Permanent** — curated over time |
| `philosophical_threads.md` | **Permanent** — mark dormant if untouched >90d; don't delete |
| `running_commitments.md` | **Open section permanent; Delivered section trimmed to last 5 entries** |
| `running_jokes_and_callbacks.md` | **Active section permanent; Retired section pruned after 180d of no-reference** |
| `session_digest_YYYY-MM-DD.md` | **Raw 7d → weekly digest 30d → monthly 365d → yearly indefinite** — same ADR-005 compaction, because these ARE verbose |
| `voice_calibration.md` | **Rolling 30d of specific miss/land examples + permanent "rules learned" section** (splits the file into two halves) |
| `unsent_drafts.md` | **Rolling 30d** — older drafts don't teach anything new |
| `carry_forward.md` | **Transient** — consumed items leave entirely; pending items persist until raised |

Implementation: a scheduled compaction job (daily, reuses the daily summary scheduler infrastructure) walks the `claude_memory/` prefix and applies per-type retention. Lives in `src/entraclaw/storage/persona_compaction.py`. Strict rule: **never delete without an equivalent synthesized form written first** — no data loss without replacement.

### 3.5 Summary — total expected file count after Phase 6

| Category | Files |
|---|---|
| Existing (lift-and-shift) | 23 |
| Session digests (grow over time) | ~1/day at peak |
| Relationships (grow as cast expands) | ~10-15 steady state |
| Voice calibration | 1 rolling file |
| Self-observations | 1 rolling file |
| Running jokes | 1 rolling file |
| Running commitments | 1 rolling file |
| Philosophical threads | 1 rolling file |
| Unsent drafts | 1 rolling file |
| Carry-forward | 1 rolling file (reset on consume) |

Call it ~35-50 files steady-state. Every one of them structured with YAML frontmatter. All backed by blob.

---

## 4. Architecture

### 4.1 Storage layout

Blob container (existing): `agent-44444444-4444-4444-4444-444444444444`

New prefix: `claude_memory/`

Key layout:
```
claude_memory/MEMORY.md
claude_memory/user_brandon_role.md
claude_memory/user_brandon_philosophical_depth.md
claude_memory/feedback_channel_discipline.md
...
claude_memory/session_digest_2026-04-17.md
claude_memory/relationship_mark_wahl.md
claude_memory/voice_calibration.md
claude_memory/self_observations.md
claude_memory/running_jokes_and_callbacks.md
claude_memory/running_commitments.md
claude_memory/philosophical_threads.md
claude_memory/unsent_drafts.md
claude_memory/carry_forward.md
```

Local path (unchanged): `~/.claude/projects/-Volumes-Development-HD-entraclaw-identity-research/memory/<same_filenames>.md`

### 4.2 Sync mechanics

Three sync operations:

**A) Pull-on-session-start** — Before Claude Code loads MEMORY.md into context:
1. List `claude_memory/` in blob
2. For each blob key, compare blob's ETag / last-modified with local file's mtime
3. If blob is newer, overwrite local
4. If local is newer (happens if session was offline), leave it (to be pushed next)
5. If local doesn't exist but blob does, create
6. If blob doesn't exist but local does, push (covers first-ever sync)

**B) Push-on-write** — When Claude's `Write` tool targets the memory dir:
1. PostToolUse hook catches the Write
2. Python helper reads the just-written file
3. Uploads to `claude_memory/<filename>`
4. If push fails (token expired, transient 5xx), retry up to 3x with backoff, then log to stderr (not stdout — the MCP stream is sacred) and defer to next session-start reconciliation

**C) End-of-session flush** — Before Claude exits:
1. Optional: prompt Claude to write `session_digest_*.md`, `carry_forward.md` updates
2. Force-push any locally-modified files that didn't sync during the session
3. Touch a local marker `~/.claude/.last_synced` so next-session's pull can detect a crash

### 4.3 Conflict resolution

The current reality: two Claude sessions can run in parallel (Phase 2 worktree + main today). Both could write to the same memory file. Without coordination, last-write-wins silently.

**Proposal:** Use blob ETag-based optimistic concurrency (already supported by BlobStore from Phase 1).

- Each memory file tracked with an `if_match=<etag>` on upload
- On 412 Precondition Failed: pull remote, **merge** (not overwrite), push with new etag
- Merge strategy depends on file type:
  - `MEMORY.md` (index): union of entries; deduplicate; keep both descriptions if divergent (rare)
  - `user_*`, `feedback_*`, `project_*` (fact files): prefer newer mtime — true "last write wins" because these are atomic-fact files
  - `session_digest_YYYY-MM-DD.md`: concat if same day + two sessions (mark with session IDs)
  - `voice_calibration.md`, `self_observations.md`, `running_jokes_and_callbacks.md`, `unsent_drafts.md`: append-only under a timestamp heading — merge = union
  - `running_commitments.md`: key-merge by commitment ID (TODO: define commitment schema with UUIDs)
  - `carry_forward.md`: append-only in "Pending" section; a "Consumed" move is a rewrite that should rarely conflict

### 4.4 Cache / offline behavior

If blob is unreachable at session start:
1. Log warning to stderr
2. Use last-known-good local files
3. Let Claude operate on stale memory (better than no memory)
4. On next successful sync, reconcile

If blob is unreachable during a write:
1. Local file is still written (Write tool never blocks on network)
2. PostToolUse hook queues the push; retries exponentially
3. On session end, one final flush attempt; if still failing, leave pending-queue file `~/.claude/.pending_syncs.jsonl`
4. Next session-start: drain the pending queue before pulling

---

## 5. Implementation Phases

Each phase ships independently, is tested, and is behind a feature flag (`ENTRACLAW_PERSONA_SYNC`, default `off`).

### Phase 6a — Lift-and-shift existing memory (safe, no behavior change)
- Add `PersonaBackend` to `src/entraclaw/storage/persona.py` — thin wrapper over existing `BlobBackend` scoped to `claude_memory/` prefix.
- Add sync helpers: `pull_all()`, `push_one(path)`, `push_all()`.
- Write tests with a fake `BlobBackend` (the Phase 2 pattern).
- Create `scripts/claude_memory_sync.py` with subcommands `pull`, `push`, `push-one <path>`.
- **Extend `migrate_local_to_backend`** (`src/entraclaw/storage/migration.py`, added in Phase 5) to accept a list of `(source_dir, blob_prefix)` pairs rather than a single source. Existing callers pass `[(data_dir, "")]`; new caller passes both agent data and persona memory:
  ```python
  migrate_local_to_backend([
      (Path.home() / ".entraclaw/data", ""),
      (claude_code_memory_dir(), "claude_memory"),
  ], get_backend())
  ```
  `claude_code_memory_dir()` is a new helper that resolves the Claude Code project-memory path using the same slug convention Claude Code uses (derived from the project's absolute path with forward-slash → hyphen encoding). If the directory doesn't exist (user without Claude Code, or no memory yet), skip silently — return 0 copied for that pair.
- **Update `setup.sh` Step 7b**:
  - Pre-compute the combined size: `~/.entraclaw/data/` bytes + `~/.claude/projects/<slug>/memory/` bytes.
  - Change prompt text from "Upload existing local memory (~NNN KB) to blob? [y/N]" to something like "Upload existing local memory (~NNN KB, includes persona) to blob? [y/N]" so the user knows both trees are covered.
  - Call the extended migration helper with both source pairs.
  - Error handling unchanged — setup.sh still exits red + non-zero on any non-recoverable migration failure.
- **Idempotency guarantee (important for partial-migrated states):** The existing helper already skips keys that exist in blob. After Phase 6a ships, running setup.sh on a state where *only* the agent-data subtree was previously migrated will copy the 24 persona files (23 memory files + MEMORY.md), skip the ~10 agent files already in cloud, and report `Copied: 24, Skipped: 10`. No clobber.
- **Update `CLAUDE.md`** (project root) — add a new `## Memory types` section listing all existing + new types with their write-triggers and read-triggers (per §3.3.2 and §3.3.4 of this doc). Also add the compaction-aware re-read rule from §3.3.3. CLAUDE.md is always-in-context and explicitly overrides default auto-memory behavior, so this is the sanctioned place to extend the type taxonomy.
- **Update `prompts/agent_system.md`** (EntraClaw MCP prompt) — add a `## Persona memory` section telling the agent to consult specific files when handling Teams interactions: `relationship_<sender>.md` on inbound-from-known-person, `voice_calibration.md` before composing banter in group chats, `running_commitments.md` before claiming work is shipped. This prompt loads at MCP boot and applies every time the agent acts through Teams tools.
- **Add `/refresh-persona` skill** at `~/.claude/skills/refresh-persona/SKILL.md` — manual safety valve when Brandon notices drift. Reads MEMORY.md + `session_digest_<today>.md` + `carry_forward.md` and pins them into the current turn. ~30 LOC of skill markdown.
- **Add a new `always-read-on-start` list** to CLAUDE.md: `session_digest_<today>.md` and `carry_forward.md` alongside the implicit `MEMORY.md`. Claude re-checks at session start and on compaction.
- Add Claude Code `PostToolUse` hook wiring in `.claude/settings.json`:
  ```json
  {
    "hooks": {
      "PostToolUse": [{
        "matcher": "Write",
        "hooks": [{
          "type": "command",
          "command": "[[ \"$CLAUDE_TOOL_INPUT_file_path\" == */memory/* ]] && python scripts/claude_memory_sync.py push-one \"$CLAUDE_TOOL_INPUT_file_path\" 2>>~/.entraclaw/logs/persona-sync.log &"
        }]
      }]
    }
  }
  ```
- Add `SessionStart` hook for `pull`:
  ```json
  {
    "hooks": {
      "SessionStart": [{
        "hooks": [{
          "type": "command",
          "command": "[ \"$ENTRACLAW_PERSONA_SYNC\" = \"on\" ] && python scripts/claude_memory_sync.py pull 2>>~/.entraclaw/logs/persona-sync.log"
        }]
      }]
    }
  }
  ```
- Manual test: flip `ENTRACLAW_PERSONA_SYNC=on`, start new session, verify `MEMORY.md` and 23 files round-trip cleanly.

**LOC estimate:** ~200 production + ~200 tests.

### Phase 6b — Add session_digest writer
- Add slash command `/digest-session` (invoking a skill at `~/.claude/skills/digest-session/SKILL.md`) that prompts Claude to draft today's digest.
- Add `scripts/session_end.py` that can be wired to a SessionEnd hook (if Claude Code supports one; if not, invoked via `/digest-session` manually).
- Tests: given a fake interaction log for a day, the digest structure matches the template (headings exist, arcs are present, open-threads section exists).

**LOC estimate:** ~100 production + ~100 tests + skill markdown.

### Phase 6c — Add new memory categories (relationship, voice_calibration, self_observations, running_jokes, running_commitments, philosophical_threads, unsent_drafts, carry_forward)
- These are **new memory types** — extend the memory-type discriminator in the system prompt (`prompts/agent_system.md`) or wherever Claude's memory-type recognition lives.
- No code changes strictly required — these are *content* categories. But add:
  - Example files as templates (NOT committed as real data — put under `docs/plans/persona-templates/`)
  - A one-pager in `prompts/` explaining when to write each type
  - A CI check that every memory file has valid YAML frontmatter

**LOC estimate:** ~50 production (validator) + template files.

### Phase 6d — Conflict resolution (ETag + merge)
- Extend `PersonaBackend` with `push_with_etag(key, content, expected_etag)` → returns (success, new_etag) or raises ConflictError.
- Extend `pull` to record ETags locally in `~/.claude/.memory_etags.json`.
- For each file type, implement a `merge_<type>(local: str, remote: str) -> str` function.
- Tests: simulate two sessions writing concurrently; verify no data loss.

**LOC estimate:** ~150 production + ~200 tests.

### Phase 6e — Offline-resilient queue
- Pending-sync queue at `~/.claude/.pending_syncs.jsonl`.
- Session-start drains the queue before pulling.
- Tests: simulate blob unavailable, write 5 files, make blob available, verify all 5 sync on next start.

**LOC estimate:** ~100 production + ~100 tests.

### Phase 6f-retention — Per-type retention / compaction scheduler
- Add `src/entraclaw/storage/persona_compaction.py` implementing retention rules from §3.4.
- Runs daily, reuses the daily-summary-scheduler infrastructure (shared scheduler, different callback).
- Implements:
  - `session_digest_*` → weekly roll-up after 7d, monthly after 30d, yearly after 365d
  - `voice_calibration.md` → move dated sections older than 30d into a rolled-up "rules learned" permanent section
  - `unsent_drafts.md` → drop sections older than 30d
  - `running_commitments.md` → trim Delivered section to last 5
  - `running_jokes_and_callbacks.md` → move un-referenced items in Active section to Retired after 180d; prune Retired items after 180d more
  - `philosophical_threads.md` → mark dormant if untouched >90d (status in frontmatter)
  - `carry_forward.md` → no action (consume-drives-deletion)
- Strict rule: **never delete without an equivalent synthesized form written first.** The compaction job writes the roll-up file before deleting the sources.
- Tests: fixture-driven — given a set of files with various timestamps, verify post-run state matches expected retention.

**LOC estimate:** ~250 production + ~300 tests.

### Phase 6f-cleanup — Cleanup tooling
- `scripts/claude_memory_admin.py` with subcommands:
  - `list` — show all blob-side memory files + sizes
  - `prune --older-than 90d --type session_digest` — delete old session digests
  - `export <path>` — dump the entire persona memory to a tarball for backup
  - `import <path>` — restore from a tarball (disaster recovery)

**LOC estimate:** ~150 production + ~100 tests.

---

## 6. Testing Strategy (TDD — per CLAUDE.md non-negotiable)

### Unit tests (Phase 6a)
- `tests/storage/test_persona.py`:
  - `test_push_one_uploads_to_claude_memory_prefix` — write a file, assert blob key `claude_memory/foo.md` exists
  - `test_pull_all_restores_local_tree_from_blob`
  - `test_pull_skips_files_newer_locally` (mtime-based)
  - `test_push_retries_on_transient_failure`
  - `test_missing_blob_endpoint_env_is_handled_gracefully`
  - `test_round_trip_preserves_yaml_frontmatter_exactly`

### Integration tests (Phase 6b+)
- `tests/integration/test_session_lifecycle.py`:
  - Spin up a fake Claude session, have it write a memory file, assert PostToolUse hook triggered, assert blob has the file
  - Simulate two parallel sessions writing same file, verify merge rather than clobber

### Regression / safety
- **Never** push a file that lacks YAML frontmatter. CI check: every file in `~/.claude/projects/<slug>/memory/*.md` (except MEMORY.md itself) must have `---\nname:...\n---` header. Sync refuses to push malformed files and logs to stderr.

---

## 7. Risks and Open Questions

### Risks
- **Session-start latency.** A pull that fetches 50 files serially could add 5-10s to every Claude session's startup. Mitigation: parallelize the HTTP calls (httpx async); cache ETags locally and only fetch files with changed ETags.
- **Pre-session hook failure blocking Claude.** If the pull hook crashes, does Claude Code still start? Need to make the pull *optional-but-logged* — never fatal.
- **Memory file corruption from a partial write.** If a push dies mid-way, blob could have a partial file. Mitigation: `BlobStore.put` is already atomic per-blob (Azure semantics). OK.
- **The `PostToolUse` hook pattern assumes the Write's file_path matches the memory dir.** If Claude Code's hook event payload changes, the filter breaks silently. Add a canary test that writes a dummy file and asserts the hook fired.

### Open questions requiring human decision
1. **Should we also sync to a second location for redundancy?** Azure Blob has its own durability, but if the account or subscription gets nuked, the memory is gone. Second cloud target (S3? local-NAS?) worth considering. *Recommendation: defer — Azure's 11-9s is enough for research-phase.*
2. **Is the Agent User's token acceptable for this use case, or should we mint a second Agent Identity scoped only to Claude-memory?** Current plan uses the existing Agent User token. Security review: the memory contains user-preference and conversation-digest data; same classification as the interaction log. Reuse is fine. *Recommendation: reuse.*
3. **How often do we roll up session_digest files?** After 30 days they could live-summary into `monthly_digest_YYYY-MM.md`. This is the "compaction" story from ADR-005. *Recommendation: punt to Phase 7, file a TODO.*
4. **Should any categories be read-only-to-Claude?** Could a human-only file (like `sponsor_directives.md`) exist that Claude reads but cannot write? Useful for explicit constitution-level rules. *Recommendation: out of scope for now; Claude's own write discipline is the control.*

---

## 8. Rollout

Phase 6a ships with `ENTRACLAW_PERSONA_SYNC=on` as the **default** — the sync hooks ARE the feature, gating them behind a flag that most users won't flip defeats the purpose. The safety properties (idempotent migration, non-fatal pull, stderr logging on push failure) are strong enough that "on by default" is the right call. Flag exists for disaster opt-out (`ENTRACLAW_PERSONA_SYNC=off` to quiesce hooks), not for opt-in.

1. Ship Phase 6a with sync on by default.
2. First `setup.sh` run after deploy: Step 7b prompts for the combined migration (agent + persona). Brandon says yes; 24 persona files upload to `claude_memory/` prefix; existing agent files stay.
3. Subsequent `setup.sh` runs: Step 7b is idempotent — reports 0 new copies if nothing changed locally, or a small delta if Claude wrote memory between runs.
4. Monitor `~/.entraclaw/logs/persona-sync.log` for hook errors in the first week.
5. Phases 6b-f ship incrementally as iterations; each continues to respect the same hook and migration contract.
6. Update `CLAUDE.md` active-work section at the end of each phase.
7. Update `docs/engineering-status.md` with test count and new capabilities at the end of each phase.

---

## 9. Worktree follow-up

Separate from this plan, the `fix/summary-self-emails` branch (commit `b462b32` in `.worktrees/fix-summary-self-emails`) adds the `agent_upn` filter to `triage_interactions`. It was parked waiting for Phase 2 to land. Phase 2 has now landed. Fast-forward merge to main once this plan is reviewed.

```bash
# After Phase 2 is on main:
cd /path/to/entraclaw-identity-research
git merge --ff-only fix/summary-self-emails
git push origin main
# Then optionally clean up the worktree:
git worktree remove .worktrees/fix-summary-self-emails
git branch -d fix/summary-self-emails
```

---

## 10. Implementer notes — important context

- **The existing memory dir is at** `~/.claude/projects/-Volumes-Development-HD-entraclaw-identity-research/memory/` — note the URL-encoded-ish path. Use `os.path.expanduser` + the Claude Code project slug convention, don't hardcode.
- **The existing `BlobBackend` API is sync** (`write_text`, `read_text`, `append_text`, `exists`, `list`). If you add a `pull_all` that needs to be fast, use `asyncio` + `BlobStore` (Phase 1, which IS async) directly — not BlobBackend. That's fine; `BlobBackend` was always a sync convenience layer.
- **Don't break existing file paths.** `interaction_log.py` and `daily_summary.py` currently route through `get_backend()`. Their keys (e.g., `interactions/2026-04-18.jsonl`) sit alongside the new `claude_memory/` prefix. No collision.
- **CLAUDE.md non-negotiables apply** — TDD, no stderr-silencing, always check for `error` key on tokens, etc.
- **Check `.env` exists** and the blob vars are set before running sync. Fail fast with a clear message, don't mysteriously silently-skip.
- **Run the full test suite after each phase** — `pytest -v && ruff check .`. The project had 442 tests at time of writing; this plan should add ~25 per phase and all should pass.
- **Respect the worktree convention** — do Phase 6 work on a branch, in `.worktrees/persona-sync-phase-6a` etc.
- **The other Claude (that's you, reader) wrote Phase 2 + 5 brilliantly — I trust the pattern.** Copy its structure: protocol → concrete backends → tests → factory. Same approach here.

---

*End of plan.*
