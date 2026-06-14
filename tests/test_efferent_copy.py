"""Tests for the efferent-copy dispatch middleware.

See `src/entrabot/efferent_copy.py`. The middleware fires a generic
`observe(tool_name, args[, result])` MCP call to any peer that advertises
a compatibly-typed `observe` tool, before and after every @mcp.tool()
dispatch. Discovery is schema-based; there are no peer-specific names.

Correctness bar: the body MUST be byte-for-byte identical when no sink
is registered, when sinks fail, and when sinks time out.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import warnings
from pathlib import Path
from unittest.mock import AsyncMock

import anyio
import pytest

from entrabot import efferent_copy as ec

# ---------------------------------------------------------------------------
# Helpers — an in-memory fake Sink that records observe invocations.
# ---------------------------------------------------------------------------


class _RecorderSink:
    """In-memory sink that captures every observe payload it receives.

    Implements the minimum surface `fire_observe` needs: it exposes a
    `factory()` async-context-manager that yields an object with an
    async `call_tool(name, payload)` method. The Sink dataclass wraps
    this in production; tests bypass the dataclass by calling the
    internal `_fire_one` directly with a stubbed Sink.
    """

    def __init__(self, name: str = "fake", delay: float = 0.0, raise_on_call: bool = False):
        self.name = name
        self.delay = delay
        self.raise_on_call = raise_on_call
        self.calls: list[dict] = []

    def as_sink(self) -> ec.Sink:
        recorder = self

        class _Session:
            async def call_tool(self, tool_name: str, payload: dict) -> dict:
                if recorder.delay:
                    await asyncio.sleep(recorder.delay)
                if recorder.raise_on_call:
                    raise RuntimeError("sink boom")
                recorder.calls.append({"tool_name": tool_name, "payload": payload})
                return {"ok": True}

        class _Ctx:
            async def __aenter__(self_inner):
                return _Session()

            async def __aexit__(self_inner, *a):
                return None

        def _factory():
            return _Ctx()

        return ec.Sink(name=self.name, factory=_factory)


# ---------------------------------------------------------------------------
# _wrap_result / _json_safe — result coercion
# ---------------------------------------------------------------------------


class TestResultCoercion:
    def test_dict_passes_through_unchanged(self):
        r = {"a": 1, "b": [2, 3]}
        assert ec._wrap_result(r) is r

    def test_string_wrapped_as_value(self):
        assert ec._wrap_result("hello") == {"value": "hello"}

    def test_list_wrapped_as_value(self):
        assert ec._wrap_result([1, 2, 3]) == {"value": [1, 2, 3]}

    def test_dataclass_coerced(self):
        import dataclasses

        @dataclasses.dataclass
        class Point:
            x: int
            y: int

        out = ec._wrap_result(Point(1, 2))
        assert out == {"value": {"x": 1, "y": 2}}

    def test_non_serializable_falls_back_to_repr(self):
        class Weird:
            def __repr__(self):
                return "<Weird>"

        out = ec._wrap_result(Weird())
        assert out == {"value": "<Weird>"}


# ---------------------------------------------------------------------------
# fire_observe — fire-and-forget plumbing
# ---------------------------------------------------------------------------


class TestFireObserve:
    async def test_zero_sinks_is_noop(self):
        # Primary correctness requirement: empty sink list = no-op.
        await ec.fire_observe([], "send_email", {"to": "x@y.z"})
        # No exception, returns immediately. Nothing to assert beyond that.

    async def test_fires_on_every_registered_sink(self):
        a = _RecorderSink("a")
        b = _RecorderSink("b")
        sinks = [a.as_sink(), b.as_sink()]

        await ec.fire_observe(sinks, "send_email", {"to": "x@y.z"})
        # Fire-and-forget: let tasks drain.
        await asyncio.sleep(0.05)

        assert len(a.calls) == 1
        assert len(b.calls) == 1
        assert a.calls[0]["tool_name"] == "observe"
        assert a.calls[0]["payload"] == {
            "tool_name": "send_email",
            "args": {"to": "x@y.z"},
        }

    async def test_sink_timeout_does_not_block(self):
        # Sink sleeps far longer than the 250ms budget. fire_observe is
        # fire-and-forget, so it MUST return immediately and the timeout
        # MUST be swallowed in the background task.
        slow = _RecorderSink("slow", delay=5.0)
        sinks = [slow.as_sink()]

        import time

        t0 = time.monotonic()
        await ec.fire_observe(sinks, "t", {})
        elapsed = time.monotonic() - t0

        assert elapsed < 0.100, f"fire_observe blocked {elapsed:.3f}s; must be fire-and-forget"
        # Give the timeout machinery a chance to fire (should be ~250ms).
        await asyncio.sleep(0.4)
        # Call never completed because it timed out; no record.
        assert slow.calls == []

    async def test_sink_exception_does_not_raise(self):
        angry = _RecorderSink("angry", raise_on_call=True)
        sinks = [angry.as_sink()]

        # Must not raise.
        await ec.fire_observe(sinks, "t", {})
        await asyncio.sleep(0.05)
        assert angry.calls == []

    async def test_post_call_includes_result(self):
        rec = _RecorderSink("r")
        sinks = [rec.as_sink()]

        await ec.fire_observe(sinks, "send", {"to": "x"}, result={"ok": True})
        await asyncio.sleep(0.05)

        assert rec.calls[0]["payload"] == {
            "tool_name": "send",
            "args": {"to": "x"},
            "result": {"ok": True},
        }


# ---------------------------------------------------------------------------
# Middleware — pre/post observe around tool dispatch
# ---------------------------------------------------------------------------


class TestMiddleware:
    async def test_pre_and_post_observe_fired(self):
        rec = _RecorderSink("r")
        sinks = [rec.as_sink()]

        async def send(to: str) -> dict:
            return {"id": "msg-1"}

        wrapped = ec.wrap_tool_fn(sinks, "send_email", send)
        out = await wrapped(to="a@b.c")

        assert out == {"id": "msg-1"}  # byte-for-byte identical return.
        await asyncio.sleep(0.05)

        assert len(rec.calls) == 2
        pre, post = rec.calls
        assert "result" not in pre["payload"]
        assert pre["payload"]["tool_name"] == "send_email"
        assert pre["payload"]["args"] == {"to": "a@b.c"}
        assert post["payload"]["result"] == {"id": "msg-1"}

    async def test_zero_sinks_wrapper_is_transparent(self):
        # Non-dict result MUST pass through unchanged too.
        async def echo(x: int) -> int:
            return x * 2

        wrapped = ec.wrap_tool_fn([], "echo", echo)
        out = await wrapped(x=21)

        assert out == 42  # identical to unwrapped

    async def test_non_dict_result_wrapped_for_sink_only(self):
        rec = _RecorderSink("r")
        sinks = [rec.as_sink()]

        async def count() -> int:
            return 7

        wrapped = ec.wrap_tool_fn(sinks, "count", count)
        out = await wrapped()

        assert out == 7  # tool return unchanged
        await asyncio.sleep(0.05)
        # Sink sees {"value": 7}, not the raw int.
        post = rec.calls[1]["payload"]
        assert post["result"] == {"value": 7}

    async def test_tool_exception_fires_post_with_error_shape_and_reraises(self):
        rec = _RecorderSink("r")
        sinks = [rec.as_sink()]

        async def boom() -> None:
            raise ValueError("nope")

        wrapped = ec.wrap_tool_fn(sinks, "boom", boom)

        with pytest.raises(ValueError, match="nope"):
            await wrapped()

        await asyncio.sleep(0.05)
        assert len(rec.calls) == 2
        post_payload = rec.calls[1]["payload"]
        assert post_payload["result"] == {
            "error": "nope",
            "error_type": "ValueError",
        }

    async def test_two_sinks_both_receive_pre_and_post(self):
        a = _RecorderSink("a")
        b = _RecorderSink("b")
        sinks = [a.as_sink(), b.as_sink()]

        async def t() -> dict:
            return {"ok": True}

        wrapped = ec.wrap_tool_fn(sinks, "t", t)
        await wrapped()
        await asyncio.sleep(0.05)

        assert len(a.calls) == 2
        assert len(b.calls) == 2

    async def test_refuses_to_wrap_observe(self):
        # The middleware MUST NOT be applied to observe itself — otherwise
        # every observe fire would recursively fire more observes.
        async def observe(tool_name, args, result=None) -> dict:
            return {}

        with pytest.raises(ValueError, match="observe"):
            ec.wrap_tool_fn([], ec.OBSERVE_TOOL, observe)

    async def test_sink_failure_does_not_break_tool(self):
        # A sink that raises/blocks MUST NOT affect the tool's return value.
        slow = _RecorderSink("slow", delay=5.0)
        angry = _RecorderSink("angry", raise_on_call=True)
        sinks = [slow.as_sink(), angry.as_sink()]

        async def send() -> dict:
            return {"id": "ok"}

        wrapped = ec.wrap_tool_fn(sinks, "send", send)
        out = await wrapped()
        assert out == {"id": "ok"}

    async def test_sync_tool_dispatched_from_worker_thread_returns_unchanged(self):
        rec = _RecorderSink("r")
        sinks = [rec.as_sink()]

        def double(x: int) -> int:
            return x * 2

        wrapped = ec.wrap_tool_fn(sinks, "double", double)

        out = await anyio.to_thread.run_sync(lambda: wrapped(21))

        assert out == 42

    async def test_sync_tool_dispatched_from_worker_thread_fires_pre_and_post_observe(self):
        rec = _RecorderSink("r")
        sinks = [rec.as_sink()]

        def double(x: int) -> int:
            return x * 2

        wrapped = ec.wrap_tool_fn(sinks, "double", double)

        out = await anyio.to_thread.run_sync(lambda: wrapped(21))
        assert out == 42

        await asyncio.sleep(0.05)
        assert len(rec.calls) == 2
        pre, post = rec.calls
        assert pre["payload"] == {
            "tool_name": "double",
            "args": {"x": 21},
        }
        assert post["payload"] == {
            "tool_name": "double",
            "args": {"x": 21},
            "result": {"value": 42},
        }

    async def test_sync_tool_worker_thread_dispatch_emits_no_deprecation_warning(self):
        rec = _RecorderSink("r")
        sinks = [rec.as_sink()]

        def double(x: int) -> int:
            return x * 2

        wrapped = ec.wrap_tool_fn(sinks, "double", double)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            out = await anyio.to_thread.run_sync(lambda: wrapped(21))

        assert out == 42
        assert not [w for w in caught if issubclass(w.category, DeprecationWarning)]

    async def test_sync_tool_without_running_main_loop_returns_unchanged(self):
        rec = _RecorderSink("r")
        sinks = [rec.as_sink()]

        def double(x: int) -> int:
            return x * 2

        wrapped = ec.wrap_tool_fn(sinks, "double", double, main_loop=None)

        out = await anyio.to_thread.run_sync(lambda: wrapped(21))

        assert out == 42
        await asyncio.sleep(0.05)
        assert rec.calls == []

    async def test_sync_tool_with_stopped_main_loop_returns_unchanged(self):
        rec = _RecorderSink("r")
        sinks = [rec.as_sink()]
        stopped_loop = asyncio.new_event_loop()

        def double(x: int) -> int:
            return x * 2

        try:
            wrapped = ec.wrap_tool_fn(sinks, "double", double, main_loop=stopped_loop)

            out = await anyio.to_thread.run_sync(lambda: wrapped(21))
        finally:
            stopped_loop.close()

        assert out == 42
        await asyncio.sleep(0.05)
        assert rec.calls == []


# ---------------------------------------------------------------------------
# Capability discovery — schema-based filtering
# ---------------------------------------------------------------------------


class TestSchemaCompat:
    def _tool(self, name: str, schema: dict | None) -> object:
        class T:
            pass

        t = T()
        t.name = name
        t.inputSchema = schema
        return t

    async def test_matching_schema_is_eligible(self):
        schema = {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string"},
                "args": {"type": "object"},
                "result": {"type": "object"},
            },
            "required": ["tool_name", "args"],
        }

        class FakeList:
            tools = [self._tool("observe", schema)]

        session = AsyncMock()
        session.list_tools = AsyncMock(return_value=FakeList())

        assert await ec._has_compatible_observe(session) is True

    async def test_no_observe_tool_is_ineligible(self):
        class FakeList:
            tools = [self._tool("something_else", {"type": "object"})]

        session = AsyncMock()
        session.list_tools = AsyncMock(return_value=FakeList())

        assert await ec._has_compatible_observe(session) is False

    async def test_observe_with_wrong_shape_is_ineligible(self):
        # Missing `args` property entirely.
        schema = {
            "type": "object",
            "properties": {"tool_name": {"type": "string"}},
        }

        class FakeList:
            tools = [self._tool("observe", schema)]

        session = AsyncMock()
        session.list_tools = AsyncMock(return_value=FakeList())

        assert await ec._has_compatible_observe(session) is False


class TestDiscoverSinks:
    async def test_missing_mcp_json_returns_empty(self, tmp_path: Path):
        # No .mcp.json → zero sinks, no crash.
        sinks = await ec.discover_sinks(tmp_path / "nope.json")
        assert sinks == []

    async def test_default_disabled_does_not_contact_peers(self, tmp_path: Path, monkeypatch):
        """Efferent copy is opt-in so routine tool calls are not mirrored.

        This protects disk-backed observer sinks from receiving pre/post
        records for every MCP tool call unless the operator explicitly asks
        for that behavior.
        """
        cfg = tmp_path / ".mcp.json"
        cfg.write_text(
            json.dumps(
                {"mcpServers": {"peer": {"type": "sse", "url": "http://localhost:9999/sse"}}}
            )
        )
        monkeypatch.delenv(ec.DISABLE_ENV, raising=False)
        monkeypatch.delenv(ec.ENABLE_ENV, raising=False)

        def failing_builder(peer):
            raise AssertionError(f"default-disabled discovery contacted {peer!r}")

        monkeypatch.setattr(ec, "_build_sink_factory", failing_builder)

        sinks = await ec.discover_sinks(cfg)
        assert sinks == []

    async def test_disable_env_short_circuits(self, tmp_path: Path, monkeypatch):
        # EFFERENT_COPY_DISABLE=1 skips registration entirely, regardless
        # of what's in .mcp.json.
        cfg = tmp_path / ".mcp.json"
        cfg.write_text(
            json.dumps(
                {"mcpServers": {"peer": {"type": "sse", "url": "http://localhost:9999/sse"}}}
            )
        )
        monkeypatch.setenv(ec.DISABLE_ENV, "1")
        monkeypatch.setenv(ec.ENABLE_ENV, "1")

        sinks = await ec.discover_sinks(cfg)
        assert sinks == []

    async def test_unreachable_peer_is_skipped_cleanly(self, tmp_path: Path, monkeypatch):
        cfg = tmp_path / ".mcp.json"
        cfg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "unreachable": {
                            "type": "sse",
                            # Unused port; connection should fail fast.
                            "url": "http://127.0.0.1:1/sse",
                        }
                    }
                }
            )
        )
        monkeypatch.delenv(ec.DISABLE_ENV, raising=False)
        monkeypatch.setenv(ec.ENABLE_ENV, "1")

        sinks = await ec.discover_sinks(cfg)
        assert sinks == []

    async def test_unknown_transport_is_skipped(self, tmp_path: Path, monkeypatch):
        cfg = tmp_path / ".mcp.json"
        cfg.write_text(
            json.dumps({"mcpServers": {"weird": {"type": "telepathy", "url": "mindwave://x"}}})
        )
        monkeypatch.delenv(ec.DISABLE_ENV, raising=False)
        monkeypatch.setenv(ec.ENABLE_ENV, "1")

        sinks = await ec.discover_sinks(cfg)
        assert sinks == []

    async def test_self_referential_peer_is_skipped_without_spawning(
        self, tmp_path: Path, monkeypatch
    ):
        """Regression: a peer whose stdio command resolves to our own
        executable MUST NOT be opened — doing so spawns a child
        entrabot-mcp which itself runs discover_sinks, which spawns a
        grandchild, recurring until every level's 5s timeout fires. The
        April 2026 incident: ~30 child entrabot-mcp subprocesses per
        minute for 2h+, each reporting clientInfo.name='mcp' and
        clobbering the leader-host cache, which silently dropped every
        Teams DM push.

        Assertion: discover_sinks MUST return empty AND MUST NOT call
        the factory. "Eventually returns empty after a 5s timeout" is
        insufficient — it still spawns the subprocess.
        """
        import sys

        cfg = tmp_path / ".mcp.json"
        cfg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "self_stdio": {
                            "type": "stdio",
                            "command": sys.argv[0],  # "ourself"
                            "args": [],
                        }
                    }
                }
            )
        )
        monkeypatch.delenv(ec.DISABLE_ENV, raising=False)
        monkeypatch.setenv(ec.ENABLE_ENV, "1")

        # Swap the factory builder so any attempt to open a stdio
        # session is visible as a test failure.
        factory_calls: list[dict] = []

        def failing_builder(peer):
            factory_calls.append(peer)
            raise AssertionError(
                f"self-referential peer MUST be skipped at build-factory "
                f"time, never reaching stdio_client; got peer={peer!r}"
            )

        monkeypatch.setattr(ec, "_build_sink_factory", failing_builder)

        sinks = await ec.discover_sinks(cfg)
        assert sinks == []
        # Zero attempts to build a factory for the self-referential peer.
        # (If _build_sink_factory WERE called, failing_builder would raise
        # and the await would propagate it. Belt: assert the list too.)
        assert factory_calls == [], f"self-referential peer reached factory build: {factory_calls}"

    async def test_wrapper_with_self_ref_marker_is_skipped(self, tmp_path: Path, monkeypatch):
        """A peer whose `command` points at a thin shell wrapper that exec's
        into our running binary MUST be skipped — same root cause as the
        direct self-reference case. The wrapper declares its target via a
        `# entrabot-self-ref-target: <path>` comment so this check works
        without parsing arbitrary shell.

        Background: the debug wrapper at scripts/entrabot-mcp-debug.sh tees
        stderr to a log file and exec's into .venv/bin/entrabot-mcp. Pointing
        .mcp.json at the wrapper bypassed _is_self_referential_peer (the
        wrapper path doesn't match sys.argv[0]), restoring the self-spawn
        cascade that PR #36 originally fixed. Learning #45 has the writeup.
        """
        import sys

        # Stage a fake "running binary" and a wrapper that exec's it.
        fake_target = tmp_path / "entrabot-mcp"
        fake_target.write_text("#!/bin/sh\nexit 0\n")
        fake_target.chmod(0o755)

        wrapper = tmp_path / "wrapper.sh"
        wrapper.write_text(
            f'#!/bin/bash\n# entrabot-self-ref-target: {fake_target}\nexec "{fake_target}"\n'
        )
        wrapper.chmod(0o755)

        # Make the running entry point look like fake_target.
        monkeypatch.setattr(sys, "argv", [str(fake_target)])

        cfg = tmp_path / ".mcp.json"
        cfg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "self_via_wrapper": {
                            "type": "stdio",
                            "command": str(wrapper),
                            "args": [],
                        }
                    }
                }
            )
        )
        monkeypatch.delenv(ec.DISABLE_ENV, raising=False)
        monkeypatch.setenv(ec.ENABLE_ENV, "1")

        # If the peer reaches the factory builder, that's a regression.
        factory_calls: list[dict] = []

        def failing_builder(peer):
            factory_calls.append(peer)
            raise AssertionError(
                f"wrapper-pointing peer MUST be skipped at build-factory "
                f"time, never reaching stdio_client; got peer={peer!r}"
            )

        monkeypatch.setattr(ec, "_build_sink_factory", failing_builder)

        sinks = await ec.discover_sinks(cfg)
        assert sinks == []
        assert factory_calls == [], f"wrapper-pointing peer reached factory build: {factory_calls}"

    def test_is_self_referential_peer_recognizes_wrapper_marker(self, tmp_path: Path, monkeypatch):
        """Unit-level coverage for the wrapper-marker branch."""
        import sys

        fake_target = tmp_path / "entrabot-mcp"
        fake_target.write_text("#!/bin/sh\nexit 0\n")
        fake_target.chmod(0o755)

        # Marker uses a relative path; resolves against the script's dir.
        wrapper = tmp_path / "wrapper.sh"
        wrapper.write_text(
            f'#!/bin/bash\n# entrabot-self-ref-target: ./entrabot-mcp\nexec "{fake_target}"\n'
        )
        wrapper.chmod(0o755)

        monkeypatch.setattr(sys, "argv", [str(fake_target)])

        assert ec._is_self_referential_peer({"type": "stdio", "command": str(wrapper)}) is True

    def test_is_self_referential_peer_ignores_wrapper_without_marker(
        self, tmp_path: Path, monkeypatch
    ):
        """A wrapper without the marker is NOT auto-detected — the marker is
        opt-in. Unmarked wrappers fall through to the existing direct-path
        check, which won't match, and the peer goes through normal discovery.
        This preserves backwards compatibility with arbitrary peer scripts
        whose commands genuinely point elsewhere.
        """
        import sys

        fake_target = tmp_path / "entrabot-mcp"
        fake_target.write_text("#!/bin/sh\nexit 0\n")
        fake_target.chmod(0o755)

        wrapper = tmp_path / "wrapper.sh"
        # No marker line — just a regular wrapper.
        wrapper.write_text(f'#!/bin/bash\nexec "{fake_target}"\n')
        wrapper.chmod(0o755)

        monkeypatch.setattr(sys, "argv", [str(fake_target)])

        assert ec._is_self_referential_peer({"type": "stdio", "command": str(wrapper)}) is False

    async def test_stdio_factory_sets_efferent_copy_disable_in_child_env(self, monkeypatch):
        """Belt-and-suspenders: any subprocess we do spawn via stdio
        MUST inherit ``EFFERENT_COPY_DISABLE=1`` so the child's own
        discover_sinks short-circuits immediately. This bounds the
        worst case at one subprocess, not a cascade.
        """
        captured: list[dict] = []

        from mcp.client import stdio as stdio_mod

        real_params_class = stdio_mod.StdioServerParameters

        def spy_params(*args, **kwargs):
            instance = real_params_class(*args, **kwargs)
            captured.append(dict(instance.env or {}))
            return instance

        monkeypatch.setattr(stdio_mod, "StdioServerParameters", spy_params)

        # Building the factory constructs StdioServerParameters; we
        # don't actually open it (which would spawn a subprocess).
        ec._build_sink_factory(
            {
                "name": "dummy",
                "type": "stdio",
                "command": "/bin/echo",
                "args": [],
            }
        )

        assert captured, "StdioServerParameters was never constructed"
        env = captured[0]
        assert env.get("EFFERENT_COPY_DISABLE") == "1", (
            "Spawned stdio subprocess MUST inherit EFFERENT_COPY_DISABLE=1 "
            f"to prevent cascade recursion; env keys: {sorted(env.keys())[:8]}"
        )


# ---------------------------------------------------------------------------
# install_into_fastmcp — the boot integration
# ---------------------------------------------------------------------------


class TestInstall:
    async def test_installs_wraps_every_tool_except_observe(self):
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("t")

        @mcp.tool()
        async def send(to: str) -> dict:
            return {"sent": to}

        @mcp.tool()
        async def observe(tool_name: str, args: dict, result: dict | None = None) -> dict:
            # Same-process observe handler; MUST NOT be wrapped.
            return {}

        rec = _RecorderSink("r")
        ec.install_into_fastmcp(mcp, [rec.as_sink()])

        # Call via the tool manager (the real dispatch path).
        out = await mcp._tool_manager._tools["send"].fn(to="a@b.c")
        assert out == {"sent": "a@b.c"}

        await asyncio.sleep(0.05)
        assert len(rec.calls) == 2

        # observe itself must remain untouched — calling it doesn't
        # recursively produce more observe calls.
        rec.calls.clear()
        await mcp._tool_manager._tools["observe"].fn(tool_name="x", args={})
        await asyncio.sleep(0.05)
        assert rec.calls == []

    async def test_zero_sinks_install_is_transparent(self):
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("t")

        @mcp.tool()
        async def echo(x: int) -> int:
            return x

        ec.install_into_fastmcp(mcp, [])
        out = await mcp._tool_manager._tools["echo"].fn(x=9)
        assert out == 9


# ---------------------------------------------------------------------------
# End-to-end: .mcp.json → discover_sinks → install_into_fastmcp → dispatch
# ---------------------------------------------------------------------------


class TestEndToEnd:
    async def test_mcpjson_to_dispatch_fakes_wired_with_monkeypatch(
        self, tmp_path: Path, monkeypatch
    ):
        """Wire the full chain: config on disk → discovery → install → dispatch.

        Transport is stubbed at ``_build_sink_factory`` so this test never
        opens a socket or spawns a subprocess. The rest of the chain
        (.mcp.json parsing, discovery schema check, install, middleware
        firing) runs real code against a fake peer that records observe
        calls.
        """
        from mcp.server.fastmcp import FastMCP

        cfg = tmp_path / ".mcp.json"
        cfg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "fakepeer": {
                            "type": "sse",
                            "url": "http://unused/sse",
                        }
                    }
                }
            )
        )
        monkeypatch.delenv(ec.DISABLE_ENV, raising=False)
        monkeypatch.setenv(ec.ENABLE_ENV, "1")

        recorder = _RecorderSink("fakepeer")
        sink = recorder.as_sink()

        # The fake peer's "tools/list" response — just enough shape that
        # _has_compatible_observe returns True.
        class _Tool:
            name = "observe"
            inputSchema = {
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string"},
                    "args": {"type": "object"},
                    "result": {"type": "object"},
                },
            }

        class _ListResult:
            tools = [_Tool()]

        class _StubSession:
            async def list_tools(self_inner):
                return _ListResult()

            async def call_tool(self_inner, name, payload):
                # Delegate to the recorder so test assertions work end-to-end.
                async with sink.factory() as s:
                    return await s.call_tool(name, payload)

        @contextlib.asynccontextmanager
        async def _fake_factory_ctx():
            yield _StubSession()

        def _fake_build(peer):
            assert peer["name"] == "fakepeer"
            return lambda: _fake_factory_ctx()

        monkeypatch.setattr(ec, "_build_sink_factory", _fake_build)

        sinks = await ec.discover_sinks(cfg)
        assert len(sinks) == 1
        assert sinks[0].name == "fakepeer"

        mcp = FastMCP("body")

        @mcp.tool()
        async def send_teams_message(chat_id: str, text: str) -> dict:
            return {"id": f"msg-for-{chat_id}"}

        ec.install_into_fastmcp(mcp, sinks)

        out = await mcp._tool_manager._tools["send_teams_message"].fn(
            chat_id="19:abc", text="hello"
        )
        assert out == {"id": "msg-for-19:abc"}

        # Drain background observe tasks.
        await asyncio.sleep(0.05)

        assert len(recorder.calls) == 2
        pre, post = recorder.calls
        assert pre["payload"] == {
            "tool_name": "send_teams_message",
            "args": {"chat_id": "19:abc", "text": "hello"},
        }
        assert post["payload"]["result"] == {"id": "msg-for-19:abc"}
