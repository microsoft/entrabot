# Co-Pilot CLI channel-port architecture

## Status

Proposed — 2026-04-24, author: Claude (PM: Brandon)

## Problem statement

Entrabot's push channel is a non-standard JSON-RPC notification — `notifications/claude/channel` — emitted from the stdio write stream by background pollers in `_push_channel_notification` ([`mcp_server.py:1432`](https://github.com/microsoft/entrabot/blob/main/src/entrabot/mcp_server.py)) and `_push_email_notification` ([`mcp_server.py:1237`](https://github.com/microsoft/entrabot/blob/main/src/entrabot/mcp_server.py)). Claude Code routes the notification into the agent's system-reminder stream because the entrabot process is launched with `--dangerously-load-development-channels`, and we declare the matching `experimental_capabilities={"claude/channel": {}}` in `_run_stdio_with_write_stream` ([`mcp_server.py:2780`](https://github.com/microsoft/entrabot/blob/main/src/entrabot/mcp_server.py)). This is the entire push mechanism — no Microsoft webhooks, no Graph subscription, no client-pull.

Brandon increasingly works in GitHub Copilot CLI. Copilot CLI is a different MCP host. It supports MCP **tools only** — not resources, not prompts, not sampling, not elicitation, not roots, and not arbitrary `notifications/*` channels ([copilot-cli#1518](https://github.com/github/copilot-cli/issues/1518), [copilot-cli#1748](https://github.com/github/copilot-cli/issues/1748), [copilot-cli#1803](https://github.com/github/copilot-cli/issues/1803)). It has no `--dangerously-load-development-channels` analogue. The model receives nothing the server doesn't send back as a tool-call result. So under Copilot CLI, every inbound Teams DM today is invisible to the agent until it happens to call a tool whose return value happens to mention it — which it never does, because no tool currently advertises inbound state.

We must keep one MCP server boot working across both hosts, with push semantics that match today's experience on Claude Code (≤10s end-to-end latency for an inbound Teams message reaching the model) and zero regression for the Claude Code path.

## Background — Copilot CLI MCP capability matrix (April 2026)

| Capability | Claude Code | Copilot CLI |
|---|---|---|
| Tools (`tools/list`, `tools/call`) | Yes | Yes — only primitive supported |
| Resources (`resources/list`, `resources/read`) | Yes | **No** — open feature request ([#1803](https://github.com/github/copilot-cli/issues/1803), [#1518](https://github.com/github/copilot-cli/issues/1518)) |
| Resource subscribe (`notifications/resources/updated`) | Yes | **No** — depends on resources |
| Prompts | Yes | **No** ([#1518](https://github.com/github/copilot-cli/issues/1518)) |
| Sampling (`sampling/createMessage`) | Yes | **No** — explicit `Method not found` ([#1748](https://github.com/github/copilot-cli/issues/1748), [community#160291](https://github.com/orgs/community/discussions/160291)) |
| Elicitation | Yes (recent) | **No** in CLI; yes in JetBrains/IDE Copilot |
| Roots | Yes | **No** |
| Arbitrary server notifications (`notifications/*`) | Routed only with experimental capability + dangerous-channels flag | **Dropped silently** — no host handler |
| `notifications/progress` | Surfaced in UI | Surfaced in **status line / timeline only**, NOT delivered to model context ([#1837](https://github.com/github/copilot-cli/issues/1837)) |
| Tool-call timeout | None observed; long polls work | **Hard 60s** regardless of config; tool config `timeout` field ignored ([#1535](https://github.com/github/copilot-cli/issues/1535)) |
| stdio transport | Yes | Yes |
| Streamable HTTP transport | Yes | Yes |
| SSE transport | Yes (legacy) | Yes (legacy, deprecated) |
| `--experimental` / preview flag | `--dangerously-load-development-channels` | `--experimental` exists but exposes preview features only — no documented arbitrary-notification opt-in |

The MCP spec itself ([modelcontextprotocol.io](https://modelcontextprotocol.io/)) does not bless server-initiated push to the model out-of-band. The pattern Brandon and Anthropic invented — `notifications/claude/channel` — is currently host-specific. The MCP community has an open issue requesting a blessed push primitive ([anthropics/claude-code#36665](https://github.com/anthropics/claude-code/issues/36665)) which is still in `enhancement` / `stale` status.

This means the cross-host bridge must be expressed in **tool semantics**, because tools are the only primitive Copilot CLI exposes to the model. There is no notification, resource, sampling, or elicitation back-channel available.

## Design options considered

The 60-second tool-call timeout is a binding constraint that reshapes the design space. Four of the six PM-side candidates collapse on contact with it.

### A. Long-poll tool — `wait_for_inbound_message(timeout_s=…)`

A tool that blocks until a message arrives or a timeout elapses, then returns the message as a normal tool result. The agent calls it in a tail-call loop: respond, then immediately call `wait_for_inbound_message` again. Background pollers continue to run; they enqueue into an in-process buffer that the long-poll drains.

The 60-second cap from [copilot-cli#1535](https://github.com/github/copilot-cli/issues/1535) means each poll has to time out at ~50s to leave headroom for transport overhead. End-to-end latency stays at 5s (the existing background poll cycle drives the buffer; the long-poll wakes within milliseconds of an enqueue via an `asyncio.Event`). The agent burns one tool call per ~50s of idle time, which is acceptable on Copilot CLI's quota and trivial cost-wise.

The hard part is convention, not code: the agent has to actually keep the loop going. That is a body-prompt rule, not a server feature. We add it to `prompts/anatomy/channel-discipline.md` as a Copilot-CLI-specific clause ("after every substantive turn, call `wait_for_inbound_message` with `timeout_s=50` and act on whatever it returns").

Score: One-server clean (yes). Latency ≤5s (yes). LOC ~150. Portable (yes — every MCP host supports tool calls). Stacking safely (yes — the buffer is bounded; old messages survive across calls until drained).

### B. Resource subscription — `chats://inbox` resource updated each tick

We expose an `chats://inbox` resource. Each background poll cycle updates its content with the latest unread Teams messages and emits `notifications/resources/updated`. Hosts that honor the spec re-read the resource and the model sees new data.

Dies on Copilot CLI: [#1518](https://github.com/github/copilot-cli/issues/1518) and [#1803](https://github.com/github/copilot-cli/issues/1803) confirm Copilot CLI does not implement the resources primitive at all. Even if it eventually does, resource read is a user-driven Add Context flow in Copilot — it does not auto-attach on update. Skip.

### C. Sampling — server initiates a model turn

`sampling/createMessage` would let entrabot say "here's an inbound message, please act on it" and the host would feed it to the model. This is the cleanest semantic match to today's behavior.

Dies on Copilot CLI: [community#160291](https://github.com/orgs/community/discussions/160291) and [copilot-cli#1748](https://github.com/github/copilot-cli/issues/1748) confirm `sampling/createMessage` returns `Method not found` in Copilot CLI. Listed as a feature request as of March 2026, still unassigned. Skip.

### D. Sidecar process + IPC tool

Polling moves to a separate daemon that writes to a queue file or socket; the MCP tool drains it. Violates the one-server bar Brandon set, doubles the operational surface (two systemd units, two sets of logs, two failure modes), and gains us nothing — the tool that drains the queue is functionally identical to (A) but with worse failure modes (the queue file can corrupt, fill the disk, or get stale across restarts). Skip.

### E. Standalone HTTP server with webhook into MCP tool space

Brandon will hate this. Two surfaces, no security story for the webhook authentication, and the MCP tool side still has to drain — same pattern as (D) plus extra public attack surface. Skip.

### F. Hybrid — host detection at boot, channel push for Claude Code, long-poll for Copilot CLI

Detect the host via `clientInfo.name` (already cached in `_state["cached_host"]` by `_capture_host_from_context`, [`mcp_server.py:280`](https://github.com/microsoft/entrabot/blob/main/src/entrabot/mcp_server.py)). For Claude Code (`name in {"claude-code", "claude-ai"}` or anything that handles the channel cap), do exactly what we do today. For Copilot CLI (`name == "github-copilot-cli"`), gate the channel push to no-op and rely on the long-poll tool from (A).

This is the recommendation, with a wrinkle: **always run both paths in parallel**, regardless of host. Push is fire-and-forget — the cost on a host that drops it is one TCP write and a swallowed exception ([`mcp_server.py:1535-1546`](https://github.com/microsoft/entrabot/blob/main/src/entrabot/mcp_server.py)). The long-poll tool is host-agnostic — it always works. So we don't actually need a host gate on the *push* path; we need a host gate only on the *body prompt convention* that tells the agent to call `wait_for_inbound_message`.

That collapses (F) into "ship (A); Claude Code body keeps using channel pushes; Copilot CLI body adds a `wait_for_inbound_message` rule." One server, one push pipeline, one new tool, one body-prompt section.

### Scoring

| Option | One-server | Latency | LOC | Portability | Stack-safety | Verdict |
|---|---|---|---|---|---|---|
| A. Long-poll tool | Yes | 5s | ~150 | Universal | Bounded buffer | **Recommended** |
| B. Resource subscribe | Yes | n/a | ~80 | Cursor/Windsurf maybe; not Copilot | n/a | Blocked by Copilot CLI |
| C. Sampling | Yes | <2s | ~50 | Claude Code only | n/a | Blocked by Copilot CLI |
| D. Sidecar | **No** | 5s | ~400 | Universal | Disk-bound | Violates Brandon's bar |
| E. HTTP webhook | **No** | <2s | ~600 | Universal | Network-bound | Violates Brandon's bar + security risk |
| F. Hybrid (collapses to A) | Yes | 5s on Copilot, <1s on Claude | ~150 | Universal | Bounded buffer | Same as A — Brandon already gets it |

## Recommendation

**Ship (A): one MCP server, one new tool `wait_for_inbound_message`, one body-prompt clause for Copilot CLI hosts.**

Why this and not anything else: the Copilot CLI MCP capability matrix is brutally narrow today (tools-only, 60s timeout, status-line-only progress). Every push-semantic alternative is a feature request, not a shipping capability, and three of them are unassigned in GitHub. A long-poll tool requires zero new MCP capabilities, runs identically across every host that exists or will exist, costs one tool call per ~50s of idle time, and lets us keep the existing channel-push code path verbatim for Claude Code. The implementation reuses the background pollers we already have — we just add an `asyncio.Queue` between them and the new tool. There is no second process, no second runtime, no second auth domain.

The only loss vs Claude Code today is that Copilot CLI's agent has to choose to listen, where Claude Code's agent is interrupted. We close that gap with body-prompt discipline ("end every substantive turn by calling `wait_for_inbound_message`"). That discipline is enforceable the same way every other body rule is enforced — it lives in `prompts/anatomy/channel-discipline.md` and we trust the body prompt to govern.

If Copilot CLI later ships sampling or arbitrary-notification routing, this design upgrades cleanly. The long-poll tool stays as a universal floor; the channel-push path layers on top for hosts that support it.

## Implementation sketch

### New module: `src/entrabot/inbound_queue.py`

A small in-process pub-sub queue. Async-safe, bounded, deduped by `message_id`, with a wakeup `asyncio.Event` so the long-poll tool unblocks within a tick of an enqueue.

```python
class InboundQueue:
    def __init__(self, maxlen: int = 200) -> None:
        self._items: deque[InboundMessage] = deque(maxlen=maxlen)
        self._seen: set[str] = set()
        self._wakeup = asyncio.Event()
        self._lock = asyncio.Lock()

    async def push(self, msg: InboundMessage) -> None: ...
    async def drain(self, max_items: int = 10, timeout_s: float = 50) -> list[InboundMessage]: ...
    async def stats(self) -> dict: ...
```

`drain` blocks on `self._wakeup` until something is queued or `timeout_s` elapses, then returns up to `max_items`. Bounded `maxlen=200` is the safety valve — if the agent falls behind for hours, the oldest 200 messages survive; older ones are dropped (logged + audited) so the process can't OOM. Tests will pin all four behaviors: enqueue/drain happy path, dedupe by `message_id`, timeout returns empty list, overflow drops + audits.

### Changes to `src/entrabot/mcp_server.py`

1. **Module-level singleton.** `_inbound_queue: InboundQueue` initialized at boot inside `_run_stdio_with_write_stream`, stored alongside the write stream.

2. **Push fan-out.** `_push_channel_notification` ([`mcp_server.py:1432`](https://github.com/microsoft/entrabot/blob/main/src/entrabot/mcp_server.py)) and `_push_email_notification` ([`mcp_server.py:1237`](https://github.com/microsoft/entrabot/blob/main/src/entrabot/mcp_server.py)) gain one extra line each: `await _inbound_queue.push(InboundMessage.from_teams(message))` (or `.from_email`). The existing notification send path is unchanged. The interaction-log write is unchanged. Order: log → enqueue → push. Enqueue precedes push so a transport-broken host still gets the message via the long-poll path.

3. **New tool.**

```python
@mcp.tool()
async def wait_for_inbound_message(
    timeout_s: int = 50,
    max_items: int = 10,
) -> str:
    """Block up to timeout_s seconds for inbound Teams DMs / emails.

    Returns a JSON list of {chat_id, message_id, sender, timestamp,
    content, source} dicts. Empty list = timeout reached, no new
    messages — call again. ON COPILOT CLI, the body prompt requires
    you to call this tool at the end of every substantive turn.

    On Claude Code, this tool is redundant with channel notifications
    but safe to call — it drains the same queue.
    """
    await _initialize()
    msgs = await _inbound_queue.drain(
        max_items=max_items,
        timeout_s=min(timeout_s, 55),  # cap at 55 to stay under Copilot's 60s
    )
    return json.dumps([asdict(m) for m in msgs])
```

The 55s cap is the load-bearing line — [copilot-cli#1535](https://github.com/github/copilot-cli/issues/1535) confirms 60s is hard, and we leave 5s for serialization overhead. We do **not** trust the caller to set this correctly; we cap it server-side.

4. **No changes to the channel push path.** Claude Code keeps everything it has. The push call still fires on every host and is silently dropped on Copilot CLI per MCP-spec behavior ([`mcp_server.py:1466-1469`](https://github.com/microsoft/entrabot/blob/main/src/entrabot/mcp_server.py)). Belt + suspenders.

### Changes to `prompts/anatomy/channel-discipline.md`

Add a section right after the existing "Respond on the channel you were pinged on" rule:

> **Inbound listening on Copilot CLI.** When the host is GitHub Copilot CLI (you can tell because channel notifications never arrive — your context only updates from tool results), call `wait_for_inbound_message(timeout_s=50)` at the end of every substantive turn. Treat each returned message exactly as you would a `<channel>` push: surface to the human via Teams, audit-log, ack with `post_thinking_placeholder` if substantive. An empty return means "no new traffic in the last ~50s — call me again." Do not spin tighter than every 50s; do not stop calling.

The body prompt does **not** need a host-detection branch. The instruction is "if no channel pushes are arriving, you are on a non-Claude host, so listen by tool." The agent reasons about its environment from observation, which is more robust than `clientInfo.name`-sniffing in the prompt.

### Changes to `.mcp.json.example`

Add a second example block showing the Copilot CLI configuration:

```json
{
  "mcpServers": {
    "entrabot": {
      "type": "local",
      "command": ".venv/bin/entrabot-mcp",
      "args": []
    }
  }
}
```

(Path is `~/.copilot/mcp-config.json` for Copilot CLI; same `entrabot-mcp` binary.)

### Sequence diagram

```mermaid
sequenceDiagram
    participant Teams as Teams Graph API
    participant Poll as background poll (5s)
    participant Q as InboundQueue
    participant Push as _push_channel_notification
    participant CC as Claude Code (notifications/claude/channel)
    participant Tool as wait_for_inbound_message tool
    participant Copilot as Copilot CLI (tool result)

    Teams->>Poll: new message
    Poll->>Push: log + enqueue + push
    Push->>Q: enqueue (always)
    Push->>CC: notification (Claude only; dropped elsewhere)
    Note over CC: agent surfaces immediately
    Tool->>Q: drain(timeout=50s)
    Q-->>Tool: [msg, ...] or []
    Tool->>Copilot: JSON-encoded list
    Note over Copilot: agent surfaces on next turn boundary
```

### Test plan

- `tests/test_inbound_queue.py` — enqueue, drain, dedupe, overflow, wakeup latency (<50ms p95).
- `tests/tools/test_wait_for_inbound.py` — empty timeout returns `[]`, populated returns sorted-by-ts list, `timeout_s` >55 silently capped, returns under 60s wallclock.
- `tests/test_mcp_server.py` — gain one assertion that `_push_channel_notification` enqueues to the queue *before* attempting the write. Test must pass with `_state["_write_stream"] = None`.
- `pytest -v && ruff check .` clean before commit, per non-negotiables in `CLAUDE.md`.

### LOC and surface estimate

- `inbound_queue.py`: ~80 lines + tests ~100.
- `mcp_server.py`: +60 lines (new tool, two enqueue lines, doc strings).
- `prompts/anatomy/channel-discipline.md`: +12 lines.
- `.mcp.json.example`: +10 lines.
- Tests: ~150 lines.

Total: ~410 lines, no dependencies added. Two days of work including the body-prompt review with the user.

## Open questions for Brandon

- **Q1. Do we want a `host_hint` config var for explicit override?** A user with a host clientInfo we haven't seen could be misclassified. **Recommended default: no.** The design doesn't depend on host detection — push fires unconditionally, queue exists unconditionally, tool exists unconditionally. The body prompt is the only host-aware artifact, and it's keyed on observed behavior ("channel notifications aren't arriving"), not host name. Skip the config knob.

- **Q2. Should `wait_for_inbound_message` block on `timeout_s=50` even when there's nothing watched?** If `_state["watched_chats"]` is empty and email is configured, an idle hour means 72 tool calls returning `[]`. **Recommended default: yes, block normally.** The cost is 72 RPCs per hour per idle agent. The benefit is the agent is never deaf. If Brandon wants to tighten this we add a "no watched chats AND no email" fast-fail returning immediately, but I'd ship without it and watch whether it's a real problem.

- **Q3. Do we expose queue stats as a tool?** A `inbound_queue_stats()` tool would help debug ("how many messages did I miss while compacting?"). **Recommended default: yes, add it cheaply.** One @mcp.tool that returns `{depth, dropped_total, oldest_ts}`. ~10 LOC.

- **Q4. Body prompt: do we add the Copilot CLI clause to a new file (`prompts/anatomy/copilot-cli.md`) or extend `channel-discipline.md`?** **Recommended default: extend `channel-discipline.md`.** The behavior IS channel discipline — same vocabulary, same audit rules, same place a body-rule reader expects to find it. A separate file fragments the rule by host and would have to be `@include`d conditionally, which the loader doesn't support today (`_expand_includes` is unconditional, [`mcp_server.py:53`](https://github.com/microsoft/entrabot/blob/main/src/entrabot/mcp_server.py)).

- **Q5. ~~Should `wait_for_inbound_message` also forward Bot Gateway inbound (`_background_poll_bot`)?~~** **Obsolete — Bot Gateway mode was removed (ADR-006).** The fan-out insight still holds: `_push_channel_notification` is the unified entry point for inbound, so `wait_for_inbound_message` enqueues from one place regardless of source (Teams Graph poll, email poll).

- **Q6. Channel-push backwards compatibility — do we remove the experimental_capabilities declaration when we detect Copilot CLI?** **Recommended default: no, leave it.** The capability is advertised in `mcp._mcp_server.create_initialization_options(experimental_capabilities={"claude/channel": {}})` ([`mcp_server.py:2780`](https://github.com/microsoft/entrabot/blob/main/src/entrabot/mcp_server.py)) and Copilot CLI ignores capabilities it doesn't understand, per MCP spec. Branching the init options on host adds complexity for zero benefit.

## Risks and rollback

**Risk 1: Copilot CLI starts truncating tool results that exceed some N kB.** A burst of 50 inbound messages in a 50s window could hit a tool-result-size cap we haven't seen documented. **Mitigation:** `max_items=10` default; the body-prompt rule says "drain, then loop again" so a backlog drains in O(N/10) calls. **Rollback:** lower `max_items` to 5 in the tool default.

**Risk 2: The body prompt rule isn't strong enough.** Agents under high cognitive load may forget to call `wait_for_inbound_message`. **Mitigation:** put the rule near the top of `channel-discipline.md`, alongside "Respond on the channel you were pinged on" — both are first-class. Watch the interaction log for the symptom (inbound logged, no human-facing response within 5 min) and tighten if it happens. **Rollback:** none needed; the channel-push path on Claude Code is unaffected.

**Risk 3: A blocking 50s tool call somehow holds the MCP event loop and starves background pollers.** It does not — the long-poll uses `asyncio.wait_for(self._wakeup.wait(), timeout=...)` which yields. Tests will pin this. **Mitigation:** explicit test `tests/test_inbound_queue.py::test_drain_does_not_block_event_loop` that runs `drain(timeout=50)` concurrently with a 100Hz `asyncio.sleep(0)` task and asserts the heartbeat never stalls. **Rollback:** revert the new tool — channel push still works on Claude Code, and Copilot CLI returns to silent-deaf which is the status quo.

**Risk 4: The 60s timeout in [copilot-cli#1535](https://github.com/github/copilot-cli/issues/1535) gets shortened.** GitHub could ship a 30s timeout next release. **Mitigation:** the `timeout_s` parameter is caller-controlled with a server cap. If the cap needs to drop to 25, it's a one-line change. **Rollback:** none.

**Risk 5: Copilot CLI ships its own push mechanism mid-flight.** If `notifications/copilot/channel` lands or sampling becomes available, we want to use it. **Mitigation:** the architecture upgrades cleanly — add the new push site to `_push_channel_notification` (same fan-out shape we already have for `claude/channel`), the long-poll tool stays as the universal floor. No rollback; pure add.

**Full rollback procedure** if the entire approach is wrong: revert the PR. The channel-push path on Claude Code is byte-identical to what ships today, so Claude Code regresses zero. Copilot CLI returns to silently-deaf, which is the current state. No data migration, no auth changes, no ADR amendments.

## Sources

- [GitHub Copilot CLI — Adding MCP servers (docs)](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers)
- [GitHub Copilot CLI — About Copilot CLI (docs)](https://docs.github.com/copilot/concepts/agents/about-copilot-cli)
- [About Model Context Protocol (MCP) — GitHub Docs](https://docs.github.com/en/copilot/concepts/context/mcp)
- [copilot-cli#1518 — Support MCP resources and prompts](https://github.com/github/copilot-cli/issues/1518)
- [copilot-cli#1535 — MCP Server keeps timing out in 60 seconds](https://github.com/github/copilot-cli/issues/1535)
- [copilot-cli#1748 — Support MCP Sampling in Copilot CLI](https://github.com/github/copilot-cli/issues/1748)
- [copilot-cli#1803 — Support MCP resources/read primitive](https://github.com/github/copilot-cli/issues/1803)
- [copilot-cli#1837 — Support OSC 8 terminal hyperlinks in MCP progress notification messages](https://github.com/github/copilot-cli/issues/1837)
- [community#160291 — Does Copilot support MCP Sampling?](https://github.com/orgs/community/discussions/160291)
- [anthropics/claude-code#36665 — feat: MCP server push notifications (unsolicited messages to client)](https://github.com/anthropics/claude-code/issues/36665)
- [Microsoft Java DevBlog — Unlocking MCP in JetBrains: How Copilot Uses Sampling, Prompts, Resources, and Elicitation](https://devblogs.microsoft.com/java/unlocking-mcp-in-jetbrains-how-copilot-uses-sampling-prompts-resources-and-elicitation/)
- [Model Context Protocol — modelcontextprotocol.io](https://modelcontextprotocol.io/)
- Repo files read:
  - `/path/to/entrabot-identity-research/src/entrabot/mcp_server.py` (channel push, write-stream capture, `clientInfo` host detection, background pollers, FastMCP boot)
  - `/path/to/entrabot-identity-research/src/entrabot/tools/teams.py`
  - `/path/to/entrabot-identity-research/prompts/anatomy/channel-discipline.md`
  - `/path/to/entrabot-identity-research/.mcp.json.example`
  - `/path/to/entrabot-identity-research/pyproject.toml`
