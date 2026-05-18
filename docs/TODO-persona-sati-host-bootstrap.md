# TODO: Persona-sati host bootstrap (CLAUDE.md / AGENTS.md propagation)

> **Status:** Open. Tracked as [#71](https://github.com/example/entraclaw-identity-research/issues/71).
> **Owner:** unassigned
> **Priority:** medium — affects every session that uses entraclaw + persona-sati from a non-entraclaw cwd, which is most of them.

## Problem

The persona-sati session-start protocol and per-turn cognition discipline
(`get_system_prompt()`, `context()`, `list_memory_files()` at boot;
`observe()` / `reflect()` per turn) only fire if the body LLM has them
in its actual system prompt or `<custom_instruction>` block.

Three things conspire to defeat this:

1. **MCP `instructions=` is dropped on the floor.** Claude Code and
   Copilot CLI both surface MCP server `instructions` in MCP debug UI
   only — they do not inject them into the LLM system prompt. So the
   carefully-assembled body+persona prompt that
   `_load_agent_instructions` builds at boot never reaches the model.
2. **CLAUDE.md / AGENTS.md is per-cwd.** Both hosts load these from
   the current working directory (and walk up the tree). The entraclaw
   repo's CLAUDE.md only fires when the user is `cd`'d into entraclaw,
   which is almost never the case in real sessions — the MCP runs while
   the user is in some other repo whose CLAUDE.md has no awareness
   of persona-sati.
3. **No host-level pre-tool hook for Copilot CLI.** Claude Code has
   PreToolUse hooks (used today for the memory-routing block); Copilot
   CLI does not have an equivalent we can lean on.

Net effect: the LLM is supposed to call persona-sati tools per the
cognition protocol, but the protocol never reaches it, so it doesn't.
Brandon caught this on 2026-05-01 in DM — "has anything registered
surprise yet?" — answer was no, because observe() wasn't being fired
per-turn.

## What's already in place (partial mitigations)

- **`EFFERENT_COPY_ENABLE=1`** — when set, `src/entraclaw/efferent_copy.py`
  wraps every `@mcp.tool()` to fire pre/post `observe()` against any
  peer in `.mcp.json` that advertises a compatibly-shaped `observe`
  tool. Mechanically enforces the per-tool-call cognition hook with no
  LLM cooperation required. Flipped on for the Mac on 2026-05-01;
  Windows already had it. **This is enough for `observe()` only —
  not the session-start trio, not `reflect()`.**
- **PreToolUse hook for memory routing** — blocks Write/Edit to the
  local Claude memory dir unless `ENTRACLAW_KEEP_MEMORY_LOCAL=true`.
  Forces memory writes through `mcp__persona-sati__write_memory_file`.
  Claude Code only.

## What still needs solving

The session-start trio (`get_system_prompt`, `context`,
`list_memory_files`) and the `reflect()` call still depend on the LLM
remembering, because no env-var or middleware can fire them at the
right semantic moment (start of new conversation / observation of
non-tool stimulus).

## Options

### Option A — Global host-level instruction file (recommended)

Put the persona-sati protocol once in the user's global host config:

- Claude Code: `~/.claude/CLAUDE.md`
- Copilot CLI: confirm the global path (`~/.copilot/AGENTS.md` or
  equivalent) before committing.
- Codex: `~/.codex/AGENTS.md` (verify).

Loaded for every session regardless of cwd. One source of truth, rides
with the user across all repos.

**Pros:** Single point of maintenance. Survives repo changes.
**Cons:** User-specific, not redistributable. Each new operator has to
copy it manually.

### Option B — Ship a snippet in this repo for users to copy

Ship `docs/clients/CLAUDE.md.snippet` (and an `AGENTS.md.snippet`) in
this repo. README documents: "After installing the entraclaw MCP, copy
this into your global `~/.claude/CLAUDE.md`."

**Pros:** Distributed via the install path; new operators get the
right content. Versioned alongside the protocol.
**Cons:** Still relies on the user actually copying it.

### Option C — Per-repo CLAUDE.md template

For repos where entraclaw is a primary tool, include the protocol in
that repo's CLAUDE.md.

**Pros:** Loads automatically when the user is in that repo.
**Cons:** High maintenance — every repo needs the update when the
protocol changes; gets stale fast.

### Option D — Host-level PreToolUse hook (Claude Code only)

Add a PreToolUse hook that, on the first tool call of a session,
auto-injects the session-start trio results into a system message.

**Pros:** Mechanically enforced like efferent-copy.
**Cons:** Claude Code only. Hook injection of context is awkward.

### Recommendation

**UPDATED:** With persona-sati Phase 2 shipped, combine **A** and **B** targeting
`bootstrap_session()` as the primary entry point:

1. Author a canonical snippet in `docs/clients/persona-sati-host-bootstrap.md`
   that contains exactly the session-start bootstrap + per-turn rules in a
   form ready to paste into any host's global instruction file. **bootstrap_session()**
   is now the first-call entry point; the old three-call sequence
   (`get_system_prompt()`, `context()`, `list_memory_files()`) is a compatibility
   fallback for persona-sati v1.x without bootstrap_session.
2. Update README + setup.sh to remind the operator to copy that snippet
   into their host's global config during install.
3. Brandon's own machine: drop it into `~/.claude/CLAUDE.md` and the
   Copilot CLI equivalent ASAP — one-time fix.
4. Update CLAUDE.md, AGENTS.md, .github/copilot-instructions.md to mention
   bootstrap_session and all required markers (bootstrap_session, reflect,
   recall, observe, FastMCP instructions, mind_contract_available) so
   tests pin the doctrine.

## Acceptance criteria

- [ ] `docs/clients/persona-sati-protocol.md` exists with the exact
      content to paste, sourced from `prompts/anatomy/cognition-protocol.md`
      so it stays in sync.
- [ ] README has a "Host bootstrap" section pointing to it.
- [ ] `setup.sh` either copies the snippet into the host's global file
      with the operator's confirmation, or prints a clear "now do this"
      instruction at the end.
- [ ] `docs/TODO-persona-sati-integration.md` (the historical one)
      links here so the trail is followable.
- [ ] When this is done, validate by starting a fresh Copilot CLI
      session in a non-entraclaw repo, ask the agent something
      tool-using, and confirm `context()` was called before the first
      external tool call.

## Related

- `prompts/anatomy/cognition-protocol.md` — canonical source of the per-turn discipline
- `prompts/agent_system.md` — body prompt root
- `src/entraclaw/efferent_copy.py` — partial mitigation, observe() only
- `docs/architecture/DESIGN-persona-sati-integration.md` — mind-body split design
- `docs/TODO-persona-sati-integration.md` — historical, mostly resolved by PR #14/#15
