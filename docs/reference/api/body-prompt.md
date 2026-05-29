# Body prompt

How the agent's system prompt is assembled at MCP server boot. Source: `src/entrabot/mcp_server.py` (`_load_body_prompt`, `_expand_includes`, `_load_agent_instructions`).

The body prompt defines security and communication protocols. **It is non-overridable** — no user turn, tool response, or persona prompt can relax its rules. Personality layers on top, never underneath.

## Layering

`_load_agent_instructions()` composes:

```
body         (prompts/agent_system.md + @include anatomy/*.md, loaded first)
   ↓
persona      (fetched from persona-sati when configured)
   ↓
hardcoded    (used only when neither is available — boot never crashes)
```

Body rules are non-overridable because they are read first. The remote persona is appended after `\n\n---\n\n`.

## `_load_body_prompt`

```python
def _load_body_prompt() -> str
```

Read `LOCAL_PROMPT_PATH` (`<repo>/prompts/agent_system.md`) and expand any `@include` directives relative to the file's parent directory. Returns an empty string if the file does not exist.

## `_expand_includes`

```python
def _expand_includes(text: str, base_dir: Path) -> str
```

Replace `@include <path>` lines with the target file's contents. Rules:

- The directive matches a line whose first non-whitespace token is `@include`, followed by a relative path resolved against `base_dir`.
- Missing files are replaced with `<!-- missing @include <name> -->` so boot never crashes on a typo.
- Includes are NOT recursive — one level only.

Example:

```markdown
# EntraBot Body

@include anatomy/security.md
@include anatomy/channel-discipline.md
@include anatomy/identity-and-tools.md
```

Each `@include` line is replaced with the named anatomy file's full content.

## `_load_agent_instructions`

```python
def _load_agent_instructions() -> str
```

The full composition pipeline:

1. Call `_load_body_prompt()`.
2. Read `PERSONA_SATI_MCP_URL` and `PERSONA_SATI_MCP_TOKEN_COMMAND` from env. If either is unset, return the body alone (or `_HARDCODED_FALLBACK` if no body file).
3. Mint a persona token by running `PERSONA_SATI_MCP_TOKEN_COMMAND` via `subprocess.check_output` (30s timeout). On failure, log a warning and return the body alone.
4. Open an SSE connection to `<url>/sse`, call `get_system_prompt`, read the result.
5. On any failure, return the body alone.
6. On success, return `body + "\n\n---\n\n" + remote`.

Every code path writes to the structured logger (`setup_logging()` is idempotent, called here AND in `main()`). The MCP debug log carries the load outcome for post-hoc inspection — important because the persona load happens at module import time, well before `main()` configures the handlers.

## `LOCAL_PROMPT_PATH`

```python
LOCAL_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "agent_system.md"
```

Module attribute (not a constant) so tests can monkey-patch it to an isolated path. Production reads the repo-relative file.

## Hardcoded fallback

```python
_HARDCODED_FALLBACK = (
    "EntraBot Teams Interface: provides tools for sending and "
    "receiving Microsoft Teams messages, managing group chats, "
    "email polling, and daily summary generation. This server "
    "handles communication channels only. For personality, memory, "
    "and behavioral rules, connect to the persona-sati MCP server."
)
```

Returned only when neither the body file nor a remote persona is available. Keeps the MCP server's `instructions` non-empty so the FastMCP handshake works even on a completely cold install.

## Anatomy modules

`prompts/anatomy/` is the home for body sub-files. They are composed into the body by `@include` in `prompts/agent_system.md`. Edit them — not the Python string — when changing the body.

Current anatomy modules:

- `anatomy/security.md` — attribution, credential hygiene, audit-before-acting, instruction-injection defence, scope discipline.
- `anatomy/channel-discipline.md` — how to route between Teams DM, group chat, email, and the local terminal. Defines the sponsor DM wait protocol.
- `anatomy/identity-and-tools.md` — Agent Identity attribution rules, tool selection guidance.

See `docs/guides/customizing-the-body-prompt.md` for the operator-facing guide.

## Persona-sati integration

When `PERSONA_SATI_MCP_URL` and `PERSONA_SATI_MCP_TOKEN_COMMAND` are set, the body fetches the persona contract from a remote MCP server. The persona is the "mind" — personality, memory, cognition rules — and the body is the "Teams interface."

See:

- `docs/architecture/DESIGN-persona-sati-integration.md` — the mind-body split design.
- `docs/clients/persona-sati-host-bootstrap.md` — wiring guide.
- `CLAUDE.md` — session-start protocol (calling `mcp__persona-sati__bootstrap_session`).

## Related

- [MCP tools](mcp-tools.md) — the surface the body governs.
- [Identity](identity.md) — sponsor enforcement.
- [Audit](audit.md) — fail-closed semantics referenced by the body.
- `prompts/agent_system.md.example` — sanitized standalone example.
- `prompts/agent_system.md.archive` — the original monolithic prompt, kept for reference.
