# Persona-Sati Host Bootstrap

> This content has moved.

The canonical bootstrap protocol for the persona-sati mind contract now lives in the [persona-sati repository](https://github.com/brandwe/persona-sati). Copy that repo's host-bootstrap snippet into your host's global instructions (`~/.claude/CLAUDE.md`, `.github/copilot-instructions.md`, or equivalent).

## Why it moved

The mind (persona-sati) is portable across agent bodies — entraclaw is one body, but a code-review agent or an email agent can attach to the same persona without duplicating the bootstrap protocol. The protocol describes how a host talks to persona-sati, so it belongs with persona-sati, not with entraclaw.

For the design discussion of the body-vs-mind split — what changes when an agent's identity, memory, and behavioral rules live in a separate process, and why that's load-bearing for agent autonomy — see [`DESIGN: Persona-Sati Integration`](../architecture/DESIGN-persona-sati-integration.md).

## Body-only mode

If you run entraclaw without persona-sati attached, it operates in body-only mode. Teams tools, identity layer, and audit work normally. Personality, memory, and cognition features are unavailable. The body prompt at `prompts/agent_system.md` (plus `prompts/anatomy/*.md`) loads regardless and contains the security and channel-discipline rules that govern the body.
