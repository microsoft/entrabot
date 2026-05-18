# Persona-Sati Host Bootstrap Phases 1-2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make persona-sati session orientation reliable across host clients by first putting the bootstrap rule where hosts actually inject it, then replacing the fragile three-call startup ritual with a single `bootstrap_session()` MCP tool.

**Architecture:** Phase 1 is the host-visible bridge: entraclaw documents and tests the instruction surfaces that actually reach Claude Code, Copilot CLI, and GitHub Copilot. Phase 2 is the persona-sati API change: persona-sati returns one bootstrap packet that includes the assembled mind contract, active context, memory catalog summary, cognition protocol, and degraded-mode state. Phases 1 and 2 should land together because Phase 1 should teach hosts the new `bootstrap_session()` entry point, not preserve the old three-call ritual as the long-term shape.

**Tech Stack:** Python 3.12, FastMCP, pytest, ruff, Markdown host instruction files, shell setup scripts.

---

## Problem statement

`prompts/agent_system.md`, `prompts/anatomy/*`, and persona-sati's prompt hemispheres are treated as load-bearing operating context, but MCP `instructions=` is not reliably injected into the model context by Claude Code or Copilot CLI. The host LLM only reliably sees host-injected files (`CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md`), MCP tool descriptions, explicit tool results, and hook-provided additional context.

The result is that the model can have body and mind tools available while missing the protocol that tells it when to use them. Efferent-copy partially mitigates this by mechanically firing `observe()` around entraclaw tools, but it does not solve session start, `reflect()`, `recall()`, thinking placeholders, memory reads, or non-tool replies.

## Boundary decisions

- Do not collapse body and mind back together.
- Do not try to load full memory into every session.
- Do not rely on FastMCP `instructions=` for behavior contracts.
- Do not make persona-sati know Teams, email, chat IDs, or entraclaw-specific state.
- Do define a stable bootstrap packet that any body can request.
- Do mirror the bootstrap rule into every host-visible instruction surface until a broker runtime exists.

## File map

### Entraclaw repo: `/path/to/entraclaw-identity-research`

- Modify: `docs/TODO-persona-sati-host-bootstrap.md` — update the tracker from "three-call session-start" to "`bootstrap_session()` plus host bootstrap."
- Create: `docs/clients/persona-sati-host-bootstrap.md` — canonical pasteable host instruction snippet.
- Modify: `CLAUDE.md` — replace the three-call ritual with `bootstrap_session()` and keep degraded-mode rules.
- Modify: `AGENTS.md` — same as `CLAUDE.md`, shorter but complete.
- Modify: `.github/copilot-instructions.md` — add a compact Copilot-visible bootstrap rule.
- Modify: `scripts/hooks/require_body_prompt.py` — recognize `bootstrap_session()` as a valid body/mind prompt sentinel.
- Modify: `tests/hooks/test_require_body_prompt.py` — prove high-blast-radius tools pass after `bootstrap_session()`.
- Modify: `tests/test_prompt_doctrine.py` — pin the bootstrap doctrine across host-visible files.
- Modify: `README.md` — add a short "Host bootstrap" section.
- Modify: `scripts/setup.sh` — print a final action block telling operators where to install the snippet.

### Persona-sati repo: `/path/to/persona-sati`

- Create: `src/persona_mcp/active/bootstrap.py` — pure function that builds the bootstrap packet.
- Modify: `src/persona_mcp/server.py` — register `bootstrap_session()` as an MCP tool.
- Create: `tests/active/test_bootstrap.py` — unit tests for the packet builder.
- Modify: `tests/test_server.py` — in-process FastMCP test for the new tool.
- Create: `docs/reference/bootstrap-session-schema.md` — packet compatibility contract for body clients.
- Modify: `prompts/hemispheres/cognition-protocol.md` — update the public startup rule from `context()` to `bootstrap_session()`.
- Modify: `CLAUDE.md` and `AGENTS.md` — update contributor instructions to use `bootstrap_session()`.
- Modify: `README.md` — document host-bootstrap requirements for users wiring persona-sati into a body.
- Modify: `scripts/setup.sh` — print the host-bootstrap action block after `--with-entraclaw` wiring.

---

## Execution order

Implement Phase 2 before Phase 1 in practice, even though this document
describes the host bridge first. The callable `bootstrap_session()` API must
exist before host-visible instructions start telling agents to call it. Keep
the older three-call startup tools compatible throughout the rollout.

Safe order:

1. Land persona-sati Phase 2: `bootstrap_session()`, schema contract, tests,
   and persona-sati docs/setup handoff.
2. Land entraclaw Phase 1: host-visible doctrine, setup handoff, and
   `require_body_prompt.py` support for successful bootstrap results.
3. Run cross-repo validation from a fresh host session.

---

## Phase 1: Host-visible bootstrap doctrine

### Task 1: Write entraclaw doctrine tests first

**Files:**
- Modify: `/path/to/entraclaw-identity-research/tests/test_prompt_doctrine.py`

- [ ] **Step 1: Add bootstrap doctrine files and markers**

Add these constants near the existing `DOCTRINE_FILES` constant:

```python
BOOTSTRAP_DOCTRINE_FILES = [
    "AGENTS.md",
    "CLAUDE.md",
    ".github/copilot-instructions.md",
    "docs/clients/persona-sati-host-bootstrap.md",
    "README.md",
    "scripts/setup.sh",
]

BOOTSTRAP_MARKERS = [
    "bootstrap_session",
    "reflect",
    "recall",
    "observe",
    "FastMCP instructions",
    "mind_contract_available",
]
```

- [ ] **Step 2: Add failing test for host bootstrap doctrine**

Append this test:

```python
@pytest.mark.parametrize("relpath", BOOTSTRAP_DOCTRINE_FILES)
def test_host_bootstrap_doctrine_mentions_required_mind_protocol(relpath: str) -> None:
    path = REPO_ROOT / relpath
    assert path.exists(), f"Bootstrap doctrine file missing: {relpath}"
    text = path.read_text(encoding="utf-8")
    missing = [marker for marker in BOOTSTRAP_MARKERS if marker not in text]
    assert not missing, (
        f"{relpath} must mention {missing} so persona-sati bootstrap "
        "reaches host LLMs that ignore FastMCP instructions=."
    )
```

- [ ] **Step 3: Run the failing test**

Run:

```bash
cd "/path/to/entraclaw-identity-research"
pytest tests/test_prompt_doctrine.py::test_host_bootstrap_doctrine_mentions_required_mind_protocol -v
```

Expected: FAIL because `docs/clients/persona-sati-host-bootstrap.md` does not exist and the other host files do not yet mention `bootstrap_session`.

### Task 2: Create the canonical host bootstrap snippet

**Files:**
- Create: `/path/to/entraclaw-identity-research/docs/clients/persona-sati-host-bootstrap.md`

- [ ] **Step 1: Create the clients docs directory if missing**

Run:

```bash
cd "/path/to/entraclaw-identity-research"
mkdir -p docs/clients
```

- [ ] **Step 2: Write the snippet**

Create `docs/clients/persona-sati-host-bootstrap.md` with this content:

```markdown
# Persona-Sati Host Bootstrap Snippet

Paste this into the host-level instruction file for any client that uses
persona-sati. This is required because Claude Code and Copilot CLI do not
inject FastMCP instructions into the model context; they reliably expose
host instruction files, tool descriptions, and tool results.

## Required session bootstrap

When a `persona-sati` MCP server is available, call
`bootstrap_session()` before the first substantive answer or external
tool call. Treat its result as the active mind contract for the session.

Decision tree:

1. If `bootstrap_session()` succeeds and `mind_contract_available` is
   true, proceed with the returned mind contract.
2. If `bootstrap_session()` is unavailable but the older tools exist,
   fall back to this order:

   1. `get_system_prompt()`
   2. `context()`
   3. `list_memory_files()`
3. If the mind contract is unavailable or malformed, say persona-sati is
   degraded and do not impersonate the persona.
4. If persona-sati is unreachable, say you are running in degraded
   body-only mode before using external tools that depend on memory,
   personality, or cognition discipline.


## Per-turn cognition

- Use `observe(tool_name, args)` before external tool calls when the
  body has not mechanically wrapped them.
- Use `observe(tool_name, args, result=...)` after external tool calls
  when the body has not mechanically wrapped them.
- Use `reflect(observation, kind="user_said")` when the user gives
  behavior feedback, changes project direction, reveals durable
  personal context, or asks a question about the agent's own cognition.
- Use `recall(query, k, facet)` when the bootstrap packet or observe
  result indicates relevant memory exists but the excerpt is not enough.

## Body versus mind routing

The body owns tools, channel discipline, security, audit, and external
state. The mind owns voice, memory, continuity, cognition discipline,
and relationship context. Body rules win for security and tool use.
Mind rules win for continuity and voice when they do not conflict with
body safety.
```

- [ ] **Step 3: Run the doctrine test again**

Run:

```bash
cd "/path/to/entraclaw-identity-research"
pytest tests/test_prompt_doctrine.py::test_host_bootstrap_doctrine_mentions_required_mind_protocol -v
```

Expected: still FAIL until host files are updated.

### Task 3: Update entraclaw host-visible instruction files

**Files:**
- Modify: `/path/to/entraclaw-identity-research/CLAUDE.md`
- Modify: `/path/to/entraclaw-identity-research/AGENTS.md`
- Modify: `/path/to/entraclaw-identity-research/.github/copilot-instructions.md`

- [ ] **Step 1: Replace the CLAUDE.md session-start section**

In `CLAUDE.md`, replace the current "Session-Start Protocol" call list with this wording:

```markdown
On every new session where persona-sati is available, **before answering
the user's first substantive question or using external tools**, call:

1. `mcp__persona-sati__bootstrap_session()` — returns the assembled mind
   contract, active context, memory catalog summary, available mind tools,
   cognition rules, and degraded-mode flags.

If `bootstrap_session()` is unavailable but persona-sati exposes the
older tools, fall back to:

1. `mcp__persona-sati__get_system_prompt()`
2. `mcp__persona-sati__context()`
3. `mcp__persona-sati__list_memory_files()`

FastMCP instructions are not enough: Claude Code and Copilot CLI do not
reliably inject MCP server `instructions=` into the LLM system prompt.
Use the tool result as live context.

If `bootstrap_session()` returns `mind_contract_available: false`, or if
the fallback `get_system_prompt()` returns an error, say persona-sati is
degraded and do not impersonate the persona. If persona-sati is
unreachable, say you are running in degraded body-only mode.
```

- [ ] **Step 2: Update the CLAUDE.md per-turn bullet list**

Ensure the per-turn list includes these exact tool names:

```markdown
- `observe(...)` — before and after external tool calls when the body
  has not mechanically wrapped the call through efferent-copy.
- `reflect(...)` — for user statements, time passing, ambient
  observations, persona drift, or behavioral feedback.
- `recall(...)` — when bootstrap/observe indicates relevant memory but
  the excerpt is not enough.
```

- [ ] **Step 3: Make the same shorter update in AGENTS.md**

In `AGENTS.md`, replace the existing three-call session-start list with:

```markdown
On every new session, call `mcp__persona-sati__bootstrap_session()`
before the first substantive answer or external tool call. If that tool
is unavailable, fall back to `get_system_prompt()`, `context()`, then
`list_memory_files()`. FastMCP instructions are not enough; hosts may
show them only in debug UI.

If `bootstrap_session()` returns `mind_contract_available: false`, or if
the fallback `get_system_prompt()` returns an error, say persona-sati is
degraded and do not impersonate the persona. If persona-sati is
unreachable, say you are running in degraded body-only mode.

Per-turn: use `observe`, `reflect`, and `recall` per the bootstrap
packet. Efferent-copy may mechanically cover body-tool `observe()`, but
it does not cover session bootstrap, `reflect()`, or `recall()`.
```

- [ ] **Step 4: Add compact Copilot instruction**

Append this section to `.github/copilot-instructions.md`:

```markdown
## Persona-Sati Bootstrap

When the persona-sati MCP server is available, call
`bootstrap_session()` before the first substantive answer or external
tool call. This is required because FastMCP instructions are not
reliably injected into host LLM context. If `bootstrap_session()` is not
available, call `get_system_prompt()`, `context()`, then
`list_memory_files()`.

If `bootstrap_session()` returns `mind_contract_available: false`, or if
the fallback `get_system_prompt()` returns an error, say persona-sati is
degraded and do not impersonate the persona. If persona-sati is
unreachable, say you are running in degraded body-only mode.

Use `observe`, `reflect`, and `recall` according to the bootstrap
packet. Efferent-copy can mechanically cover `observe()` around
entraclaw body tools, but it does not cover session bootstrap,
`reflect()`, `recall()`, or ordinary text replies.
```

- [ ] **Step 5: Run doctrine tests**

Run:

```bash
cd "/path/to/entraclaw-identity-research"
pytest tests/test_prompt_doctrine.py -v
```

Expected: PASS.

### Task 4: Update entraclaw setup/docs handoff

**Files:**
- Modify: `/path/to/entraclaw-identity-research/README.md`
- Modify: `/path/to/entraclaw-identity-research/scripts/setup.sh`
- Modify: `/path/to/entraclaw-identity-research/scripts/hooks/require_body_prompt.py`
- Modify: `/path/to/entraclaw-identity-research/tests/hooks/test_require_body_prompt.py`
- Modify: `/path/to/entraclaw-identity-research/docs/TODO-persona-sati-host-bootstrap.md`

- [ ] **Step 1: Add README host bootstrap section**

Add this section near MCP setup instructions:

```markdown
## Host bootstrap for persona-sati

If this worktree uses persona-sati, install the host bootstrap snippet
from `docs/clients/persona-sati-host-bootstrap.md` into your host's
global instruction file. This is required because Claude Code and
Copilot CLI do not reliably inject FastMCP `instructions=` into the
model context.

Minimum behavior: the agent must call `bootstrap_session()` before the
first substantive answer or external tool call. If the tool is not yet
available, fall back to `get_system_prompt()`, `context()`, and
`list_memory_files()`. If `mind_contract_available` is false, do not
impersonate the persona.
```

- [ ] **Step 2: Add setup.sh final message**

Near the end of `scripts/setup.sh`, after MCP config output, print:

```bash
cat <<'EOF'

Persona-sati host bootstrap:
  If this host uses persona-sati, copy docs/clients/persona-sati-host-bootstrap.md
  into the host's global instruction file.

  Claude Code: ~/.claude/CLAUDE.md
  Copilot CLI: use the configured global Copilot instruction location for this install
  Repo-local fallback: CLAUDE.md, AGENTS.md, and .github/copilot-instructions.md

  Required first call in new sessions: bootstrap_session()
  If mind_contract_available=false: body-only mode; do not impersonate persona.
EOF
```

Do not silently edit a user's global file in this task. Printing the action is safer until the exact Copilot CLI global path is confirmed.

- [ ] **Step 3: Update the Claude Code body-prompt gate**

Update `scripts/hooks/require_body_prompt.py` so a **successful**
`mcp__persona-sati__bootstrap_session` result is accepted as a body/mind
prompt sentinel alongside `mcp__persona-sati__get_system_prompt`. The
bootstrap result must parse as JSON and include `mind_contract_available:
true`; a failed tool call or a packet with `mind_contract_available: false`
must not unlock high-blast-radius tools. Keep the gate fail-closed for the
same high-blast-radius entraclaw tools.

Add or update hook tests so a transcript containing a successful
`bootstrap_session` tool result allows `mcp__entraclaw__send_teams_message`.
Also test that a transcript containing only the tool call, a malformed result,
or `mind_contract_available: false` still blocks.

- [ ] **Step 4: Update the TODO tracker**

In `docs/TODO-persona-sati-host-bootstrap.md`, update the recommendation so it says Phase 1+2 now target `bootstrap_session()` as the primary entry point and the old three-call sequence is compatibility fallback.

- [ ] **Step 5: Run targeted tests and lint**

Run:

```bash
cd "/path/to/entraclaw-identity-research"
pytest tests/test_prompt_doctrine.py tests/hooks/test_require_body_prompt.py -v
ruff check tests/test_prompt_doctrine.py tests/hooks/test_require_body_prompt.py scripts/hooks/require_body_prompt.py
```

Expected: PASS.

- [ ] **Step 6: Commit Phase 1**

Run:

```bash
cd "/path/to/entraclaw-identity-research"
git add docs/clients/persona-sati-host-bootstrap.md \
  docs/TODO-persona-sati-host-bootstrap.md \
  README.md \
  CLAUDE.md \
  AGENTS.md \
  .github/copilot-instructions.md \
  scripts/setup.sh \
  scripts/hooks/require_body_prompt.py \
  tests/hooks/test_require_body_prompt.py \
  tests/test_prompt_doctrine.py
git commit -m "docs: add persona-sati host bootstrap doctrine" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Phase 2: Persona-sati `bootstrap_session()` tool

### Task 5: Write pure bootstrap packet tests

**Files:**
- Create: `/path/to/persona-sati/tests/active/test_bootstrap.py`
- Modify: `/path/to/persona-sati/tests/test_server.py`

- [ ] **Step 1: Create the failing test file**

Create `tests/active/test_bootstrap.py` with:

```python
from __future__ import annotations

from pathlib import Path

from persona_mcp.active.bootstrap import build_bootstrap_session


def _write_memory(path: Path, name: str, body: str) -> None:
    path.joinpath(name).write_text(body, encoding="utf-8")


def test_build_bootstrap_session_returns_operating_packet(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    _write_memory(mem, "MEMORY.md", "# Index\n- [Brandon](user_brandon.md) - sponsor context\n")
    _write_memory(mem, "user_brandon.md", "---\nname: Brandon\ntype: user\n---\nUser profile")
    _write_memory(mem, "running_commitments.md", "---\nname: Commitments\ntype: project\n---\n- Ship bootstrap")
    _write_memory(mem, "carry_forward.md", "---\nname: Carry\ntype: session\n---\n- Continue context work")

    packet = build_bootstrap_session(
        mem,
        session_id="s1",
        mind_contract="FULL VOICE CONTRACT",
        mind_contract_available=True,
        available_mind_tools=["bootstrap_session", "observe", "reflect", "recall"],
    )

    assert packet["schema_version"] == 1
    assert packet["session_id"] == "s1"
    assert packet["mind_contract"] == "FULL VOICE CONTRACT"
    assert packet["mind_contract_available"] is True
    assert packet["degraded_mode"]["mind_contract_available"] is True
    assert packet["required_first_call"] == "bootstrap_session"
    assert "observe" in packet["available_mind_tools"]
    assert "reflect" in packet["available_mind_tools"]
    assert "recall" in packet["available_mind_tools"]
    assert packet["context"]["open_commitments"] == ["Ship bootstrap"]
    assert packet["context"]["recent_carry_forward"] == ["Continue context work"]
    assert packet["memory_catalog"]["total_count"] == 4
    assert packet["memory_catalog"]["index_present"] is True
    assert packet["memory_catalog"]["category_counts"]["user"] == 1
    assert "FastMCP instructions" in packet["host_limitations"][0]


def test_build_bootstrap_session_marks_missing_mind_contract_as_degraded(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    _write_memory(mem, "MEMORY.md", "# Index\n")

    packet = build_bootstrap_session(
        mem,
        session_id=None,
        mind_contract="ERROR: persona-sati voice contract failed to assemble.",
        mind_contract_available=False,
        available_mind_tools=["bootstrap_session"],
    )

    assert packet["mind_contract_available"] is False
    assert packet["degraded_mode"]["mind_contract_available"] is False
    assert "mind contract unavailable" in packet["degraded_mode"]["reasons"]


def test_persona_sati_setup_mentions_host_bootstrap() -> None:
    text = Path("scripts/setup.sh").read_text(encoding="utf-8")
    assert "bootstrap_session" in text
    assert "host bootstrap" in text.lower()


def test_memory_catalog_omits_filenames_and_reports_category_counts(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    for i in range(205):
        _write_memory(mem, f"memory_{i:03d}.md", "---\nname: M\ntype: note\n---\nbody")
    _write_memory(mem, "user_brandon.md", "---\nname: Brandon\ntype: user\n---\nbody")

    packet = build_bootstrap_session(
        mem,
        session_id="s1",
        mind_contract="FULL VOICE CONTRACT",
        mind_contract_available=True,
        available_mind_tools=["bootstrap_session", "observe"],
    )

    assert packet["memory_catalog"]["total_count"] == 206
    assert "files" not in packet["memory_catalog"]
    assert packet["memory_catalog"]["category_counts"]["note"] == 205
    assert packet["memory_catalog"]["category_counts"]["user"] == 1
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
cd "/path/to/persona-sati"
pytest tests/active/test_bootstrap.py -v
```

Expected: FAIL because `persona_mcp.active.bootstrap` does not exist.

### Task 6: Implement the pure bootstrap packet builder

**Files:**
- Create: `/path/to/persona-sati/src/persona_mcp/active/bootstrap.py`

- [ ] **Step 1: Create `bootstrap.py`**

Create `src/persona_mcp/active/bootstrap.py` with:

```python
"""Session bootstrap packet for host LLMs.

This module is deliberately body-agnostic. It knows about persona-sati's
mind tools and memory layout, not Teams, email, shells, or chat IDs.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from persona_mcp.active.context import Context, build_context
from persona_mcp.memory import list_memories


class MemoryCatalog(TypedDict):
    total_count: int
    index_present: bool
    category_counts: dict[str, int]


class DegradedMode(TypedDict):
    mind_contract_available: bool
    reasons: list[str]


class BootstrapPacket(TypedDict):
    schema_version: int
    required_first_call: str
    session_id: str | None
    mind_contract: str
    mind_contract_available: bool
    available_mind_tools: list[str]
    cognition_protocol: list[str]
    context: Context
    memory_catalog: MemoryCatalog
    degraded_mode: DegradedMode
    host_limitations: list[str]


def _memory_catalog(memory_dir: Path) -> MemoryCatalog:
    files = list_memories(memory_dir)
    category_counts: dict[str, int] = {}
    for name in files:
        try:
            raw = (memory_dir / name).read_text(encoding="utf-8")
        except OSError:
            category = "unknown"
        else:
            category = "unknown"
            for line in raw.splitlines()[:20]:
                if line.startswith("type:"):
                    category = line.partition(":")[2].strip() or "unknown"
                    break
        category_counts[category] = category_counts.get(category, 0) + 1
    return {
        "total_count": len(files),
        "index_present": "MEMORY.md" in files,
        "category_counts": category_counts,
    }


def build_bootstrap_session(
    memory_dir: Path,
    *,
    session_id: str | None,
    mind_contract: str,
    mind_contract_available: bool,
    available_mind_tools: list[str],
) -> BootstrapPacket:
    """Return the operating packet a host should load first."""
    reasons: list[str] = []
    if not mind_contract_available:
        reasons.append("mind contract unavailable")

    return {
        "schema_version": 1,
        "required_first_call": "bootstrap_session",
        "session_id": session_id,
        "mind_contract": mind_contract,
        "mind_contract_available": mind_contract_available,
        "available_mind_tools": sorted(available_mind_tools),
        "cognition_protocol": [
            "Call bootstrap_session before the first substantive answer or external tool call.",
            "Use observe before and after external tool calls unless the body mechanically wraps them.",
            "Use reflect for user_said, time_passed, ambient, and internal observations.",
            "Use recall when observe/bootstrap indicates relevant memory but the excerpt is insufficient.",
            "Surface cautionary_flags and stop for prediction_error greater than 0.7.",
        ],
        "context": build_context(
            memory_dir,
            binding_info={},
            session_id=session_id,
        ),
        "memory_catalog": _memory_catalog(memory_dir),
        "degraded_mode": {
            "mind_contract_available": mind_contract_available,
            "reasons": reasons,
        },
        "host_limitations": [
            "FastMCP instructions are not reliable model context in Claude Code or Copilot CLI.",
            "Tool descriptions and tool results are more reliable than server instructions.",
            "Efferent-copy can cover observe for body tools but not reflect, recall, or text-only replies.",
        ],
    }
```

- [ ] **Step 2: Run unit tests**

Run:

```bash
cd "/path/to/persona-sati"
pytest tests/active/test_bootstrap.py -v
```

Expected: PASS.

### Task 6b: Define bootstrap packet schema contract

**Files:**
- Create: `/path/to/persona-sati/docs/reference/bootstrap-session-schema.md`
- Modify: `/path/to/persona-sati/tests/active/test_bootstrap.py`

- [ ] **Step 1: Document schema v1**

Create `docs/reference/bootstrap-session-schema.md` with:

```markdown
# bootstrap_session() schema

`bootstrap_session()` returns JSON. `schema_version` is currently `1`.

## Compatibility rules

- Clients must require `schema_version`, `required_first_call`,
  `mind_contract`, `mind_contract_available`, `available_mind_tools`,
  `cognition_protocol`, `context`, `memory_catalog`, `degraded_mode`, and
  `host_limitations`.
- Servers may add fields in schema v1.
- Clients must ignore unknown fields.
- Removing or renaming required fields requires `schema_version = 2`.
- If `mind_contract_available` is false, clients must not impersonate the
  persona; body-only behavior is allowed if body safety rules are loaded.
- `memory_catalog` must not expose memory filenames. Use `recall()` or
  `read_memory_file()` for intentional deeper access.
```

- [ ] **Step 2: Add a required-field test**

Add a unit test that builds a packet and asserts the exact required top-level
keys are present. Also assert `schema_version == 1` and that unknown future
fields are permitted by the docs contract rather than rejected in code.

Run:

```bash
cd "/path/to/persona-sati"
pytest tests/active/test_bootstrap.py -v
```

Expected: PASS.

### Task 7: Register `bootstrap_session()` as an MCP tool

**Files:**
- Modify: `/path/to/persona-sati/src/persona_mcp/server.py`
- Modify: `/path/to/persona-sati/tests/test_server.py`

- [ ] **Step 1: Add failing FastMCP test**

Append to `tests/test_server.py`:

```python
@pytest.mark.anyio
async def test_bootstrap_session_tool_returns_operating_packet(server):
    result = await server.call_tool("bootstrap_session", {"session_id": "test-session"})
    packet = json.loads(result[0][0].text)

    assert packet["schema_version"] == 1
    assert packet["required_first_call"] == "bootstrap_session"
    assert packet["session_id"] == "test-session"
    assert packet["mind_contract_available"] is True
    assert "test agent" in packet["mind_contract"]
    assert "observe" in packet["available_mind_tools"]
    assert "reflect" in packet["available_mind_tools"]
    assert "recall" in packet["available_mind_tools"]
    assert packet["memory_catalog"]["index_present"] is True


@pytest.mark.anyio
async def test_bootstrap_session_reports_broken_mind_contract(memory_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("ENTRACLAW_PERSONA_SYNC", "off")
    prompt = tmp_path / "agent_system.md"
    prompt.write_text("@include missing.md\n", encoding="utf-8")
    srv = create_server(memory_dir=memory_dir, prompt_path=prompt)

    result = await srv.call_tool("bootstrap_session", {})
    packet = json.loads(result[0][0].text)

    assert packet["mind_contract_available"] is False
    assert packet["degraded_mode"]["mind_contract_available"] is False
    assert "ERROR: persona-sati voice contract failed to assemble" in packet["mind_contract"]


@pytest.mark.anyio
async def test_get_system_prompt_and_bootstrap_share_broken_prompt_error(memory_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("ENTRACLAW_PERSONA_SYNC", "off")
    prompt = tmp_path / "agent_system.md"
    prompt.write_text("@include missing.md\n", encoding="utf-8")
    srv = create_server(memory_dir=memory_dir, prompt_path=prompt)

    prompt_result = await srv.call_tool("get_system_prompt", {})
    bootstrap_result = await srv.call_tool("bootstrap_session", {})
    packet = json.loads(bootstrap_result[0][0].text)

    assert prompt_result[0][0].text == packet["mind_contract"]
```

- [ ] **Step 2: Run the failing FastMCP test**

Run:

```bash
cd "/path/to/persona-sati"
pytest tests/test_server.py::test_bootstrap_session_tool_returns_operating_packet -v
```

Expected: FAIL because the tool is not registered.

- [ ] **Step 3: Import the builder in `server.py`**

Add near the other active imports:

```python
from persona_mcp.active.bootstrap import build_bootstrap_session
```

- [ ] **Step 4: Register the MCP tool**

First add a private helper near the existing `get_system_prompt()` tool so both
startup surfaces share one fail-loud prompt-loading path:

```python
    def _load_mind_contract() -> tuple[str, bool]:
        if not p_path.is_file():
            return f"Error: system prompt not found at {p_path}", False
        try:
            return load_prompt(p_path).text, True
        except PromptAssemblyError as exc:
            return format_body_error(exc), False
```

Then update `get_system_prompt()` to return only the first tuple element from
`_load_mind_contract()`.

Add a small runtime introspection helper so `available_mind_tools` reflects the
registered FastMCP tool surface instead of a hardcoded list:

```python
    def _available_mind_tools() -> list[str]:
        return sorted(tool.name for tool in server._tool_manager.list_tools())
```

Add this tool after `refresh_persona()` and before `observe()`:

```python
    @server.tool()
    def bootstrap_session(session_id: str | None = None) -> str:
        """Return the mind bootstrap packet for a new host session.

        Call this before the first substantive answer or external tool
        call. It replaces the older three-call startup ritual
        (`get_system_prompt`, `context`, `list_memory_files`) with one
        operating packet that includes the assembled mind contract.
        """
        if hook:
            hook("bootstrap_session", {"session_id": session_id})
        mind_contract, mind_contract_available = _load_mind_contract()
        packet = build_bootstrap_session(
            mem_dir,
            session_id=session_id,
            mind_contract=mind_contract,
            mind_contract_available=mind_contract_available,
            available_mind_tools=_available_mind_tools(),
        )
        return json.dumps(packet)
```

Add a server test that asserts `packet["available_mind_tools"]` matches
`await server.list_tools()` so the packet cannot drift from registered tools.

- [ ] **Step 5: Run server tests**

Run:

```bash
cd "/path/to/persona-sati"
pytest tests/test_server.py::test_bootstrap_session_tool_returns_operating_packet -v
```

Expected: PASS.

### Task 8: Update persona-sati prompt and host docs

**Files:**
- Modify: `/path/to/persona-sati/prompts/hemispheres/cognition-protocol.md`
- Modify: `/path/to/persona-sati/CLAUDE.md`
- Modify: `/path/to/persona-sati/AGENTS.md`
- Modify: `/path/to/persona-sati/README.md`
- Modify: `/path/to/persona-sati/scripts/setup.sh`

- [ ] **Step 1: Update cognition protocol startup wording**

In `prompts/hemispheres/cognition-protocol.md`, replace the session-start bullet:

```markdown
- **At session start:** I call `context()` once to orient — who's
  here, what's active, what I was in the middle of.
```

with:

```markdown
- **At session start:** I call `bootstrap_session()` once before the
  first substantive answer or external tool call. It returns the assembled
  mind contract, active context, memory catalog summary, available mind
  tools, cognition protocol, and degraded-mode state. If an older client
  lacks `bootstrap_session`, I fall back to `context()` plus
  `list_memory_files()`.
```

- [ ] **Step 2: Update "What the four calls are for"**

Replace that subsection with:

```markdown
### What the active cognition calls are for

- `bootstrap_session` — the first call. It orients the host to the
  mind contract, active context, memory catalog, tool surface, and
  degraded-mode state.
- `observe` — the heartbeat. Every tool call (as defined above)
  passes through it unless the body mechanically wraps the call.
- `recall` — when observe's excerpts aren't enough. Rich semantic
  retrieval with optional facets (`relationship:bob`, `project:portal`,
  `type:session_digest`).
- `context` — lower-level session snapshot used by bootstrap and older
  clients.
- `reflect` — for signals that aren't tool calls. Same pipeline as
  observe but the input is free text.
```

- [ ] **Step 3: Update persona-sati CLAUDE.md and AGENTS.md**

In both files, add a short note near the MCP/tool description:

```markdown
`bootstrap_session()` is the primary startup tool for host LLMs. It
replaces the old instruction to call `get_system_prompt()`, `context()`,
and `list_memory_files()` separately. Keep the older tools for
compatibility, but teach new bodies to call `bootstrap_session()` first.
```

- [ ] **Step 4: Update persona-sati README and setup handoff**

Add a short README section explaining that hosts using persona-sati must
load the host bootstrap rule because FastMCP `instructions=` is not reliable
model context.

In `scripts/setup.sh`, after successful `--with-entraclaw` MCP wiring, print
the same host-bootstrap action block as entraclaw setup: install the canonical
snippet into the host-global instruction file, and ensure new sessions call
`bootstrap_session()` first.

- [ ] **Step 5: Run prompt/server tests**

Run:

```bash
cd "/path/to/persona-sati"
pytest tests/test_server.py tests/active/test_bootstrap.py tests/test_prompt_loader.py -v
ruff check src/persona_mcp/active/bootstrap.py src/persona_mcp/server.py tests/active/test_bootstrap.py tests/test_server.py
```

Expected: PASS.

- [ ] **Step 6: Commit Phase 2**

Run:

```bash
cd "/path/to/persona-sati"
git add src/persona_mcp/active/bootstrap.py \
  src/persona_mcp/server.py \
  tests/active/test_bootstrap.py \
  tests/test_server.py \
  docs/reference/bootstrap-session-schema.md \
  prompts/hemispheres/cognition-protocol.md \
  CLAUDE.md \
  AGENTS.md \
  README.md \
  scripts/setup.sh
git commit -m "feat: add persona-sati bootstrap_session tool" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Cross-repo validation

### Task 9: Validate fresh-session behavior

**Files:**
- No code changes.

- [ ] **Step 1: Install both repos in their own virtual environments**

Run:

```bash
cd "/path/to/persona-sati"
source .venv/bin/activate
pip install -e ".[dev,prediction,snn,blob,provisioning]"

cd "/path/to/entraclaw-identity-research"
source .venv/bin/activate
pip install -e ".[dev]"
```

- [ ] **Step 2: Run targeted test suites**

Run:

```bash
cd "/path/to/persona-sati"
pytest tests/test_server.py tests/active/test_bootstrap.py -v && ruff check .

cd "/path/to/entraclaw-identity-research"
pytest tests/test_prompt_doctrine.py tests/hooks/test_inject_body_prompt.py -v && ruff check .
```

Expected: PASS in both repos.

- [ ] **Step 3: Manual smoke test in a non-entraclaw repo**

Start a fresh host session from a directory that is not the entraclaw repo and has the `.mcp.json` entries for entraclaw and persona-sati. Ask:

```text
What persona-sati bootstrap context do you have?
```

Expected: the agent calls `bootstrap_session()` before answering. If it does not, update the host-global snippet installation instructions before merging.

- [ ] **Step 4: Close issue tracker only after smoke test**

After the smoke test passes, update GitHub issue #71 with:

```text
Phase 1+2 landed. bootstrap_session() is now the primary persona-sati startup contract; host-visible docs and doctrine tests point to it. Manual non-entraclaw session smoke test passed.
```

Do not close #71 until the non-entraclaw smoke test confirms the host actually calls `bootstrap_session()`.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | clean | Historical run, not specific to this Phase 1+2 plan |
| Codex Review | `/codex review` | Independent 2nd opinion | 4 | issues_found | Outside voice found sequencing, schema, gate-result, metadata, and fallback gaps; accepted fixes are folded into this plan |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 4 | clean | 8 section findings, 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | Not applicable: no UI changes |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | Not run |

- **CODEX:** Accepted material findings: implement Phase 2 before Phase 1, add schema contract, require successful bootstrap result for gate unlock, remove memory filenames from bootstrap, add deterministic fallback semantics, and derive `available_mind_tools` from registered tools.
- **UNRESOLVED:** 0
- **VERDICT:** ENG CLEARED — ready to implement Phase 2 first, then Phase 1.
