# Hard-Won Learnings

Append-only log of gotchas, surprises, and non-obvious behaviors discovered during development and operations. Never delete entries — mark obsolete ones as `[HISTORICAL]`.

## Active Learnings

### Learning #1: Azure CLI Tokens Rejected by Agent Identity APIs

**Date:** 2026-04-06
**Context:** Running setup.sh to create Agent Identity Blueprint
**Problem:** `az rest` calls to Agent Identity beta APIs returned 403
**Root cause:** Azure CLI tokens always include `Directory.AccessAsUser.All` delegated permission. Agent Identity APIs explicitly reject any token containing this permission.
**Fix:** Created a dedicated "Openclaw Provisioner" app registration. Use `ClientSecretCredential` from `azure-identity` to get a clean `client_credentials` token.
**Prevention:** Never use `az rest` or `DefaultAzureCredential` for Agent Identity APIs. Always use a dedicated app with `client_credentials`.

### Learning #2: BlueprintPrincipal Must Be Created Separately

**Date:** 2026-04-06
**Context:** Creating Agent Identity after Blueprint
**Problem:** Agent Identity creation failed with 400: "The Agent Blueprint Principal for the Agent Blueprint does not exist"
**Root cause:** Creating a Blueprint (`POST /applications`) does NOT auto-create its BlueprintPrincipal (service principal). This is an explicit second step.
**Fix:** Always `POST /servicePrincipals` with `@odata.type: AgentIdentityBlueprintPrincipal` immediately after Blueprint creation. Also check on the skip path (idempotent re-runs).
**Prevention:** Follow the implement-agent-id skill checklist.

### Learning #3: Token Responses Return Error Dicts, Not Exceptions

**Date:** 2026-04-06
**Context:** Token exchange returning errors
**Problem:** Accessing `result["access_token"]` threw KeyError with no context
**Root cause:** Entra token endpoint returns `{"error": "...", "error_description": "..."}` on failure as JSON, not HTTP errors. This is the OAuth2 convention.
**Fix:** Check every token response: `if "error" in data: raise TokenExchangeError(...)`.
**Prevention:** Never access `access_token` without checking for `error` key first.

### Learning #5: Agent IDs Cannot Have Password Credentials

**Date:** 2026-04-06
**Context:** Trying to create an agent as a regular Entra user with a password
**Problem:** Agent Identities are service principals without backing application objects. `passwordCredentials` returns `PropertyNotCompatibleWithAgentIdentity`.
**Root cause:** Agent IDs are designed for managed identity federation and certificates, not passwords.
**Fix:** Use client credentials on the Blueprint (which IS an application) for device-local scenarios. Production uses managed identity + federated credentials.
**Prevention:** Never create "fake users" for agents. Always use the Agent Identity Blueprint → Agent Identity pattern.

### Learning #6: Never Redirect Stderr to /dev/null

**Date:** 2026-04-06
**Context:** Admin consent failure was invisible, token acquisition failure was invisible
**Problem:** `2>/dev/null` hid the actual error messages, turning specific failures into generic "something failed" messages
**Root cause:** Copy-pasted shell patterns from examples that prioritize clean output over debuggability
**Fix:** Removed all instances of `2>/dev/null` from scripts. Guard `source .env` with `[ -f .env ]` instead.
**Prevention:** Never swallow stderr. Errors must always be visible.

### Learning #7: az CLI JSON Output Safer Than TSV

**Date:** 2026-04-06
**Context:** `az ad app credential reset --query password -o tsv` included Azure CLI WARNING text
**Problem:** The extracted password was corrupted by a WARNING message about protecting credentials
**Root cause:** `-o tsv` outputs to stdout, but Azure CLI also writes warnings to stdout (not stderr) in some cases
**Fix:** Parse full JSON output with Python: `json.loads(output)['password']`
**Prevention:** Use `-o json` and parse with Python/jq, not `-o tsv`.

### Learning #8: Permission Propagation Takes 30-120 Seconds

**Date:** 2026-04-06
**Context:** Token acquisition after admin consent returned cached claims without new permissions
**Problem:** Immediate token acquisition after consent got a token without Agent Identity permissions
**Root cause:** Entra's token endpoint serves cached claims for 30-120s after permission changes.
**Fix:** 10-40s retry backoff + 30s explicit wait after consent.
**Prevention:** Always add propagation delay after permission changes.

### Learning #9: Agent User UPN Must Use a Verified Domain

**Date:** 2026-04-06
**Context:** Creating Agent User via `POST /beta/users` with `@odata.type: microsoft.graph.agentUser`
**Problem:** 400: "The root domain of the specified UPN does not belong to a verified domain"
**Root cause:** `az account show` has no `tenantDefaultDomain` field. Code fell back to `{tenant-id}.onmicrosoft.com` which is not a verified domain.
**Fix:** Extract the domain from the signed-in user's UPN via `az ad signed-in-user show --query userPrincipalName`. That domain is always verified.
**Prevention:** Never construct UPN domains from tenant IDs. Always derive from an existing verified UPN.

### Learning #10: oAuth2PermissionGrant Requires startTime

**Date:** 2026-04-06
**Context:** Creating consent grant for Agent User to use Graph Chat/Teams permissions
**Problem:** 400: "Missing property: startTime"
**Root cause:** The Graph API now requires a `startTime` field on `oAuth2PermissionGrant` creation. This wasn't required in older API versions and isn't mentioned in most examples.
**Fix:** Add `"startTime": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")` to the request body.
**Prevention:** Always include `startTime` in `oAuth2PermissionGrant` creation.

### Learning #11: Provisioner Needs DelegatedPermissionGrant.ReadWrite.All for Consent

**Date:** 2026-04-06
**Context:** Creating `oAuth2PermissionGrant` for Agent User → Graph permissions
**Problem:** 403: "Insufficient privileges to complete the operation"
**Root cause:** The provisioner app had Agent Identity and Application permissions but lacked `DelegatedPermissionGrant.ReadWrite.All` — needed to create delegated permission grants on behalf of the Agent User.
**Fix:** Added `DelegatedPermissionGrant.ReadWrite.All` and `User.ReadWrite.All` to `BASE_PERMISSION_VALUES` in `entra_provisioning.py`.
**Prevention:** The provisioner needs permissions for everything it does: Blueprint CRUD, Agent Identity CRUD, Agent User CRUD, license assignment, AND consent grants. All are in `BASE_PERMISSION_VALUES` + dynamic `AgentIdentity`/`AgentIdUser` discovery.

### Learning #12: Three-Hop Flow Requires fmi_path Parameter

**Date:** 2026-04-06
**Context:** Hop 2 of the three-hop Agent User flow failing with AADSTS700211
**Problem:** "No matching federated identity record found for presented assertion issuer"
**Root cause:** Hop 1 was requesting `scope=https://graph.microsoft.com/.default` (a Graph resource token) instead of `scope=api://AzureADTokenExchange/.default` (a token exchange token). It also lacked the `fmi_path` parameter that tells Entra which Agent Identity this token is for.
**Fix:** Hop 1: `scope=api://AzureADTokenExchange/.default`, `fmi_path={agent-identity-id}`. Hop 3: add `requested_token_use=on_behalf_of`.
**Prevention:** Follow the exact protocol from the Microsoft docs: "Agent's user account impersonation protocol". The `fmi_path` parameter is essential and non-obvious.

### Learning #13: Existing Non-Teams Licenses Don't Count

**Date:** 2026-04-06
**Context:** License assignment step skipping because Agent User already had a license
**Problem:** Agent User had Azure AD Premium P1 inherited from an "All Users" group, but P1 doesn't include Teams. The license check saw "has 1 license" and skipped.
**Root cause:** Checking `len(assignedLicenses) > 0` instead of checking whether any license is Teams-capable.
**Fix:** Resolve SKU IDs to part numbers and check against `TEAMS_CAPABLE_SKUS` list.
**Prevention:** Always check license capabilities, not just presence.

### Learning #14: MCP Tool Names Must Match User Intent

**Date:** 2026-04-06
**Context:** LLM client not calling `openclaw_teams_send` when user said "message brandon"
**Problem:** The LLM read the tool descriptions but didn't connect "message brandon@werner.ac" with a tool named `openclaw_teams_send`
**Root cause:** Namespaced tool names (`openclaw_teams_send`) are jargon. The LLM looks for intent matches, not namespace patterns.
**Fix:** Renamed to `send_teams_message`, `read_teams_messages`, `whoami`, `audit_log`. Added trigger phrases to descriptions: "message", "notify", "tell", "ping", "contact". Added FastMCP `instructions` field with intent→tool mapping.
**Prevention:** Name tools as verbs the user would say. Pack descriptions with synonyms.

### Learning #15: oAuth2PermissionGrants Must Use v1.0 API, Not Beta

**Date:** 2026-04-06
**Context:** Consent grant for Agent User returning 403 even with correct permissions
**Problem:** `graph_request()` helper prepends `GRAPH_BASE` which is `https://graph.microsoft.com/beta`. The `oAuth2PermissionGrants` endpoint on beta either behaves differently or has stricter permission requirements than v1.0.
**Root cause:** The consent grant function used `graph_request("POST", "/oauth2PermissionGrants", ...)` which called `https://graph.microsoft.com/beta/oauth2PermissionGrants`. The provisioner's permissions worked on v1.0 but got 403 on beta.
**Fix:** Use `requests.post("https://graph.microsoft.com/v1.0/oauth2PermissionGrants", ...)` directly instead of `graph_request()`. Also changed the error from a WARNING (non-blocking) to `sys.exit(1)` (blocking) because without consent, hop 3 always fails.
**Prevention:** When a Graph API exists on both v1.0 and beta, use v1.0 for stability. Don't assume `graph_request()` is correct for everything — check which API version the endpoint needs.

### Learning #16: Graph API $filter and $orderby Unreliable for Chat Messages

**Date:** 2026-04-06
**Context:** Designing bidirectional Teams polling loop, researching existing Teams MCP servers
**Problem:** Graph API chat message endpoints don't reliably support `$orderby` or `$filter`. Requesting ascending order returns errors. Server-side filtering produces inconsistent results.
**Root cause:** Confirmed by floriscornel/teams-mcp (most feature-complete Teams MCP server, 9k+ users). This appears to be a Graph API limitation for `/chats/{id}/messages` endpoints specifically.
**Fix:** Always sort and filter client-side after retrieval. Never trust Graph API server-side filtering for chat messages.
**Prevention:** Treat Graph API response ordering as "newest-first, descending only" for chat messages. Do all filtering in Python.

### Learning #17: Timestamp-Based Polling Needs Overlap Window for Message Boundary Safety

**Date:** 2026-04-06
**Context:** Designing message dedup for `watch_teams_replies`, researching iMessage MCP servers
**Problem:** Polling with `WHERE sent_at > last_seen_timestamp` can miss messages that arrive at the exact timestamp boundary due to clock precision and write ordering.
**Root cause:** photon-hq/imessage-kit (reference iMessage SDK) documented this: messages written to the database at the same clock tick as the poll cutoff may be missed if the poll fires before the write commits.
**Fix:** Use a 2-second overlap window: query `sent_at >= last_seen_timestamp - 2s`, then filter duplicates via a message ID seen-set. The overlap guarantees boundary messages are caught; the seen-set prevents reprocessing.
**Prevention:** Never use strict `>` comparison for timestamp-based polling. Always overlap + dedup.

### Learning #18: Token Refresh Is the #1 Pain Point Across All MCP Messaging Servers

**Date:** 2026-04-06
**Context:** Researching Slack, iMessage, Discord, and Teams MCP servers for bidirectional loop design
**Problem:** The official Slack MCP server (mcp.slack.com) has 1-hour OAuth tokens with NO refresh token, causing 18 re-authentications over 5 days (anthropics/claude-code#29257). Our three-hop OBO flow is even more complex.
**Root cause:** OAuth token expiry is the universal pain point. Every MCP messaging server that doesn't handle refresh creates user-facing auth failures during active sessions.
**Fix:** Eager refresh (55-min threshold, 5-min buffer) + lazy retry (catch 401, re-auth, retry once). Both update the same `_state` fields.
**Prevention:** For the three-hop flow specifically: all three hops share the same ~60-min expiry window since they're acquired sequentially. Refreshing the full chain (all 3 hops) is simpler than tracking per-hop expiry. Monitor for edge cases — nobody else has refreshed a chained OBO flow mid-session.

### Learning #19: Every MCP Messaging Server Uses Stateless Request-Response, Not Background Polling

**Date:** 2026-04-06
**Context:** Researching polling patterns across Slack, iMessage, Discord, and Teams MCP servers
**Problem:** We considered background polling threads and CronCreate-based approaches for the bidirectional loop.
**Root cause:** The MCP protocol's request-response model maps naturally to on-demand tool calls. The LLM decides when to check for messages. Background polling requires a push notification mechanism, but Claude Desktop doesn't support MCP resource subscriptions.
**Fix:** Our design — a blocking `watch_teams_replies` tool that polls internally — aligns with the ecosystem pattern. The LLM calls it explicitly, and it blocks for up to `timeout` seconds.
**Prevention:** Don't fight the MCP model. On-demand polling tools are the pragmatic choice until the MCP Tasks primitive (experimental, spec 2025-11-25) is broadly supported.

### Learning #20: Bounded Seen-Set Prevents Memory Leaks in Long-Running MCP Servers

**Date:** 2026-04-06
**Context:** Designing message dedup for long-running polling sessions
**Problem:** A naive dedup approach (append every message ID to a set forever) leaks memory proportional to session length.
**Root cause:** photon-hq/imessage-kit solved this with threshold-triggered cleanup: when the Map exceeds 10,000 entries, prune to only the last hour's records.
**Fix:** Cap seen-set at 500 entries (our volume is much lower than iMessage). When threshold is hit, prune to IDs from last 10 minutes.
**Prevention:** Always bound in-memory state in long-running processes. Define a cleanup threshold and retention window.

### Learning #21: Graph API Delta Queries — Powerful but Complex, Deferred for Now

**Date:** 2026-04-06
**Context:** Evaluating cursor strategies for Teams message polling
**Problem:** Graph API's `/chats/{id}/messages/delta` returns a `$deltaLink` token (monotonic cursor, no clock issues), but adds complexity: delta responses include `@removed` entries (deleted messages), read-state changes, and unexpected change types that don't match the original filter.
**Root cause:** Delta queries are designed for sync scenarios (mailbox sync, etc.), not simple "what's new" polling. The extra event types require handling code that adds surface area for bugs.
**Fix:** Start with timestamp overlap + message ID seen-set (proven by iMessage servers, simpler). Defer delta queries as an optimization for when polling volume increases or timestamp approach proves insufficient.
**Prevention:** Evaluate the full contract of an API before adopting it. Delta queries solve a different problem (bidirectional sync) than what we need (new message detection).

### Learning #22: The MCP "Close the Loop" Problem — No Solution Exists in Any Major Client

**Date:** 2026-04-06
**Context:** After building `watch_teams_replies`, discovered the LLM doesn't call it automatically after `send_teams_message` — it says "done" and stops. Human's replies go into the void.
**Problem:** MCP is request-response. The LLM drives all interaction. There is no mechanism for the server to wake up the LLM when new data arrives. This is not a bug in our implementation — it is a fundamental protocol gap.
**Root cause:** LLMs are request-response systems. Message roles ("user", "assistant", "system") don't accommodate external events. There is no "tool_push" role. Even with perfect MCP notifications, something must inject a new "turn" into the conversation.
**Industry status:** The MCP Triggers & Events Working Group was chartered March 24, 2026 (led by AWS + Anthropic). RFC "Events in MCP v1" targeting end of April 2026. No solution exists today.
**What we tried:** Discord MCP sends JSON-RPC notifications — Claude Code ignores them. Resource subscriptions — closed as "not planned" (Issue #7252). Tasks primitive — no client supports it. Hook-based tool chaining — closed as "not planned" (Issue #4992).
**Current workarounds:** (1) PostToolUse hook with `additionalContext` to hint the LLM should poll, (2) Stop hook with agent subagent to catch missed replies, (3) Desktop scheduled task for autonomous loops.
**Prevention:** When the Triggers & Events WG ships its spec, adopt immediately. Our polling infrastructure (`watch_teams_replies`) already works — we just need to swap "LLM decides to poll" to "server pushes event."
**See also:** `docs/platform-learnings/mcp-close-the-loop.md` for the full research with sources.

### Learning #24: Human Tokens Cannot Bootstrap the Agent Identity Chain

**Date:** 2026-04-06
**Context:** Investigating whether a human interactive sign-in could replace client_credentials in Hop 1 of the three-hop flow, eliminating the need for client secrets on devices.
**Problem:** Client secrets in `.env` files are fragile, hard to rotate, and explicitly warned against by Microsoft for production.
**Root cause:** All agent entities (Blueprint, Agent Identity, Agent User) are **confidential clients**. Microsoft states: "Interactive flows aren't supported for any agent entity type." Hop 2's audience validation requires T1 to come from the Blueprint specifically — a human token has the wrong audience.
**Fix:** Use certificate-based auth instead. Replace `client_secret` with `client_assertion` (JWT signed by a private key in macOS Keychain / Windows TPM). Drop-in replacement for Hop 1, no architecture change needed.
**Prevention:** When looking for auth alternatives, check the client type requirement first. Confidential clients can never use interactive flows. See ADR-003.

### Learning #25: Agent OBO Is a Separate Flow Where Human Tokens Enter at Hop 2

**Date:** 2026-04-06
**Context:** Researching human-to-agent auth alternatives
**Discovery:** Microsoft documents an "Agent OBO" flow where a human user's token IS used — but it enters at Hop 2 as the OBO `assertion`, not at Hop 1 as the Blueprint credential. The Blueprint still authenticates with its own confidential credentials. This flow is for "interactive agents" that act on behalf of a signed-in user, NOT for autonomous agents like Openclaw.
**Implication:** If Openclaw ever adds a mode where the agent acts on behalf of a specific human (not as its own digital worker), the Agent OBO flow provides that pattern. The human token + Blueprint credential together produce an Agent Identity token scoped to that human's permissions.

### Learning #26: Channel Notifications Require Experimental Capability + Startup Flag

**Date:** 2026-04-07
**Context:** Background poll detected Teams messages and pushed notifications via MCP write stream, but Claude Code silently dropped them.
**Problem:** `notifications/claude/channel` was being sent correctly through the transport but Claude Code never reacted.
**Root cause:** Three requirements for channel notifications, all undocumented outside source code:
1. Server must declare `experimental: {"claude/channel": {}}` capability during MCP initialization
2. Claude Code must be started with `--dangerously-load-development-channels server:<name>` (or `--channels` for allowlisted plugins)
3. Server must NOT be spoofed as a marketplace plugin — just use `.mcp.json` with the flag
**Fix:** Added `experimental_capabilities={"claude/channel": {}}` to `create_initialization_options()`. User starts Claude Code with `claude --dangerously-load-development-channels server:openclaw`.
**Prevention:** When implementing MCP notifications, check the iMessage channel plugin source for the exact capability declarations and startup requirements. The official docs at `code.claude.com/docs/en/channels-reference` document the flags.

### Learning #27: Background Poll Must Not Share State With Polling Tool

**Date:** 2026-04-07
**Context:** Background poll and `watch_teams_replies` tool both detecting messages, but messages only visible to one.
**Problem:** Both used the same `_state["seen_message_ids"]` and cursor. Background poll detected a message, marked it "seen", pushed a notification. If the notification didn't reach Claude Code (before we fixed Learning #26), the message was consumed but never delivered. `watch_teams_replies` couldn't see it either — already in the seen-set.
**Fix:** Background poll uses its own local variables (`bg_seen_ids`, `bg_last_ts`) completely independent of `watch_teams_replies`' state. Both can detect the same message independently — belt and suspenders.
**Prevention:** Concurrent consumers of the same data source must have independent tracking state. Never share dedup state between a "best-effort" path (notifications) and a "guaranteed" path (explicit tool call).

### Learning #23: FastMCP Context Object Has Untapped Capabilities

**Date:** 2026-04-06
**Context:** Researching mechanisms for server-to-LLM communication within a tool call
**Problem:** We needed to understand what FastMCP provides beyond basic tool return values.
**Discovery:** FastMCP's `Context` object exposes: `ctx.sample()` (ask the LLM to generate text mid-tool), `ctx.elicit()` (request structured input), `ctx.report_progress()`, `ctx.set_state()`/`ctx.get_state()` (session state persistence), and `ctx.send_notification()` (for spec-defined notification types).
**Implications:** `ctx.sample()` could theoretically let `watch_teams_replies` re-engage the LLM when a reply arrives — but this is untested with Claude Code's MCP client and likely unsupported. `ctx.set_state()`/`ctx.get_state()` could replace our manual `_state` dict for cursor and seen-set management in a future refactor.
**Prevention:** Before building custom infrastructure, always check what the framework provides. FastMCP's Context is much richer than we initially used.

### Learning #28: B2B Guest Messaging Requires Federated Chat (Example 7), NOT Guest Role (Example 6)

**Date:** 2026-04-07 (updated 2026-04-08)
**Context:** Messaging Microsoft employees invited as B2B guests into the werner.ac tenant
**Problem:** `POST /chats` returned 200 and `POST /chats/{id}/messages` returned 200, but the external user never received the messages. Tried multiple approaches — all returned 200 but produced invisible chats.
**Investigation (what DIDN'T work):**
1. `chatType: "oneOnOne"` + `role: "owner"` + guest object ID → phantom chat, invisible
2. `chatType: "group"` + `role: "guest"` + guest object ID (Example 6) → chat created with correct members verified via `GET /members`, but completely invisible in Teams
3. The guest object ID (`963835fc-...`) simply cannot receive Teams messages regardless of role or chatType. Graph API accepts it silently every time.
**Root cause:** B2B guest objects in your tenant are NOT the same as the real user identity. The guest object ID is a local shadow — Teams doesn't deliver messages to it. You must reference the user by their **home tenant identity** via Example 7 (federated).
**What WORKS — Example 7 (federated):**
- `user@odata.bind`: use the user's **email** (e.g., `brandwe@microsoft.com`), NOT the guest object ID
- `tenantId`: the user's **home tenant GUID** (e.g., `72f988bf-...` for microsoft.com)
- `role`: `"owner"` (NOT "guest")
- `chatType`: `"oneOnOne"` works fine
- Graph resolves the email + tenantId to the user's REAL identity in their home tenant, creating a proper federated chat
**Additional gotcha:** `az ad user show` can return `userType: null` for guests — Python `print(None)` outputs literal `"None"`. Must convert null → empty string, then fall back to UPN `#EXT#` pattern for guest detection.
**Fix:** Detect guest via `userType` or `#EXT#` UPN, resolve home tenant GUID via OpenID discovery, use email + tenantId in chat payload (Example 7).
**Prevention:** Never use the guest object ID for Teams messaging. Always resolve the user's home tenant and use their email as a federated reference.

### Learning #29: Shell Capture of stdout-bearing Diagnostics Corrupts .env

**Date:** 2026-04-17
**Context:** `setup.sh` regenerating Blueprint cert. Inline Python called `get_graph_token()` (which prints diagnostic lines to stdout) then printed the cert thumbprint as the final line. Outer shell did `CERT_THUMBPRINT=$(...)` and wrote to `.env`.
**Problem:** `.env` ended up with `ENTRACLAW_BLUEPRINT_CERT_THUMBPRINT=  Ensuring 25 Graph application permissions on provisioner app...` — multi-line garbage, not the thumbprint. Hop 1 then failed `invalid_client` because the JWT `x5t` header didn't match any registered cert.
**Root cause:** Anything that writes to stdout inside a `$(...)` capture becomes part of the captured value. Diagnostic prints from helper functions are easy to forget about.
**Fix:** `with contextlib.redirect_stdout(sys.stderr): token = get_graph_token(...)` so diagnostic output goes to stderr (visible to the user, not captured). Plus: validate the captured value matches the expected shape (`^[A-Za-z0-9_-]{43}$` for SHA-256 base64url-no-pad) before writing `.env`. Fail loud on mismatch.
**Prevention:** Any shell `$(...)` capture of an inline-Python block must redirect or suppress diagnostic output. Always shape-check captured values before writing them to config files.

### Learning #30: Lazy `_initialize()` Leaves the MCP Server Deaf

**Date:** 2026-04-17
**Context:** MCP server boot — background polls only started inside `_initialize()`, which was called lazily from each `@mcp.tool()` (`await _initialize()` at the top of every tool function).
**Problem:** Fresh MCP server processes that hadn't been hit by any tool call were observed to silently miss every inbound DM and email. The "Pushed Teams message" log line never appeared because `_background_poll()` was never spawned. Brandon could see DMs in Teams; the agent saw none.
**Root cause:** The eager-init code paths only fired on first tool invocation. A long-idle session (or a session where the agent had nothing to call) would never wake the polls.
**Fix:** Spawn `_initialize()` as a concurrent task in `_run_stdio_with_write_stream`, immediately after capturing the write_stream. Background polls start at server boot, regardless of tool activity.
**Prevention:** Anything that should start "when the server is alive" belongs in the stdio-server lifecycle, not gated behind tool calls.

### Learning #31: Teams Chat `replyToId` Is Channel-Only — Use `<attachment id=…>` in Body

**Date:** 2026-04-17
**Context:** Adding reply-detection so the agent can continue active 1:1 exchanges in group chats without re-`@`-tagging on every turn.
**Problem:** Graph's `replyToId` field on chat messages is always `null`. Verified empirically: 8/8 recent IDNA chat messages had `replyToId: None`, including ones that were unambiguously quote-replies via the Teams UI.
**Root cause:** `replyToId` is populated only in **channel** messages (the formally-threaded ones). Chats are flat sequences. When a user hits the Teams "Reply" UI in a chat, Graph encodes the quoted source as an `<attachment id="SOURCE_MESSAGE_ID"></attachment>` tag embedded in the body HTML — that's the only signal.
**Fix:** Parse `<attachment id="…">` out of the body in `tools/teams.py` `read()` (`extract_reply_to_ids()`), surface as `reply_to_ids: list[str]` per message. Implicit-continuation reply detection (no formal Reply UI use) requires a heuristic — we use "my last message in this chat was within 10 min and no other human posted since."
**Prevention:** When you see "we should detect X," check whether Graph actually exposes the metadata. Channel-vs-chat semantics differ in surprising ways.

### Learning #32: MCP Notification Schema Divergence Closes the Stream Silently

**Date:** 2026-04-17
**Context:** Email-push notifications via `notifications/claude/channel`. Email push schema diverged from Teams push schema in two ways: (a) content rendered sender as `Name <email@addr>` (looks like an unknown HTML tag); (b) meta carried extra keys (`channel`, `subject`, `encrypted`) not present in Teams push meta.
**Problem:** Every time the email poll fired and pushed a notification, the MCP server died silently within ~1 second. No exception, no signal, no traceback in `entraclaw.log`. Looked like a Python crash; was actually a clean shutdown via stdin EOF — Claude Code closed the stream after our notification, the server's `mcp._mcp_server.run()` returned, anyio teardown ran. Captured via `scripts/entraclaw-mcp-debug.sh` (a wrapper that tees stderr to `/tmp/entraclaw-debug.log`).
**Root cause (likely):** Strict client-side channel handler refused the notification — either the angle-bracketed content (HTML-tag-like) or the unfamiliar meta keys triggered a close.
**Fix:** Render sender as `Name (addr)`. Shrink meta to exactly the Teams-push superset (`chat_id` synthetic value `"email"`, `message_id`, `user`, `ts`). Wrap `write_stream.send` in try/except so future transport failures log and return instead of propagating. ALSO: per-session message-id dedup in `_background_poll_email` to defend against cursor-precision drift causing repeated push of the same message.
**Prevention:** Channel-notification payloads should follow a single schema across all sources. Any new source's meta keys go through the same shape as existing sources or risk silent rejection. When the MCP server "crashes" with no Python trace, suspect stdin EOF (clean teardown) before suspecting a bug in your code.

### Learning #33: Chat-Creation Code Paths Must All Auto-Register for Polling

**Date:** 2026-04-17
**Context:** Diana's reply in the repo-share group chat went unanswered for 2.5 hours. Ryan's similar message went 4 minutes. Weinong's DM 17 hours.
**Problem:** The MCP `create_chat` tool wrapper auto-registered new chats into `watched_chats`. The underlying `entraclaw.tools.teams.create_or_find_chat` and `create_one_on_one_chat` functions DID NOT. Chats created via raw Python scripts (or by external humans adding the Agent User) silently never got polled.
**Root cause:** Auto-registration was a side-effect of one specific entry point, not a property of the underlying chat-creation primitive. Easy to bypass.
**Fix:** Background `_background_discover_chats()` task hits `GET /me/chats` every 120s and registers any chat not in `_state["watched_chats"]`. Catches chats from raw Python, MCP tool, or external-add. Also persists to file so restarts inherit. Net latency from "chat exists" → "agent watching it": ≤2m05s.
**Prevention:** Don't rely on a single entry point for state-shaping side effects. If "I want all chats polled," that's a property of the polling system, not of the tool that happens to create chats. Auto-discovery via the canonical Graph endpoint is more robust.

### Learning #34: Storage Scope Needs Its Own Consent Grant — RBAC Alone Isn't Enough

**Date:** 2026-04-17
**Context:** ADR-005 Phase 5 shipped. Setup.sh successfully provisioned the storage account, container, and `Storage Blob Data Contributor` RBAC scoped to the Agent User's oid. Then migration failed on every file with `AADSTS65001: The user or administrator has not consented to use the application`.
**Problem:** RBAC governs **what a token can do**. The third hop of the Agent User flow (`user_fic` grant for `https://storage.azure.com/.default`) only succeeds if there's an existing `oauth2PermissionGrant` authorizing the Agent Identity to request delegated Storage scopes **on behalf of** the Agent User. Storage RBAC is necessary but not sufficient.
**Root cause:** The provisioner only did Azure resource-plane work (`az storage ...`, `az role assignment create`). It never touched Graph to add the `user_impersonation` scope grant on the Azure Storage SP (appId `e406a681-f3d4-42a8-90b6-c2b029497af1`).
**Fix:** Added `grant_agent_user_storage_consent()` to `scripts/create_entra_agent_ids.py` — same Principal-scoped `oauth2PermissionGrant` pattern as the existing Graph consent, but targeting the Storage SP with scope `user_impersonation`. Wired into `main()`. Idempotent (PATCH to merge scopes if grant already exists).
**Prevention:** For any new resource-plane capability that the Agent User needs to act against, the provisioning flow has TWO steps: (1) Azure data-plane RBAC, and (2) Graph `oauth2PermissionGrant` for the delegated scope on that resource's SP. Both are required. Separate `_resolve_sp_object_id_by_app_id(token, app_id)` helper makes adding future resource scopes trivial.

### Learning #35: Setup.sh Must Track Sub-Step Failures — Don't Print "Setup Complete" After a Failed Migration

**Date:** 2026-04-17
**Context:** Step 7b migration printed 10 errors in plain text, then `[8/8] Setup complete` banner in green. User correctly called this out as brittle.
**Problem:** `setup.sh` steps were treated as pass/fail at the shell-exit-code level only, but a Python heredoc that iterates files and collects errors in a list doesn't exit non-zero unless the entire script raises. The inner migration saw 10 AADSTS errors but completed "successfully."
**Root cause:** The inline `python -c` heredoc printed errors to stdout but exited 0. There was no shell-level tracking of sub-step failure, and the summary banner unconditionally printed "Setup complete".
**Fix:** (1) Python heredoc now calls `sys.exit(2)` when `report.errors` is non-empty. (2) Shell captures that exit code into `MIGRATION_FAILED` flag via `|| MIGRATION_RC=$?`. (3) Summary banner branches on `MIGRATION_FAILED` — renders red "Setup INCOMPLETE" block instead of green "Setup complete". (4) Script exits with code 2 on failure. (5) Errors render in ANSI red so they don't hide in the success-green noise.
**Prevention:** Any multi-step shell orchestrator that calls sub-tools must: (a) treat sub-tool non-zero exit as first-class failure data, (b) never paint over failures in the final summary, (c) render error output in a visually distinct color, (d) propagate the failure via its own exit code so CI / wrapping automation sees it.

### Learning #36: Sub-Agent Worktree `pip install -e .` Silently Re-Points the Parent Venv

**Date:** 2026-04-21
**Context:** After PRs #27 and #28 (lifecycle + cached-host fixes) merged to main, production MCP server kept behaving as if the fixes weren't there — Teams polling looked alive in logs, but zero inbound messages were ever pushed through. Spent hours writing more patches; none took effect.
**Problem:** The MCP server's Python process imported `entraclaw` from one of the sub-agent worktrees (`.claude/worktrees/agent-*/src/entraclaw/...`), not from the main tree. Worktrees don't carry `.env`, so `_load_dotenv()` resolved `Path(__file__).resolve().parents[2] / ".env"` to a path inside the worktree where no `.env` exists — `ENTRACLAW_BLUEPRINT_APP_ID` never loaded, auth never initialized, and every Graph call 401'd silently inside the poll loop's `except Exception`.
**Root cause:** Several sub-agents, when their isolated worktree didn't have a venv, ran `pip install -e .` using the **parent venv** (the main repo's `.venv/bin/pip`). `-e .` is a PATH-modifying operation: it rewrites the parent venv's editable-install pointer (`site-packages/_openclaw_identity_research.pth` / the equivalent `direct_url.json` entry) to point at the worktree's source tree. Every subsequent `entraclaw-mcp` boot from the parent venv loaded the worktree's code. The change is silent — no warning from pip, no diff visible in `git status`, no error at server boot.
**Fix:** From the main repo, re-run `cd /Volumes/Development\ HD/openclaw-identity-research && .venv/bin/pip install -e . --no-deps`. That repoints the editable install back at the main tree. Verify with `.venv/bin/python3 -c "from entraclaw import config; print(config.__file__)"` — the path must not contain `.claude/worktrees/`.
**Prevention:** (1) Every sub-agent dispatch prompt that expects to run `pip install -e .` MUST explicitly create a fresh venv inside the worktree first (`python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`) and never invoke the parent venv's pip. (2) After any session that used sub-agent worktrees, verify the main venv's editable-install target via `.venv/bin/python3 -c "from entraclaw import config; print(config.__file__)"` before trusting the production server. (3) Consider a pre-boot assertion in `mcp_server.py::_load_dotenv` that logs a fatal warning when the resolved `.env` path contains `.claude/worktrees/` — the one place this fails silently is the one place it most needs to fail loud.

### Learning #37: Listing Yourself as an MCP Peer = Fork-Bomb at Boot

**Date:** 2026-04-22
**Context:** PR #35 (efferent-copy dispatch middleware) shipped. `discover_sinks()` at boot enumerates every peer in `.mcp.json` and opens a `stdio_client` session to check for a compatibly-shaped `observe` tool. `.mcp.json` in this repo lists `entraclaw` itself as a stdio peer (so other hosts can find it).
**Problem:** Within 60 seconds of PR #35 merging, `~/.entraclaw/logs/entraclaw.log` began showing ~30 `Starting EntraClaw MCP server` events per minute from short-lived child processes. Continued for 2h+ before being caught. Chained with Learning #38 to silently drop ~99% of Teams DM pushes for the afternoon.
**Root cause:** Parent entraclaw's `discover_sinks` spawned a child entraclaw-mcp to check for `observe`. Child booted and ran its OWN `discover_sinks`, spawning a grandchild. Grandchild spawned a great-grandchild. Each level's 5-second per-peer timeout only partially bounded the recursion — processes piled up faster than they drained. Each child did a full boot (auth, poll-loop, background tasks) before dying, which also clobbered shared blob state. `ClientSession(read, write)` opened without an explicit `client_info` inherits the MCP SDK default `Implementation(name="mcp", version="0.1.0")` — so every child initialized identifying as `"mcp"`, not `"claude-code"` (Learning #38 chain).
**Fix:** `efferent_copy._is_self_referential_peer(peer)` resolves `peer.command` against `sys.argv[0]` / `sys.executable`; matching peer is skipped at factory-build time, never reaching `stdio_client`. Belt-and-suspenders: `_stdio_factory` sets `EFFERENT_COPY_DISABLE=1` in the spawned subprocess's env so any subprocess we do spawn short-circuits its own discovery. Spawn depth bounded at 1. Ships in PR #36 (commit `8a00939`).
**Prevention:** (1) Any middleware that iterates `.mcp.json` peers MUST filter peers whose stdio `command` resolves to our own executable — never open a session against yourself. (2) Any MCP client session we open as a subprocess MUST carry an explicit `EFFERENT_COPY_DISABLE=1` (or equivalent feature-flag) in its env so recursive discovery is impossible even if (1) is bypassed. (3) A `.mcp.json` structure that names the current server as a peer should be inspected at boot and logged (not as an error — it's valid config — but as "skipping self-referential peer `<name>`" so future debugging can see the decision). (4) Regression test both the filter and the env propagation: see `tests/test_efferent_copy.py::TestDiscoverSinks::test_self_referential_peer_is_skipped_without_spawning` and `test_stdio_factory_sets_efferent_copy_disable_in_child_env`.

### Learning #38: Leader-Cache Overwrite Turns Cascade Noise into Silent Data Loss

**Date:** 2026-04-22
**Context:** Entraclaw's `_capture_host_from_initialize` stored `clientInfo.name` from every MCP Initialize handshake into `_state["cached_host"]` unconditionally. `_is_leader_host()` read the cache and returned `True` only if the value was in `LEADER_HOSTS = frozenset({"claude-code", "claude code"})`. `_push_channel_notification` gated every Teams DM push on `_is_leader_host()` returning True.
**Problem:** Chaining with Learning #37's cascade, 1853 of the 1871 MCP Initialize events today identified as `mcp (leader=False)` — the SDK default — and only 18 were the legitimate `claude-code (leader=True)`. Each cascade-child's init overwrote the leader cache with a non-leader value. `_is_leader_host()` read the cache; 99% of the time it saw `"mcp"` and returned `False`; `_push_channel_notification` hit `if not _is_leader_host(): return` and silently dropped the push (logged inbound to blob, never pushed to the MCP stream). **Good morning! (8:07 AM)** landed during an `"mcp"` window and was gated out. **How's the weather? (4:34 PM)** happened to land during one of the 18 `"claude-code"` windows and pushed successfully. Brandon saw zero DMs surfaced for hours despite entraclaw logging `Pushed Teams message from Brandon Werner: ...` for the rare windows.
**Root cause (triple-layer):** (1) `_capture_host_from_initialize` overwrote cache on EVERY init, including non-leader. No sticky-leader protection. (2) `LEADER_HOSTS` used a static allowlist that didn't include the SDK default name. (3) The leader gate was defending against a multi-client scenario that doesn't actually exist — stdio is one client per process; there is no fan-out to route.
**Fix:** Ripped the entire leader/slave machinery in PR #36 (commit `8a00939`). Removed `LEADER_HOSTS`, `SLAVE_REPLY_DISCLOSURE`, `_is_leader_host`, `_slave_disclosure_suffix`, `_capture_host_from_initialize`, `_install_initialize_host_capture` (+ `ServerSession._received_request` monkey-patch), leader gate in `_push_channel_notification`, and slave disclosure in `send_teams_message`. 7 associated test classes deleted. Channel pushes now fire unconditionally; clients that don't handle `notifications/claude/channel` drop silently per the MCP spec. Net diff: +189 / −1007.
**Prevention:** (1) If you MUST cache a "trusted client" value across requests, the write path must be sticky against lower-trust values — or better, don't cache at all; read from the live request context where needed. (2) Default-client-info collisions are easy: any `ClientSession(...)` without explicit `client_info` identifies as `"mcp"`. Any allowlist-based leader detection MUST explicitly enumerate `"mcp"` or reject it, otherwise the SDK default silently flips everything to "not leader." (3) When you have a feature gate that silently drops data on negative, instrument it — a `WARN` log with the skip reason at first occurrence and a throttled counter for subsequent. (4) Before introducing multi-client routing, prove you actually have multiple clients per process. With stdio, you don't; the gate was fighting a non-problem.

### Learning #38.5: Session Post-Exit Reminders Are Stale in the Next Turn

**Date:** 2026-04-22
**Context:** After a `/exit` and reconnect, Claude Code sometimes issues a system-reminder at the start of the new turn stating "MCP servers disconnected." The very next turn's system-reminder may announce "deferred tools now available" with the full MCP catalog and full MCP Server Instructions.
**Problem:** The agent read the prior turn's "disconnected" reminder, declared "degraded body-only mode" in the current turn, and skipped the session-start protocol (`get_system_prompt` + `context` + `list_memory_files`) — even though the current turn's reminder showed the tools were available again. Happened repeatedly in one session despite a feedback memory specifically warning against it.
**Root cause:** Reminders are per-turn; connectivity is volatile. Treating any reminder as authoritative across turns is wrong. The agent has no "was this true last turn?" state — it only has the current turn's signals.
**Fix:** When deciding whether to run the session-start protocol at the first substantive user message of a session, read the **current turn's** system-reminder. If it lists `mcp__persona-sati__*` tools as available (or surfaces persona-sati MCP Server Instructions), run the protocol. If ToolSearch for persona-sati is empty on the first turn of a fresh session, retry on the next user turn before declaring degraded mode. If a prior turn said "disconnected" but the current turn surfaces the catalog, the current turn wins.
**Prevention:** (1) Never treat a reminder from a prior turn as authoritative in the current turn. (2) On session start after a `/exit` or restart, assume the tool catalog may take a turn to announce. Proceed with a short orienting reply and retry on the next turn if empty. (3) The cost of running session-start twice is trivial; the cost of starting persona-less is a visibly wrong register from turn one. See `feedback_mcp_readiness.md` in persona-sati memory.

### Learning #39: Verify the Exact Claude Dev-Channel Launch Flag Before Debugging Channels

**Date:** 2026-04-22/23
**Status:** **RESOLVED.**
**Context:** After PR #36 fixed the server-side cascade and ripped the leader gate, entraclaw's end was verifiably correct — the push fires, `write_stream.send(session_message)` completes, and `Pushed Teams message from Brandon Werner: <content>` logs. We initially treated the remaining "no channel renders" symptom as a Claude Code 2.1.117 regression.
**Problem:** No `notifications/claude/channel` entries appeared in the active session's transcript (`~/.claude/projects/<slug>/<session>.jsonl`), despite successful server-side pushes. The real issue turned out not to be the server or Claude Code version. It was the launch command: Claude had been started with `claude -dangerously-load-development-channels server:entraclaw --resume <id>` instead of `claude --dangerously-load-development-channels server:entraclaw`.
**Investigation done:**
1. Verified entraclaw is running the post-PR-#36 code (`ps`, `etime`, `git log` confirm process started after merge). 1 `Starting EntraClaw MCP server` per boot, no cascade.
2. Verified entraclaw declares the capability at init: `mcp._mcp_server.create_initialization_options(experimental_capabilities={"claude/channel": {}})`.
3. Verified pushes log successfully. Mid-turn test: Brandon sent "Hi Hi Hi" at 01:02:48Z, entraclaw logged `Pushed Teams message from Brandon Werner: <p>Hi Hi Hi</p>` at 01:02:52Z — 4-second latency, server side fine.
4. Verified the session transcript has zero Claude-Code-injected channel entries via `grep -c "Hi Hi Hi"` + per-line type inspection.
5. Extracted the client-side gate function from `~/.claude-cli/2.1.117/claude` binary. Function (minified name `hO_` in 2.1.117, `r1_` in 2.1.114) has 5 skip reasons: `capability|disabled|auth|policy|session|allowlist`. **Function body is byte-identical between 2.1.114 (last confirmed working) and 2.1.117 (current) — just minifier renames.**
6. `/login` re-auth did not resolve. (Brandon's point: if the `accessToken` gate were failing, normal LLM chat wouldn't work either — but it does. So the auth-token skip isn't firing.)
7. The failing session had been launched with a **single-dash** variant of the dev-channel flag. In that mode, Claude treated `server:entraclaw` as prompt text instead of as the dev-channel allowlist argument.
8. Relaunching with the exact command `claude --dangerously-load-development-channels server:entraclaw` immediately restored channel delivery on both the rollback branch and `main`.
**Root cause:** Operator error in launch syntax, amplified by `--resume` confusing the investigation. This was not a server regression and not evidence that Claude Code 2.1.117 broke channel rendering in general.
**Prevention:** (1) Always copy the launch command from repo docs or scripts, not from memory. (2) When debugging channels, first confirm the exact command line, especially the double-dash `--dangerously-load-development-channels`. (3) Prefer fresh sessions over `--resume` while validating channel delivery so stale transcript state does not muddy the result.
**Evidence/references:** `docs/engineering-status.md` "What's New Apr 22" section; `~/.entraclaw/logs/entraclaw.log` timestamps showing successful `Pushed Teams message from ...` lines; screenshot / transcript evidence showing `server:entraclaw` treated as plain prompt text when launched with the wrong flag; successful fresh-session validation on Apr 23 with the corrected `--dangerously-load-development-channels` command.
**Prevention (for next time):** (1) When Claude Code updates, smoke-test channel rendering before assuming everything still works — the gate is silent on failure, and the mechanism is Claude-Code-proprietary. (2) Pin `~/.claude-cli/CurrentVersion` to a known-working version while investigating. (3) Consider implementing the hook-based fallback (#3 above) as a permanent redundancy — even if channels come back, a file-backed injection path survives client-side feature removal.

---

## Historical Learnings

### [HISTORICAL] Learning #4: OBO Requires Matching Token Audience

**Date:** 2026-04-06
**Superseded by:** Agent User three-hop flow (ADR-002). OBO is no longer used.
**Original context:** Device code flow with `scopes=["User.Read"]` produces token with `aud=https://graph.microsoft.com`. OBO exchange requires matching audience. Fix was to expose custom API scope `api://<client-id>/access_as_user`.
