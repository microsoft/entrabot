# Entraclaw Identity Research — Engineering Summary

**Date:** April 29, 2026
**Team:** the user
**Status:** v1 released. Three auth modes working (Agent User / Delegated / Bot Gateway). Progressive identity state machine. **791 tests** across the suite. MCP tools + 4 background tasks (Teams 5s / email 60s / chat-discovery 120s / daily summary 5pm PDT). Multi-tenant lightweight chat shipped. **Mind-body split complete** — body-first prompt architecture loads locally, persona-sati MCP wired for personality/memory when configured. ADR-005 cloud-memory Phases 1, 2, 5, 6a shipped; blob-hosted operational storage is opt-in via `setup.sh --use-cloud-memory`. Efferent-copy middleware shipped and immediately hot-fixed for self-spawn cascade (PRs #35/#36), then hardened again against wrapper indirection (PR #41), and is now opt-in (`EFFERENT_COPY_ENABLE=1`) so normal MCP runs do not mirror every tool call. Leader/slave gating ripped out per "one stdio client per process" reality. **Windows port (PR #58) acceptance-tested on ARM64 Windows 11 VM.** Full CNG signing via TPM-backed cert, three-hop flow live against Entra, Copilot CLI MCP registration, Teams DM round-trip confirmed. `send_teams_message` auto-wait merged — non-Claude-Code hosts block inline until sponsor replies (deterministic, not model-dependent). See Learning #54/#55.

---

## Known Issues (Open)

### `add_file_comment` is non-functional for Word — `/beta/drives/{id}/items/{id}/comments` returns 404

**Status:** Work IQ Word implementation exists on `a365-upgrade`; live setup
and smoke passed on 2026-05-15. Graph beta `/comments` is no longer
considered the Word-comment reply path.
**Impact:** Any production use of `add_file_comment` against a `.docx` file 404s with `itemNotFound`. The tool ships against an endpoint that does not expose Word document comments at all — confirmed live 2026-05-04 against the agent's own ODB doc, in multiple URL variants (drives-direct, v1.0, /me/drive, sites→drives), with both POST and GET, both with and without an actual comment in the doc body. The endpoint family appears to be SharePoint list-item metadata comments, not document-content comments. For `.xlsx` the existing code targets the wrong path entirely (correct surface is `/workbook/comments`).
**Why it shipped:** All `TestAddFileComment` tests are respx-mocked; nothing exercised the live endpoint. PR1 eng-review note "Microsoft's beta surface uses one path for both Word and Excel" was incorrect.
**Pivot direction:** Microsoft Agent 365's Work IQ Word MCP server (`mcp_WordServer`) exposes the right primitives (`WordCreateNewComment`, `WordReplyToComment`, `WordGetDocumentContent`, `WordCreateNewDocument`). Auth model uses Entra Agent ID — same identity primitive entraclaw already implements. The branch adds an `entraclaw.a365` provider, Word adapter, ODSP metadata adapter, and MCP wrappers. Live validation created a Word document, read it back, resolved ODSP metadata, added a Word comment, and replied to that comment as the Agent User.
**Reference:** Learning #60 in `docs/runbooks/hard-won-learnings.md`.

### CLI commitment-language detection — unenforced

**Status:** Open by design. Server-side commitment-language detection
ships today as part of the `send_teams_message` outbound-discipline
hooks, but only fires on outbound Teams text — the MCP server never
sees CLI/host-terminal text. Commitments uttered to the operator in
the host terminal ("I'll batch this up later", "at the next pause")
do not become durable `add_promise` records and silently drift.

**Why this matters.** Significant design conversation happens in CLI,
not Teams — meaning many real commitments today are CLI-only.
`prompts/anatomy/channel-discipline.md` already requires
`add_promise` on commitment language regardless of channel, but the
rule drifts because there is no mechanical enforcement on the CLI
side. The lapse on 2026-05-04 (the lapse that motivated this PR) was
exactly CLI commitment without `add_promise`.

**Trade-off accepted in this PR.** Server-side enforcement is
host-portable (works on Claude Code, Copilot CLI, Codex, Cursor, any
direct MCP client) and storage-portable (works on `LocalBackend` and
`BlobBackend` alike). CLI enforcement would require harness coupling
— a `Stop` hook in Claude Code that reads the transcript — which
breaks portability and only covers one host. We chose host-portable
coverage of the Teams path over Claude-Code-only coverage of both
paths.

**Mitigations considered.**
- **`Stop` hook reading the transcript.** Rejected for now — couples
  enforcement to one host, and the same gap reopens on every other
  host that doesn't have an equivalent. Worth revisiting if Claude
  Code becomes the dominant host long-term.
- **Body-prompt strengthening.** In flight — adding a TL;DR
  checklist to `prompts/anatomy/channel-discipline.md` so the
  commitment-on-CLI rule is more salient at every turn.
- **Per-host shims.** Deferred. Each host gets its own
  Stop-equivalent enforcement script. Duplication, but each script
  is cheap.

**Cross-reference.** See `scripts/hooks/README.md` "Known coverage
gaps" for the gate-by-gate registry, including this gap.

### Persona-sati 12h MCP refresh bug — PR #47 paused at architectural constraint

**Status:** Open. Draft PR `persona-sati#47` (branch `feature/entra-delegated-oauth`).

**Impact:** Every ~12 hours, Claude Code's cached MCP bearer expires and persona-sati tools start returning Zod schema errors until the user restarts the MCP session. Affects every entraclaw session that connects to persona-sati after the 12h boundary.

**What's implemented in PR #47 (550/550 tests pass, ruff clean):**

- `OAuthConfig` dataclass with env-var plumbing (`PERSONA_SATI_AUTH_SCOPE`).
- Entra OIDC discovery shim at `/.well-known/oauth-authorization-server-entra` injecting MCP-spec-required fields Entra v2.0 omits (`code_challenge_methods_supported`, `grant_types_supported`).
- PRM advertises the shim as `authorization_servers` when delegation is configured; legacy fallback when not.
- Persona-sati's own AS metadata is now MCP-schema-compliant with safe stubs pointing at the `/authorize` tombstone.
- `provision_identity.py` updated to declare future fresh-tenant scopes as `User`-consentable.

**What's blocked:** the live OAuth flow at the 12h boundary. Microsoft's **Agent Blueprint** app type — which the Persona-Sati Blueprint (`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`) uses — cannot have public-client redirect URIs and cannot be flipped to fallback-public-client mode. So the Blueprint cannot be the `client_id` in a browser-based PKCE auth-code flow against Entra. Without the OAuth client role, Claude Code's MCP client passes Zod validation but then fails at `/authorize` with `AADSTS70001: Application not found`. **User-visible behavior at 12h is unchanged** (just a different error string).

**Live tenant state:** REVERTED to original by `az rest --method PATCH` after testing the constraint. yourtenant.onmicrosoft.com's `access` scope is back to `type: Admin`, redirect URI lists empty. No behavioral change for any existing flow (cert-based three-hop, headersHelper, OBO).

**Possible resolutions:**

1. **Phase 2A — separate "Persona-Sati MCP Client" Entra app reg** (public-client type, not Agent Blueprint). Plumbed through persona-sati's DCR shim (`/register`) so dynamic-client-registration from Claude Code resolves to a real Entra app reg. Estimated 2–3 commits + one `az ad app create` Brandon runs.
2. **Phase 2B — persona-sati implements OAuth 2.1 endpoints itself** (real `/authorize` + `/token` + PKCE state management), brokering to Entra on the backend via the existing FIC three-hop. Larger surgery; buys independence from Entra's DCR limitation.
3. **Land PR #47 as Phase 1 only** — discovery surface improvements without the OAuth client app. Honest about scope; doesn't actually close the user-visible 12h bug. File Phase 2A as a follow-up.

Decision pending.

**Why this wasn't caught in design:** the Agent Blueprint constraint (post-GA May 1, 2026) was not in `docs/platform-learnings/msal-entra-agent-ids.md` (last updated 2025-07-14) at the time of PR design. Planning missed it; the Codex consult that informed the design was reasoning from RFC 8414 + MCP spec, not from Microsoft Agent Identity post-GA semantics. Filed: deep-spike on Agent ID / Blueprint / User post-GA semantics + cross-repo platform-learnings sync (entraclaw + persona-sati + a related internal prototype) is happening 2026-05-05; the new doc will be `docs/platform-learnings/agent-id-blueprints-and-users.md`.

### Agent Identity missing `Application.Read.All` after provisioning

**Status:** Open (workaround applied manually on Windows VM).
**Impact:** `wait_for_sponsor_dm` and sponsor-gated flows fail with 403 `Authorization_RequestDenied` when calling `/servicePrincipals/{id}/microsoft.graph.agentIdentity/sponsors`.
**Root cause:** `scripts/create_entra_agent_ids.py` does not grant `Application.Read.All` to the Agent Identity service principal. The sponsors API requires this permission but it was never tested live on any platform (Mac included — the Sati Agent Identity also lacks it).
**Workaround:** Manually grant via `New-MgServicePrincipalAppRoleAssignment` or the Python provisioner token flow (see `grant_app_read.py` in this branch).
**Fix:** Add `Application.Read.All` grant to the Agent Identity provisioning flow in `create_entra_agent_ids.py`.

---

## Recently Resolved

## What's New Apr 29 — Windows Port Acceptance (PR #58)

**Branch:** `feat/windows-port` — ARM64 Windows 11 VM acceptance pass. All 6 acceptance steps from `PLAN-windows-port.md` completed (Steps 1–5 live-verified, Step 6 teardown deferred by user).

### Key results

| Step | What | Result |
|------|------|--------|
| 1 | `setup-windows.ps1` | ✅ venv, deps, self-signed cert (Software KSP — VM has no TPM), keyring markers, MCP registration for both Claude Code and Copilot CLI |
| 2 | Cert verification | ✅ Thumbprint matches `.env`; `windows.find_cert_by_thumbprint()` returns True/False correctly |
| 3 | Three-hop CNG signing | ✅ Real Graph access token obtained via CNG-signed JWT assertion against Entra |
| 4 | MCP registration | ✅ Both `claude.json` and `copilot mcp-config.json` entries point at `.venv\Scripts\entraclaw-mcp.exe` |
| 5 | Live MCP in Copilot CLI | ✅ Real Teams DM sent and received |
| 6 | Teardown | Deferred (user elected to keep environment) |

### Challenges and fixes shipped during acceptance

1. **`wait_for_sponsor_dm` 403 on sponsors API** — Agent Identity SP lacked `Application.Read.All`. Neither the Windows nor Mac provisioning script grants it. Manually granted via provisioner token. Tracked as open issue above.

2. **Model non-determinism with `wait_for_sponsor_dm`** — On Copilot CLI (which doesn't load `.github/copilot-instructions.md` unless launched from repo root), the model inconsistently called the wait tool after sending a DM. Root cause: the instruction to call it was in `copilot-instructions.md` which only loads from the correct CWD.

3. **`send_teams_message` auto-wait (commits `ef83609`, `88fbaa7`)** — Merged the wait loop directly INTO `send_teams_message` with host detection. Claude Code (has channel push) returns immediately; all other hosts (Copilot CLI, Codex, etc.) auto-block until the sponsor replies. Initially exposed `wait_for_reply` as a parameter — the model immediately started passing `false` to skip it. Fixed by removing the parameter entirely; auto-wait is now unconditional and purely host-detection driven.

4. **Debug code crash** — Adding `os.path.expanduser()` without importing `os` at function scope crashed the MCP server silently (empty error on every tool call). Copilot CLI swallows MCP server stderr, making diagnosis difficult. Fixed by removing debug code; file-based logging on Windows MCP is fragile.

5. **Git symlink skills on Windows** — 36 `.claude/skills/*/SKILL.md` files were git symlinks pointing to a Mac-only path (`/path/to/...`). Windows checks these out as plain text files containing the path (since `core.symlinks=false`). Deleted locally for clean demo — not a code fix, just a Windows git limitation.

### Platform learnings (Windows-specific)

- **MCP stderr is invisible on Copilot CLI.** The host swallows all stderr from the MCP server process. Debugging requires file-based logging or testing outside the MCP host context.
- **`pip install -e .` fails if `entraclaw-mcp.exe` is running.** Windows locks the `.exe`; must kill all MCP server processes before reinstalling. Stale processes can persist after Copilot CLI exits.
- **ARM64 Windows 11 + Software KSP works end-to-end.** No TPM needed for the acceptance pass. CNG signing via `NCryptSignHash` with the Software KSP produces valid JWT assertions that Entra accepts.
- **`core.symlinks=false` is the default on Windows Git.** Any repo using symlinks for shared content (skills, configs) will break. Use copies or conditional paths instead.

---

### Entraclaw MCP clean-closes on raw Teams HTML in channel notifications

**Status:** Fixed Apr 24, 2026 in `f0d29ea` (`fix: sanitize Teams channel notification HTML`). The original "minutes of sustained activity" framing was too broad: the deterministic trigger was the first inbound Teams push whose notification payload carried raw Graph HTML such as `<attachment ...></attachment><p>...</p>`.

**Root cause.** `_push_channel_notification` passed `message.get("content", "")` directly into `notifications/claude/channel` params. Teams Graph message bodies are HTML. Claude Code's MCP channel parser clean-closed the connection on angle-bracket content in the notification payload, matching the earlier email-path bug documented around `mcp_server.py:1249-1252`.

**Fix.** `src/entraclaw/mcp_server.py` now runs `_summarize_content(...)` over both the top-level channel notification content and fetched quoted-message `meta["quoted_messages"][*]["content"]`. Regression tests in `tests/test_mcp_server_integration.py` cover both paths.

**Verification.** Focused tests passed, full suite passed with `ENTRACLAW_KEEP_MEMORY_LOCAL=true` (`652 passed`), `ruff check .` passed, a 65-minute real Claude Code channel soak survived the original raw-HTML crash trigger, and a follow-up 30-minute quote-reply soak verified sanitized quoted-message metadata.

### Per-tool observe mirroring and log file growth

**Status:** Fixed Apr 24, 2026. Efferent-copy discovery is now opt-in via `EFFERENT_COPY_ENABLE=1`; the existing `EFFERENT_COPY_DISABLE=1` remains a hard override. Default MCP boots register zero observer sinks and do not wrap every tool call.

**Disk guard.** `src/entraclaw/logging_config.py` now uses a rotating JSON file handler for `~/.entraclaw/logs/entraclaw.log` instead of an unbounded `FileHandler`.

**Verification.** Full suite passed with `ENTRACLAW_KEEP_MEMORY_LOCAL=true` (`654 passed`), and `ruff check .` passed.

---

## What's New Apr 24 — MCP logging + wrapper hardening

Two PRs merged Apr 24 targeting the MCP-disconnect symptom. Both were amplifier fixes, both reduced the drop frequency, neither eliminated it. The remaining root cause was fixed later the same day; see "Recently Resolved" and `docs/runbooks/mcp-disconnect-investigation.md` for the full dossier.

**PR #40 — stop entraclaw records propagating to root** (commit `9c74cd1`). `src/entraclaw/logging_config.py` sets `logger.propagate = False`. FastMCP's `configure_logging()` had attached a `RichHandler` to the root logger via `basicConfig`; every entraclaw record was rendering twice on stderr (JSON + rich) and doubling the byte volume the parent Claude CLI had to drain. Added `tests/conftest.py` with an autouse fixture that attaches `caplog.handler` directly to the entraclaw logger per test, because `caplog` attaches to root and with propagation off would have lost visibility of entraclaw records. Ships with TDD tests in `tests/test_logging_config.py`. Also removed 4 throttling-sleep tests that were pushing the suite past Bash 300s timeout; unrelated to runtime behavior.

**PR #41 — wrapper self-ref marker** (commit `fc2e49b`). `src/entraclaw/efferent_copy.py::_is_self_referential_peer` now detects wrapper scripts via an opt-in `# entraclaw-self-ref-target: <path>` marker comment. Reads up to 16 KB of the script, parses the marker, matches the declared target against `sys.argv[0]` / `sys.executable`. `scripts/entraclaw-mcp-debug.sh` carries the marker. Prevents the PR #36 self-spawn cascade regressing when `.mcp.json`'s `command` is swapped to a wrapper for stderr capture. See Learning #45 for the full post-mortem.

**Follow-up fix later Apr 24.** The remaining drop was not parent stdio backpressure after all. It was raw Teams HTML in `notifications/claude/channel` payloads. `f0d29ea` sanitized top-level push content and quoted-message metadata; see "Recently Resolved" and Learning #46.

---

## What's New Apr 22 (efferent-copy + leader-gate rip + channel launch-flag discovery)

Three PRs merged in sequence; one deep debugging session; one follow-up the next day (Apr 23) that pinned the "channel not rendering" symptom on a launch-flag typo rather than a Claude Code regression.

**PR #35 — efferent-copy dispatch middleware** (commit `bf400c7`, merged 20:24Z). Schema-discovered `observe(tool_name, args[, result])` side-channel fired pre/post every `@mcp.tool()` invocation to any `.mcp.json` peer advertising a compatibly-shaped `observe` tool. Fire-and-forget, 250ms per-sink timeout, failures swallowed. All three transports supported (stdio/SSE/streamable-HTTP). Zero-sink case is a true no-op (install short-circuits, tool.fn unchanged). `EFFERENT_COPY_DISABLE=1` opt-out. 27 tests.

**Regression from PR #35 — self-spawn cascade.** Within 60 seconds of merge, the log began showing ~30 `Starting EntraClaw MCP server` events per minute from short-lived child processes, continuing for 2h+ before being caught. Root cause: `.mcp.json` lists entraclaw as a stdio peer of itself; `discover_sinks` opens `stdio_client(params)` against every peer, including itself, spawning a child entraclaw-mcp which runs its own `discover_sinks`, spawning a grandchild, and so on. The 5s per-peer discovery timeout only partially bounds recursion. Each child ran a full boot (auth, poll-loop, background tasks) before dying. Chained directly with a pre-existing leader-cache bug (see below) to silently drop ~99% of Teams DM pushes for the afternoon. **See Learning #37.**

**Pre-existing bug exposed by #35 — leader-cache overwrite.** `_capture_host_from_initialize` stored `clientInfo.name` from every MCP Initialize handshake into `_state["cached_host"]` unconditionally. Each cascade-child's `ClientSession(...)` (opened without explicit `client_info`) inherited the SDK default `Implementation(name="mcp", version="0.1.0")` — i.e., identified as `"mcp"`. `LEADER_HOSTS = frozenset({"claude-code", "claude code"})` did not include `"mcp"`, so every cascade-child init overwrote the cached leader value with a non-leader name. `_is_leader_host()` reads the cache; 99% of the time today it saw `"mcp"` and returned `False`; `_push_channel_notification` hit its fail-closed branch and silently dropped the push (logged inbound to blob, never pushed to the MCP stream). Log histogram: **1853 of 1871 initialize events today identified as `mcp (leader=False)`**, vs only 18 legit `claude-code (leader=True)` events. The 4:34 PM "How's the weather today?" push happened to land inside one of the rare `claude-code` windows; 8:07 AM "Good morning!" landed during a `mcp` window and was gated out. **See Learning #38.**

**PR #36 — kill efferent-copy self-spawn cascade + rip out leader/slave gating** (commit `8a00939`, merged later the same day). Two fixes in one PR:

- *Fix 1:* `efferent_copy._is_self_referential_peer(peer)` resolves peer.command vs `sys.argv[0]` / `sys.executable`; matching peer is skipped at factory-build time, never reaching `stdio_client`. Belt-and-suspenders: `_stdio_factory` sets `EFFERENT_COPY_DISABLE=1` in the child's env so any subprocess we *do* spawn short-circuits its own discovery. Spawn depth bounded at 1.
- *Fix 2:* Ripped out `LEADER_HOSTS`, `SLAVE_REPLY_DISCLOSURE`, `_is_leader_host`, `_slave_disclosure_suffix`, `_capture_host_from_initialize`, `_install_initialize_host_capture` (+ `ServerSession._received_request` monkey-patch), leader gate in `_push_channel_notification`, slave disclosure in `send_teams_message`, and 7 associated test classes. Kept `_current_host` + `_capture_host_from_context` for log annotation only.

**Rationale for rip-out.** Every MCP client that spawns entraclaw via stdio gets its own process and its own poll loops. There is no multi-client sharing at runtime, so the leader/slave gate was fighting a problem that doesn't exist in the stdio model. The gate was also the failure mode that turned the efferent-copy cascade from "wasteful" into "silent DM drop." Channel pushes now fire unconditionally; clients that don't handle `notifications/claude/channel` drop them silently per the MCP spec. Net diff: **+189 / −1007**, 618 tests passing, ruff clean.

**Post-#36 behavior verified.** After PR #36 merged and `entraclaw-mcp` was restarted, the log shows **one** `Starting EntraClaw MCP server` per reconnect (previously 30+/minute), zero cascade spawns, and successful push lines for inbound DMs (`Pushed Teams message from the user: <p>Hi Hi Hi</p>` appeared 4 seconds after the DM was sent).

**Channel-render symptom resolved Apr 23 — launch-flag typo, not a Claude Code regression.** The symptom was real (no `notifications/claude/channel` entries in session transcript, zero LLM-visible DMs) but the root cause turned out to be the Claude CLI launch command: `-dangerously-load-development-channels` (single dash) instead of `--dangerously-load-development-channels` (double). In single-dash mode Claude treats `server:entraclaw` as prompt text instead of the dev-channel allowlist argument. Relaunching with the correct double-dash form immediately restored channel delivery on both the rollback branch and `main`. Server-side investigation (cascade fix, leader-gate rip, gate-function byte-diff, capability-declaration audit) was real work and correct — just not the blocker for this particular symptom. **See Learning #39 for the full post-mortem and prevention guidance.**

---

## What's New Apr 20–21 (mind-body-tools push)

Twelve PRs merged across two days (#17–#28). Tooling and body-prompt discipline both hardened significantly.

**New MCP tools / body surface**
- **PR #17** — `read_teams_messages` now surfaces attachment metadata (list of `{id, content_type, content_url, name, thumbnail_url}`) so `<attachment id=...>` references resolve without extra Graph calls.
- **PR #18** — Dual-host MCP support. `clientInfo.name` detection drives leader (Claude Code) vs. slave (Copilot CLI and other hosts) mode. Slave-mode adds a disclosure suffix to outbound tool responses. Original implementation gated background tasks on host identity at boot — see PRs #27/#28 below for the subsequent lifecycle fix.
- **PR #19** — `post_thinking_placeholder` + `resolve_placeholder` MCP tools. When the agent decides to answer a Teams chat with real work behind it, it posts a placeholder first and edits/deletes-reposts when the reply is ready. Modes: `edit` (quiet, safer), `delete_repost` (fresh ping), `fallback_new` on Graph failure.
- **PR #20** — `delete_teams_message` MCP tool (standalone soft-delete of the agent's own messages). Also fixes the Graph URL for softDelete: `/me/chats/{chat_id}/messages/{message_id}/softDelete` (the `/me/` prefix is mandatory — v1.0 returns 405 without it).
- **PR #23** — PreToolUse hook at `scripts/hooks/block_local_memory_write.py` blocks `Write`/`Edit`/`NotebookEdit` to `~/.claude/projects/<slug>/memory/**` unless `ENTRACLAW_KEEP_MEMORY_LOCAL=true`. Reuses the existing `setup.sh --keep-memory-local` switch. Paired with a three-way memory-routing tree in `CLAUDE.md`: body/channel rules → `prompts/anatomy/` (PR); mind content → `mcp__persona-sati__write_memory_file`; operational state → entraclaw blob.
- **PR #24** — `_push_channel_notification` forwards `reply_to_ids` into the notification `meta` and concurrently fetches each quoted message body via new `fetch_message()` helper, attaching results as `meta.quoted_messages`. Fail-open: individual fetch failures drop from the list; total failure still pushes `reply_to_ids` without `quoted_messages`. The agent now has context for quote-replies without a round-trip.
- **PR #25** — `send_email` MCP tool + new `src/entraclaw/tools/email.py` helper. Supports `reply_to_message_id` for thread-preserving replies via Graph's `/me/messages/{id}/reply`. `daily_summary.py` now routes through the same helper — one send path in the codebase. `Mail.Send` delegation was already granted on the Agent User token chain; no re-provisioning required.
- **PR #26** — `add_promise` / `list_promises` / `resolve_promise` MCP tools backed by `promises.jsonl` in entraclaw blob, identity-scoped. Supersedes the session-scoped `TaskCreate` pattern for human-facing commitments — promises now survive MCP server restart, Claude Code restart, and cross-session hand-off (terminal ↔ Teams). ETag-concurrency on writes, compaction at >1000 lines with 30-day resolved retention.

**Body-prompt rules** (`prompts/anatomy/channel-discipline.md`)
- **PR #21** — "No cross-chat context bleed." Outbound messages may only reference work that the specific chat has visible history of. Don't name-drop parallel work from another chat, even with the same human in both.
- **PR #22** — "Promises become tasks." Any time the agent says "I'll report back / post the PR link / confirm when X lands," create a durable entry the same turn. Mark done only after the human-facing follow-up has been posted, not when the internal signal arrives. Superseded by PR #26 (promises become **durable**, backed by blob, not `TaskCreate`).
- PR #19 — "Signal when you're working." When decides to answer a Teams chat and the response will involve real work, post a `post_thinking_placeholder` first and resolve via `resolve_placeholder` when the reply is ready.
- PR #23 body rule — "Spawn sub-agents for side-work" and the memory-routing decision tree in `CLAUDE.md`.
- PR #20 body rule — "Deleting your own messages." Use `delete_teams_message`, not `resolve_placeholder delete_repost` as a hack.

**Lifecycle bug — slave-mode gate, two attempts, third-time-right**
- **PR #27** — First attempt. `_init_poll()` had been gating all background-task startup on `_is_leader_host()`, which reads `clientInfo.name` from the live MCP request context. At boot time no `initialize` request has been processed yet, so `_current_host()` returned `"unknown"` and the entire background stack silently skipped — Teams poll, email poll, daily summary, chat auto-discovery. Fix: remove the boot-time gate; move it to `_push_channel_notification` where the decision is actually needed.
- **PR #28** — Second attempt. PR #27 moved the gate from boot to push-time, but `_push_channel_notification` is called from the background poll task, which runs in an asyncio context **detached from any MCP request** — so `_current_host()` STILL returned `"unknown"` and the push gate silently dropped every inbound message. Fix: cache `clientInfo.name` in `_state["cached_host"]` at every tool invocation; `_is_leader_host()` prefers live context but falls back to the cached value. Background tasks now see the right answer after at least one leader-host tool call.

**The footgun that hid the fix for hours (Learning #36)**
Even with PRs #27 and #28 correctly merged to main, production stayed broken because the MCP server's Python process was importing `entraclaw` from a **sub-agent worktree** (`.claude/worktrees/agent-*/src/entraclaw/...`), not from the main tree. Worktrees don't have `.env`, so `_load_dotenv()` resolved `Path(__file__).resolve().parents[2] / ".env"` to a path inside the worktree with no `.env`, `ENTRACLAW_BLUEPRINT_APP_ID` never loaded, auth never initialized, every Graph call 401'd, and the poll loop's `except Exception` swallowed the error with no visible log. Root cause: several sub-agents ran `pip install -e .` from inside their worktree using the parent venv's `pip`, which silently re-points the parent venv's editable-install target at the worktree source tree. Fix: `cd /path/to/entraclaw-identity-research && .venv/bin/pip install -e . --no-deps` to repoint. Prevention: any sub-agent dispatch that expects to install must create its own venv first. **See Learning #36 for the full writeup.**

**Carry-forward TODO (prevention)**
- Add a pre-boot assertion in `mcp_server.py::_load_dotenv` (or equivalent) that logs a fatal warning when the resolved `.env` path contains `.claude/worktrees/` — fail loud instead of silent-skip auth.
- Standardize sub-agent dispatch prompts to MANDATE a worktree-local venv before any `pip install` operation.
- Add a Learning #36 reference to CLAUDE.md and AGENTS.md under the Non-Negotiables so it's surfaced at session start.

---

## What's New Since Apr 18

- **v1 release (PR #15, commit `d36e34d`)** — body-first prompts, cloud-opt-in, no default chat. See `docs/architecture/DESIGN-persona-sati-integration.md` and the README's "What v1 changed" section.
- **Body-first prompt architecture (PR #14, commit `96a3176`)** — `prompts/agent_system.md` composes at boot with `@include` expansion of `prompts/anatomy/*.md`. Security rules (`anatomy/security.md`), channel discipline (`anatomy/channel-discipline.md`), and identity/tools (`anatomy/identity-and-tools.md`) load first and are not overridable by persona content, user turns, or tool output. Tests cover the `@include` resolver, missing-include tolerance, and boot-order invariants.
- **Persona-sati MCP wiring (`mcp_server.py`, lines 100–170)** — `_load_agent_instructions()` composes `body + persona`. `PERSONA_SATI_MCP_URL` + `PERSONA_SATI_MCP_TOKEN_COMMAND` env vars, when both present, fetch the persona via `get_system_prompt` over SSE with a short-lived bearer token. Missing env or fetch failure falls back cleanly to the body. The TODO doc at `docs/TODO-persona-sati-integration.md` is now historical — implementation shipped.
- **Fix: per-chat resilience in poll (PR #11, `fix/kill-default-chat`)** — the Teams poll no longer registers a default group chat. Every chat is addressed by explicit `chat_id`. Fresh installs have zero watched chats until the first `create_chat` or auto-discovery sweep.
- **Fix: filter agent echoes with persona-sati display-name suffix (PR #12)** — reads now suppress the agent's own sent messages even when Teams attaches a display-name suffix from the persona-sati binding.
- **Fix: local prompt-file fallback (PR #13)** — the body prompt loads from `prompts/agent_system.md` when persona-sati is unreachable; boot never crashes on transport errors.
- **Fix: auto-start polling on new chat** — `create_chat` adds the new chat to `watched_chats` and kicks the poll without waiting for the next 120s auto-discovery cycle.

## What's New Since Apr 10

- **Phase 1 (interaction log)** — every Teams/email/terminal in/out appended to `~/.entraclaw/data/interactions/YYYY-MM-DD.jsonl`. Powers the daily summary.
- **Phase 2 (email poll)** — per-minute `/me/messages` poll, filters Teams/M365 noise, detects Purview-encrypted mail via `message.rpmsg` lookup, persists cursor + per-session message-id dedup.
- **Phase 3 (daily summary)** — 5pm PDT scheduler, triages day's interactions into `needs_you / handled / heads_up`, renders HTML, sends via `/me/sendMail`, archives to `<data_dir>/summaries/<day>.html`.
- **Chat auto-discovery (`a75d043`)** — background task hits `GET /me/chats` every 120s; any chat not in `watched_chats` gets auto-registered (in memory + persisted) so chats created via raw Python or by other humans adding the Agent User get polled within ~2 min.
- **Reply detection (`0732b8b`)** — `read_teams_messages` surfaces `reply_to_ids` from the Teams `<attachment id=…>` quote tag. The body prompt's Exception #3 lets the agent continue active 1:1 exchanges in group chats without re-`@`-tagging.
- **Eager MCP init (`d6cc640`)** — `_initialize()` runs as a background task at server boot instead of waiting for the first tool call. Fresh servers no longer sit deaf to inbound DMs/email until someone invokes a tool.
- **Email-push schema fix (`9a71d6c`)** — email push notification meta + content aligned with Teams push (no `<sender@addr>` angle brackets that read as HTML tags; meta carries only `chat_id="email"` / `message_id` / `user` / `ts`). Fixed: silent MCP-stream close after every email push.
- **`setup.sh` hardening** — tenant-wide UPN lookup before Agent User creation (`8541d75`), warn-and-confirm before replacing Blueprint certs (`2338a7a`), cached-cert verification against Entra (`22e81d9`), `redirect_stdout(sys.stderr)` to stop diagnostic spam from corrupting `.env` cert thumbprint (`c99d66a`), `entraclaw-mcp` console script in `.mcp.json` (`5bb3bc4`).
- **ADR-005 Phase 1 (`f900ba1`)** — `BlobStore` async client in `src/entraclaw/storage/blob.py` (put/get/list/delete/exists + ETag concurrency + 401→`TokenExpiredError`). 22 tests.
- **ADR-005 Phase 2** — `MemoryBackend` protocol + `LocalBackend` / `BlobBackend` impls + `get_backend()` factory in `src/entraclaw/storage/backend.py`. `interaction_log.py` and `daily_summary.py` route through it. 22 tests.
- **ADR-005 Phase 5** — `acquire_agent_user_storage_token` (storage-scope third hop), `--keep-memory-local` flag in `setup.sh`, `scripts/provision_blob_storage.py` (idempotent Storage Account + container + RBAC), migration helper in `src/entraclaw/storage/migration.py`, blob endpoint/container/keep-memory-local config fields. 23 tests. Setup now exits red + non-zero on migration failure.
- **ADR-005 Phase 6a** — Claude Code persona-memory sync. `PersonaBackend` + `claude_code_memory_dir()` in `src/entraclaw/storage/persona.py`. `scripts/claude_memory_sync.py` CLI with `pull` / `push` / `push-one` subcommands. Memory sync hooks removed from `.claude/settings.json` — persona-sati owns memory sync via its own MCP tools (`write_memory_file`, `read_memory_file`, `refresh_persona`). `claude_memory_sync.py` retained as a manual migration tool.
- **Multi-tenant lightweight chat** — landed to `main` (commit `c8ec521`, 47 commits, +9,331 / −2,484).

---

## What We're Building

A proof-of-concept demonstrating that **device-local AI agents can have their own identity** in Microsoft Entra, separate from the human user. Three identity modes:

1. **Agent User** (production path) — Blueprint → Agent Identity → Agent User via three-hop flow. Agent sends as its own Entra user.
2. **Delegated** (instant start) — MSAL interactive auth with human's token. Messages prefixed `[EntraClaw]`. No provisioning needed.
3. **Bot Gateway** — M365 Agents SDK bot server with Dev Tunnel. Bot has its own identity in Teams by design. No Agent User provisioning, no M365 license.

**Identity Chain (Agent User):** Blueprint (certificate auth) → Agent Identity (FIC exchange) → Agent User (`user_fic` grant, `idtyp=user`) → Graph API (Teams, Mail, OneDrive)

**Channel:** Background poll every 5s (Graph) or 2s (bot JSONL) → push via `notifications/claude/channel` → Claude Code receives messages automatically.

### The Demo Scenario — WORKING (Three Modes)

| Step | Agent User Mode | Delegated Mode | Bot Mode |
|------|----------------|----------------|----------|
| Setup | `./scripts/setup.sh` (10–15 min) | `./scripts/setup_delegated.sh` (60s) | `./scripts/start_bot.sh` + Dev Tunnel |
| Auth | Three-hop flow (automatic) | MSAL browser sign-in (cached) | Bot app credentials |
| Identity | Agent's own Entra user | Human's identity + `[EntraClaw]` prefix | Bot's app identity |
| Send | Graph API as Agent User | Graph API as human | Bot Framework relay |
| Receive | Graph API poll (5s) | Graph API poll (5s) | Bot activity handler (instant) |

### MCP Tools

| Tool | Purpose | Status |
|------|---------|--------|
| `send_teams_message` | Send text/HTML to a chat (requires `chat_id`); supports `@mentions` | ✅ Live |
| `send_card` | Adaptive Card (tool_activity / task_status / build_result) | ✅ Live |
| `create_chat` | Open a 1:1 DM by email; auto-registers for polling | ✅ Live |
| `read_teams_messages` | Read recent messages from a chat | ✅ Live |
| `list_chat_members` | Resolve display names to Entra GUIDs for `@mentions` | ✅ Live |
| `add_teams_member` | Add user to chat (cross-tenant auto-resolved) | ✅ Live |
| `watch_teams_replies` | Blocking poll with dedup — fallback when channel push is unavailable | ✅ Live |
| `whoami` | Show agent identity, mode, and connection status | ✅ Live |
| `audit_log` | Record an audit event before performing a security-sensitive action | ✅ Live |
| `run_daily_summary` | Generate and email the day's interaction digest on demand | ✅ Live |
| `view_image` | Read an image file and return as base64 for the LLM | ✅ Live |

Full reference: `docs/reference/mcp-tools.md`.

---

## TDD Status

```
484 tests collected
```

Key modules:

```
src/entraclaw/auth/          — certificate JWT + MSAL delegated auth
src/entraclaw/bot/           — Bot Gateway (server, handler, tunnel, convo_store)
src/entraclaw/identity/      — progressive identity state machine
src/entraclaw/storage/       — LocalBackend / BlobBackend / PersonaBackend + migration
src/entraclaw/tools/         — Teams Graph API tools + interaction log + email + daily summary + cards
src/entraclaw/config.py      — ENTRACLAW_MODE + all env config
src/entraclaw/mcp_server.py  — FastMCP + 3 auth modes + body-first prompt loader + persona-sati fetch + background poll + channel push
prompts/                     — body prompt + anatomy/ modules
```

Invariant: `pytest -v && ruff check .` passes before every commit.

---

## Close the Loop (Channel Push Architecture)

**Problem:** LLM doesn't automatically check for replies after sending a Teams message. The MCP protocol is request-response — no mechanism for the server to wake up the LLM when new data arrives.

**Solution:** Background polling + `notifications/claude/channel` push. The MCP server declares `experimental: {"claude/channel": {}}` capability and pushes inbound Teams messages directly into the Claude Code conversation — same mechanism as the iMessage channel plugin.

**Requirements:** Start Claude Code with `--dangerously-load-development-channels server:entraclaw` to enable channel notifications for development servers. Without the flag, the background poll still runs and appends to the interaction log; `read_teams_messages` retrieves them on demand.

**Fallback:** `watch_teams_replies` tool still available for explicit polling. Background poll uses separate dedup state so both can detect the same message independently (Learning #27).

**Research:** `docs/platform-learnings/mcp-close-the-loop.md` — analysis of 12+ MCP messaging servers, the MCP Triggers & Events Working Group, and the three problems we solved (capability declaration, startup flag, separate state).

---

## What Works (Shipped)

- End-to-end: `setup.sh` → MCP server → Teams message delivery
- Three-hop Agent User token flow (Blueprint → Agent Identity → Agent User)
- Agent User creation via Graph beta API (`microsoft.graph.agentUser`)
- Agent User license assignment (auto-detects Teams-capable SKUs)
- Consent grant (`oAuth2PermissionGrant`) for Teams/Chat permissions
- Dedicated provisioner app (avoids Azure CLI token rejection)
- State persisted in `.entraclaw-state.json` (idempotent, no secret reset)
- MCP server auto-discovered via `.mcp.json`
- `--teams-user` flag to set Teams recipient separately from admin
- `read_teams_messages` with null-from handling (system messages)
- 29 hard-won learnings documented in runbooks
- Bidirectional Teams channel — background polling + push notifications
- Certificate auth for Blueprint — private key in OS keystore, no secrets on disk (ADR-003)
- Token auto-refresh: eager (55-min) + lazy (401 retry) for all tools
- `notifications/claude/channel` push — same mechanism as iMessage channel plugin
- Message dedup: 2s overlap window + bounded seen-set (imessage-kit pattern)
- 429 rate limit handling propagates through polling tool with `Retry-After`
- Autonomous agent behaviour — acts on Teams messages without terminal prompting
- Multi-user group chat support (`setup.sh --teams-user=user1,user2`)
- Cross-tenant federated chats for B2B guests — auto-detects guest UPN, resolves home tenant via OpenID discovery
- `add_teams_member` — add users to chat at runtime without restart
- Chat ID persistence across restarts — no duplicate group chats
- All code passes `ruff` lint + format
- Progressive identity state machine — `UNAUTHENTICATED → DELEGATED → PROVISIONING → AGENT_USER` with `asyncio.Lock`-protected transitions
- MSAL delegated auth — localhost redirect + device code fallback, OS-encrypted token cache via `msal-extensions`
- Delegated setup script (`scripts/setup_delegated.sh`) — sign in once, cache token, launch MCP server
- Bot Gateway — M365 Agents SDK bot server + JSONL IPC (inbound/outbound with `fcntl.flock`) + Dev Tunnel manager + conversation reference persistence. Coexists via `ENTRACLAW_MODE=bot`
- Identity-aware user ID — `_effective_user_id()` returns the correct ID for the current mode (agent user OID vs signed-in human OID)
- Body-first prompt architecture with `@include` expansion — security, channel discipline, identity/tools layered under non-overridable body
- Persona-sati MCP integration — body composes `body + persona` when `PERSONA_SATI_MCP_URL` + `PERSONA_SATI_MCP_TOKEN_COMMAND` env vars are set; clean fallback when not
- Adaptive Cards: `send_card` tool with `tool_activity`, `task_status`, `build_result` templates
- Azure Blob Storage backend — opt-in via `./scripts/setup.sh --use-cloud-memory` (ADR-005 Phases 1, 2, 5, 6a)

### What's Not Started / Deferred

- Azure Bot resource registration on yourtenant.onmicrosoft.com (needed for live bot test)
- Windows VM provisioning and testing (rescheduled)
- AppContainer sandbox spike — kernel-level agent isolation on Windows
- Delta query optimization — replace timestamp polling with `/messages/delta` if rate-limit becomes an issue
- Dynamic precision weighting for the polling cadence (still static per source)
- Purview-protected email decryption — `Mail.Read` can't decrypt `.rpmsg` attachments; needs separate IRM scope (low priority, see `docs/platform-learnings/teams-graph-api.md`)

---

## Architecture

```
Blueprint (client_credentials)
  → Agent Identity (FIC exchange)
    → Agent User (user_fic grant, idtyp=user)
      → Graph API: Teams, Mail, OneDrive
      → Azure Blob Storage (parallel third hop, ADR-005 Phase 5)

┌─────────────────────────────────────────────────────────┐
│  Local Device (Mac / Windows / Linux)                   │
│                                                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │ Claude Code / Copilot CLI (MCP Client)           │   │
│  │   └── stdio + channels ────┐                     │   │
│  └────────────────────────────┼─────────────────────┘   │
│                               │                         │
│                               ▼                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │ Entraclaw MCP Server (Python)                    │   │
│  │                                                  │   │
│  │  Body prompt: agent_system.md + anatomy/*.md     │   │
│  │    + Persona (optional): persona-sati /sse       │   │
│  │                                                  │   │
│  │  send_teams_message ───▶ Graph API (Agent User)  │   │
│  │  read_teams_messages ──▶ Graph API (Agent User)  │   │
│  │  whoami ───────────────▶ cached state            │   │
│  │  audit_log ────────────▶ interaction log         │   │
│  │                                                  │   │
│  │  Background: Teams 5s, email 60s, discovery 120s,│   │
│  │  daily summary 5pm PDT                           │   │
│  │                                                  │   │
│  │  Tokens: Agent User (three-hop, idtyp=user)      │   │
│  └──────────────────────────────────────────────────┘   │
└───────────┬──────────────────────────┬──────────────────┘
            │                          │
            ▼                          ▼
    ┌───────────────┐          ┌──────────────┐
    │ Entra ID      │          │ Graph API    │
    │ Agent IDs     │          │ Teams Chat   │
    │ Agent Users   │          │ Mail / Drive │
    └───────────────┘          └──────────────┘
```

---

## Next Steps (Priority Order)

1. ~~Bidirectional Teams loop~~ — ✅ DONE. Background poll + channel push + dedup + token refresh.
2. ~~Token auto-refresh~~ — ✅ DONE. Eager (55-min) + lazy (401 retry).
3. ~~Certificate auth~~ — ✅ DONE. No secrets on disk. Private key in OS keystore (ADR-003).
4. ~~Close the loop~~ — ✅ DONE. `notifications/claude/channel` push via experimental capability.
5. ~~Multi-tenant lightweight chat~~ — ✅ DONE. Progressive identity state machine + MSAL delegated auth (PR #1).
6. ~~Bot Gateway~~ — ✅ DONE. M365 Agents SDK bot server + JSONL IPC + tunnel manager. Coexists via `ENTRACLAW_MODE=bot`.
7. ~~Body-first prompt architecture~~ — ✅ DONE. `@include` expansion, non-overridable body rules, persona layered on top (PRs #14, #15).
8. ~~Persona-sati MCP wiring~~ — ✅ DONE. `PERSONA_SATI_MCP_URL` + `PERSONA_SATI_MCP_TOKEN_COMMAND` consumed at boot.
9. **Bot Gateway live test** — Register Azure Bot on yourtenant.onmicrosoft.com, sideload Teams app, verify end-to-end with Dev Tunnel.
10. **Entra sign-in log verification** — confirm `idtyp=user` and agent attribution in tenant audit logs.
11. **Windows VM provisioning** — verify cross-platform `setup.sh`.
12. **AppContainer sandbox spike** — kernel-level agent isolation on Windows.
13. **Delta query optimization** — replace timestamp polling with `/messages/delta` if rate-limit becomes an issue.

---

## Bugs Encountered & Resolved (Selected)

| # | Bug | Impact | Fix |
|---|-----|--------|-----|
| 1 | Provisioner secret reset on every re-run | High | Cache in state file, use `--append` |
| 2 | Agent User UPN used tenant ID as domain | Blocking | Extract domain from signed-in user's UPN |
| 3 | `oAuth2PermissionGrant` missing `startTime` | Blocking | Add `startTime: now()` to request body |
| 4 | Provisioner lacked `DelegatedPermissionGrant` permission | Blocking | Added to `BASE_PERMISSION_VALUES` |
| 5 | Three-hop flow missing `fmi_path` parameter | Blocking | Added `fmi_path={agent-id}` to hop 1 |
| 6 | Consent grant used beta API instead of v1.0 | Blocking | Use v1.0 URL directly, not `graph_request()` |
| 7 | Chat creation `/me` doesn't work for Agent Users | Blocking | Use explicit user IDs for both members |
| 8 | `read_teams_messages` crashed on null `from` field | Crash | `(m.get("from") or {})` pattern |
| 9 | Non-Teams licenses triggered skip | Wrong | Check `TEAMS_CAPABLE_SKUS`, not any license |
| 10 | MCP tool names not discoverable by LLM | UX | Renamed to verb-first, added trigger phrases |
| 11 | No `httpx` timeout on token flow | Hang | Added 15s timeout to all hops |
| 12 | `teardown.sh` silent exit on missing `.env` | Silent | Guard with `[ -f .env ]` check |
| 13 | `stderr` swallowed throughout scripts | Hidden errors | Removed all `2>/dev/null` |
| 14 | Admin and Teams user conflated | Wrong recipient | Added `--teams-user` flag |
| 15 | Default group chat registered at install | Wrong | No default chat — explicit `create_chat` only (v1) |
| 16 | Agent reading its own messages back as new input | Loop | Filter agent echoes including persona-display-name suffix (v1) |
| 17 | Body prompt not loading from file | Wrong | Fall back to `prompts/agent_system.md` when persona-sati unreachable (v1) |

Full append-only log: `docs/runbooks/hard-won-learnings.md` (29 entries).
