# TODO — Entraclaw: consume `PERSONA_SATI_MCP_URL` at boot

**Status: DONE (2026-04-18, v1).**
Implemented in `src/entraclaw/mcp_server.py:_load_agent_instructions()` (lines ~100–170). The function composes `body + persona`: `PERSONA_SATI_MCP_URL` + `PERSONA_SATI_MCP_TOKEN_COMMAND` env vars, when both present, trigger an SSE fetch of the persona via `get_system_prompt`; missing env or fetch failure falls back to the body prompt alone.

This doc is retained for historical context — the spec below is what shipped.

---

**Original scope: `src/entraclaw/mcp_server.py` only**
**Original size: ~60 LOC + ~30 LOC of tests**
**Dependency:** `persona-sati` repo's setup.sh --with-entraclaw must have been run first (so the cross-blueprint consent grant and `.mcp.json` env vars are in place).

---

## Why this exists

Persona-sati's setup.sh --with-entraclaw now wires two env vars into this repo's `.mcp.json`:

```json
{
  "mcpServers": {
    "entraclaw": {
      ...
      "env": {
        "PERSONA_SATI_MCP_URL": "https://persona-sati-<suffix>.<region>.cloudapp.azure.com",
        "PERSONA_SATI_MCP_TOKEN_COMMAND": "/abs/path/to/persona-sati/scripts/persona-sati-token.py"
      }
    }
  }
}
```

Without consuming those, entraclaw still boots with its hardcoded local tool-description prompt (`_load_agent_instructions()`). The persona lives remotely but the body doesn't know. This TODO wires them together.

The governing principle from `persona-sati/docs/plans/end-to-end-mind-body.md`:

> **EntraClaw is a body. Persona-sati is the mind.** A body's behavior should come from its mind, not from a local fallback. Revokng the mind should downgrade the body to a generic tool (fallback prompt), not crash it.

## The change

Replace `_load_agent_instructions()` in `src/entraclaw/mcp_server.py` with a function that:

1. Checks `PERSONA_SATI_MCP_URL` + `PERSONA_SATI_MCP_TOKEN_COMMAND` env vars.
2. If either is missing → return the existing local fallback prompt.
3. Otherwise:
   - Shell out to `PERSONA_SATI_MCP_TOKEN_COMMAND` to mint a bearer JWT.
   - Open an MCP SSE session to `{PERSONA_SATI_MCP_URL}/sse` with `Authorization: Bearer <jwt>` header.
   - `session.call_tool("get_system_prompt", {})`.
   - Return the text.
4. On **any** failure along the way (token mint timeout, network error, 401, unexpected response shape), log to stderr and return the local fallback. Never raise — boot must succeed even when persona-sati is unreachable.

## Reference implementation

Paste the following as the new `_load_agent_instructions()`. The rest of the file is unchanged; only this single function is replaced.

```python
def _load_agent_instructions() -> str:
    """Return the agent's system prompt.

    If PERSONA_SATI_MCP_URL is set, fetch the prompt from the cloud
    persona-sati MCP server via get_system_prompt(). Otherwise (or on
    any failure), return the local tool-description fallback.

    The body (entraclaw) delegates personality to the mind (persona-
    sati). Revoking persona-sati access gracefully degrades entraclaw
    to a generic communication tool; it never crashes the boot.
    """
    import os
    import subprocess
    import sys

    local_fallback = (
        "EntraClaw Teams Interface: provides tools for sending and "
        "receiving Microsoft Teams messages, managing group chats, "
        "email polling, and daily summary generation. This server "
        "handles communication channels only. For personality, memory, "
        "and behavioral rules, connect to the persona-sati MCP server."
    )

    remote_url = os.environ.get("PERSONA_SATI_MCP_URL", "").strip()
    token_cmd = os.environ.get("PERSONA_SATI_MCP_TOKEN_COMMAND", "").strip()
    if not remote_url or not token_cmd:
        return local_fallback

    try:
        token = subprocess.check_output(
            [token_cmd], text=True, timeout=30
        ).strip()
    except (subprocess.SubprocessError, OSError) as exc:
        print(
            f"[entraclaw] could not mint persona-sati token "
            f"({token_cmd}): {exc}; using local fallback prompt",
            file=sys.stderr,
        )
        return local_fallback
    if not token:
        print(
            f"[entraclaw] token command {token_cmd} returned empty; "
            "using local fallback prompt",
            file=sys.stderr,
        )
        return local_fallback

    try:
        import asyncio

        from mcp import ClientSession
        from mcp.client.sse import sse_client

        async def _fetch_remote_prompt() -> str | None:
            sse_url = f"{remote_url.rstrip('/')}/sse"
            headers = {"Authorization": f"Bearer {token}"}
            async with sse_client(sse_url, headers=headers) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "get_system_prompt", {}
                    )
                    for item in result.content:
                        if hasattr(item, "text") and item.text:
                            return item.text
            return None

        remote = asyncio.run(_fetch_remote_prompt())
    except Exception as exc:  # noqa: BLE001 — never break boot
        print(
            f"[entraclaw] persona-sati fetch failed: {exc}; "
            "using local fallback prompt",
            file=sys.stderr,
        )
        return local_fallback

    if not remote:
        print(
            "[entraclaw] persona-sati returned empty prompt; "
            "using local fallback",
            file=sys.stderr,
        )
        return local_fallback

    print(
        f"[entraclaw] loaded system prompt from persona-sati ({remote_url})",
        file=sys.stderr,
    )
    return remote
```

## Tests to add

Add to `tests/test_mcp_server_integration.py` (or a new file):

1. **`test_load_instructions_uses_local_when_env_unset`** — both env vars cleared → returns the local fallback string (check the "EntraClaw Teams Interface" prefix).

2. **`test_load_instructions_uses_local_when_token_cmd_fails`** — env vars set but token command returns non-zero / timeout / missing. Use `monkeypatch` on `subprocess.check_output` to raise. Returns local fallback. Verify a stderr log mentions the failure.

3. **`test_load_instructions_uses_remote_when_all_works`** — mock the token command to return a fake JWT, mock `asyncio.run` + the MCP client path to return a canned remote prompt. Verify the remote string is what comes back.

4. **`test_load_instructions_uses_local_when_remote_fetch_fails`** — token ok, but the MCP client raises (network error, auth error, malformed response). Local fallback.

## Verification checklist

Before marking this TODO closed:

- [ ] `pytest -v` passes (existing suite plus the 4 new tests)
- [ ] `ruff check .` clean
- [ ] Live test: unset `PERSONA_SATI_MCP_URL`, boot the MCP server → local prompt used, no errors
- [ ] Live test: set env vars pointing at a running persona-sati cluster → stderr shows `loaded system prompt from persona-sati (...)`, the agent's voice in Teams reflects the remote prompt
- [ ] Live test: set the env vars but take persona-sati offline (scale pod to 0) → boot still succeeds, stderr shows fallback reason, local prompt used

## Related

- `persona-sati/docs/plans/remaining-work-to-two-command-goal.md` — TODO 4.
- `persona-sati/scripts/persona-sati-token.py` — the token CLI this calls.
- `persona-sati/scripts/wire_mcp_json.py` — what writes the env vars.
