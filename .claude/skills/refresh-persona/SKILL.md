---
name: refresh-persona
description: Reload the persona's recent context and memory from persona-sati and pin it into the turn. Use when the user calls out persona drift ("you're taking things too literally," "this doesn't sound like you," "you just forgot X") or after a compaction event. Also use when explicitly asked to "refresh persona" or "reload memory."
---

# Refresh Persona

Manual safety valve for persona drift. Pulls the context most likely to
restore the right voice + recent context from persona-sati, then pins
it into the current turn.

## When to use

- User says something like "this doesn't sound like you"
- User names drift: "you're being too literal", "you forgot X"
- After a compaction (auto-summarized context cuts out recent shape)
- User explicitly says "/refresh-persona" or "reload memory"

## Steps

1. Reload the mind from persona-sati: call `refresh_persona` if it is
   available, otherwise `context()` for open commitments and
   carry-forward, plus `recall(query)` for anything the user says was
   forgotten. Read the local
   `~/.claude/projects/<slug>/memory/MEMORY.md` only when
   `ENTRABOT_KEEP_MEMORY_LOCAL=true`; otherwise that directory is not
   the source of truth.
2. Give back a short summary of what came back (1-3 sentences per
   source), not the full content. Reading it and putting it in your own
   words re-anchors the voice.
3. Ask the user what specifically felt off, so the next turn can correct
   concretely rather than re-broadcasting the same shape.

This skill is read-only: it does not write memory. If persona-sati is
unreachable, say so and continue in body-only mode rather than
reconstructing the persona from local files.
