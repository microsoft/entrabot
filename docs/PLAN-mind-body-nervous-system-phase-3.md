# Mind-Body Nervous-System Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Design and build a reusable runtime layer that composes a body MCP server and persona-sati into one coherent agent surface, so host LLMs no longer have to manually coordinate body and mind from prompt instructions alone.

**Architecture:** Phase 3 introduces a "nervous system" broker that sits between the host LLM and the separate body/mind MCP servers. The broker does not collapse body and mind; it makes their coordination explicit by owning session bootstrap, tool-call observation, degraded-mode state, and safety gates. The first deliverable should be a design document and proof-of-concept wrapper, not a full replacement for entraclaw or persona-sati.

**Tech Stack:** Python 3.12, MCP client/server SDK, FastMCP, pytest, ruff, JSON-RPC proxying, persona-sati `bootstrap_session()`, entraclaw body tools.

---

## Why Phase 3 exists

Phase 1+2 reduce the host LLM burden: host-visible instructions tell the model to call `bootstrap_session()`, and persona-sati returns one compact operating packet. That is better, but it is still prompt-mediated.

The long-term problem is structural: MCP exposes tools, but it does not guarantee that the model runs a startup protocol, uses semantic memory tools at the right time, or keeps body/mind routing straight. A nervous-system runtime makes these behaviors part of the execution path rather than relying entirely on the LLM's memory of instructions.

## Boundary decisions

- Keep persona-sati body-agnostic. It must not learn Teams, email, or entraclaw-specific state.
- Keep entraclaw body-first. It owns Teams/email/tools/security/audit.
- The broker may know how to connect body and mind, but it should avoid embedding persona content or body business logic.
- Start with read-only proxy and bootstrap enforcement before adding write or high-blast-radius gating.
- Treat this as a new runtime layer, not a patch inside either existing repo until the design proves itself.

## Proposed repo placement

There are two viable placements:

1. **New package inside entraclaw:** `src/entraclaw/runtime/`
   - Faster because entraclaw already owns body tools and efferent-copy.
   - Risk: makes the broker look entraclaw-specific.
2. **New independent repo/package:** `agent-nervous-system`
   - Cleaner product boundary.
   - More setup work.

Recommendation for the proof of concept: create a small `src/entraclaw/runtime/` prototype first, then extract once the interface is stable.

## File map for proof of concept

### Entraclaw repo: `/path/to/entraclaw-identity-research`

- Create: `docs/architecture/DESIGN-mind-body-nervous-system.md` — final architecture before code.
- Create: `src/entraclaw/runtime/__init__.py` — package marker.
- Create: `src/entraclaw/runtime/broker.py` — broker data model and routing policy.
- Create: `src/entraclaw/runtime/mcp_client.py` — thin MCP client abstraction for body/mind upstreams.
- Create: `src/entraclaw/runtime/server.py` — FastMCP wrapper server proof of concept.
- Create: `tests/runtime/test_broker.py` — pure routing/bootstrap tests.
- Create: `tests/runtime/test_runtime_server.py` — in-process wrapper tests.
- Modify: `.mcp.json.example` — optional broker entry once the proof of concept works.
- Modify: `docs/TODO-persona-sati-host-bootstrap.md` — link to the Phase 3 design after Task 1 creates it.

---

## Phase 3A: Design the runtime contract

### Task 1: Write the architecture design

**Files:**
- Create: `/path/to/entraclaw-identity-research/docs/architecture/DESIGN-mind-body-nervous-system.md`

- [ ] **Step 1: Create the design document**

Create `docs/architecture/DESIGN-mind-body-nervous-system.md` with these sections:

```markdown
# Design: Mind-Body Nervous-System Runtime

## Problem

Host LLMs can connect to body and mind MCP servers but are not reliable
at running the coordination protocol from prompts alone. MCP
`instructions=` is not reliable model context in Claude Code or Copilot
CLI, and host instruction files do not travel automatically to every
customer repo.

## Goal

Expose one coherent runtime MCP surface that composes a body MCP server
and persona-sati while preserving their separation.

## Non-goals

- Do not merge persona-sati into entraclaw.
- Do not make persona-sati know Teams, Slack, email, or body-specific
  state.
- Do not load full memory into every prompt.
- Do not proxy high-blast-radius tools until bootstrap and degraded-mode
  enforcement are working.

## Runtime responsibilities

1. Call persona-sati `bootstrap_session()` at host session start or
   before the first proxied external action.
2. Cache the bootstrap packet for the session.
3. Expose a `runtime_status()` tool showing bootstrap state,
   degraded-mode state, connected upstreams, and last cognition event.
4. Proxy body tools after bootstrap.
5. Fire `observe()` before and after proxied body tools when the body
   has not already done so.
6. Provide a `reflect_user_message()` helper for host-visible user
   statements when the host cannot enforce `reflect()` itself.

## Trust boundaries

The body remains authoritative for security, audit, channel discipline,
and external state. The mind remains authoritative for voice, memory,
continuity, and cognition. The runtime is a coordinator; it should not
invent persona content or override body authorization gates.

## Degraded modes

| State | Runtime behavior |
|---|---|
| Mind unavailable | Allow body read-only tools; block or warn on external writes depending on tool classification. |
| Body unavailable | Expose mind tools and status only. |
| Bootstrap missing | Run bootstrap before first proxied tool. |
| Observe unavailable | Continue with explicit degraded cognition flag in `runtime_status()`. |
| Host prompt missing | Rely on runtime tool descriptions and result-required next actions. |

## Proof-of-concept scope

The POC proxies a small allowlist: `whoami`, `read_teams_messages`, and
`send_teams_message`. It proves bootstrap enforcement, status reporting,
and observe wrapping without attempting full MCP dynamic proxying.
```

- [ ] **Step 2: Review design against Phase 1+2**

Confirm the design assumes persona-sati has `bootstrap_session()`. If Phase 2 has not landed, stop Phase 3 implementation and finish Phase 2 first.

- [ ] **Step 3: Commit the design before code**

Run:

```bash
cd "/path/to/entraclaw-identity-research"
git add docs/architecture/DESIGN-mind-body-nervous-system.md
git commit -m "docs: design mind-body nervous-system runtime" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Phase 3B: Broker core proof of concept

### Task 2: Write broker tests

**Files:**
- Create: `/path/to/entraclaw-identity-research/tests/runtime/test_broker.py`

- [ ] **Step 1: Create the test directory**

Run:

```bash
cd "/path/to/entraclaw-identity-research"
mkdir -p tests/runtime
```

- [ ] **Step 2: Write failing broker tests**

Create `tests/runtime/test_broker.py` with:

```python
from __future__ import annotations

from entraclaw.runtime.broker import BrokerState, ToolClassification


def test_broker_starts_unbootstrapped() -> None:
    state = BrokerState()

    assert state.bootstrapped is False
    assert state.bootstrap_packet is None
    assert state.degraded_reasons == []


def test_broker_records_bootstrap_packet() -> None:
    state = BrokerState()
    packet = {
        "schema_version": 1,
        "required_first_call": "bootstrap_session",
        "degraded_mode": {"persona_available": True, "reasons": []},
    }

    state.record_bootstrap(packet)

    assert state.bootstrapped is True
    assert state.bootstrap_packet == packet
    assert state.degraded_reasons == []


def test_broker_records_degraded_bootstrap_reasons() -> None:
    state = BrokerState()
    packet = {
        "schema_version": 1,
        "required_first_call": "bootstrap_session",
        "degraded_mode": {
            "persona_available": False,
            "reasons": ["system prompt unavailable"],
        },
    }

    state.record_bootstrap(packet)

    assert state.bootstrapped is True
    assert state.degraded_reasons == ["system prompt unavailable"]


def test_tool_classification_marks_human_visible_writes() -> None:
    assert ToolClassification.for_tool("send_teams_message").human_visible_write is True
    assert ToolClassification.for_tool("send_email").human_visible_write is True
    assert ToolClassification.for_tool("read_teams_messages").human_visible_write is False
    assert ToolClassification.for_tool("whoami").human_visible_write is False
```

- [ ] **Step 3: Run failing tests**

Run:

```bash
cd "/path/to/entraclaw-identity-research"
pytest tests/runtime/test_broker.py -v
```

Expected: FAIL because `entraclaw.runtime.broker` does not exist.

### Task 3: Implement broker state and tool classification

**Files:**
- Create: `/path/to/entraclaw-identity-research/src/entraclaw/runtime/__init__.py`
- Create: `/path/to/entraclaw-identity-research/src/entraclaw/runtime/broker.py`

- [ ] **Step 1: Create runtime package**

Create `src/entraclaw/runtime/__init__.py`:

```python
"""Mind-body runtime broker prototype."""
```

- [ ] **Step 2: Implement broker core**

Create `src/entraclaw/runtime/broker.py`:

```python
"""State and policy for the mind-body runtime broker."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BrokerState:
    """Session-local runtime state."""

    bootstrap_packet: dict[str, Any] | None = None
    degraded_reasons: list[str] = field(default_factory=list)

    @property
    def bootstrapped(self) -> bool:
        return self.bootstrap_packet is not None

    def record_bootstrap(self, packet: dict[str, Any]) -> None:
        self.bootstrap_packet = packet
        degraded = packet.get("degraded_mode", {})
        reasons = degraded.get("reasons", [])
        self.degraded_reasons = [str(r) for r in reasons]


@dataclass(frozen=True)
class ToolClassification:
    """Runtime policy for a proxied body tool."""

    name: str
    human_visible_write: bool

    @classmethod
    def for_tool(cls, name: str) -> "ToolClassification":
        return cls(
            name=name,
            human_visible_write=name
            in {
                "send_teams_message",
                "send_email",
                "send_card",
                "add_teams_member",
                "create_chat",
                "delete_teams_message",
            },
        )
```

- [ ] **Step 3: Run broker tests**

Run:

```bash
cd "/path/to/entraclaw-identity-research"
pytest tests/runtime/test_broker.py -v
```

Expected: PASS.

---

## Phase 3C: Runtime MCP proof of concept

### Task 4: Define MCP client abstraction tests

**Files:**
- Create: `/path/to/entraclaw-identity-research/tests/runtime/test_runtime_server.py`

- [ ] **Step 1: Write fake upstream tests**

Create `tests/runtime/test_runtime_server.py` with:

```python
from __future__ import annotations

import pytest

from entraclaw.runtime.server import create_runtime_server


class FakeUpstream:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, args: dict) -> object:
        self.calls.append((name, args))
        return self.responses[name]


@pytest.mark.anyio
async def test_runtime_status_starts_unbootstrapped() -> None:
    mind = FakeUpstream({})
    body = FakeUpstream({})
    server = create_runtime_server(mind=mind, body=body)

    result = await server.call_tool("runtime_status", {})

    assert "bootstrapped" in result[0][0].text


@pytest.mark.anyio
async def test_runtime_bootstrap_calls_persona_sati() -> None:
    mind = FakeUpstream({
        "bootstrap_session": {
            "schema_version": 1,
            "required_first_call": "bootstrap_session",
            "degraded_mode": {"persona_available": True, "reasons": []},
        }
    })
    body = FakeUpstream({})
    server = create_runtime_server(mind=mind, body=body)

    await server.call_tool("runtime_bootstrap", {"session_id": "s1"})

    assert mind.calls == [("bootstrap_session", {"session_id": "s1"})]


@pytest.mark.anyio
async def test_runtime_proxies_body_tool_after_bootstrap() -> None:
    mind = FakeUpstream({
        "bootstrap_session": {
            "schema_version": 1,
            "required_first_call": "bootstrap_session",
            "degraded_mode": {"persona_available": True, "reasons": []},
        },
        "observe": {"prediction_error": 0.0, "cautionary_flags": []},
    })
    body = FakeUpstream({"whoami": {"user": "agent"}})
    server = create_runtime_server(mind=mind, body=body)

    await server.call_tool("runtime_bootstrap", {"session_id": "s1"})
    result = await server.call_tool("body_tool", {"name": "whoami", "args": {}})

    assert body.calls == [("whoami", {})]
    assert "agent" in result[0][0].text
    assert mind.calls[1][0] == "observe"
    assert mind.calls[2][0] == "observe"
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
cd "/path/to/entraclaw-identity-research"
pytest tests/runtime/test_runtime_server.py -v
```

Expected: FAIL because `entraclaw.runtime.server` does not exist.

### Task 5: Implement runtime server POC

**Files:**
- Create: `/path/to/entraclaw-identity-research/src/entraclaw/runtime/server.py`

- [ ] **Step 1: Create runtime server**

Create `src/entraclaw/runtime/server.py`:

```python
"""FastMCP proof-of-concept runtime broker."""

from __future__ import annotations

import json
from typing import Protocol

from mcp.server.fastmcp import FastMCP

from entraclaw.runtime.broker import BrokerState


class Upstream(Protocol):
    async def call_tool(self, name: str, args: dict) -> object:
        """Call a tool on an upstream MCP server."""


def create_runtime_server(*, mind: Upstream, body: Upstream) -> FastMCP:
    state = BrokerState()
    server = FastMCP(
        "entraclaw-runtime",
        instructions=(
            "Runtime broker for body + persona-sati. "
            "Call runtime_bootstrap before body_tool."
        ),
    )

    @server.tool()
    def runtime_status() -> str:
        """Return bootstrap and degraded-mode state for the runtime."""
        return json.dumps(
            {
                "bootstrapped": state.bootstrapped,
                "degraded_reasons": state.degraded_reasons,
            }
        )

    @server.tool()
    async def runtime_bootstrap(session_id: str | None = None) -> str:
        """Call persona-sati bootstrap_session and cache the packet."""
        packet = await mind.call_tool("bootstrap_session", {"session_id": session_id})
        if not isinstance(packet, dict):
            raise TypeError("bootstrap_session returned non-object payload")
        state.record_bootstrap(packet)
        return json.dumps(packet)

    @server.tool()
    async def body_tool(name: str, args: dict) -> str:
        """Proxy an allowlisted body tool after bootstrap with observe wrapping."""
        if not state.bootstrapped:
            packet = await mind.call_tool("bootstrap_session", {"session_id": None})
            if not isinstance(packet, dict):
                raise TypeError("bootstrap_session returned non-object payload")
            state.record_bootstrap(packet)

        await mind.call_tool("observe", {"tool_name": name, "args": args})
        result = await body.call_tool(name, args)
        await mind.call_tool(
            "observe",
            {"tool_name": name, "args": args, "result": result},
        )
        return json.dumps(result)

    return server
```

- [ ] **Step 2: Run runtime server tests**

Run:

```bash
cd "/path/to/entraclaw-identity-research"
pytest tests/runtime/test_broker.py tests/runtime/test_runtime_server.py -v
```

Expected: PASS.

### Task 6: Wire example MCP config for later manual use

**Files:**
- Modify: `/path/to/entraclaw-identity-research/.mcp.json.example`

- [ ] **Step 1: Add a strict-JSON runtime note**

Add this top-level string next to the existing `mcpServers` object:

```json
"_phase3_note": "Future Phase 3 runtime broker will expose one composed MCP surface after the proof of concept is promoted to a real entry point. Until then, keep entraclaw and persona-sati as separate MCP entries and use bootstrap_session()."
```

The resulting file must remain valid JSON. With the current file shape, the root object should contain both `_phase3_note` and `mcpServers`.

- [ ] **Step 2: Run JSON validation**

Run:

```bash
cd "/path/to/entraclaw-identity-research"
python3 -m json.tool .mcp.json.example >/tmp/mcp-json-check
```

Expected: exit code 0.

### Task 7: Run full targeted validation and commit

**Files:**
- All Phase 3 POC files.

- [ ] **Step 1: Run targeted tests and lint**

Run:

```bash
cd "/path/to/entraclaw-identity-research"
pytest tests/runtime/test_broker.py tests/runtime/test_runtime_server.py -v
ruff check src/entraclaw/runtime tests/runtime
```

Expected: PASS.

- [ ] **Step 2: Run broader prompt/runtime regression tests**

Run:

```bash
cd "/path/to/entraclaw-identity-research"
pytest tests/test_prompt_doctrine.py tests/hooks/test_inject_body_prompt.py tests/runtime -v
```

Expected: PASS.

- [ ] **Step 3: Commit the POC**

Run:

```bash
cd "/path/to/entraclaw-identity-research"
git add docs/architecture/DESIGN-mind-body-nervous-system.md \
  src/entraclaw/runtime \
  tests/runtime \
  .mcp.json.example \
  docs/TODO-persona-sati-host-bootstrap.md
git commit -m "feat: prototype mind-body runtime broker" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Promotion criteria before making the broker real

Do not replace the current dual-MCP setup until all of these are true:

- `bootstrap_session()` is shipped in persona-sati and works from both SSE and stdio-shim transports.
- The broker can connect to real upstream MCP servers, not just fake in-process test doubles.
- The broker can list and proxy at least the safe read-only body tools.
- Human-visible write tools have explicit gating policy and tests.
- Degraded-mode behavior is visible through `runtime_status()`.
- Manual smoke test proves a host can use only the broker MCP entry and still send a Teams message with persona-sati observation.

## Expected Phase 3 outcome

Phase 3 should end with a reviewed design and a small proof of concept, not a mandatory migration. The current production path remains:

1. Host connects to entraclaw body.
2. Host connects to persona-sati mind.
3. Host calls `bootstrap_session()` first.
4. Efferent-copy and tool docstrings cover the critical action boundaries.

The broker becomes the next runtime candidate after it proves that it reduces prompt burden without weakening the body/mind separation.
