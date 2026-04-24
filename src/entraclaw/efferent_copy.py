"""Efferent-copy dispatch middleware for entraclaw's MCP server.

The biological metaphor: every motor command the brain issues also
generates a copy routed to sensory-prediction circuits so they can
anticipate the consequences of the command. This module is the
infrastructure version — every @mcp.tool() call on entraclaw fires a
side-channel ``observe(tool_name, args[, result])`` to any MCP peer
that advertises a compatibly-typed ``observe`` tool.

The body is authoritative. Sinks are passive observers. Whether zero,
one, or many sinks are registered, tool semantics are identical and
return values are byte-for-byte unchanged.

Discovery is purely schema-based: any peer in ``.mcp.json`` that
exposes a tool named ``observe`` accepting ``{tool_name: string,
args: object}`` is eligible. No peer-specific names or URLs live in
this module.

Opt-out: set ``EFFERENT_COPY_DISABLE=1`` to skip registration entirely.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import inspect
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

OBSERVE_TOOL = "observe"
OBSERVE_TIMEOUT_S = 0.250
DISCOVERY_TIMEOUT_S = 5.0
WARN_THROTTLE_S = 60.0
DISABLE_ENV = "EFFERENT_COPY_DISABLE"


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Sink:
    """A registered efferent-copy target.

    ``factory`` is a zero-arg callable returning an async context
    manager that yields an object with an async ``call_tool(name,
    payload)`` method. This is deliberately narrower than the mcp SDK's
    ``ClientSession`` so tests can swap in a pure in-memory recorder
    without pulling the full transport stack.
    """

    name: str
    factory: Callable[[], Any]
    _last_warn_ts: float = 0.0


# ---------------------------------------------------------------------------
# Result coercion — tools return mixed shapes; observe wants dicts.
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    """Coerce ``value`` into something ``json.dumps`` will accept."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        pass
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            return dataclasses.asdict(value)
        except TypeError:
            pass
    for attr in ("model_dump", "dict"):  # pydantic v2, v1
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                dumped = fn()
                json.dumps(dumped)
                return dumped
            except (TypeError, ValueError):
                continue
    return repr(value)


def _wrap_result(result: Any) -> dict:
    """Return ``result`` as a dict payload for observe's ``result`` arg.

    Dict results pass through untouched so sinks see tool return values
    verbatim. Everything else becomes ``{"value": <json-safe>}``.
    """
    if isinstance(result, dict):
        return result
    return {"value": _json_safe(result)}


# ---------------------------------------------------------------------------
# fire_observe — the fire-and-forget dispatch primitive.
# ---------------------------------------------------------------------------


def _throttled_warn(sink: Sink, msg: str) -> None:
    now = time.monotonic()
    if now - sink._last_warn_ts < WARN_THROTTLE_S:
        return
    sink._last_warn_ts = now
    log.warning("efferent-copy sink %s %s", sink.name, msg)


async def _fire_one(
    sink: Sink, tool_name: str, args: dict, result: dict | None
) -> None:
    """Fire a single observe call to one sink. Swallows all exceptions."""
    payload: dict[str, Any] = {"tool_name": tool_name, "args": args}
    if result is not None:
        payload["result"] = result
    try:
        async with (
            asyncio.timeout(OBSERVE_TIMEOUT_S),
            sink.factory() as session,
        ):
            await session.call_tool(OBSERVE_TOOL, payload)
    except TimeoutError:
        _throttled_warn(sink, f"timed out after {OBSERVE_TIMEOUT_S:.3f}s")
    except Exception as exc:  # noqa: BLE001 — sinks MUST NOT break the body
        _throttled_warn(sink, f"raised {type(exc).__name__}: {exc}")


async def fire_observe(
    sinks: list[Sink],
    tool_name: str,
    args: dict,
    result: Any = None,
) -> None:
    """Schedule observe on every sink without awaiting any of them.

    Returns immediately after scheduling. Per-sink timeout is applied
    inside each background task, not here.
    """
    if not sinks:
        return
    payload_result = _wrap_result(result) if result is not None else None
    for sink in sinks:
        asyncio.create_task(_fire_one(sink, tool_name, args, payload_result))


# ---------------------------------------------------------------------------
# wrap_tool_fn — the per-tool dispatch wrapper.
# ---------------------------------------------------------------------------


def _collect_kwargs(fn: Callable, args: tuple, kwargs: dict) -> dict:
    """Best-effort bind of (args, kwargs) back to a named-argument dict."""
    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
        return {k: _json_safe(v) for k, v in bound.arguments.items()}
    except (TypeError, ValueError):
        return {
            "args": [_json_safe(a) for a in args],
            "kwargs": {k: _json_safe(v) for k, v in kwargs.items()},
        }


def wrap_tool_fn(
    sinks: list[Sink], tool_name: str, fn: Callable
) -> Callable:
    """Wrap ``fn`` with pre/post observe firing.

    The wrapped function:
    - Fires ``observe(tool_name, args)`` before calling ``fn``.
    - Calls ``fn`` with the same (args, kwargs) it received.
    - On success, fires ``observe(tool_name, args, result=...)``.
    - On exception, fires ``observe(tool_name, args, result={"error":
      str(exc), "error_type": type(exc).__name__})`` and re-raises.

    The tool's return value is byte-for-byte unchanged.

    Raises ``ValueError`` if ``tool_name == OBSERVE_TOOL`` — the
    middleware MUST NOT be applied to observe itself.
    """
    if tool_name == OBSERVE_TOOL:
        raise ValueError(
            f"refusing to wrap the {OBSERVE_TOOL!r} tool "
            "(would recurse infinitely)"
        )
    if not sinks:
        # Transparent pass-through. Preserves identity so FastMCP's
        # fn_metadata introspection still sees the original function.
        return fn

    is_async = asyncio.iscoroutinefunction(fn)

    if is_async:

        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            args_dict = _collect_kwargs(fn, args, kwargs)
            await fire_observe(sinks, tool_name, args_dict)
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                await fire_observe(
                    sinks,
                    tool_name,
                    args_dict,
                    result={
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )
                raise
            await fire_observe(sinks, tool_name, args_dict, result=result)
            return result

        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(*args, **kwargs):
        args_dict = _collect_kwargs(fn, args, kwargs)
        loop = asyncio.get_event_loop()
        loop.create_task(fire_observe(sinks, tool_name, args_dict))
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            loop.create_task(
                fire_observe(
                    sinks,
                    tool_name,
                    args_dict,
                    result={
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )
            )
            raise
        loop.create_task(fire_observe(sinks, tool_name, args_dict, result=result))
        return result

    return sync_wrapper


# ---------------------------------------------------------------------------
# Capability discovery — parse .mcp.json, pick peers that expose observe.
# ---------------------------------------------------------------------------


def _load_peers_from_config(path: Path) -> list[dict]:
    """Parse a ``.mcp.json`` file into a list of peer specs."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("efferent-copy: could not read %s: %s", path, exc)
        return []
    servers = data.get("mcpServers") or {}
    return [{"name": name, **spec} for name, spec in servers.items()]


def _resolve_headers(peer: dict) -> dict[str, str]:
    """Build request headers from a peer spec (e.g. Authorization)."""
    return dict(peer.get("headers") or {})


def _build_sink_factory(peer: dict) -> Callable[[], Any] | None:
    """Return a zero-arg async-ctx-factory for a peer, or None if unsupported."""
    name = peer.get("name", "<anon>")
    transport = (peer.get("type") or peer.get("transport") or "stdio").lower()

    if transport == "stdio":
        return _stdio_factory(peer)
    if transport == "sse":
        return _sse_factory(peer)
    if transport in {"http", "streamable-http", "streamable_http"}:
        return _http_factory(peer)

    log.debug(
        "efferent-copy: peer %s has unsupported transport %r; skipping",
        name,
        transport,
    )
    return None


def _stdio_factory(peer: dict) -> Callable[[], Any]:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    command = peer["command"]
    args = list(peer.get("args") or [])
    env_overrides = peer.get("env") or {}
    # Belt-and-suspenders for the self-referential-peer cascade (April
    # 2026 incident). If a peer's command somehow points at an MCP
    # server that ALSO runs ``discover_sinks`` against the same
    # ``.mcp.json`` (e.g., entraclaw listed as a stdio peer of
    # itself), the child's discovery would spawn a grandchild, and so
    # on. The caller-side self-referential filter is the primary
    # defense; this env var short-circuits the child's own discovery
    # as a second line, bounding worst-case spawn depth at 1.
    env = {**os.environ, **env_overrides, DISABLE_ENV: "1"}
    params = StdioServerParameters(command=command, args=args, env=env)

    @contextlib.asynccontextmanager
    async def _open():
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session

    return _open


def _sse_factory(peer: dict) -> Callable[[], Any]:
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    url = peer["url"]
    headers = _resolve_headers(peer)

    @contextlib.asynccontextmanager
    async def _open():
        async with (
            sse_client(url, headers=headers) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session

    return _open


def _http_factory(peer: dict) -> Callable[[], Any]:
    from mcp import ClientSession

    try:
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError:  # older SDK fallback
        streamablehttp_client = None  # type: ignore[assignment]

    if streamablehttp_client is None:
        log.debug(
            "efferent-copy: mcp SDK lacks streamable_http client; "
            "skipping http peer %s",
            peer.get("name"),
        )

        def _unsupported():
            raise RuntimeError("streamable_http not available in this mcp SDK")

        return _unsupported

    url = peer["url"]
    headers = _resolve_headers(peer)

    @contextlib.asynccontextmanager
    async def _open():
        async with streamablehttp_client(url, headers=headers) as streams:
            read, write, *_ = streams
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    return _open  # noqa: SIM117 — streams must unpack between the two contexts


def _is_object_schema(schema: Any) -> bool:
    """Return True if a schema accepts an object value (loose check)."""
    if not isinstance(schema, dict):
        return False
    t = schema.get("type")
    if t == "object":
        return True
    for variant in (schema.get("anyOf") or []) + (schema.get("oneOf") or []):
        if isinstance(variant, dict) and variant.get("type") == "object":
            return True
    # Unconstrained schema — permissive by default.
    return t is None and "properties" not in schema and not schema.get("anyOf")


async def _has_compatible_observe(session: Any) -> bool:
    """Check via tools/list whether a session's peer advertises observe.

    Match rules:
      - A tool named exactly ``observe``.
      - inputSchema has a ``properties`` object containing at minimum
        ``tool_name`` (string) and ``args`` (object-accepting).
    """
    tools_result = await session.list_tools()
    for tool in getattr(tools_result, "tools", []) or []:
        if getattr(tool, "name", None) != OBSERVE_TOOL:
            continue
        schema = getattr(tool, "inputSchema", None) or {}
        props = schema.get("properties") if isinstance(schema, dict) else None
        if not isinstance(props, dict):
            return False
        if "tool_name" not in props or "args" not in props:
            return False
        tool_name_prop = props["tool_name"]
        if not isinstance(tool_name_prop, dict):
            return False
        if tool_name_prop.get("type") not in {"string", None}:
            return False
        return _is_object_schema(props["args"])
    return False


SELF_REF_MARKER = "# entraclaw-self-ref-target:"
SELF_REF_MAX_BYTES = 16 * 1024


def _wrapper_self_ref_target(script_path: Path) -> Path | None:
    """If ``script_path`` is a wrapper that declares its exec target via a
    ``# entraclaw-self-ref-target: <path>`` comment, return the resolved
    target path. Returns None for non-wrappers, missing markers, missing
    files, or unreadable content.

    The marker is opt-in. Arbitrary shell scripts (which may build their
    target via ``$(cd ... && pwd)`` or other dynamic expressions) are not
    parsed — that path is fragile and silently breaks. The marker is the
    wrapper saying "the binary I exec into is at this path." Path resolves
    relative to the script's own directory if not absolute.

    Bounded reads: see ``SELF_REF_MAX_BYTES``. Wrappers are short shell
    scripts; anything larger is presumed to be a real program, not a
    wrapper, and is ignored.
    """
    try:
        if script_path.stat().st_size > SELF_REF_MAX_BYTES:
            return None
        text = script_path.read_text(errors="replace")
    except OSError:
        return None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(SELF_REF_MARKER):
            continue
        target = stripped[len(SELF_REF_MARKER):].strip()
        if not target:
            return None
        target_path = Path(target)
        if not target_path.is_absolute():
            target_path = script_path.parent / target_path
        try:
            return target_path.resolve()
        except (OSError, ValueError):
            return None
    return None


def _is_self_referential_peer(peer: dict) -> bool:
    """True if the peer's stdio command points back at *this* process.

    Opening a stdio session to a peer whose command is our own entry
    point spawns a child of ourselves. The child boots and runs its
    own ``discover_sinks``, which spawns a grandchild, recursing until
    every level's 5-second timeout fires. The April 2026 incident:
    ~30 child ``entraclaw-mcp`` subprocesses per minute for 2h+,
    silently dropping every Teams DM push in the process.

    Matching is on resolved absolute paths against ``sys.argv[0]`` and
    ``sys.executable``. Wrapper scripts that exec into the running
    binary are also detected when they include a
    ``# entraclaw-self-ref-target: <path>`` marker — see
    ``_wrapper_self_ref_target``. The April 2026 sequel (Learning #45):
    swapping ``.mcp.json``'s command to a stderr-capture wrapper
    bypassed the path-only check and reintroduced the cascade.

    Non-stdio peers return False immediately; stdio peers with no
    ``command`` field also return False (the transport construction
    will fail elsewhere).
    """
    import sys

    transport = (peer.get("type") or peer.get("transport") or "").lower()
    if transport != "stdio":
        return False
    command = peer.get("command")
    if not command:
        return False
    try:
        peer_resolved = Path(command).resolve()
    except (OSError, ValueError):
        return False

    candidates: list[str] = []
    if sys.argv and sys.argv[0]:
        candidates.append(sys.argv[0])
    if sys.executable:
        candidates.append(sys.executable)

    def _matches_running(path: Path) -> bool:
        for cand in candidates:
            try:
                if Path(cand).resolve() == path:
                    return True
            except (OSError, ValueError):
                continue
        return False

    if _matches_running(peer_resolved):
        return True

    # Wrapper detection: thin shell wrappers (e.g., debug stderr capture
    # at scripts/entraclaw-mcp-debug.sh) may exec into the running binary.
    # The wrapper opts in to detection by including the marker comment.
    if peer_resolved.is_file():
        wrapper_target = _wrapper_self_ref_target(peer_resolved)
        if wrapper_target is not None and _matches_running(wrapper_target):
            return True

    return False


async def discover_sinks(config_path: Path | None = None) -> list[Sink]:
    """Enumerate peers and return those that expose a compatible observe.

    Honors ``EFFERENT_COPY_DISABLE=1`` as a short-circuit: when set,
    no peer is contacted and an empty list is returned.
    """
    if os.environ.get(DISABLE_ENV) == "1":
        log.info(
            "efferent-copy: %s=1; registering 0 sinks",
            DISABLE_ENV,
        )
        return []

    path = config_path or Path.cwd() / ".mcp.json"
    peers = _load_peers_from_config(path)
    sinks: list[Sink] = []

    for peer in peers:
        name = peer["name"]
        if _is_self_referential_peer(peer):
            log.debug(
                "efferent-copy: peer %s points at our own entry point; "
                "skipping to avoid spawn cascade",
                name,
            )
            continue
        factory = _build_sink_factory(peer)
        if factory is None:
            continue
        try:
            async with asyncio.timeout(DISCOVERY_TIMEOUT_S):
                async with factory() as session:
                    ok = await _has_compatible_observe(session)
        except Exception as exc:  # noqa: BLE001 — any error → peer is ineligible
            log.debug(
                "efferent-copy: peer %s not eligible (%s: %s)",
                name,
                type(exc).__name__,
                exc,
            )
            continue
        if ok:
            sinks.append(Sink(name=name, factory=factory))
            log.debug("efferent-copy: peer %s registered as sink", name)

    log.info("efferent-copy sinks registered: %d", len(sinks))
    return sinks


# ---------------------------------------------------------------------------
# install_into_fastmcp — boot-time wiring.
# ---------------------------------------------------------------------------


def install_into_fastmcp(mcp: Any, sinks: list[Sink]) -> None:
    """Wrap every @mcp.tool() registration on ``mcp`` with the middleware.

    Called at boot, after all @mcp.tool() decorators have run. Iterates
    the FastMCP tool manager and replaces each tool's ``fn`` attribute
    with a middleware-wrapped version. The tool named ``observe`` is
    never wrapped — wrapping it would cause unbounded recursion.

    When ``sinks`` is empty, this function is a no-op: tool.fn is left
    untouched, so behavior is byte-for-byte identical to a build without
    efferent copy.
    """
    if not sinks:
        return

    tool_manager = getattr(mcp, "_tool_manager", None)
    if tool_manager is None or not hasattr(tool_manager, "_tools"):
        log.warning(
            "efferent-copy: FastMCP instance has no tool manager; skipping install"
        )
        return

    for name, tool in list(tool_manager._tools.items()):
        if name == OBSERVE_TOOL:
            continue
        original_fn = tool.fn
        tool.fn = wrap_tool_fn(sinks, name, original_fn)
