# Body prompt

How the agent's system prompt is assembled at MCP server boot. Source: `src/entrabot/mcp_server.py` (`_load_body_prompt`, `_expand_includes`, `_load_agent_instructions`).

The body prompt defines security and communication protocols. **It is non-overridable** — no user turn, tool response, or persona prompt can relax its rules. Personality layers on top, never underneath.

## Layering

`_load_agent_instructions()` composes:

```
body         (prompts/agent_system.md + @include anatomy/*.md, loaded first)
   ↓
persona      (fetched from persona-sati when configured and reachable)
   ↓
hardcoded    (used only when neither is available — boot never crashes)
```

Body rules are non-overridable because they are read first. When a persona is fetched, it is appended after `body + "\n\n---\n\n"`.

The composed string becomes the `instructions=` argument passed to `FastMCP(...)`. Not every MCP host surfaces `instructions` in the model's system prompt — some only expose it in debug tooling — so the body prompt reaching the model at all can depend on host-side behavior the host bootstrap protocol is designed to compensate for. See [Persona-Sati Host Bootstrap](../../clients/persona-sati-host-bootstrap.md).

## `_load_body_prompt`

```python
def _load_body_prompt() -> str
```

Read `LOCAL_PROMPT_PATH` (`<repo>/prompts/agent_system.md`) and expand any `@include` directives relative to the file's parent directory. Returns an empty string if the file does not exist or can't be read.

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
2. Read `PERSONA_SATI_MCP_URL` and `PERSONA_SATI_MCP_TOKEN_COMMAND` from the environment. If either is unset, return the body alone, or `_HARDCODED_FALLBACK` if there is no body file.
3. Mint a persona token by running the command in `PERSONA_SATI_MCP_TOKEN_COMMAND` via `subprocess.check_output` (30s timeout). On any subprocess error, timeout, or empty output, log a warning and return the body (or fallback) alone.
4. Open an SSE connection to `<PERSONA_SATI_MCP_URL>/sse`, call the `get_system_prompt` tool, and read the result text.
5. On any failure (connection error, empty result), return the body (or fallback) alone.
6. On success, return `body + "\n\n---\n\n" + remote`.

Every branch logs its outcome via the structured logger (`setup_logging()`, which is idempotent and safe to call again in `main()`). This matters because `_load_agent_instructions` runs at module import time — before `main()` configures logging handlers — so without this the load outcome would only ever appear as a transient stderr line most hosts discard.

## `LOCAL_PROMPT_PATH`

```python
LOCAL_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "agent_system.md"
```

Module attribute (not a constant) so tests can monkeypatch it to an isolated path. Production reads the repo-relative file.

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

Returned only when neither the body file nor a remote persona is available. Keeps the MCP server's `instructions` non-empty so the FastMCP handshake works even on a cold install with no `prompts/agent_system.md`.

## Anatomy modules

`prompts/anatomy/` holds body sub-files, composed into the body by `@include` in `prompts/agent_system.md`. Edit them — not the Python string — when changing the body.

Current anatomy modules:

- `anatomy/security.md` — attribution, credential hygiene, audit-before-acting, instruction-injection defense, scope discipline.
- `anatomy/channel-discipline.md` — how to route between Teams DM, group chat, email, and the local terminal; defines the sponsor-DM wait pattern.
- `anatomy/identity-and-tools.md` — Agent Identity attribution rules, tool selection guidance.

See [Customizing the Body Prompt](../../guides/customizing-the-body-prompt.md) for the operator-facing guide.

## Persona layering and degraded fallback

When `PERSONA_SATI_MCP_URL` and `PERSONA_SATI_MCP_TOKEN_COMMAND` are both set and the remote fetch succeeds, the body is appended with the persona-sati "mind" contract — personality, memory, cognition rules. If persona content contradicts the body, the body still governs: it was read first, and nothing in `_load_agent_instructions` allows the remote fetch to replace or precede the body text.

Any failure in the persona pipeline (env unset, token-mint failure, unreachable server, empty response) falls back cleanly to serving the body alone — the MCP server always boots with valid, non-empty instructions. Without persona-sati configured, entrabot runs in body-only mode: Teams/email/Files tools, identity, and audit continue to work exactly as documented; personality, long-term memory, and the `observe`/`reflect`/`recall` cognition loop are unavailable.

## Related

- [MCP Tools](../mcp-tools.md) — the surface the body governs.
- [Identity](identity.md) — sponsor enforcement.
- [Audit](audit.md) — fail-closed attribution referenced by the body's security rules.
- [Persona-Sati Host Bootstrap](../../clients/persona-sati-host-bootstrap.md) — the per-host protocol for surfacing the persona-sati mind contract to a model.
- [Storage and Memory](../../architecture/storage-and-memory.md) — how the body/persona split maps to operational vs. persona memory storage.
