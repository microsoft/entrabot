# Efferent-copy dispatch

Observer-sink middleware that broadcasts every `@mcp.tool()` call as a side-channel `observe(tool_name, args[, result])` to compatible MCP peers. Source: `src/entrabot/efferent_copy.py`.

The biological metaphor: every motor command the brain issues also generates a copy routed to sensory-prediction circuits so they can anticipate the consequences. This module is the infrastructure version.

**The body is authoritative.** Sinks are passive observers. Whether zero, one, or many sinks are registered, tool semantics are identical and return values are byte-for-byte unchanged.

## When it activates

Opt-in only. Discovery and wrapping happen at server boot inside `_run_stdio_with_write_stream`.

- `EFFERENT_COPY_ENABLE=1` — register sinks. Without this flag, discovery returns zero sinks and no tool functions are wrapped.
- `EFFERENT_COPY_DISABLE=1` — force registration off even when enable is set.

Body behaviour is identical with or without sinks.

## Discovery is schema-based

Any peer in `.mcp.json` that exposes a tool named `observe` accepting `{tool_name: string, args: object}` is eligible. No peer-specific names or URLs live in this module. The peer can be stdio, SSE, or HTTP; the factory functions in `efferent_copy.py` cover all three.

`_has_compatible_observe(session)` is the gate: it lists the peer's tools and checks the `observe` tool's input schema matches the expected shape.

## Self-reference defence

Wrapping the entrabot MCP server itself as one of its own sinks would create an infinite loop. Two defences:

1. `_is_self_referential_peer(peer)` checks whether the peer's `command` is the same script as the running process. The debug wrapper at `scripts/entrabot-mcp-debug.sh` carries an `entrabot-self-ref-target: ../.venv/bin/entrabot-mcp` marker so swapping the `command` to the wrapper still gets recognized as self.
2. `observe` itself is never wrapped — no recursion when one sink calls `observe` on the same server.

See Learning #45 for the underlying incident.

## API

### `Sink`

```python
@dataclass
class Sink:
    name: str
    factory: Callable[[], Any]
    _last_warn_ts: float = 0.0
```

A registered efferent-copy target. `factory` is a zero-arg callable returning an async context manager that yields an object with an async `call_tool(name, payload)` method.

### `discover_sinks`

```python
async def discover_sinks(config_path: Path | None = None) -> list[Sink]
```

Read `.mcp.json` (or `config_path`), instantiate a sink factory per peer, probe each one for a compatible `observe` tool, and return the matching sinks. Returns an empty list when `EFFERENT_COPY_ENABLE` is unset.

Discovery timeout: `DISCOVERY_TIMEOUT_S = 5.0` per peer.

### `install_into_fastmcp`

```python
def install_into_fastmcp(mcp: Any, sinks: list[Sink]) -> None
```

Wrap every registered tool's `fn` with pre/post `observe` firing. Idempotent. `audit_log` and `observe` itself are skipped.

### `fire_observe`

```python
async def fire_observe(
    sinks: list[Sink],
    tool_name: str,
    args: dict,
    result: Any = None,
) -> None
```

Schedule `observe` on every sink without awaiting any of them. Returns immediately after scheduling. Per-sink timeout (`OBSERVE_TIMEOUT_S = 0.250`) is applied inside each background task.

Failures are swallowed and warned via a throttled log (`WARN_THROTTLE_S = 60.0`).

## Result coercion

Tools return mixed shapes; `observe` wants dicts. `_wrap_result(result)`:

- Dict result → pass through unchanged.
- Non-dict → `{"value": <json-safe-repr>}`.

`_json_safe(value)`:

- Plain JSON-serializable → as-is.
- Dataclass → `dataclasses.asdict`.
- Pydantic v2 → `model_dump()`.
- Pydantic v1 → `dict()`.
- Anything else → `repr(value)`.

On tool exception the post-call fires `{"error": str, "error_type": str}` and the exception re-raises to the caller unchanged.

## Use case

The reference sink is persona-sati, which uses `observe` calls to update its prediction-error estimate and feed the per-turn cognition protocol (see `mcp__persona-sati__bootstrap_session`). Any other peer with the right schema is equally eligible.
