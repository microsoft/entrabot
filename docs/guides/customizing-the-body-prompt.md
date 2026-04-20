# Customizing the agent body prompt

The EntraClaw agent's system prompt is composed from a **body** (shipped with the repo) and, optionally, a **persona** (from the `persona-sati` MCP server). This guide explains how the body works and how to customize it.

## Why the split

The body carries the rules that protect the agent, the human, and everyone the agent talks to:

- Security protocols (identity, authorization, trust boundaries, social-engineering resistance)
- Communication protocols (respond-in-channel, group-chat watch-only, HTML for structured content)
- Tool reference and bidirectional-workflow conventions

The persona carries personality, callbacks, long-term memory, and per-user relational context. Everything that makes the agent feel like *someone* rather than *something*.

When both are present the body **loads first** and the persona is appended after. The body is intentionally non-overridable — a persona that tried to relax a security rule or invent a new channel would be ignored.

See the `## Non-Negotiables` section in [`CLAUDE.md`](../../CLAUDE.md) for the formal rule.

## File layout

```
prompts/
├── agent_system.md              # the body: loads first, has @include directives
└── anatomy/
    ├── security.md              # baseline posture + 28-rule Critical Security Rules
    ├── channel-discipline.md    # respond-in-channel, watch-only-in-groups, HTML, quiet-by-default
    └── identity-and-tools.md    # who the agent is, tool reference, multi-chat model
```

`agent_system.md` is the root. Its job is to set the non-overridable framing and pull in anatomy modules via `@include`.

## The `@include` directive

Inside `agent_system.md` (or any anatomy file), a line whose first non-whitespace token is `@include` is replaced with the contents of the referenced file at load time:

```markdown
@include anatomy/security.md
```

Rules:

- Paths are resolved relative to the parent directory of `agent_system.md`.
- The directive consumes the entire line; anything after the path is ignored.
- Missing files leave a visible HTML comment (`<!-- missing @include anatomy/foo.md -->`) so boot never crashes on a typo — you'll see the gap in the loaded prompt.
- `@include` is **not recursive.** One level of inlining only. This keeps the mental model simple and prevents runaway expansion.

Anything that doesn't match `@include <path>` is passed through verbatim, so feel free to mix inline content with includes.

## Customization patterns

### Adding a new anatomy module

Drop a file into `prompts/anatomy/` and reference it from `agent_system.md`:

```markdown
# agent_system.md

@include anatomy/security.md
@include anatomy/channel-discipline.md
@include anatomy/identity-and-tools.md

## Team-specific conventions

@include anatomy/my-team-conventions.md
```

Good candidates for separate modules:

- Company-specific policies ("always cc the incident channel on P1 pages")
- Domain-specific vocabulary ("we call it 'fleet'; the product does not")
- Custom escalation paths ("if X, route to Y via Z")

### Overriding an existing module

The stock anatomy modules are opinionated but not sacred. To replace one entirely:

1. Write your own (e.g. `anatomy/channel-discipline.md`) matching the filename.
2. Or change `agent_system.md` to `@include` a different path.

Keep in mind the body's self-framing says "rules below are non-overridable" — it's up to *you* to make sure anything you add is consistent with that claim, or to edit the framing accordingly.

### Turning off the stock persona

If you don't want the stock body either, clear `agent_system.md`:

```markdown
# My custom prompt

You are my agent. Here are my rules.
```

The loader falls through from body → persona → hardcoded string. An empty or whitespace-only `agent_system.md` is treated as "no body present" and the loader skips to persona (if configured) or the hardcoded fallback.

## How the loader behaves

`src/entraclaw/mcp_server.py::_load_agent_instructions` runs at MCP server boot:

1. **Read body.** Open `LOCAL_PROMPT_PATH` (default `prompts/agent_system.md`), expand `@include` directives, strip surrounding whitespace. If the file doesn't exist or is empty, `body = ""`.
2. **Try persona.** If `PERSONA_SATI_MCP_URL` and `PERSONA_SATI_MCP_TOKEN_COMMAND` are both set, open an SSE session to persona-sati and call its `get_system_prompt` tool. On any failure (token mint, network, empty response) persona is dropped and a diagnostic goes to stderr.
3. **Compose.**
   - Both present → `body + "\n\n---\n\n" + persona`
   - Body only → `body`
   - Persona only → `persona`
   - Neither → a hardcoded tool-description string (so FastMCP still has `instructions`)

Any failure is non-fatal. Boot always succeeds.

## Testing a new prompt locally

After editing, restart the MCP server so the new prompt is loaded. In Claude Code that's a `/mcp` reconnect; in Copilot CLI it's a restart. Then verify:

```bash
# Confirms the body prompt file is the one you expect
.venv/bin/python -c "from entraclaw.mcp_server import LOCAL_PROMPT_PATH; print(LOCAL_PROMPT_PATH)"

# Dumps the composed prompt to stdout
.venv/bin/python -c "from entraclaw.mcp_server import _load_agent_instructions; print(_load_agent_instructions())"
```

For programmatic checks, `tests/test_mcp_server_integration.py::TestLoadAgentInstructions` covers the composition logic — use that as a pattern for your own prompt-related tests.

## When to put something in the body vs. in persona-sati

**Body** (`prompts/` in this repo):

- Security, safety, audit behavior
- Channel and communication etiquette
- Tool reference and conventions
- Anything you want to survive a persona change or a persona outage

**Persona** (served separately by `persona-sati`):

- Tone, voice, running jokes
- Long-term memory and callbacks across sessions
- Per-user relational context
- Anything you want to evolve without redeploying the body

Rule of thumb: if changing it would affect *what the agent is allowed to do*, it belongs in the body. If changing it would only affect *how the agent sounds*, it belongs in persona.

## See also

- [`prompts/agent_system.md`](../../prompts/agent_system.md) — the stock body
- [`prompts/anatomy/security.md`](../../prompts/anatomy/security.md) — baseline + Critical Security Rules
- [`prompts/anatomy/channel-discipline.md`](../../prompts/anatomy/channel-discipline.md) — response etiquette
- [`prompts/anatomy/identity-and-tools.md`](../../prompts/anatomy/identity-and-tools.md) — identity framing + tool reference
- `CLAUDE.md` / `AGENTS.md` — the non-negotiable that anchors the body
