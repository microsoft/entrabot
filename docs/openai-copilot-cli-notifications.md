# Copilot CLI Notifications — Portable Inbound Channel for Entraclaw

**Status:** Draft / Architecture proposal
**Author:** Agent 1 (research) / Product Manager review
**Date:** 2026-04-24
**Owners:** Entraclaw maintainers
**Scope:** Make inbound Teams + Email work on GitHub Copilot CLI without
losing the Claude Code experience.
**Non-goals:** Re-implementing the persona-sati mind. Changing Entra
auth, Graph polling cadence, or interaction-log schema. Building a
new Teams transport.

---

## 1. Executive summary

Today Entraclaw ships inbound Teams DMs and email into the LLM session
through a **Claude-Code-proprietary** transport: a JSON-RPC
notification with method `notifications/claude/channel`, gated on
the `experimental: {"claude/channel": {}}` capability and the
`--dangerously-load-development-channels server:entraclaw` launch
flag (Learning #26, #39). **Copilot CLI does not implement this
method, has no published equivalent, and per the public help surface
exposes no documented client API for server-initiated turn injection.**

Recommendation, in order:

1. **Phase 0 — Compatibility probe.** Detect the connected client at
   `Initialize` time (`clientInfo.name`). If it is `claude-code`,
   keep firing `notifications/claude/channel` exactly as today. If it
   is anything else (Copilot CLI advertises as `copilot`, VS Code as
   `vscode`, etc.), stop firing the proprietary notification and turn
   on the portable surfaces below. **One MCP server. Same binary.**
2. **Phase 1 — Portable inbox in the existing MCP server.** Surface
   inbound messages through three orthogonal mechanisms that any MCP
   client can consume:
   (a) a durable on-disk **inbox file** (`~/.entraclaw/inbox.jsonl`
   plus an `unread` count in `inbox.state.json`) that the agent
   reads via a new `inbox_pull(since=...)` tool;
   (b) a spec-defined MCP logging notification
   (`notifications/message`) with logger `entraclaw.inbox`, harmless
   on clients that ignore it, useful on clients that surface server
   logs;
   (c) a **MCP resource** (`entraclaw://inbox`) that emits
   `notifications/resources/updated` whenever the inbox grows, for
   any future client that subscribes.
   The interaction log + email/Teams polling **already exists**;
   Phase 1 is mostly wiring, not new I/O.
3. **Phase 2 — Optional terminal companion.** A tiny long-lived
   sidecar (`entraclaw watch`) prints colored one-line nudges to the
   user's terminal when new messages arrive, plus optional OS toast
   (`osascript`/`notify-send`/`BurntToast`). This restores the
   "you have mail" UX outside the LLM. It is **not** a second MCP
   server; it talks to the same inbox file. Strictly optional.
4. **Phase 3 — Adopt MCP Triggers & Events when it lands.** When the
   official WG ships event push (RFC tracked in
   `docs/platform-learnings/mcp-close-the-loop.md`), wire
   `_push_channel_notification` to also emit the spec-defined event.
   The portable inbox stays as the durable backstop.

**Net result:** one MCP server process, one polling loop, one
auth/refresh path, one interaction log. Claude Code keeps the
turn-injection UX; Copilot CLI gets a tool-pull + log-ping +
optional terminal-toast UX. Nothing about Entra, Graph, or the
three-hop token flow changes.

---

## 2. Current Claude Code architecture (verified)

What `--dangerously-load-development-channels server:entraclaw` plus
`experimental_capabilities={"claude/channel": {}}` actually buys us:

- **Server side** (`src/entraclaw/mcp_server.py`):
  - `_run_stdio_with_write_stream` (line ~2726) wraps the FastMCP
    `stdio_server()` so we keep a handle to the write stream after
    `Initialize`. Default `mcp.run(transport="stdio")` does not
    expose this stream.
  - `create_initialization_options(experimental_capabilities=
    {"claude/channel": {}})` advertises the experimental capability
    to the client during the MCP handshake.
  - `_push_channel_notification(message, chat_id=...)` (line ~1432)
    builds a `JSONRPCNotification` with
    `method="notifications/claude/channel"` and
    `params={"content": <str>, "meta": {"chat_id", "message_id",
    "user", "ts", optional "reply_to_ids", optional
    "quoted_messages"}}` and calls `write_stream.send(...)`. Failure
    is swallowed (Learning #29).
  - Background loops that fire it: `_background_poll` (5 s Teams DM
    poll, line ~965), `_background_poll_email` (60 s, line ~1063),
    `_background_poll_bot` (Bot Gateway, line ~820).
  - Every push is **observed first** to the interaction log. The
    log is the durable record; the channel push is "best effort"
    (Learning #29 / #38).
- **Client side** (Claude Code):
  - On receiving `notifications/claude/channel` from a server it
    started with `--dangerously-load-development-channels
    server:<name>`, Claude Code injects the `params.content` plus
    `params.meta` into the next LLM turn as a synthetic input — the
    same path the iMessage channel plugin uses.
  - Without the launch flag the notification is **silently
    dropped**, even if the experimental capability is advertised
    (Learning #39).
- **Failure modes that have actually bitten us:**
  - Marketplace-plugin spoofing → don't (Learning #26 note 3).
  - Schema drift between Teams push and email push → unified meta
    shape required (Learning #29).
  - Sharing dedup state with `watch_teams_replies` → independent
    cursors required (Learning #27).
  - Cascading server restarts amplifying drops → ripped leader gate
    in PR #36 (Learning #38).

**Bottom line:** the Claude Code path is one proprietary
`method=` string (`notifications/claude/channel`) plus one launch
flag. Everything else — auth, polling, dedup, sanitization,
observation — is portable.

### What is proprietary vs portable

| Layer | Proprietary to Claude Code | Portable |
|---|---|---|
| `experimental_capabilities={"claude/channel": {}}` | ✅ key name | — |
| `notifications/claude/channel` JSON-RPC method | ✅ | — |
| `--dangerously-load-development-channels` launch flag | ✅ | — |
| Turn-injection UX | ✅ | — |
| Three-hop Agent User token flow | — | ✅ |
| Graph polling (`_background_poll`, email, bot) | — | ✅ |
| Interaction log + dedup + cursors | — | ✅ |
| HTML sanitization (`_summarize_content`) | — | ✅ |
| FastMCP stdio + tool surface | — | ✅ |

Phase 1 of this plan keeps the proprietary row intact for Claude
Code and replaces the turn-injection UX with three portable
surfaces for everyone else.

---

## 3. Copilot CLI capability analysis

### Verified (from `fetch_copilot_cli_documentation`, official help text, and public docs)

- Copilot CLI is the official terminal harness for the GitHub Copilot
  coding agent ([docs.github.com/copilot/concepts/agents/about-copilot-cli](https://docs.github.com/copilot/concepts/agents/about-copilot-cli)).
  npm package `@github/copilot`, brew cask `copilot-cli`,
  `winget install GitHub.Copilot`.
- **MCP support is real and first-class.** The `/mcp` slash command
  manages MCP server configuration. Official docs document
  `~/.copilot/mcp-config.json` as the editable user-level config.
  Entraclaw's `scripts/mcp_config.py` already writes that file and
  also writes project-local `.mcp.json`; whether Copilot CLI honors the
  project-local file without the user-level copy is repo-documented but
  should be verified in Phase 0.
- **Custom instructions surfaces:** `AGENTS.md` (git root + cwd),
  `CLAUDE.md`, `GEMINI.md`, `.github/instructions/**/*.instructions.md`,
  `.github/copilot-instructions.md`,
  `~/.copilot/copilot-instructions.md`. Entraclaw already has
  `AGENTS.md` and `CLAUDE.md` — Copilot CLI loads both.
- **Background tasks exist:** `/tasks` is a documented slash command
  ("View and manage background tasks (subagents and shell sessions)").
  Useful for *spawning* work, **not** for receiving push events.
- **Skills, plugins, plugin marketplaces** exist (`/skills`,
  `/plugin`). Skills can wrap tool sequences but are still
  user-initiated.
- **Sessions are interactive and persistent** (`/resume`, `/session`,
  `/rewind`, `/keep-alive`). `/keep-alive` prevents system sleep —
  relevant when we want polling to keep working overnight.
- **Remote control:** `/remote` toggles "remote control from GitHub
  web and mobile." Out of scope for this plan but worth noting — it
  is *not* a pushed-event surface either.
- **Shell escape:** `!` in the prompt runs a shell command. The
  output goes back into the conversation. This is a user-initiated
  surface; not useful for unsolicited push.
- **Instruction files are listed in the Copilot CLI help surface.**
  Treat them as session-start guidance unless live reload is verified;
  editing `AGENTS.md` mid-session should not be the primary
  notification path.

### Inferred / unverified (flagged, with how to verify)

- **No documented handler for arbitrary MCP server-initiated
  notifications.** A web search (cited below) finds no Copilot CLI
  doc, issue, or release note describing handling of
  `notifications/claude/channel`, `notifications/message`,
  `notifications/resources/updated`, or any other server-initiated
  method as a way to inject a turn.
  *Verification:* run `copilot` with `entraclaw` configured, fire each
  notification method from a tracer MCP, and inspect what (if
  anything) appears in the conversation. Capture the JSON-RPC trace
  via whatever debug log surface Copilot exposes; do not assume a
  particular env var name. Phase 0 ships this probe.
- **No documented client API for "wake the agent" without user
  input.** Skills and slash commands all originate from the user.
  `/keep-alive` keeps the host awake but does not poll.
  *Verification:* search `github/copilot-cli` repo issues for
  `notification`, `push`, `wake`, `inject`. Track findings in this
  doc.
- **`/tasks` semantics for our use case.** Reads as "long-running
  shell or subagent sessions you launched," not "events that wake
  the parent session." Likely **not** suitable as a push surface for
  inbound Teams. *Verification:* `copilot` → `/tasks` →
  experiment with a sleep+echo task and confirm the parent session
  does not auto-resurface its output mid-conversation.
- **MCP resource subscriptions.** Copilot CLI ships an MCP client;
  whether that client implements `resources/subscribe` and reacts to
  `notifications/resources/updated` is undocumented. Claude Code
  closed this as "not planned" (mcp-close-the-loop.md, Issue #7252).
  *Verification:* register a resource on the entraclaw server and
  see whether Copilot CLI subscribes during init.

### Citations (web)

- GitHub Copilot CLI feature page — <https://github.com/features/copilot/cli/>
- About GitHub Copilot CLI — <https://docs.github.com/copilot/concepts/agents/about-copilot-cli>
- Getting started with GitHub Copilot CLI — <https://docs.github.com/en/copilot/how-tos/copilot-cli/cli-getting-started>
- Awesome Copilot Customizations — <https://github.com/github/awesome-copilot-customizations>
- MCP Triggers & Events WG charter — <https://modelcontextprotocol.io/community/triggers-events/charter>
- Claude Code resource subscription, closed "not planned" (#7252) — <https://github.com/anthropics/claude-code/issues/7252>
- MCP Discussion #1192 (server notification best practices) — <https://github.com/modelcontextprotocol/modelcontextprotocol/discussions/1192>

---

## 4. Option matrix

Five concrete approaches considered. "Single MCP" = does this preserve
the one-running-MCP-server constraint?

| # | Approach | UX on Copilot CLI | Feasibility | Effort | Risks | Single MCP? |
|---|---|---|---|---|---|---|
| **A** | **Portable inbox tool + log ping + resource update** (recommended Phase 1) | Agent calls `inbox_pull` on demand or after body-prompt/tool-description nudges; `notifications/message` shows in any client that surfaces logs; resource update fires for future MCP-spec-conformant clients. Latency ≈ poll cadence + agent's next turn. | High — uses only spec-defined primitives, plus a plain JSONL file. | Small. The polling, dedup, sanitization, and interaction log already exist. | Agent forgets to call `inbox_pull` (mitigated by `AGENTS.md` rule + tool descriptions). | ✅ Yes |
| **B** | **Companion daemon `entraclaw watch`** (Phase 2 add-on) | Terminal banner / OS toast / tray icon when a message arrives. The human sees the alert and tells Copilot to read the inbox. | High — independent process, talks to same JSONL. | Small-medium. Terminal printer first; OS toast across three platforms can follow. | Two processes to keep alive; user has to start it; dies on logoff. | ✅ Still one MCP; companion is not an MCP. |
| **C** | **Local SSE/WebSocket push from MCP to a Copilot-CLI–side bridge** | Real-time inbound, but Copilot CLI has to *consume* the bridge. Today there is no such consumer; we'd need a Copilot skill/plugin that re-injects, which the docs do not support. | Low — depends on undocumented Copilot internals. | Medium-high. | Likely impossible without Copilot CLI changes; speculative. | ❌ Adds a bridge process. |
| **D** | **Copilot skill that the user manually triggers (`/inbox`)** | User types `/inbox` to flush queue into the conversation. Equivalent to option A but with a slash command instead of a tool call. | High — skills are documented. | Small. Mostly a thin wrapper over `inbox_pull`. | Still user-initiated; doesn't give "push" feel. | ✅ Yes |
| **E** | **MCP `ctx.sample()` / `ctx.elicit()` from a long-running tool** | A `watch_teams_replies`-style tool that calls `ctx.sample()` to make the LLM react mid-tool when a message arrives. | Unverified. Untested with Copilot CLI. (Learning #23 says even Claude Code likely doesn't honor it.) | Medium. | High — speculative; would block one tool slot indefinitely; client may reject. | ✅ Yes |
| **F** | **Adopt MCP Triggers & Events spec when it lands** (Phase 3) | True spec-blessed push, identical UX to today's Claude Code path but portable. | Pending RFC adoption (mid-2026 earliest). | Small once spec is final, since polling/sanitization stay. | Spec slippage; client adoption lag. | ✅ Yes |

**Recommendation:** A + B + Phase-0 client detection. D is a free
add-on (one wrapper). E is not worth the risk. C is rejected. F is
the eventual end state.

---

## 5. Recommended architecture

### 5.1 Phase 0 — Compatibility probe

**Goal:** make the existing server detect at runtime whether the
connected client is Claude Code (with channels) or anything else, and
suppress proprietary pushes for non-Claude clients without breaking
them.

- Reuse `_capture_host_from_context` (already present, line ~250) to
  read `clientInfo.name` from the active request context, but do not
  reintroduce the old leader/slave gate. Learning #38 was caused by a
  stale cached host controlling delivery. In the stdio model there is
  one MCP client per process, so a host cache may annotate logs and
  choose a best-effort notification branch, but durable inbox writes
  must not depend on it.
- Introduce `_supports_claude_channel(host: str) -> bool`. Returns
  `True` for `host in {"claude-code", "claude code"}`. Defaults
  `False`. **No env-var override** — observed-client-name only,
  always lowercased.
- Wrap `write_stream.send(session_message)` in
  `_push_channel_notification` so proprietary
  `notifications/claude/channel` sends only run when the process has
  seen a channel-capable host. For background polls before any tool
  call, default to **not** sending the proprietary notification and
  rely on the durable inbox. This is safe because Phase 1 makes inbox
  delivery unconditional.
- Add a startup banner log: `"channel notifications: enabled
  (host=claude-code)"` vs `"channel notifications: disabled
  (host=copilot); using portable inbox"`.

This is a **no-op** for Claude Code (the host check passes; current
behavior is preserved). For Copilot CLI it stops firing a method the
client cannot route, which is harmless today but makes the Phase 1
inbox the visible path.

### 5.2 Phase 1 — Portable inbox in the same MCP server

The existing `_push_channel_notification` already does **observe →
push**. Phase 1 adds two more "outputs" alongside the existing
proprietary push, all from the same call site, all from the same
process, all reading the same dedup state.

#### 5.2.1 Durable inbox JSONL

Path: `~/.entraclaw/inbox/<YYYY-MM-DD>.jsonl` (rolling daily; matches
existing data-dir layout). Each line is the same shape we already
push over the channel:

```json
{
  "id": "<message_id>",
  "received_at": "2026-04-24T18:21:03Z",
  "channel": "teams" | "email",
  "chat_id": "<graph chat id or 'email'>",
  "from": "the user",
  "ts": "<source ts>",
    "content_text_sanitized": "Hi",
  "meta": { ...same shape as today's notifications/claude/channel meta... },
  "consumed_by": []
}
```

Durability rules:

- Append-only. One write per inbound message. Atomic via
  `os.replace` on a `.tmp` sibling, or `O_APPEND` short writes
  (single-process, fine).
- A separate `~/.entraclaw/inbox/state.json` holds
  `{ "unread": <int>, "last_id": "<id>", "last_seen_consumer":
  {"<consumer_id>": "<id>"} }`.
- **Same sanitization as today's push** (`_summarize_content`).
  Raw Teams HTML never lands on disk — Learning #29 / channel
  discipline.
- **Tokens never written.** Audited by re-using the existing
  `_log_interaction_safe` discipline — inbox writer goes through
  the same observe path so any future redaction rule applies once.

#### 5.2.2 New MCP tool: `inbox_pull`

```python
@mcp.tool()
async def inbox_pull(
    since_id: str = "",
    limit: int = 20,
    mark_consumed: bool = True,
    channel: str = "",  # "" | "teams" | "email"
) -> str:
    """Return inbound messages received while you weren't looking."""
```

Returns a JSON envelope `{"unread_remaining": N, "messages": [...]}`.
This is the portable equivalent of the channel push: the agent
reads the inbox on demand. Default cadence is "every turn the agent
is about to reply on a watched chat" — encoded as a body-prompt rule
(see § 8). Fast — purely local file I/O; no Graph hits, no token
refresh.

#### 5.2.3 Spec-defined logging notification ping

Existing MCP clients may surface the spec-defined logging
notification method, `notifications/message`. On every inbound event
we emit:

```
notifications/message
  level: "info"
  logger: "entraclaw.inbox"
  data: { "channel": "teams", "from": "...", "summary": "...", "unread": <int>,
          "id": "<message_id>" }
```

This is harmless on Claude Code (it ignores arbitrary log loggers),
useful on any client that surfaces server logs (VS Code MCP panel
does), and free to emit. **Not** a substitute for the inbox file —
it's a hint.

#### 5.2.4 MCP resource `entraclaw://inbox`

Register a resource exposing the latest inbox state. Emit
`notifications/resources/updated` on every inbox grow. Per
mcp-close-the-loop.md, no current major client subscribes — this is
a free-now / works-later move that costs ~10 lines of code.

#### 5.2.5 What happens on Claude Code

`_push_channel_notification` keeps firing
`notifications/claude/channel` exactly as today (gated by the Phase
0 host check). The new inbox/log/resource paths fire **in addition**.
Worst case Claude Code sees the same message twice — solved by the
existing dedup (separate background-poll seen-set, Learning #27),
plus an `id`-keyed dedup in `inbox_pull` so a Claude Code agent that
already saw a turn-injection won't re-pull it.

### 5.3 Phase 2 — Optional terminal companion `entraclaw watch`

A separate command in the existing CLI entry point:

```bash
entraclaw watch          # tails ~/.entraclaw/inbox/*.jsonl, prints + toasts
entraclaw watch --quiet  # toast only, no terminal print
entraclaw watch --teams  # only teams channel
```

- Reads the **same** inbox JSONL the MCP server writes. Zero new
  network, zero new auth.
- Prints a single colored line per message: `[teams 18:21] Brandon:
  "Hi there"` plus a hint: `→ tell Copilot: /env or "check inbox"`.
- Optional OS toast via `osascript` (mac), `notify-send` (linux),
  PowerShell `BurntToast` (win). All best-effort.
- Not a daemon, not a service. Run by the user in a side terminal.
  Dies cleanly on Ctrl-C. No lock file beyond a PID hint, no
  installation.

This is the "human can see new mail" loop for Copilot CLI users.
It's **not** an MCP server, so the constraint is preserved.

### 5.4 Phase 3 — Native channel support when a spec lands

When MCP Triggers & Events ships (mcp-close-the-loop.md):

- Add a third branch in `_push_channel_notification`: emit the
  spec-defined event payload alongside the existing proprietary
  push and the inbox write.
- Drop the Phase 0 host-name check once **all** target clients
  support the spec event.
- The inbox JSONL stays as a durable backstop forever — it's
  cheaper than re-debugging silent transport failures
  (Learnings #26, #29, #38, #39).

---

## 6. Implementation tasks (high level, file-by-file)

Architecture, not engineering tickets — exact line counts will fall
out of TDD.

| Phase | Repo file(s) | Change |
|---|---|---|
| 0 | `src/entraclaw/mcp_server.py` | New `_supports_claude_channel(host)`. Wrap the `write_stream.send` call in `_push_channel_notification` behind the check. Startup banner log of detected host. |
| 0 | `tests/test_mcp_server_integration.py` (extend existing channel-push tests) | Unit tests: claude-code host → proprietary push fires; copilot/unknown host → proprietary push suppressed but inbox write still happens. |
| 1 | `src/entraclaw/inbox/__init__.py` (new module) | `Inbox` class — append, read-since, mark-consumed, state-file accessor. Atomic writes. Same sanitization helper as channel push. |
| 1 | `src/entraclaw/mcp_server.py` | Call `Inbox.append(...)` inside `_push_channel_notification` *before* the proprietary send (so observe → durable → push). Add `inbox_pull` tool. Register `entraclaw://inbox` resource + emit `resources/updated`. Emit `notifications/message` log ping. |
| 1 | `prompts/anatomy/channel-discipline.md` | Add "On Copilot CLI / non-channel clients: every turn that interacts with a watched chat, call `inbox_pull` first." Body-rule, non-overridable. |
| 1 | `AGENTS.md` (root + this doc, optional) | Tiny note linking to the rule above so Copilot CLI surfaces it from session start. |
| 1 | `tests/inbox/` (new) | Unit tests for the inbox module (append, read-since, dedup, sanitization). MCP-tool tests via `respx` and an in-memory Inbox. |
| 2 | `src/entraclaw/cli/watch.py` (new) | `entraclaw watch` subcommand. Tail JSONL, format, optional OS toast. |
| 2 | `pyproject.toml` | Add `entraclaw watch` script entry if not already routed through a Click/Typer group. |
| 2 | `docs/runbooks/copilot-cli-runbook.md` (new) | Operator instructions: install, configure, start `entraclaw watch`, body-rule reminder. |
| 3 | `src/entraclaw/mcp_server.py` | Add MCP-spec event emit alongside proprietary push when WG ships. Eventually retire the host gate. |

Dedicated **non-changes** worth calling out:
- `src/entraclaw/tools/teams.py` — unchanged. Polling, sanitization,
  send/read/filter all stay.
- `src/entraclaw/auth/` — unchanged.
- The three-hop token flow — unchanged.
- The interaction log — unchanged. Inbox JSONL is **additional**;
  it does not replace the audit log.

---

## 7. Data / state model

Three independent state surfaces, deliberately kept separate so a
failure in one cannot starve another (Learning #27):

### 7.1 Watched chats (existing)

`<data_dir>/watched_chats.json` → `{ "<chat_id>": { "title": str,
"added_at": ISO8601 } }`. Auto-discovery sweep + `create_chat`
already maintain this. Unchanged.

### 7.2 Background-poll cursor (existing)

In-memory per-chat `{ "last_ts": ISO8601, "seen_ids": set[str],
"bootstrapped": bool }`. **Independent** from `watch_teams_replies`
(Learning #27). Independent **also** from the inbox-consumer cursor
below.

### 7.3 Inbox (new)

Two files in `~/.entraclaw/inbox/`:

```
<YYYY-MM-DD>.jsonl     # append-only, one record per inbound
state.json             # { "unread": int, "last_id": str,
                       #   "last_seen_consumer": { "<consumer>": "<id>" } }
```

Consumers identify themselves by string (`"copilot-cli"`,
`"claude-code"`, `"watch-cli"`). Each consumer carries its own cursor
in `last_seen_consumer` so:
- Claude Code can mark turn-injected messages "consumed" and have
  `inbox_pull` skip them.
- Copilot CLI's `inbox_pull` advances independently.
- `entraclaw watch` advances a third cursor for terminal display.

Dedup at the inbox layer is by `id` (Graph `message_id` for Teams,
`internetMessageId` for email, synthetic UUID for Bot Gateway).

### 7.4 Notification surfaces

| Surface | Direction | Carries | Lossy? |
|---|---|---|---|
| `notifications/claude/channel` | server → Claude Code | Full meta, sanitized content | Yes — best effort |
| Inbox JSONL | server → disk | Full meta, sanitized content, consumer cursors | No — durable |
| `inbox_pull` tool result | server → any MCP client | Same | No |
| `notifications/message` (log) | server → any MCP client | Summary + count | Yes — best effort |
| `entraclaw://inbox` resource updated | server → subscribers | Pointer | Yes — depends on subscription |
| Interaction log | server → blob/local | Audit record (every event) | No — durable, source of truth for daily summary |

The interaction log remains the audit source of truth. The inbox is
the **agent-readable** mirror tuned for low-latency consumption.

---

## 8. Security and privacy

- **Sanitization is non-negotiable.** Every write to the inbox JSONL
  goes through the existing `_summarize_content` and (for inbound
  Teams) the same HTML-escape rules used in the proprietary channel
  push. Raw Teams HTML never lands on disk in the inbox. Quoted
  messages are sanitized recursively (matches today's behavior in
  `_push_channel_notification`).
- **Inbox is local-only by default.** `~/.entraclaw/inbox/` is
  user-mode 0700, files 0600. **No cloud sync.** ADR-005 cloud
  memory does not extend to the inbox — it is operational state, not
  persona memory, and pushing it to blob would expand the
  attack/privacy surface for no benefit (the interaction log is
  already there for auditing).
- **No tokens, no secrets, no PII beyond what Teams/email already
  surface.** Inbox writer reuses the existing log-redaction
  discipline; any field we wouldn't put in `entraclaw.log` we don't
  put in the inbox.
- **`entraclaw watch` does not authenticate, does not network, does
  not handle Graph tokens.** It only reads the local JSONL. Reduces
  blast radius if the watch process is compromised.
- **Dedup state is per-consumer.** A misbehaving client cannot
  "consume" a message out from under another client — they have
  independent cursors (§ 7.3).
- **No regression on the `notifications/message` ping.** Spec-defined
  logging notifications are a documented MCP primitive;
  clients that don't implement them MUST drop them. Our schema is
  intentionally minimal (no HTML, no body) to avoid a Learning #29
  re-run on a different transport.
- **HTML in the inbox stays escaped.** `inbox_pull` returns the
  same sanitized content it stored; the agent must wrap any reply
  in HTML per the existing channel-discipline rule. Body prompt
  unchanged.

---

## 9. Open questions and validation plan

1. **Does Copilot CLI surface `notifications/message`?** Run
   `copilot` with entraclaw, fire a synthetic `notifications/message`,
   inspect `/env` and any debug log. If yes, it is bonus visibility;
   if no, the inbox tool path is sufficient.
2. **Does Copilot CLI honor MCP `resources/subscribe` /
   `notifications/resources/updated`?** Probe by registering
   `entraclaw://inbox` and watching the wire. Likely no today.
3. **Does Copilot CLI's `clientInfo.name` arrive as `copilot` or
   something more specific?** Verify by logging the value during
   first connect. Adjust `_supports_claude_channel` allowlist if a
   Copilot variant ever advertises channel support.
4. **Is there a Copilot CLI hook equivalent to Claude Code's
   PostToolUse?** None documented. Confirm via repo search; if not,
   the body-prompt rule + `inbox_pull` tool is the only nudge
   mechanism.
5. **Does the existing background-poll cadence (5 s Teams, 60 s
   email) still feel right for an `inbox_pull`-on-turn UX?** Likely
   yes — `inbox_pull` is local-disk read, so cadence is unchanged.
   Validate by measuring perceived end-to-end latency on Copilot
   CLI in a live run.
6. **Should `inbox_pull` auto-fire on tool boot?** Tempting, but it
   would silently consume messages without the agent reading them.
   Reject. Use the body-prompt rule instead.
7. **Should `entraclaw watch` ship by default?** No — opt-in. It is
   a UX nicety, not a correctness requirement. Document in the
   runbook.

**Validation plan, prioritized:**

- Phase 0 validation: implement compatibility probing, verify Claude Code unchanged, verify
  Copilot CLI no longer receives the proprietary notification (no
  silent failure).
- Phase 1 validation: implement the inbox, run a soak with both Claude Code
  and Copilot CLI sessions sharing the inbox. Confirm independent
  cursors. Confirm sanitization parity by diffing
  channel-push payloads vs JSONL records.
- Phase 2 validation: implement the watch CLI, confirm OS toast on at least
  macOS + linux.
- Phase 3 validation: revisit the MCP Triggers & Events RFC before
  implementing native event support.

---

## 10. Can this be one running MCP server?

**Yes.** Phases 0 and 1 keep all polling, all auth, all push, all
inbox writes inside the existing `entraclaw-mcp` process. Claude
Code keeps its proprietary turn-injection path; Copilot CLI gets a
durable inbox + a portable tool + a spec-conformant log ping + a
resource update — all from the same process, the same poll loop,
the same dedup state. Phase 2's `entraclaw watch` is **not** an MCP
server; it is a separate user-launched terminal companion that
reads the same JSONL on disk. Phase 3 is also single-process.

The only multi-process scenario in this plan is the optional Phase 2
companion, and it is read-only against a local file. The constraint
"one running MCP server if feasible" is fully preserved.

---

## 11. Change log

- **2026-04-24 — initial draft (Agent 1) + Product review.** Researched current
  Claude Code dependency, Copilot CLI MCP surface, and the
  mcp-close-the-loop landscape. Recommended phased portable inbox.
