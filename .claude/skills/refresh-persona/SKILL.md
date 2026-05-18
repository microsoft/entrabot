---
name: refresh-persona
description: Re-read current session digest + carry-forward + MEMORY.md index and pin into the turn. Use when the user calls out persona drift ("you're taking things too literally," "this doesn't sound like you," "you just forgot X") or after a compaction event. Also use when explicitly asked to "refresh persona" or "reload memory."
---

# Refresh Persona

Manual safety valve for persona drift. Reads the files most likely to
restore the right voice + recent context, then pins them into the
current turn.

## When to use

- User says something like "this doesn't sound like you"
- User names drift: "you're being too literal", "you forgot X"
- After a compaction (auto-summarized context cuts out recent shape)
- User explicitly says "/refresh-persona" or "reload memory"

## Steps

1. Read, in order, if they exist:
   - `~/.claude/projects/<this-project-slug>/memory/MEMORY.md`
   - `~/.claude/projects/<this-project-slug>/memory/session_digest_<YYYY-MM-DD>.md`
     (today's digest — skip if not present, Phase 6b adds the writer)
   - `~/.claude/projects/<this-project-slug>/memory/carry_forward.md`
     (Phase 6c file — skip if not present)
2. For each file that exists, echo a short summary back to the turn
   (1-3 sentences per file) — NOT the full content. The act of reading
   + paraphrasing re-anchors the voice.
3. Ask the user what specifically felt off, so the next turn can correct
   concretely rather than re-broadcasting the same shape.

## Non-goals

- Do not write new memory files here — this skill is read-only.
- Do not pull from the blob backend directly — the SessionStart hook
  already did that. This skill uses whatever is currently on disk.
- Do not re-read the full 23-file memory dir — that's what the auto-
  memory system already does at session start. This skill is the
  surgical subset for *recent* + *index*.
