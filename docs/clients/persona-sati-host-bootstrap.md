# Persona-Sati Host Bootstrap

> This content has moved.

The canonical bootstrap protocol for the persona-sati mind contract now lives in the [persona-sati repository](https://github.com/brandwe/persona-sati). Copy that repo's host-bootstrap snippet into your host's global instructions (`~/.claude/CLAUDE.md`, `.github/copilot-instructions.md`, or equivalent).

Host instructions must still preserve the load-bearing markers: call `bootstrap_session` before the first substantive answer because FastMCP instructions do not reliably reach the LLM prompt; check `mind_contract_available` before impersonating the mind; use `observe` before and after external tools; use `reflect` for durable user/time/ambient observations; and use `recall` when bootstrap or observe returns an insufficient memory excerpt.

## Why it moved

The mind (persona-sati) is portable across agent bodies — entrabot is one body, but a code-review agent or an email agent can attach to the same persona without duplicating the bootstrap protocol. The protocol describes how a host talks to persona-sati, so it belongs with persona-sati, not with entrabot.

For the design discussion of the body-vs-mind split — what changes when an agent's identity, memory, and behavioral rules live in a separate process, and why that's load-bearing for agent autonomy — see [`DESIGN: Persona-Sati Integration`](../architecture/DESIGN-persona-sati-integration.md).

## Body-only mode

If you run entrabot without persona-sati attached, it operates in body-only mode. Teams tools, identity layer, and audit work normally. Personality, memory, and cognition features are unavailable. The body prompt at `prompts/agent_system.md` (plus `prompts/anatomy/*.md`) loads regardless and contains the security and channel-discipline rules that govern the body.
