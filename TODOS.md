# TODOS

## P0 — DO NEXT

### persona-sati: implement /authorize + /token PKCE flow (blocks Claude Code SSE)
Claude Code v2.1.152 now does MCP OAuth 2.1 discovery and ignores `.mcp.json` `headersHelper` when the server advertises OAuth metadata. Persona-sati advertises metadata (ADR-006 in persona-sati) but never shipped the browser PKCE `/authorize` endpoint — clicking "Authenticate" in Claude Code's `/mcp` UI lands on `{"error":"invalid_request","error_description":"Missing or malformed Authorization header"}`.

**Current workaround:** entrabot's `.mcp.json` was rewritten to use the persona-sati **stdio shim** (`persona-sati-stdio-shim.sh`) instead of native SSE+headersHelper. Works, but moves Claude Code users off the path ADR-005 designed around (SSE-native with mid-session token refresh).

**Tracking:** persona-sati issue tracker. Fix lives in persona-sati, not here.

**Bonus bug surfaced:** `persona-sati/scripts/setup.sh` line 280 gates the `.mcp.json` rewire behind `! --skip-deploy`, so `--mcp-transport=stdio --skip-deploy` silently fails to rewrite. Workaround: invoke `wire_mcp_json.py` directly. Also tracked in the persona-sati issue tracker.

- **Effort:** M (persona-sati side — auth design decision + `/authorize` consent page + `/token` PKCE + redirect-URI allowlist on DCR). No work in this repo until persona-sati ships.
- **Depends on:** persona-sati design choice for browser-flow identity binding (Entra device-code? B2B SSO? localhost-only?).
- **Source:** Diagnosed 2026-05-27 in entrabot session — Claude Code v2.1.152.

## P1

### Follow-up: two-phase sponsor confirmation flow for mutating tools
The active-sponsor-channel binding shipped in PR `fix/msrc-active-sponsor-channel-binding` closes Chain A from the security confused-deputy report (attacker in low-priv chat manipulating action on a chat where sponsor is passive) but does NOT close the residual window where a sponsor IS actively engaged in the target chat. An attacker who gets a sponsor to read an injected SharePoint doc (Chain B) can still trigger a malicious `add_member` / `share_file` because all binding checks pass.

The architectural fix is a two-phase confirmation: when a high-risk mutation is requested, the server posts a concrete summary of the action ("share `spec.docx` with `bob@example.com`?") into the bound chat, addressed to the sponsor; the mutation only executes after the sponsor explicitly approves in Teams. Active-channel binding remains as a pre-check.

- **Why deferred:** larger UX shift than the authorization fix — needs a UI for the confirmation card, a pending-mutation store, and an inbound-approval matcher. Worth its own design ADR.
- **Effort:** L
- **Source:** Internal security report 2026-06-04; rubber-duck review on the authorization fix plan flagged this as the only path to fully close Chain B without LLM-level taint tracking.

### Follow-up: `read_file` content sanitization / spotlighting
Document content currently enters LLM context unguarded. Sponsor-readable but attacker-authored content can carry instructions that survive into tool-call reasoning. Pair with a content-spotlighting pass on `read_file` output so injected instructions are marked as data, not commands.

- **Why deferred:** distinct from the security authorization fix; covers a wider class of prompt-injection vectors than just the add/share confused-deputy.
- **Effort:** M
- **Source:** Internal security report 2026-06-04, Chain B.

### Script toolkit final phase: README + GitHub Pages script reference
After the remaining script-toolkit phases land, do a dedicated documentation
closeout pass so the new operational surface is discoverable and stable.

Required deliverables:
- Update `README.md` to make `./status.sh` the canonical one-command status
  entry point, and mention that `./scripts/setup.sh --status` delegates to the
  same consolidated status implementation.
- Add a GitHub Pages/MkDocs reference page under `docs/reference/` that lists
  every supported root and `scripts/` entry point, what it does, whether it is
  read-only or mutating, required prerequisites/permissions, common flags, and
  safe example invocations.
- Add that page to `mkdocs.yml` under the Reference section and link it from
  `docs/index.md` and the README's project-layout / docs sections.
- Include deprecated compatibility wrappers such as `scripts/health_check.py`,
  explaining their canonical replacement rather than leaving users to infer it.
- Verify the docs site builds (`mkdocs build`) and that every documented script
  has at least one non-destructive smoke command (`--help`, `--dry-run`,
  `--json`, `--health-only`, or equivalent) validated during the final pass.

- **Effort:** S-M (README + one reference page + nav + smoke-check table)
- **Depends on:** All script-toolkit behavior settling, especially status,
  health, setup/status delegation, cert inventory, sponsor/license reporting,
  and non-mutating token acquisition paths.
- **Source:** Script-toolkit follow-up request, 2026-05-19.

### ADR-005 Phase 2: MemoryBackend protocol + Local/Blob impls
Land the next phase of cloud-hosted memory. Spec: `docs/decisions/005-cloud-hosted-memory.md` §"Implementation phases" (Phase 2 row). Define `MemoryBackend` protocol in `src/entrabot/storage/backend.py` with `LocalBackend` (current behavior) and `BlobBackend` (uses Phase 1 `BlobStore`). Route `interaction_log.py`, `daily_summary.py`, and memory-file access through it. Driven by `ENTRABOT_KEEP_MEMORY_LOCAL` env var.
- **Effort:** S (~150 LOC + tests)
- **Depends on:** Phase 1 (`f900ba1`, shipped)
- **Source:** ADR-005

### Test isolation: interaction_log tests leak into production blob when ENTRABOT_BLOB_ENDPOINT is set
The `tmp_data_dir` fixture in `tests/tools/test_interaction_log.py` sets `ENTRABOT_DATA_DIR` to a pytest tmp path but does NOT clear `ENTRABOT_BLOB_ENDPOINT` / `ENTRABOT_BLOB_CONTAINER`. Since Phase 2/5 routed `log_interaction` and `read_day` through `get_backend()`, the factory reads those env vars and returns `BlobBackend` — which hits the real production container and ignores the tmp_data_dir. Result: 10 tests in `test_interaction_log.py` fail on any machine with the blob env configured (passed on the Phase 6a author's machine because they hadn't exported those vars). Observed 2026-04-17 during Phase 6a review: test run produced 443 passed / 10 failed; failing tests were reading 75 real chat entries from blob when they expected 2 from the tmp dir.
Fix: make the `tmp_data_dir` fixture (and any sibling fixture that patches config) also `monkeypatch.delenv("ENTRABOT_BLOB_ENDPOINT", raising=False)` + same for `ENTRABOT_BLOB_CONTAINER`. Consider a session-scoped autouse fixture that unsets blob env for *all* tests unless a test opts in. Also audit other test files that might have the same latent bug (`test_daily_summary.py`, `test_email_poll.py`, anywhere using `get_backend()`).
- **Effort:** S (~30 LOC — fixture edit + audit)
- **Source:** Phase 6a review 2026-04-17; failure is pre-existing on main, not introduced by Phase 6a

### PersonaBackend.pull_all() missing mtime-newer-local check (Phase 6d scope)
`src/entrabot/storage/persona.py` `pull_all()` currently overwrites local files unconditionally — cloud is authoritative on pull. The persona-persistence plan §4.2 specified: "If local is newer (happens if session was offline), leave it (to be pushed next)." Phase 6a shipped without that check for the safe-starting-point framing, but it's a race-loss risk: if a session writes a memory file offline, the next online session's SessionStart pull will clobber it before the PostToolUse-Write push fires. The mitigation of this was planned for Phase 6d (ETag-based conflict resolution) but the simple mtime check should land sooner.
Fix: compare local file mtime vs blob's last-modified on pull, skip overwrite if local is newer, add to `PersonaReport` a new `skipped_local_newer` counter. Test: pytest fixture with a local file newer than the (fake) blob's content → pull_all must leave it.
- **Effort:** XS (~20 LOC + 2 tests)
- **Depends on:** Phase 6a (`1514dcd`, shipped)
- **Source:** Phase 6a review 2026-04-17; plan §4.2 said we'd do this, Phase 6a deferred

### MCP server orphans when Claude Code exits
Observed twice: when the parent Claude process exits, the `entrabot-mcp` child keeps running. The new Claude session spawns a *second* MCP server, and both servers poll Graph independently — causing dual interaction-log writes (observed 2026-04-17: local log 54 lines vs blob log 19 lines on the same UTC day) and dual channel-push attempts. Root cause: `_background_poll_teams`, `_background_poll_email`, `_background_discover_chats`, and `_background_daily_summary` are spawned as top-level asyncio tasks inside `_initialize()`. They sit outside FastMCP's lifespan cancel scope, so when stdin closes and FastMCP's stdio read loop exits, the polling tasks keep the event loop alive and the process never terminates. Fixes in priority order: (a) spawn background tasks inside FastMCP's lifespan context manager so shutdown cancels them, (b) explicitly watch stdin for EOF in `_initialize` and cancel the task group, or (c) have polling tasks poll a shared shutdown event that FastMCP's stop hook sets. Workaround until fixed: manually `kill <pid>` old `entrabot-mcp` processes.
- **Effort:** S (~40 LOC + test that proves stdin-EOF cancels polls)
- **Source:** Live observation 2026-04-17 (second occurrence in one day)

### Daily summary scheduler: wrong day + double-fire
Two bugs, both observed at 2026-04-17T17:00:00 PDT (= 00:00:01 UTC 2026-04-18):
1. `_run_daily_summary_internal` defaults `target_day = datetime.now(UTC).strftime("%Y-%m-%d")`. At 5pm PDT the UTC clock is already past midnight, so the scheduler summarizes the brand-new UTC day (empty) instead of the one that just ended. Fix: when called from the scheduler, target `now_utc - 1 day` — or compute the "just-ended PDT day" explicitly.
2. Scheduler fired twice at the same second — two summary emails arrived simultaneously (one for 2026-04-17, one for 2026-04-18). Suggests either a boot-time catch-up colliding with the scheduled tick or a loop that doesn't gate on "already sent today." Inspect `_background_daily_summary` for idempotency + single-fire semantics.
- **Effort:** S (~30 LOC + tests for both)
- **Source:** Live observation 2026-04-17 evening (first real scheduled fire)

### ~~Email cursor sub-second precision~~ ✅ DONE
`email_poll.poll_once` returns `latest_ts` verbatim from Graph; the cursor file may end up at second precision while Graph internally compares with sub-second. Result: an email at the cursor's exact second gets re-returned every poll. Per-session dedup in `_background_poll_email` handles within-session, but the email re-pushes once on every server restart. Real fix: bump cursor by 1ms when it equals the latest receivedDateTime, or store sub-second precision unconditionally.
- **Shipped:** `advance_cursor()` bumps the poll watermark by 1 ms after each batch (PR pending).
- **Source:** Live observation 2026-04-17 (a teammate's "Ball game tonight" loop)

### ~~Token auto-refresh in teams_send~~ ✅ DONE
Implemented as `_with_token_retry()` in `mcp_server.py` and `_ensure_valid_token()` (proactive refresh at 55 min). All tools use it.

### ~~AppContainer sandbox production implementation~~ ✅ DONE (MXC sandbox integration)
**Shipped as MXC sandbox integration (Issue #84, ADR-007).** Phase 1 complete: process-level containment via MXC 0.6.0-alpha (macOS Seatbelt). Positive-allowlist filesystem, network blocking, operator ceiling enforcement, binary SHA256 verification, opt-in `run_code` tool. Phase 2 stub (session-bound Entra identity attribution) ready for future APIs. Windows AppContainer + Linux seccomp-bpf deferred to T4/T10.
- **Status:** Phase 1 shipped (1605 tests passing), Phase 2 stub in place
- **Source:** CEO review, refined premise (sandbox co-equal with identity)
- **See:** `docs/decisions/007-mxc-sandbox-integration.md`, `docs/architecture/DESIGN-mxc-sandbox.md`

## P2

### Move provisioner to standalone service for production
Extract the background provisioner from the MCP server process into a separate service that handles Agent User creation server-side. Shipping `Application.ReadWrite.All` client_credentials to end-user machines is a trust boundary issue for production. Embedded provisioner is acceptable for research (single developer machine).
- **Effort:** L (CC: ~M)
- **Depends on:** PR #2 (embedded provisioner ships first as proof of concept)
- **Source:** CEO review + Codex cross-model review, tension point #2

### ~~Graph API 429 rate limit handler~~ ✅ DONE
Implemented as `RetryOn429Transport` in `tools/rate_limit.py`. Wraps httpx async transport — all Graph calls (send, read, create_or_find_chat) auto-retry up to 3 times with Retry-After backoff. 7 tests.

### Persist sent-message IDs across restarts
Serialize the in-memory sent-message-ID set to keyring or local file, reload on startup. Currently the set is lost on restart, meaning prior agent-sent messages in delegated mode could be re-processed as human instructions. The `[EntraBot]` prefix provides a secondary defense (filter messages starting with prefix after restart), but persistence eliminates the gap entirely. ~50 LOC + corruption handling.
- **Effort:** S (CC: ~S)
- **Depends on:** PR #1 (sent-message tracking must ship first)
- **Source:** Eng review + Codex outside voice, tension point #3

### CA policy pre-audit tool (`scripts/audit_ca_policies.py`)
Enumerate Conditional Access policies applicable to the Agent User and flag any that would block silent sign-in (MFA required, device compliance required, sign-in risk thresholds, location-based blocks). Graph `GET /identity/conditionalAccess/policies`, filter by `conditions.users.includeUsers` / included groups containing the Agent User's object ID. Emit a pass/fail report with the policy names that would break silent OIDC federation. Reusable for every future federated target (GitHub, Slack, Jira, Linear), not GitHub-specific.
- **Effort:** S (CC: ~S) — Graph call + policy predicate matching
- **Depends on:** Admin with `Policy.Read.All` Graph scope available to the provisioner token
- **Source:** Eng review of GitHub OIDC federation design, 2026-04-23 (Premise P3/P5 surfaced the need)

### Files MCP — `search_files` + `list_sites` (KQL site search + site enumeration)
Cut from PR1 of `PLAN-files-mcp-tools.md` to keep the permission scope of PR1 coherent (`Files.ReadWrite.All` only). Adding these requires consenting `Sites.Read.All` on the Agent Identity app registration. Adds two MCP tools mirroring the rest of `tools/files.py`. Useful when the agent doesn't have a URL to start from and needs to discover relevant specs by topic.
- **Effort:** S (CC: ~S) — two more tools matching the established shape
- **Depends on:** PR1 of files plan ships first
- **Source:** Eng review of PLAN-files-mcp-tools.md (2026-04-30)
- **See:** `docs/architecture/PLAN-files-mcp-tools.md` §"Deferred / TODOs"

### Files MCP — Excel writes (`excel_write_range`, `excel_append_table_rows`) + workbook session manager
V1.1 follow-up to PR3 (read-only workbook). Adds write capability for Excel ranges and table rows. Workbook session manager batches multiple range operations into a single `workbook` session for performance. Significant Graph API surface for the workbook write endpoints.
- **Effort:** M (CC: ~M)
- **Depends on:** PR3 of files plan ships first (read-only workbook + session lifecycle helper)
- **Source:** Eng review of PLAN-files-mcp-tools.md (2026-04-30)

### Files MCP — Webhook subscriptions for comment-reply notifications
V1.1 currently uses `_background_poll_comments()` at 60s interval for comment replies. Webhook subscriptions on commented driveItems would drop latency to seconds and remove the 60s poll. Requires `subscriptions.create` + tunnel/endpoint for the webhook callback (or use the existing notification channel infra).
- **Effort:** M (CC: ~M)
- **Depends on:** PR1 of files plan ships first; comment-reply polling needs to exist before webhooks replace it
- **Source:** Eng review of PLAN-files-mcp-tools.md (2026-04-30)

### Files MCP — `unshare_file` for clean revocation
`share_file` records the Graph permission ID in the audit log per the failure-mode registry. `unshare_file(drive_id, item_id, permission_id)` calls `DELETE /drives/{drive-id}/items/{item-id}/permissions/{permission-id}` to revoke. Useful when the user says "stop sharing yesterday's draft."
- **Effort:** S (CC: ~S)
- **Depends on:** PR2 of files plan ships first
- **Source:** Eng review of PLAN-files-mcp-tools.md (2026-04-30)

### Files MCP — Office-format authoring (.docx / .xlsx / .pptx) — V2 plan
Deferred to V2 per CEO D2 (HOLD SCOPE on V1). Template + IR + renderer pipeline architecture documented in `docs/architecture/PLAN-files-llm-authoring-v2.md`. PowerPoint authoring in particular needs `python-pptx`. Significantly more surface than V1's Markdown-only authoring.
- **Effort:** L (CC: ~L)
- **Depends on:** V1 (PR1 + PR2 + PR3) ships and stabilizes
- **Source:** CEO review D2 → V2 plan stub
- **See:** `docs/architecture/PLAN-files-llm-authoring-v2.md`

### Files MCP — Site / library creation tools
Not in V1 — `tools/files.py` only operates on existing sites and libraries. Tools for creating new SharePoint sites or document libraries would mirror `POST /sites/{parent-id}/sites` and `POST /sites/{site-id}/lists`. Mostly an admin operation; rare for an agent.
- **Effort:** S (CC: ~S)
- **Source:** Eng review of PLAN-files-mcp-tools.md (2026-04-30)

### Generalize OIDC federation test-fixture pattern (`tests/conftest.py`)
Extract the Hop 4 / OIDC-federation fixture pattern (parametrized `mock_token_endpoint` routing by `grant_type`, `mock_sso_driver`, `mock_oidc_rp_callback`) into session-scoped conftest fixtures keyed by target SaaS. Each future federated target (Slack, Jira, Linear, Copilot Workspace) then adds tests in ~10 lines of test-data instead of ~300 lines of test-machinery. Write the conftest AFTER GitHub fixtures stabilize (so we're generalizing from working code, not imagined code).
- **Effort:** S (CC: ~S) — one conftest module + documentation in a platform-learning doc
- **Depends on:** GitHub OIDC federation ships and its test fixtures stabilize
- **Source:** Eng review of GitHub OIDC federation design, 2026-04-23 — Approach B chosen specifically to generalize, tests should generalize too

### ~~CBA-based Agent User sign-in for external OIDC federation (Phase 0B spike)~~ — BLOCKED, see Learning #41
Phase 0B spike (2026-04-24, evening) proved the CBA pivot is also architecturally blocked. Tenant CBA was enabled, root CA uploaded, user cert generated with correct SANs — but `POST /common/GetCredentialType` for the Agent User returns `CertAuthParams=null, FidoParams=null, RemoteNgcParams=null, SasParams=null`. The `#microsoft.graph.agentUser` directory subtype architecturally excludes ALL interactive auth credential types. Not a config gap; a deliberate Microsoft design decision. TODO superseded by "Entra Agent ID feature request" below. Evidence: `docs/runbooks/hard-won-learnings.md` Learning #41 + design doc "Phase 0B Findings: CBA Also Blocked for agentUser Type" section.

### ~~Phase 0C spike: validate agent-user → SAML helper app → OBO flow end-to-end~~ — COMPLETED 2026-04-24
Phase A + B + C empirically executed 2026-04-24 against werner.ac + werner-co. Results: Entra OBO-SAML flow works end-to-end for assertion minting (Phase A + B validated). GitHub ACS session establishment blocked by InResponseTo protocol incompatibility (Phase C). See Learning #43 in `docs/runbooks/hard-won-learnings.md` for the full evidence trail and `~/Documents/entra-agent-user-oidc-federation-findings.docx` v3 for the publication-ready research artifact. TODO superseded by the feature request entry below.

### Entra Agent ID feature request: close the OIDC-SAML asymmetry for external federation
CORRECTED framing (was "enable OIDC federation to external SaaS"): Microsoft ships the preview agent-user-to-SAML-application flow, so the research finding is not a missing primitive but a specific asymmetry. Feature request to the Entra Agent ID team: (A) add the OIDC-shaped equivalent of the preview SAML flow — same helper-app-OBO pattern but with `requested_token_type=urn:ietf:params:oauth:token-type:id_token` and an `audience` parameter naming the external OIDC RP's client_id — unblocking GitHub OIDC, Slack, Jira, Linear, Copilot Workspace in one primitive; (B) productize the preview SAML flow to GA with clarity on the `<samlp:Response>` envelope vs bare `<Assertion>` distinction and published compatibility guidance for common SAML RPs including GitHub EMU. Evidence: Learnings #40, #41, #42 + curl repros + the SAML preview doc. The ask is narrower and more actionable than the original "add Hop 4" framing.
- **Effort:** S (human: ~4 hours / CC: ~30 min) — draft the post, link Learnings, include the curl repros
- **Depends on:** Phase 0C spike results (useful but not strictly required — the feature request is defensible on the documented asymmetry alone)
- **Source:** Phase 0 + 0B + 0C spike results 2026-04-24

### ADR-006: Agent User OIDC federation infeasibility (write the research artifact)
Write `docs/decisions/006-agent-user-external-oidc-federation-infeasible.md` capturing both spike outcomes and the platform feedback request. This is the research artifact for the GitHub federation thread. Fold in both Learnings #40 and #41, the curl repros, the Microsoft docs confirmations, the GetCredentialType diagnostic, and the three remaining paths (regular User + CBA; GitHub App impersonation; SCIM-admin-minted PAT) with explicit rejection rationale for each against the original thesis. Close the design doc thread.
- **Effort:** S (human: ~4 hours / CC: ~30 min)
- **Depends on:** Phase 0 + 0B design-doc sections (already written)
- **Source:** Phase 0/0B 2026-04-24

## P3

### Unify HTTP stacks (MSAL requests → httpx adapter)
Replace MSAL's default `requests` HTTP backend with an httpx adapter via `msal-extensions`, so the project uses a single HTTP library. Two HTTP libraries in one process increases attack surface and dependency weight.
- **Effort:** S (CC: ~S)
- **Depends on:** PR #1 (MSAL integration must ship first)
- **Source:** CEO review Section 10, technical debt item #1

### Tenant-scoped runtime state for true multi-user support
Add per-tenant scoping for watched_chats, token cache keys, and data directories. Currently acceptable because each Claude Code session spawns its own MCP server process (per-process model). Future scaling may require shared-process support.
- **Effort:** M (CC: ~S)
- **Depends on:** PR #1
- **Source:** CEO review + Codex cross-model review, tension point #4

### Multi-account identity selection (login_hint)
Pass `login_hint` from persisted `IdentitySession` to MSAL `acquire_token_interactive()` on restart, so users with multiple Entra accounts don't get re-prompted. Currently MSAL picks the most recent account, which works for single-account research.
- **Effort:** S (CC: ~S)
- **Depends on:** PR #1 (IdentitySession dataclass + MSAL integration)
- **Source:** Eng review + Codex outside voice, tension point #6

### Restart-after-provisioning as live-swap fallback
If live token swap (PR #2) proves too flaky in practice, implement a restart path: provisioner completes → writes creds to keyring → signals MCP to restart → fast path picks up AGENT_USER on next boot. Insurance policy for the live swap design.
- **Effort:** S (CC: ~S)
- **Depends on:** PR #2 (provisioner + live swap must ship first)
- **Source:** Eng review + Codex outside voice, tension point #5

---

### Unify Mac/Linux/Windows orchestrators on Python (replace setup.sh + setup-windows.ps1)
After Phase 1 of the Windows port ships, the repo will have two parallel orchestrators (`scripts/setup.sh` 1,032 lines bash for Mac/Linux, `scripts/setup-windows.ps1` ~250 lines PowerShell for Windows) calling the same Python helpers. Same learnings (e.g., Learning #7 az JSON-not-TSV) will need fixing in both shells. When the drift causes real bugs, replace both with a single Python orchestrator package (`scripts/entrabot_setup/`).
- **Why:** DRY, testable from pytest on all platforms, easier contributor onboarding.
- **Pros:** One codepath for prereq probes, prompts, env wiring, logging.
- **Cons:** Large refactor; touches working production setup; premature without operational evidence of drift.
- **Effort:** L (CC: ~M-L)
- **Depends on:** Phase 1 of Windows port shipping; ~3 months of operational signal.
- **Source:** /plan-eng-review D1 (2026-04-28) rejected this as scope creep on the Windows port. See `docs/architecture/PLAN-windows-port.md.v1-bak` for the v1 file layout.

### Re-evaluate ctypes ncrypt signer vs .NET subprocess signer (Windows Hop 1)
After ~6 months of operational signal on `auth/cncrypt_signer.py` (ctypes binding to ncrypt.dll), reassess whether the ABI binding is causing maintenance pain. If yes, replace with a small C# console binary called via subprocess for Hop 1 JWT signing.
- **Why:** Codex outside-voice flagged ctypes as fragile interop for security-critical code. We kept ctypes (stable Windows ABI, ~100 lines, no extra build pipeline) but the call could age.
- **Pros:** Managed code more readable; .NET handles padding/struct layout; easier debugging.
- **Cons:** Extra build pipeline (dotnet publish), ship a binary, IPC overhead (~5-20ms per signature), Windows-only complication.
- **Effort:** M (CC: ~M)
- **Depends on:** Windows port Phase 1 shipping with ctypes signer; operational signal of NTSTATUS bugs / struct drift.
- **Source:** /plan-eng-review codex outside voice, tension #1 (2026-04-28).
