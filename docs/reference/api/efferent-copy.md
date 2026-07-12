# Efferent-copy dispatch

Opt-in observer-sink middleware that broadcasts every `@mcp.tool()` call as a side-channel `observe(tool_name, args[, result])` to compatible MCP peers. Source: `src/entrabot/efferent_copy.py`.

The biological metaphor: every motor command the brain issues also generates a copy routed to sensory-prediction circuits so they can anticipate the consequences. This module is the infrastructure version.

**The body is authoritative. Sinks are passive observers, not a default-on feature.** Whether zero, one, or many sinks are registered, tool semantics are identical and return values are byte-for-byte unchanged.

## When it activates

Discovery and wrapping happen once, at server boot, inside `_run_stdio_with_write_stream`.

- `EFFERENT_COPY_ENABLE=1` — required to register any sink. Without it, `discover_sinks()` returns an empty list and no tool functions are wrapped.
- `EFFERENT_COPY_DISABLE=1` — forces registration off even when `EFFERENT_COPY_ENABLE=1` is also set. Checked first, so it always wins.
- `EFFERENT_COPY_SINKS=name1,name2` — optional allowlist of `.mcp.json` peer names. When set, only listed peers are probed as sink candidates; peers not in the list are skipped before any connection is attempted. When unset, every schema-compatible peer is eligible, and `install_into_fastmcp` logs a one-time warning naming every sink that will receive `observe` traffic.

Body behavior is identical with or without sinks — this is not always-on, and it must not be described as such.

## Discovery is schema-based, not name-based

Any peer listed in `.mcp.json` (stdio, SSE, or streamable-HTTP transport) is eligible if it exposes a tool named `observe` whose input schema has `properties.tool_name` (string) and an object-accepting `properties.args`. There are no peer-specific names, URLs, or tokens hardcoded into the discovery logic — `_has_compatible_observe(session)` is the schema check, applied uniformly to every peer.

## Self-reference defense

Wrapping entrabot's own MCP server as one of its own sinks would spawn a child that runs its own `discover_sinks()`, which spawns a grandchild, and so on. Two defenses:

1. `_is_self_referential_peer(peer)` resolves a stdio peer's `command` against the running process's `sys.argv[0]` and `sys.executable`. A thin wrapper script (e.g. the debug wrapper at `scripts/entrabot-mcp-debug.sh`) that `exec`s into the real binary is still detected if it declares its target via a `# entrabot-self-ref-target: <path>` marker comment, read by `_wrapper_self_ref_target`.
2. Every stdio sink factory injects `EFFERENT_COPY_DISABLE=1` into the child process's environment, so even if a self-referential peer slips past check 1, its own discovery short-circuits immediately — bounding worst-case spawn depth at one level.
3. `observe` itself is never wrapped — `install_into_fastmcp` skips it by name, and `wrap_tool_fn` raises `ValueError` if asked to wrap it.

## Redaction — and its limits

Before any payload leaves the process, `_redact_sensitive` walks dict keys (recursively, including within lists/tuples) and replaces the value with the literal string `"<redacted>"` wherever the key name case-insensitively contains one of a fixed set of substrings: `token`, `secret`, `password`, `passwd`, `authorization`, `client_secret`, `refresh_token`, `access_token`, `api_key`, `apikey`, `bearer`, `credential`, `private_key`. This applies both to tool arguments (via `_collect_kwargs`) and to dict-shaped results (via `_wrap_result`).

This is **name/key-substring redaction, not semantic content inspection**. A sensitive value stored under a non-obvious key name (the module's own example: `auth_blob`) is not caught unless the name matches the denylist. Treat any attached sink as part of the trusted observability boundary, not as something the redaction logic fully protects against.

## API

### `Sink`

```python
@dataclasses.dataclass
class Sink:
    name: str
    factory: Callable[[], Any]
    _last_warn_ts: float = 0.0
```

A registered efferent-copy target. `factory` is a zero-arg callable returning an async context manager that yields an object with an async `call_tool(name, payload)` method — narrower than the full `mcp` SDK `ClientSession`, so tests can swap in an in-memory recorder without a real transport.

### `discover_sinks`

```python
async def discover_sinks(config_path: Path | None = None) -> list[Sink]
```

Read `.mcp.json` (or `config_path`), honor `EFFERENT_COPY_DISABLE` / `EFFERENT_COPY_ENABLE` / `EFFERENT_COPY_SINKS`, skip self-referential peers, build a transport-specific sink factory per remaining peer, and probe each one for a compatible `observe` tool. Each probe is bounded by `DISCOVERY_TIMEOUT_S = 5.0`; any exception during probing makes that peer ineligible (logged, not raised). Returns the list of sinks that passed the schema check.

### `install_into_fastmcp`

```python
def install_into_fastmcp(
    mcp: Any,
    sinks: list[Sink],
    *,
    main_loop: asyncio.AbstractEventLoop | None = None,
) -> None
```

Called at boot after all `@mcp.tool()` registrations. Iterates the FastMCP tool manager and replaces each tool's `fn` with a `wrap_tool_fn`-wrapped version, except the tool named `observe`. No-op when `sinks` is empty — `tool.fn` is left untouched, so behavior is byte-for-byte identical to a build without efferent copy.

### `wrap_tool_fn`

```python
def wrap_tool_fn(
    sinks: list[Sink],
    tool_name: str,
    fn: Callable,
    *,
    main_loop: asyncio.AbstractEventLoop | None | object = _MAIN_LOOP_UNSET,
) -> Callable
```

Wraps a single tool function: fires `observe(tool_name, args)` before calling `fn`, then `observe(tool_name, args, result=...)` after — with `result={"error": str(exc), "error_type": type(exc).__name__}` on exception, still re-raised unchanged. Raises `ValueError` if `tool_name == "observe"`. Returns `fn` unchanged (identity, preserving FastMCP's introspection) when `sinks` is empty.

### `fire_observe`

```python
async def fire_observe(
    sinks: list[Sink],
    tool_name: str,
    args: dict,
    result: Any = None,
) -> None
```

Schedules an `observe` call on every sink via `asyncio.create_task` and returns immediately without awaiting any of them — fire-and-forget. Each per-sink task applies its own `OBSERVE_TIMEOUT_S = 0.250` second timeout; a timeout or any exception is caught, throttle-logged (`WARN_THROTTLE_S = 60.0` seconds between repeated warnings for the same sink), and swallowed. Nothing here can propagate back to the calling tool.

## Result coercion

`_wrap_result(result)` turns a tool's return value into the dict shape `observe` expects: a dict result is redacted and passed through; anything else becomes `{"value": <json-safe-and-redacted>}`.

`_json_safe(value)`:

- Already JSON-serializable → returned as-is.
- Dataclass instance → `dataclasses.asdict(value)`.
- Has `model_dump()` (pydantic v2) or `dict()` (pydantic v1) → the dumped result, if it's JSON-serializable.
- Anything else → `repr(value)`.

## Use case

The reference sink is persona-sati, which uses `observe` calls to update its prediction-error estimate and feed the per-turn cognition protocol described in [Persona-Sati Host Bootstrap](../../clients/persona-sati-host-bootstrap.md). Any other peer exposing the right `observe` schema is equally eligible — there is nothing persona-sati-specific in this module.

## Related

- [MCP Runtime](../../architecture/mcp-runtime.md) — where sink discovery and installation fit in server boot.
- [Security Boundaries](../../architecture/security-boundaries.md) — the efferent-copy trust boundary and redaction caveats in context.
- [Body Prompt](body-prompt.md) — the instructions this middleware wraps around.
