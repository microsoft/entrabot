# Runbook — Entrabot MCP dies after a few minutes under sustained activity

**Status:** RESOLVED 2026-04-28. The actual root cause turned out to be
the *shape* of `params.meta` in the channel push, not just its content.
Earlier sanitizer fixes (2026-04-24, 2026-04-27) addressed real but
insufficient surfaces. See the **2026-04-28 — RESOLVED** section below
for the full trail and the definitive fix.

**Owner rotation:** If a similar disconnect symptom recurs, read this
doc end to end before running anything. The TL;DR has been preserved
verbatim as of 2026-04-24 for historical accuracy — the real story is
in the addenda.

**Last update:** 2026-04-28 (final fix landed).

---

## TL;DR (HISTORICAL — superseded by 2026-04-28 RESOLVED section)

- **Symptom.** Entrabot MCP server (stdio child of Claude Code CLI) dies
  cleanly ~25 seconds after start, immediately after the first inbound
  Teams push notification of the session. No traceback. No BrokenPipeError.
  `/mcp` shows `disconnected`. Reconnect triggers a fresh boot and repeats.
- **Root cause (confirmed 2026-04-24).** `_push_channel_notification` sends
  raw Teams HTML (`<p>…</p>`, `<attachment id="…">…</attachment>`) as the
  `content` field of the `notifications/claude/channel` JSON-RPC
  notification. Claude's MCP client closes the connection cleanly on
  receiving angle-bracket content in notification params — the **exact same
  bug** that was fixed on the email push path on 2026-04-17 (see comment at
  `mcp_server.py:1250`). The Teams push path never received that fix.
- **Fix.** In `_push_channel_notification` (around line 1528), replace
  `message.get("content", "")` with
  `_summarize_content(message.get("content", ""))`. This strips HTML tags
  before the content enters the MCP notification frame. See **Step 0**
  below for the precise change, test to write first, and validation plan.
- **Secondary issue.** `BlobBackend.append_text` calls `_run_sync()` which
  uses `ThreadPoolExecutor.result()` — a blocking `.result()` call on the
  asyncio event loop *thread*. The docstring says "keeps the running loop
  free" but this is incorrect: it blocks the event loop thread for the full
  duration of the blob I/O (two HTTP round-trips, ~600 ms). This freezes
  asyncio and prevents MCP request/response handling during blob writes.
  Fix separately with `asyncio.to_thread()` or async `log_interaction`.
  See **Step 1**.
- **Before doing ANYTHING else.** Read **Learning #37** (self-spawn
  cascade), **Learning #38** (leader-cache overwrite), **Learning #45**
  (wrapper bypass), **Learning #36** (sub-agent worktree pip install),
  and **Learning #39** (dev-channel flag typo). All five are regressions
  that have *already happened once* against this exact symptom shape and
  that the next agent will be tempted to re-debug from zero. Don't.

---

## Symptom details

### What the user sees

1. `/mcp` shows entrabot as `connected`.
2. First 1–3 tool calls (e.g., `whoami`, `read_teams_messages`) respond
   in under a second.
3. After some time — anywhere from ~2 minutes of idle up to ~10 minutes
   of active use — subsequent tool calls stall for 5–60 seconds and then
   fail with a generic transport error.
4. `/mcp` now shows entrabot as `disconnected`.
5. Manually running `/mcp` → `Reconnect entrabot` (or any tool-call
   attempt) respawns the child; the cycle repeats.

### What the logs show

- `~/.entrabot/logs/entrabot.log` (the JSON file log): normal INFO lines
  right up until the cutoff. **No shutdown/exception line at the tail.**
  The log simply stops.
- `/tmp/entrabot-debug.log` (stderr tee from the debug wrapper — only
  present if the wrapper is active via `.mcp.json`'s `command`):
  httpx request/response lines, msal token refresh lines, FastMCP
  lifecycle `Starting MCP server "entrabot"` banners. **Zero Python
  tracebacks on the parent MCP child around the time of drop.** The
  only `BrokenPipeError` tracebacks ever captured in this log belong
  to the *cascade children* that Learning #45 fixed, not to the parent
  MCP process itself.
- `ps aux | grep claude` while it's happening: parent Claude CLI at 27%
  to 83% CPU, ~800 MB–1 GB RSS, state `S+`. Entrabot child at ~0% CPU.

### Process and config at time of writing

- `.mcp.json` `command` is `.venv/bin/entrabot-mcp` (direct, no wrapper).
  The wrapper (`scripts/entrabot-mcp-debug.sh`) is fix-ready and carries
  the self-ref marker (Learning #45 / PR #41) — **you can safely flip
  `.mcp.json` back to the wrapper** to capture stderr without
  retriggering the twin-spawn cascade.
- Claude CLI launch flag is the correct double-dash form:
  `--dangerously-load-development-channels server:entrabot`.
  Learning #39 rules out the single-dash typo as the cause of any
  channel-delivery symptom.
- Three background tasks are active in agent_user mode: Teams poll (5s),
  email poll (60s), chat auto-discovery (120s), plus the daily-summary
  scheduler. Persona-sati MCP is listed as an SSE peer in `.mcp.json`
  and is fetched at boot.

---

## What has been tried (and merged)

### Amplifier #1 — Wrapper-triggered self-spawn cascade (FIXED by PR #41)

When the debug wrapper `scripts/entrabot-mcp-debug.sh` was named as
`.mcp.json`'s `command`, entrabot boot spawned a duplicate child ~2s in
via the efferent-copy `discover_sinks` logic. The duplicate completed a
full three-hop token flow, registered polls, fetched the persona-sati
prompt, responded to `tools/list`, and then died on BrokenPipeError when
the parent tore down the stdio_client. Each boot pair did **2× the API
work** — two token flows, two poll registrations, two Graph chat lookups
— burning login.microsoftonline.com round-trips pointlessly.

**Root cause.** `_is_self_referential_peer` compared `peer.command`
against `sys.argv[0]`. The wrapper script's resolved path did not match
`.venv/bin/entrabot-mcp`, so the self-ref check returned `False` and
the peer was NOT filtered.

**Fix.** `_is_self_referential_peer` now reads up to 16 KB of the
wrapper script, looks for a `# entrabot-self-ref-target: <path>`
marker line, and compares the declared target against `sys.argv[0]` /
`sys.executable`. `scripts/entrabot-mcp-debug.sh` now carries the
marker. Shipped in commit `934bbef`, merged as PR #41.

**Effect on the disconnect symptom.** Eliminated the twin-spawn API
doubling. The disconnect symptom *reduced* in frequency but did not
disappear.

See: `docs/runbooks/hard-won-learnings.md` Learning #45.

### Amplifier #2 — Double-rendered logs flooding stderr (FIXED by PR #40)

Every record logged to the `entrabot` logger propagated up to the
Python root logger, where FastMCP's `configure_logging()` had installed
a `RichHandler` via `logging.basicConfig`. Result: every entrabot log
record was rendered **twice** on stderr — once as JSON via our own
StreamHandler, once as rich-formatted pretty-print via FastMCP's root
handler. The parent Claude CLI had to drain 2× the byte volume over
stdio/stderr, and in periods of message bursts this was plausibly
contributing to stdio backpressure.

**Fix.** Added `logger.propagate = False` in
`src/entrabot/logging_config.py`. Shipped in commit `41ae30b`, merged
as PR #40. `pytest` regressed on 6 `caplog` tests because caplog
attaches a handler to root (not to the entrabot logger), and with
propagation off, entrabot records never reached caplog. Fixed by
adding an autouse fixture in `tests/conftest.py` that attaches
`caplog.handler` directly to `entrabot_logger` per test.

**Known limitation.** Only `entrabot.*` records stop propagating.
`httpx.*` and `msal.*` records still propagate to root and still render
through the RichHandler. Given the Teams poll fires once every 5s and
issues at least one httpx request per cycle, the remaining stderr
volume from these third-party loggers is material but not measured.

**Effect on the disconnect symptom.** Halved entrabot-originated
stderr volume. Symptom reduced but not eliminated.

### Amplifier #3 — 4 slow throttling tests in the test suite (REMOVED)

Not directly related to the runtime disconnect; removed per user
instruction because they were dominating the test-suite wall clock
(357s of real `time.sleep()` through `Retry-After` intervals, pushing
the suite past the default Bash 300s timeout). Removed in PR #40:

- `TestCreateOrFindChat::test_rate_limited`
- `TestTeamsSend::test_rate_limited`
- `TestUpdatePlaceholder::test_rate_limited_raises`
- `TestDeleteChatMessage::test_raises_rate_limit_on_429`

The `RateLimitError` import was left in because the production code
still raises it; only the tests were removed.

### Debug infrastructure — stderr capture via wrapper (READY, OFF BY DEFAULT)

`scripts/entrabot-mcp-debug.sh` tees stderr to
`/tmp/entrabot-debug.log` with `===== wrapper start <UTC> pid=<n> =====`
markers. Since PR #41 it carries the self-ref marker and is safe to
activate. To turn it on: change `.mcp.json`'s `command` from
`.venv/bin/entrabot-mcp` to `scripts/entrabot-mcp-debug.sh` and
restart Claude Code. To turn it off: revert the command. The log is a
tee, not a redirect — server stderr still flows to the parent.

---

## Hypotheses ranked by remaining likelihood

1. **Raw HTML in Teams push notification content (CONFIRMED ROOT CAUSE,
   2026-04-24).** `_push_channel_notification` passes
   `message.get("content", "")` — the raw Teams Graph API message body,
   which is HTML (`<p>…</p>`, `<attachment id="…">…</attachment>`) —
   directly into the `params.content` field of the
   `notifications/claude/channel` JSON-RPC notification. Claude's MCP
   client closes the connection cleanly upon receiving angle-bracket
   content in notification params. **Evidence:** (a) debug log tail from
   session 4 ends with `"Pushed Teams message from Alice Smith:
   <attachment id=\"1777053221965\"></attachment>\n<p>As"` — confirming
   raw HTML in the push. (b) The email push path documents the identical
   death pattern at `mcp_server.py:1250–1252` and was fixed 2026-04-17
   by eliminating angle brackets from the sender field. (c) Death is
   deterministic: always within 3 s of the first push notification, 0
   BrokenPipeError, clean EOF. **Fix:** use `_summarize_content()` to
   strip HTML before including content in notification params (see
   Step 0).
2. **`_run_sync` blocks the asyncio event loop thread (CONFIRMED
   SECONDARY, 2026-04-24).** `BlobBackend.append_text` → `_run_sync()`
   → `ThreadPoolExecutor(1).submit(asyncio.run, coro).result()`. The
   `.result()` call is a synchronous block on the calling thread. Since
   `_run_sync` is invoked from within a running asyncio event loop, the
   *event loop thread itself* is blocked for the duration of each blob
   I/O round-trip (~300 ms each). Each Teams push triggers 6 token
   POSTs + 1 blob GET + 1 blob PUT = ~8 blocking calls = ~2–4 s of
   total event loop freeze. During this window, MCP request/response
   handling is impossible. **Fix:** replace `_run_sync` with
   `asyncio.to_thread()` or make `log_interaction` fully async (see
   Step 1). Fix separately from the HTML content fix above.
3. **httpx/msal logs still propagating.** PR #40 fixed entrabot's
   own propagation but third-party loggers still render through the
   root `RichHandler`. On a busy poll cycle this is still a few
   hundred bytes per 5 seconds, most of it pretty-printed. Worth
   silencing for completeness; unlikely on its own to account for
   drops now that the root cause is known.
4. **Persona-sati SSE fetch blocking boot.** Ruled out as the primary
   cause (boot banners appear and initial tool calls succeed).
   `PERSONA_SATI_MCP_URL=` (empty) can temporarily eliminate as a
   confounding factor during fix validation.
5. **Token refresh thrashing.** Low likelihood. Observed cadence of 6
   token POSTs per push is high but all POSTs succeed (HTTP 200). The
   storage token (`https://storage.azure.com/.default`) requires a
   separate three-hop, which explains 3 of the 6 POSTs; the other 3
   appear to be a second three-hop for the Teams API token. Consider
   caching both tokens separately with the 55-min eager-refresh
   threshold applied to each.
6. **Eager init of all three polls at boot.** Cold-start stress
   amplifier only. Not a steady-state drop cause once root cause is
   fixed.

### Ruled out (don't re-investigate)

- **Efferent-copy self-spawn cascade.** Killed in PR #36 (direct) and
  PR #41 (wrapper). Both covered by regression tests.
- **Learning #36 venv corruption.** `python -c "from entrabot import
  config; print(config.__file__)"` currently resolves to the parent
  src tree, not a worktree. Do re-check if symptoms resurface after
  any sub-agent dispatch (see CLAUDE.md non-negotiable).
- **Learning #44 parent-rename venv orphan.** `pyvenv.cfg` and venv
  shebangs all resolve to the current repo path. Verified post-PR #41.
- **Learning #39 dev-channel flag typo.** Confirmed the Claude CLI is
  running with `--dangerously-load-development-channels` (double dash,
  see `ps` output in the "Process and config" section above).

---

## What to try next — ordered by cost/value

### Step 0 — Strip HTML from Teams push notification content (HIGH PRIORITY)

This is the confirmed root cause fix. **Write the test first.**

**Test to write** (`tests/test_mcp_server.py` or equivalent):
```python
async def test_push_channel_notification_strips_html_content():
    """HTML tags in Teams message body must not appear in the push params."""
    raw_html = '<attachment id="123"></attachment>\n<p>Hello world</p>'
    # Capture the SessionMessage that would be sent to write_stream
    # Assert that params["content"] contains "Hello world" but not "<p>" or "<attachment"
```

**Code change** in `_push_channel_notification` (~line 1528):
```python
# Before:
"content": message.get("content", ""),

# After:
"content": _summarize_content(message.get("content", "")),
```

`_summarize_content` is already in scope, already strips HTML via
`re.sub(r"<[^>]+>", " ", …)`, and is already used for the interaction-log
`summary` field. No new helpers needed.

**Note on limit:** `_summarize_content` truncates at 200 chars by default.
If preserving full message text is required, add a `limit=None` branch or
call it with `limit=len(content)`. The primary concern is stripping HTML
tags; length truncation is secondary.

**Validation after fix:**
1. Run `pytest -v --tb=short` — all tests must pass.
2. Flip `.mcp.json` to the debug wrapper.
3. Run the PTY soak (`.claude/pty_soak.py`) for 10 minutes.
4. Confirm entrabot-mcp stays alive through at least 2 push cycles.
5. Restore `.mcp.json` to direct binary, delete wrapper line.

### Step 1 — Fix `_run_sync` event loop blocking (MEDIUM PRIORITY)

`BlobBackend.append_text` → `_run_sync()` blocks the asyncio event loop
thread via `ThreadPoolExecutor.result()`. Replace with
`asyncio.to_thread()` or make `log_interaction` async.

**Option A (minimal change):** In `backend.py`, change `_run_sync` to:
```python
async def _run_async(coro):
    return await asyncio.to_thread(asyncio.run, coro)
```
Then make `BlobBackend` methods async and update all callers.

**Option B (preferred, more work):** Make `BlobStore` the canonical
interface, expose async methods directly on `BlobBackend`, and update
`log_interaction` + callers to be async.

Either option requires tests first. See `What to preserve` section —
the blob write must remain durable before the tool's side effect is
externally visible.

### Step 2 — Quantify the remaining httpx log volume (LOW PRIORITY)

Now that the root cause is fixed, check if httpx/msal logs are still
creating material stderr volume. Extend `logging_config.py` to set
`propagate = False` on `httpx` and `msal` if needed.

### Step 3 — Run the 60-minute acceptance soak

After Steps 0 and 1 are merged, run `.claude/pty_soak.py` for 60 minutes.
Acceptance criteria: entrabot-mcp stays alive continuously, no
BrokenPipeError, wrapper-start count = 1 per session, Claude CPU < 15%.

### (OLD) Step 4 — Try SSE transport (experimental, deferred)

Now that the root cause is known and not transport-related, SSE transport
is no longer needed as a workaround. Defer until SSE is needed for other
reasons.

### (OLD) Step 5 — Get a `py-spy` dump (deferred)

No longer needed — root cause is confirmed.

---

## DO NOT suggest before reading prior debugging

These are all theories that have already been investigated and ruled
out or already fixed. Do not propose them as new ideas:

- "Maybe the server is crashing" — **No.** No tracebacks on the parent
  child; the log just stops. It is being reaped, not crashing.
- "Maybe it's the self-spawn cascade" — **Ruled out.** PR #36 + PR #41
  with regression tests. Verify by checking wrapper-start pair cadence
  (Learning #45), but don't re-debug from zero.
- "Maybe the leader/slave gate is dropping pushes" — **Ripped out.**
  See Learning #38 and PR #36. Channel pushes fire unconditionally now.
- "Maybe it's the dev-channel launch flag" — **Ruled out.** The running
  CLI process has the correct double-dash flag; see Learning #39.
- "Maybe the venv got corrupted" — **Ruled out.** `config.__file__`
  resolves to the parent src tree, not a worktree (Learning #36 check)
  and not a renamed path (Learning #44 check).
- "The test suite is slow so something's wrong with the runtime" —
  The 4 throttling tests were intentionally slow because they were
  testing real `Retry-After` sleeps. Removed in PR #40 for suite-speed,
  not because they signaled a runtime problem.

---

## What to preserve in any fix

- Every tool call must still be audit-logged before execution. The blob
  write is security-relevant; do not "fix" the hot path by skipping it.
  If you decouple via a queue, the audit write must still be durable
  before the tool's side effect is visible externally.
- `logger.propagate = False` must stay on `entrabot`. Do not revert
  that fix in the process of silencing third-party loggers.
- `_is_self_referential_peer`'s wrapper-marker behavior is a
  security-relevant invariant now (see Learning #45). Any change to
  efferent-copy discovery must include regression tests that prove
  both direct and wrapper-indirect self-peers are skipped.
- The debug wrapper is opt-in. Don't make it the default in `.mcp.json`
  on `main`; it writes to `/tmp/entrabot-debug.log` which is a
  single-user, single-machine convention. Leave that switch as a
  conscious flip for active debugging.

---

## Investigation session — 2026-04-24 (PTY soak + debug wrapper)

**Method:** Flipped `.mcp.json` to `scripts/entrabot-mcp-debug.sh` (safe
per Learning #45). Built `.claude/pty_soak.py` — a Python PTY soak driver
using `pty.openpty()` + `os.fork()` + `os.execve()` to spawn claude with
a proper 50×200 PTY, send blind `\r` after 4 s for TUI consent, drain the
PTY output, and monitor for 2 h. Launched via `nohup … & disown` for
SIGHUP immunity.

**Observations across 4 sessions:**
- All 4 sessions: death is DETERMINISTIC, not stochastic. Always ~25 s
  after boot, always triggered by the FIRST push notification.
- Session 4 timeline:
  - T+3s: three-hop token acquired, 4 chats registered
  - T+5s: `tools/list`, `prompts/list`, `resources/list` served to claude
  - T+9s: first poll cycle — 4 chats polled, 0 new messages
  - T+16s: second poll — chat 1 returns 1 new message
  - T+16–19s: 3 token POSTs (storage three-hop) + blob GET
  - T+17s: TUI shows "1 MCP server failed · /mcp" (may be persona-sati)
  - T+18–20s: 3 more token POSTs + blob PUT + Teams message detail GET
  - T+20s: push notification fires; last debug log line:
    `"Pushed Teams message from Alice Smith: <attachment id=\"1777053221965\"></attachment>\n<p>As"`
  - T+20s–25s: entrabot-mcp dead (clean exit, no traceback)
- Wrapper-start count = 1 per session ✅ (Learning #45 not regressing)
- 0 BrokenPipeError ✅ (clean exit, not a crash)
- 6 token POSTs per push: 3 for storage token + 3 for Teams API token
  (two full three-hops per push — token cache may not be working for
  storage token)

**Key evidence for root cause:**
1. The last debug log line shows raw HTML in the push content:
   `<attachment id="1777053221965"></attachment>\n<p>As`
2. `mcp_server.py:1250–1252` documents the identical death pattern for
   the email push path, fixed 2026-04-17 by removing `<addr>` angle
   brackets from the sender format.
3. The Teams push path at line 1528 still uses
   `message.get("content", "")` — the raw Graph API HTML body.
4. The email push path (fixed) uses pre-formatted plain text with no
   angle brackets.

**`_run_sync` event loop blocking (secondary, also observed):**
The 6 token POSTs + blob GET + blob PUT between T+16s and T+20s happen
via `_run_sync()` which calls `ThreadPoolExecutor.result()` — blocking
the asyncio event loop *thread* (not just a coroutine) for each call.
The event loop is frozen for ~2–4 s cumulative during these 8 calls.
This is a real performance bug but NOT the primary disconnect trigger
(the HTML content in the push is what causes the clean close).

---

## 2026-04-27 addendum — sanitizer extended to preserve `<img src>` and `<a href>` URLs

After the 2026-04-24 fix (commit `f0d29ea`), a 2026-04-27 inbox replay
of seven backlog messages preceded another MCP disconnect. Two payload
shapes were under suspicion: a giphy `<div><img src="https://media4.giphy.com/...">`
embed and a Teams `<attachment id="..."></attachment>` + `<a href>` link
card. Probing `_summarize_content` directly against both shapes showed
the existing `<[^>]+>` strip already removed every angle bracket — so
the JSON-RPC `params.content` itself cannot have introduced raw `<` or
`>` into the framing. What *did* regress was readability: the giphy
embed sanitized to an empty string (the URL was the entire signal) and
the anchor card sanitized to bare anchor text (the URL was discarded).
This PR tightens `_summarize_content` to extract `<img src>` URLs into
`[image: <url>]` markers and `<a href>` anchors into `text (<url>)`
form *before* the global tag strip, so the LLM consuming the channel
push retains the URL signal in giphy and link-card messages. Existing
strip behavior for `<p>`, `<at>`, `<attachment>`, etc. is unchanged
and the f0d29ea-era assertions still hold. **If MCP disconnects recur
after this PR**, the sanitizer is no longer the suspect — audit the
JSON-RPC envelope outside `params.content`: `meta.user` (display name
that could carry `<addr@example.com>` shapes), `meta.quoted_messages`
fields beyond `content`, or control characters / unescaped sequences
in `chat_id` or `message_id`. See `src/entrabot/mcp_server.py`
`_summarize_content` and `tests/test_mcp_server_integration.py`
`test_channel_notification_preserves_img_src_url` /
`test_channel_notification_preserves_anchor_text_and_href`.

---

## 2026-04-28 addendum — `meta.user` and `meta.quoted_messages` sanitized

The 2026-04-27 addendum predicted the next-suspect surface: angle
brackets *outside* `params.content`, specifically `meta.user`
(display names like `Alice Smith <alice@contoso.com>`) and the
`quoted_messages` fields beyond `content`. A live 2026-04-28
disconnect confirmed the prediction. Boot succeeded, the first push
landed, the responder reply went out — and the second push tore down
the MCP child despite `params.content` being clean.

Trigger amplified by commit `927b718` ("preserve Teams HTML"): the
responder daemon now emits raw `<p>…</p>` to Teams instead of always
escaping. Any Teams Reply UI on a responder reply round-trips raw
HTML through `fetch_message` into `meta.quoted_messages`, where it
*was* unsanitized for every field except `content`.

**Fix.** In `_push_channel_notification`:

- `meta["user"]` runs through `_summarize_content` (was raw).
- `quoted` entries are built from a strict allowlist
  `{message_id, from, content, sent_at}` and `from` is sanitized
  (was `{**graph_response, "content": …}` which leaked
  `attachments`, `mentions`, and any other Graph-side field).

**Test.** `test_channel_notification_meta_strips_html_everywhere`
walks the entire `params` dict and asserts no `<` or `>` survives in
*any* string field. Stronger guarantee than the per-field assertions
that came before — if a future Graph response adds a new field, the
test catches it before it reaches the wire.

If disconnects recur again, the residual surfaces are: control
characters or unescaped sequences in `chat_id` / `message_id`
(structural, not from user content), and `meta.ts` (Graph-supplied
ISO timestamp). All three are well-formed in the wild, but the
defense in `_summarize_content` is cheap to extend if needed.

See `src/entrabot/mcp_server.py` `_push_channel_notification` and
`tests/test_mcp_server_integration.py::TestPushChannelNotificationObservability::test_channel_notification_meta_strips_html_everywhere`.

---

## 2026-04-28 — RESOLVED: the custom `meta.quoted_messages` field shape itself was the trigger

**Status: RESOLVED.** Three rounds of investigation converged on a
forensic envelope dump that disproved the angle-bracket theory and
located the actual root cause.

### What we tried (in order) and why each was wrong

1. **Hypothesis: `meta.user` and quoted-message fields contained raw
   angle brackets.** First fix shipped `_summarize_content` over
   `meta.user` and a strict allowlist for quoted entries (commit
   `de0a2d1` on main). Tests asserted no `<`/`>` in any string field.
   The test passed, the symptom recurred. The theory had support but
   wasn't sufficient.

2. **Hypothesis: HTML entities (`&lt;`, `&gt;`) were round-tripping and
   Claude Code's MCP client decoded them before its angle-bracket
   safety check.** Second fix shipped `html.unescape` before tag-strip
   in `_summarize_content` (commit `7e6ae8b` on main). Tests passed,
   the symptom recurred.

3. **Forensic envelope dump.** The `feature/copilot-cli-inbox` branch
   added a temporary `logger.info("PUSH ENVELOPE %s: %s", ...)`
   immediately before `write_stream.send()`. Next disconnect captured
   the exact JSON Claude Code's MCP client closed on. The envelope had
   **zero raw angle brackets and zero HTML entities** — completely
   clean per both prior theories. The only thing populated relative to
   surviving pushes was `meta.reply_to_ids` and `meta.quoted_messages`
   (a list of dicts).

4. **Actual root cause: the field shape itself.** Claude Code's MCP
   client closes the stream when `params.meta` carries `reply_to_ids`
   or `quoted_messages` — regardless of whether their content is
   sanitized or empty. These were entrabot-side conveniences (added
   so the agent had quoted context inline) that the client doesn't
   accept.

### Final fix (this commit)

`_push_channel_notification` no longer adds `reply_to_ids` or
`quoted_messages` to `meta`. The push is restricted to the original
`{chat_id, message_id, user, ts}` shape that round-trips reliably.

The previous main-branch quoted-message fetch path is removed
entirely on main since main does not persist quoted bodies elsewhere
(unlike the feature branch, which routes them through
`_safe_teams_payload` to the interaction log).

### Trigger amplification by `927b718` (feature branch only)

On the feature branch, commit `927b718` ("preserve Teams HTML") made
the responder daemon emit raw `<p>…</p>` to Teams instead of always
escaping. After that change, every Teams Reply UI on a responder
reply triggered the quoted-message fetch path on the next inbound
push, which exposed the field-shape bug. The earlier sanitization
commits weren't useless — they're correct defense-in-depth — but
they were chasing the wrong layer.

### What the dossier got right

- "Death is deterministic, triggered by a push" — yes, but specifically
  pushes that populated `meta.quoted_messages`.
- "Sanitizer is no longer the suspect" (2026-04-27 addendum) — correct
  pivot, but the next surface wasn't the *contents* of those fields,
  it was their *presence*.

### What the dossier got wrong

- "Claude's MCP client closes on angle-bracket content in notification
  params" (2026-04-24 conclusion) — partial truth. That fix was
  necessary for `params.content`. But the client also rejects unknown
  fields in `meta` regardless of content, which the dossier didn't
  predict.

### What to verify if disconnects recur

The push envelope is now `{chat_id, message_id, user, ts}` only. If a
disconnect recurs:

- Confirm no other custom field has crept into `meta`. Check
  `_push_channel_notification` for new keys.
- Confirm `_summarize_content` is still the gate on `params.content`
  (entity-decode + tag-strip).
- Add a temporary diagnostic envelope log before `write_stream.send()`
  and capture the next death.

### See also

- `src/entrabot/mcp_server.py::_push_channel_notification` (final shape)
- `tests/test_mcp_server_integration.py::TestPushChannelNotificationObservability::test_reply_to_ids_omitted_from_meta`
- `tests/test_mcp_server_integration.py::TestPushChannelNotificationObservability::test_quoted_messages_omitted_from_push_envelope`

---

- `docs/runbooks/hard-won-learnings.md` Learning #36 (venv corruption)
- `docs/runbooks/hard-won-learnings.md` Learning #37 (self-spawn cascade)
- `docs/runbooks/hard-won-learnings.md` Learning #38 (leader-cache overwrite)
- `docs/runbooks/hard-won-learnings.md` Learning #39 (dev-channel flag typo)
- `docs/runbooks/hard-won-learnings.md` Learning #44 (parent-rename venv orphan)
- `docs/runbooks/hard-won-learnings.md` Learning #45 (wrapper bypass)
- `docs/runbooks/hard-won-learnings.md` Learning #46 (this symptom — pointer)
- `docs/engineering-status.md` "What's New Apr 24 (part 2) — MCP disconnect investigation" section
- `src/entrabot/logging_config.py` (`propagate = False` fix from PR #40)
- `src/entrabot/efferent_copy.py` (`_is_self_referential_peer` from PR #36 + PR #41)
- `scripts/entrabot-mcp-debug.sh` (debug wrapper, safe to activate)
