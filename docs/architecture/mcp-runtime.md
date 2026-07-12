# MCP Runtime

## Server shape

Entrabot is a stdio [MCP](https://modelcontextprotocol.io/) server built on FastMCP. Every host-facing capability — Teams, email, Files, cards, promises, the daily summary, A365 Work IQ — is registered as an `@mcp.tool()` function in `src/entrabot/mcp_server.py`. Each tool wrapper does its work and returns a JSON string (`json.dumps(...)`), not a raw Python object, so every tool has the same wire shape regardless of what it does internally.

`_run_stdio_with_write_stream()` is the process entry point: it opens the stdio transport, stashes the write stream in module state (`_state["_write_stream"]`) so background tasks can push notifications outside of a tool call, discovers efferent-copy sinks, eagerly schedules `_initialize()`, and hands control to FastMCP's request loop.

## Initialization lifecycle

`_initialize()` is eagerly scheduled by `_run_stdio_with_write_stream()` when the server starts, so auth, watched-chat loading, and background polling do not wait for the first tool call. Tool wrappers also call `_initialize()` as an idempotent fallback; once `_state["initialized"]` is set, later calls short-circuit.

1. **`_init_auth()`** — resolves `ENTRABOT_MODE` (`agent_user`, `delegated`, or `auto`) and authenticates. In `auto` mode this tries the three-hop Agent User fast path first (unless `SKIP_PROVISIONING` is set), falling back to MSAL delegated auth if that fails, landing in `UNAUTHENTICATED` if both fail. See [Identity and Token Flow](identity-and-token-flow.md) for the flow itself and the state machine it drives.
2. **`_init_poll()`** — loads any persisted `watched_chats` file and starts the Teams poll task if at least one chat is already watched; starts email poll, chat auto-discovery, and the daily summary scheduler only when the resolved auth mode is `agent_user`.
3. **Storage backend validation** — `assert_backend_config()` resolves and validates the configured `MemoryBackend` once at boot. A half-configured Blob environment raises here and aborts initialization rather than silently falling back to `LocalBackend`.
4. **Active-sponsor-channel binding log** — logs the configured TTL for the in-memory sponsor/chat binding table used to authorize mutating tools (`add_teams_member`, `share_file`).

Every call also captures the connected client's `clientInfo.name` into `_state["cached_host"]` (`_capture_host_from_context()`), independent of whether this is the first call — background tasks that run outside a live request context read this cached value.

## Background task matrix

| Task | Interval | Gate | Notes |
|---|---|---|---|
| Teams chat poll (`_background_poll`) | 5s | At least one chat in `watched_chats` at boot or registered later; not gated on auth mode | Iterates every watched chat each cycle; a single chat's failure is logged and doesn't stop the others in that cycle. |
| Email poll (`_background_poll_email`) | 60s | Agent User mode only | Polls `/me/messages` — targets the agent's own mailbox, which only resolves correctly in Agent User mode. |
| Chat auto-discovery (`_background_discover_chats`) | 120s | Agent User mode only | Sweeps `GET /me/chats` (no `$orderby` — it 400s there) and registers any chat not already watched. |
| Daily summary scheduler (`_background_daily_summary`) | Fixed 5pm UTC-7 trigger, computed once per cycle | Agent User mode only | The offset is a fixed UTC-7 shift, not DST-adjusted, despite being labeled "PDT" in logs. |
| persona-sati heartbeat (`_background_persona_sati_heartbeat`) | 300s | Agent User mode only | Self-skips (returns `"skipped"`, no warning) whenever `PERSONA_SATI_MCP_URL` / `PERSONA_SATI_MCP_TOKEN_COMMAND` are unset — enabling it costs nothing with no persona-sati peer attached. |

Each loop wraps its own iteration in `try`/`except` and sleeps before continuing, so a single exception logs a warning and the loop keeps running — there is no separate external process supervising or restarting a task from outside; resilience is per-loop, not fleet-wide.

## Token handling

`_ensure_valid_token()` and `_with_token_retry()` (both in `mcp_server.py`) are the two token-refresh mechanisms used by Graph-backed tools and background tasks — see [Identity and Token Flow](identity-and-token-flow.md#token-lifecycle) for the eager-refresh-and-lazy-retry mechanics. Graph background tasks call `_ensure_valid_token()` directly before making a call; Graph-backed tool wrappers use `_with_token_retry()` around the underlying operation. A365/provider-only and local tools may use their provider's authentication or local storage instead of these Graph token helpers.

## Body prompt assembly

`_load_agent_instructions()` composes the system prompt FastMCP registers at boot, body-first:

1. **Body** — `prompts/agent_system.md` with `@include` expansion of `prompts/anatomy/*.md`. Loaded first, always, when the file exists.
2. **Persona** — when `PERSONA_SATI_MCP_URL` and `PERSONA_SATI_MCP_TOKEN_COMMAND` are both set, Entrabot mints a token via the configured command, opens an SSE session to the remote persona-sati MCP, and calls its `get_system_prompt` tool. The result is appended after the body. Any failure at any step (missing env, token-mint failure, unreachable remote) falls back to the body alone — boot never crashes because persona-sati is unreachable.
3. **Hardcoded fallback** — used only if the body prompt file itself is missing.

This means Entrabot *does* contact persona-sati directly on this one path (fetching the prompt at boot) even though the runtime resource path (Teams, email, Files) has no dependency on it. Hosts may also list persona-sati as a second, independent MCP server in `.mcp.json` so the LLM can call its `bootstrap_session`/`observe`/`reflect`/`recall` tools directly — that is a separate wiring from the boot-time prompt fetch described here.

## Host-aware delivery, decided server-side

The channel-push notification (`notifications/claude/channel`) fires unconditionally from the background poll, regardless of which client is connected — clients that don't understand the method simply ignore it, per the MCP spec. Separately, `send_teams_message` decides whether to auto-block waiting for a sponsor reply by checking the connected client's `clientInfo.name` (current or last-cached) against a small hardcoded set of recognized channel-push host names in `mcp_server.py`. This is a server-side lookup table, not a parameter exposed to the model — there is no tool argument the LLM can set to skip or force the wait. See [Messaging and Delivery](messaging-and-delivery.md) for the full behavior on each side of that check.

## Efferent-copy observer mechanism

`src/entrabot/efferent_copy.py` is an opt-in side-channel: when enabled, every registered tool's call fires a fire-and-forget `observe(tool_name, args[, result])` call to any MCP peer that looks like it can receive one.

- **Opt-in, not default.** `discover_sinks()` returns zero sinks unless `EFFERENT_COPY_ENABLE=1` is set; `EFFERENT_COPY_DISABLE=1` forces registration off even when enable is set. With zero sinks, `install_into_fastmcp` wraps nothing and tool behavior is unchanged.
- **Schema-based discovery, not name-based.** A peer is eligible only if its `tools/list` advertises a tool named `observe` accepting an object-shaped `{tool_name: string, args: object}` schema. There is no persona-sati-specific name or URL hardcoded into the discovery logic; operators can further restrict the eligible set with `EFFERENT_COPY_SINKS=name1,name2`.
- **Wrapping.** `install_into_fastmcp` wraps every registered tool's underlying function except `observe` itself (no recursion). Each wrapped call fires `observe` before execution (tool name + bound arguments) and again after (tool name + result), both on a 250ms per-sink timeout via `asyncio.timeout`. A timeout or any exception from a sink is caught, throttle-logged, and swallowed — it never propagates to the caller, and the wrapped tool's return value (or raised exception) is unchanged regardless of how many sinks are attached or whether they succeed.
- **Redaction, not a payload allowlist.** Before a payload leaves the process, `_collect_kwargs`/`_redact_sensitive` replace any argument or (recursively) any dict-result key whose name contains a case-insensitive substring from a fixed denylist (`token`, `secret`, `password`, `client_secret`, `access_token`, `bearer`, `credential`, `private_key`, and similar) with the literal string `"<redacted>"`. This is name-based redaction, not content inspection — a sensitive value stored under a non-obvious key name is not caught. Non-JSON-safe values are coerced via dataclass/pydantic dumping or `repr()` before being sent, so the argument set forwarded to a sink is not an arbitrary or unbounded payload, but it is also not a curated allowlist of "safe" fields — everything not name-matched by the denylist is forwarded as captured.

## Degraded / body-only mode

Without persona-sati configured (or with it unreachable), Entrabot runs in body-only mode: identity, Teams/email/Files tools, and audit all continue to work exactly as documented, but no personality, long-term memory, or `observe`/`reflect`/`recall` cognition loop is available. This is distinct from the efferent-copy mechanism above — a session can have efferent-copy enabled with zero eligible sinks (also effectively degraded) independent of whether a host has separately wired up persona-sati's own tools in `.mcp.json`.

## See also

- [Identity and Token Flow](identity-and-token-flow.md) — the auth modes and token lifecycle this runtime drives.
- [Messaging and Delivery](messaging-and-delivery.md) — background poll, channel push, and the sponsor-DM wait pattern in detail.
- [Clients Overview](../clients/overview.md) — per-host behavior differences.
- [Security Boundaries](security-boundaries.md) — the fail-closed model the storage backend check and sponsor-channel binding both belong to.
- [Storage Configuration guide](../guides/storage-configuration.md) — the `MemoryBackend` resolution this runtime validates at boot, and how operational memory differs from persona-sati's memory.
