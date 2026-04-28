# Hard-Won Learnings

Append-only log of gotchas, surprises, and non-obvious behaviors discovered during development and operations. Never delete entries — mark obsolete ones as `[HISTORICAL]`.

## Active Learnings

### Learning #1: Azure CLI Tokens Rejected by Agent Identity APIs

**Date:** 2026-04-06
**Context:** Running setup.sh to create Agent Identity Blueprint
**Problem:** `az rest` calls to Agent Identity beta APIs returned 403
**Root cause:** Azure CLI tokens always include `Directory.AccessAsUser.All` delegated permission. Agent Identity APIs explicitly reject any token containing this permission.
**Fix:** Created a dedicated "Entraclaw Provisioner" app registration. Use `ClientSecretCredential` from `azure-identity` to get a clean `client_credentials` token.
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
**Context:** LLM client not calling `entraclaw_teams_send` when user said "message brandon"
**Problem:** The LLM read the tool descriptions but didn't connect "message brandon@werner.ac" with a tool named `entraclaw_teams_send`
**Root cause:** Namespaced tool names (`entraclaw_teams_send`) are jargon. The LLM looks for intent matches, not namespace patterns.
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
**Discovery:** Microsoft documents an "Agent OBO" flow where a human user's token IS used — but it enters at Hop 2 as the OBO `assertion`, not at Hop 1 as the Blueprint credential. The Blueprint still authenticates with its own confidential credentials. This flow is for "interactive agents" that act on behalf of a signed-in user, NOT for autonomous agents like Entraclaw.
**Implication:** If Entraclaw ever adds a mode where the agent acts on behalf of a specific human (not as its own digital worker), the Agent OBO flow provides that pattern. The human token + Blueprint credential together produce an Agent Identity token scoped to that human's permissions.

### Learning #26: Channel Notifications Require Experimental Capability + Startup Flag

**Date:** 2026-04-07
**Context:** Background poll detected Teams messages and pushed notifications via MCP write stream, but Claude Code silently dropped them.
**Problem:** `notifications/claude/channel` was being sent correctly through the transport but Claude Code never reacted.
**Root cause:** Three requirements for channel notifications, all undocumented outside source code:
1. Server must declare `experimental: {"claude/channel": {}}` capability during MCP initialization
2. Claude Code must be started with `--dangerously-load-development-channels server:<name>` (or `--channels` for allowlisted plugins)
3. Server must NOT be spoofed as a marketplace plugin — just use `.mcp.json` with the flag
**Fix:** Added `experimental_capabilities={"claude/channel": {}}` to `create_initialization_options()`. User starts Claude Code with `claude --dangerously-load-development-channels server:entraclaw`.
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
**Root cause:** Several sub-agents, when their isolated worktree didn't have a venv, ran `pip install -e .` using the **parent venv** (the main repo's `.venv/bin/pip`). `-e .` is a PATH-modifying operation: it rewrites the parent venv's editable-install pointer (`site-packages/_entraclaw_identity_research.pth` / the equivalent `direct_url.json` entry) to point at the worktree's source tree. Every subsequent `entraclaw-mcp` boot from the parent venv loaded the worktree's code. The change is silent — no warning from pip, no diff visible in `git status`, no error at server boot.
**Fix:** From the main repo, re-run `cd /Volumes/Development\ HD/entraclaw-identity-research && .venv/bin/pip install -e . --no-deps`. That repoints the editable install back at the main tree. Verify with `.venv/bin/python3 -c "from entraclaw import config; print(config.__file__)"` — the path must not contain `.claude/worktrees/`.
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

### Learning #40: Entra Agent Users Cannot Silently Federate to External OIDC RPs Without a User-Level Credential

**Date:** 2026-04-24
**Status:** **RESEARCH FINDING, applied as Phase 0 pivot in GitHub OIDC federation design.**
**Context:** Phase 0 kill-gate spike for the "Agent User → GitHub Copilot via OIDC" design. Original design (Approach B) assumed a 4th hop using `grant_type=urn:ietf:params:oauth:grant-type:token-exchange` would mint an id_token with `aud=<github-oidc-client-id>` for the Agent User.
**Problem:** Approach B is architecturally impossible, and the fallback of priming `/authorize?prompt=none` with `id_token_hint` does not work for a user who has never interactively signed in. The Agent User has a Blueprint cert (authorizes the 3-hop impersonation chain) but no credential that Entra accepts at the `/authorize` sign-in page.
**Investigation done:**
1. Five variants of Hop 4 probed via `/tmp/spike_hop4_variants.py`. All failed: AADSTS70003 (token-exchange unsupported), AADSTS70025 (GitHub gallery app has no FICs), AADSTS50013 (jwt-bearer signature validation when T3 used as assertion), AADSTS65001 (consent missing for mixed scope). The only 200 OK was `user_fic + scope=openid` which returned an id_token with `oid=<agent_user>` but `aud=<agent_identity>` (not GitHub).
2. Microsoft docs confirm: [agent-oauth-protocols](https://learn.microsoft.com/en-us/entra/agent-id/agent-oauth-protocols) explicit list of supported grant types for Agent Identity is `client_credentials, jwt-bearer, refresh_token`. No token-exchange. [agent-user-oauth-flow](https://learn.microsoft.com/en-us/entra/agent-id/agent-user-oauth-flow) specifies the Agent User flow as exactly 3 hops ending at a Microsoft resource.
3. Id_token audience is always `client_id of the requester`; external `aud` only happens when the external app is the OAuth client making the `/authorize` call.
4. Q2 spike (`/tmp/spike_q2_id_token_hint.py`) confirmed AADSTS50058 — `id_token_hint` is a session-lookup hint, not a session-creation primer.
**Root cause:** The conceptual error was conflating "Agent User has no password" with "Agent User has no credential." The Blueprint cert is a credential registered on the Blueprint *application* for client_credentials authentication of the impersonation chain. It does NOT authorize the Agent User to present itself at an OIDC sign-in ceremony — that requires a credential registered on the Agent User's own directory object.
**The pivot (Phase 0B):** The Agent User model needs *two* credentials:
1. **Blueprint cert** (existing) — authorizes the 3-hop impersonation chain for API-layer tokens
2. **Agent User Sign-In Cert** (new) — registered on the Agent User's directory entry via Entra Certificate-Based Authentication (CBA). Presented via TLS client-cert at `/authorize`, Entra validates against the registered CA chain, matches Subject/SAN to the Agent User's UPN, sets ESTSAUTH. From there, OIDC federation to external RPs (GitHub) works normally.
CBA is a production Entra feature (GA) used by government and regulated industries. Nothing custom; we're applying a shipped Entra primitive to Agent User accounts. The research contribution becomes: "Agent User portability across OIDC-federated SaaS via user-level CBA certs."
**Prevention (for next time):** (1) When designing OIDC federation flows, identify the `/authorize` credential source FIRST. "A credential is a credential" — but the credential must be registered on the identity that's signing in, not on a chained impersonator. (2) Do not assume a new grant type exists because it would be convenient. Verify in Microsoft docs (`/entra/identity-platform/v2-*` pages) before building around it. (3) The Agent User protocol is explicitly 3 hops per Microsoft's own docs; any design assuming Hop 4 needs to name the grant type and verify its existence. (4) Before burning spike cycles on a custom federation path, check whether the identity's `/authorize` credential exists. If the identity is passwordless AND has no FIDO2/CBA/TAP registered, OIDC federation to external RPs is not possible until one is provisioned.
**Evidence/references:** `/tmp/spike_hop4.py`, `/tmp/spike_hop4_variants.py`, `/tmp/spike_q2_id_token_hint.py` (local, non-committed); `~/.gstack/projects/brandwe-entraclaw-identity-research/brandonwerner-main-design-20260423-183328.md` "Phase 0 Findings & Pivot to CBA" section (full findings); [Microsoft Entra Agent ID OAuth protocols doc](https://learn.microsoft.com/en-us/entra/agent-id/agent-oauth-protocols); [Microsoft Entra Agent User OAuth flow doc](https://learn.microsoft.com/en-us/entra/agent-id/agent-user-oauth-flow).
**See also:** Learning #41 (the CBA pivot we tried next — also blocked by design).

---

### Learning #41: Entra `agentUser` Subtype Architecturally Blocks ALL Interactive Authentication Credentials

**Date:** 2026-04-24 (same evening as #40)
**Status:** **DEFINITIVE BLOCK, research finding applied as Phase 0B outcome.**
**Context:** After Learning #40, we pivoted the GitHub OIDC federation design to use Entra Certificate-Based Authentication (CBA) on the Agent User. Hypothesis: the Agent User has no password but could have a cert registered on its directory object, which Entra would accept at `/authorize` TLS client-cert time, establishing an ESTSAUTH session. From there the OIDC dance to GitHub would complete normally.
**Problem:** Tenant CBA + root CA upload + user cert generation with correct UPN-bound SANs (PrincipalName + RFC822Name) all succeeded. But `POST /common/GetCredentialType` — the exact API Entra's sign-in page uses to decide what credentials to offer — returns for the Agent User: `{"HasPassword": true, "CertAuthParams": null, "FidoParams": null, "RemoteNgcParams": null, "SasParams": null}`. CBA not offered. FIDO2 not offered. Windows Hello not offered. TAP not offered. Only password, which has no value set (passwordless by design) = unusable.
**Investigation done:**
1. Admin consent obtained for `Policy.ReadWrite.AuthenticationMethod`, `Organization.ReadWrite.All`, `UserAuthenticationMethod.ReadWrite.All` (provisioner app, werner.ac tenant).
2. Root CA uploaded to `/beta/organization/{tenantId}/certificateBasedAuthConfiguration` — 201 Created. Note: the `issuer` property is read-only on POST, Entra derives it from the cert itself.
3. Tenant CBA policy enabled: `/beta/policies/authenticationMethodsPolicy/authenticationMethodConfigurations/X509Certificate` PATCH to `state=enabled`, includeTargets=all_users. 204 success.
4. User cert generated with both `otherName:1.3.6.1.4.1.311.20.2.3;UTF8:<upn>` (PrincipalName) and `email:<upn>` (RFC822Name) SANs for maximum binding coverage.
5. Attempted to register cert on Agent User via three beta endpoints; all returned 400 "Resource not found for the segment":
   - `/users/{id}/authentication/x509CertificateMethods`
   - `/users/{id}/authentication/certificateBasedAuthConfiguration`
   - `/users/{id}/authentication/certificateBasedAuthMethods`
6. Attempted to set `authorizationInfo.certificateUserIds` on the Agent User; PATCH returned 400 "Property is not applicable and cannot be set. paramName: CertificateUserIds, paramValue: , objectType: Microsoft.Online.DirectoryServices.User".
7. Attempted to add a CBA user-binding rule `PrincipalName → userPrincipalName` to the tenant policy; PATCH returned 400 "One X509CertificateField: PrincipalName cannot bind to different userProperty fields." (existing rule already maps PrincipalName to onPremisesUserPrincipalName, which is null for cloud-only agentUsers).
8. Crucial diagnostic: `POST /common/GetCredentialType` returns CertAuthParams=null, FidoParams=null, RemoteNgcParams=null, SasParams=null for the Agent User's UPN. This confirms Entra's sign-in page itself wouldn't offer CBA to this user regardless of any other config.
**Root cause:** The `#microsoft.graph.agentUser` directory subtype is **architecturally excluded from all interactive authentication credential types**. Microsoft has intentionally scoped the Agent User primitive to non-interactive API-layer impersonation (the 3-hop chain). There is no credential — cert, FIDO2 key, Windows Hello, TAP, password — that can authenticate an agentUser object interactively. This forecloses BOTH of the research thesis's required primitives: (a) no external-audience token minting via `/token` endpoint (Learning #40), and (b) no interactive credential for `/authorize` ESTSAUTH session establishment (Learning #41).
**The research contribution crystallizes:** The Entra Agent User primitive as shipped cannot participate in OIDC sign-in to third-party SaaS requiring SP-initiated auth. Microsoft would need to extend the protocol with either: (a) a Hop-4 grant minting id_tokens with external audiences, or (b) permitting at least one interactive credential type on agentUser objects to complete standard OIDC auth. Both are concrete, narrow feature requests for the Entra platform team.
**Prevention (for next time):** (1) Before designing identity federation that requires interactive sign-in, verify the identity subtype supports at least one credential type via `POST /common/GetCredentialType`. This single API call forecloses entire categories of dead-end designs. (2) `agentUser` subtype ≠ regular `user` — many directory-object properties and auth method endpoints that apply to `user` fail silently or reject writes on `agentUser`. Always test writes on the exact subtype before building. (3) Tenant-level CBA enablement is necessary but NOT sufficient — per-user credential-type availability is a separate gate that the `GetCredentialType` API surfaces. Silent passing tenant-level checks can mask per-user exclusions.
**Evidence/references:** `/tmp/spike_phase0b_cba_auth.py`, `/tmp/run_phase0b_setup.py` (local, non-committed); `~/.gstack/projects/brandwe-entraclaw-identity-research/brandonwerner-main-design-20260423-183328.md` "Phase 0B Findings: CBA Also Blocked for agentUser Type" section (full evidence + tenant state + rollback commands); GetCredentialType response captured verbatim in that section.
**CORRECTION applied same evening — see Learning #42:** Learnings #40 and #41 together say "Agent User federation to external RPs is architecturally impossible." That framing was too broad. It is correct for OIDC (proved here and in #40), but Microsoft ships a preview SAML-shaped four-hop flow for the same capability (agent-user → SAML helper app → OBO with `requested_token_type=saml2` → SAML assertion). Missed this in the initial spikes because we were OIDC-focused. The corrected framing is an OIDC-SAML asymmetry, not a total block.

---

### Learning #42: Microsoft's Agent User → SAML Application Preview Flow Is the Missing "Hop 4" We Claimed Didn't Exist

**Date:** 2026-04-24 (correction, same evening as #40 and #41)
**Status:** **DOCUMENTED PREVIEW, PENDING EMPIRICAL VALIDATION (Phase 0C spike).**
**Context:** After Learnings #40 and #41 documented the OIDC + CBA blocks and concluded "Agent Users cannot federate to external RPs," a cross-model challenge (ChatGPT) correctly identified that Microsoft ships a documented preview feature we hadn't probed: an agent-user-to-SAML-application four-hop flow that mints SAML assertions on behalf of agent users for external SAML-based applications.
**Problem:** Learning #40 + #41's framing was over-general. The OIDC conclusion remains correct (no token-exchange grant on Entra's /token endpoint mints id_tokens with external audiences; all variants probed returned specific AADSTS error codes). But the broader claim — "the agent user primitive architecturally forecloses external federation" — is wrong. Microsoft ships the primitive in SAML shape; the OIDC equivalent is the gap.
**The corrected mental model:**

Microsoft's agent-user-to-SAML-app flow, from `learn.microsoft.com/entra/identity/enterprise-apps/assign-agent-identities-to-applications#assigning-to-saml-based-applications`:

```
Hop 1:  Blueprint → blueprint token (unchanged from today's 3-hop)
Hop 2:  Agent Identity FIC token with T1 as assertion (unchanged)
Hop 3:  Agent User user_fic scoped to SAML HELPER APP (not Graph)
Hop 4:  POST /oauth2/v2.0/token
        grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer
        assertion=<Hop 3 token>
        client_id=<SAML helper app>
        client_secret=<SAML helper app secret>
        scope=<target enterprise app ID>/.default
        requested_token_use=on_behalf_of
        requested_token_type=urn:ietf:params:oauth:token-type:saml2
        → base64url-encoded SAML assertion in response
```

Required tenant artifacts (all in preview, documented):
- SAML helper application registration
- Target enterprise application (the external SAML RP, e.g., GitHub EMU in SAML mode)
- oAuth2PermissionGrant: SAML helper → target enterprise app, scope=`<enterprise entity ID>/.default`
- oAuth2PermissionGrant: agent identity → SAML helper, scope=`api://<helper>/.default`
- App role assignment: agent user → target enterprise app role
- (The agent identity blueprint, agent identity, and agent user already exist)

This is exactly the "Hop 4" primitive Learning #40 claimed didn't exist. It produces a SAML assertion rather than an OIDC id_token, which is why OIDC-focused probing missed it.

**What this changes:**
- Learning #40 stays correct as bound to OIDC specifically: token-exchange is unsupported, no OIDC grant mints external-audience id_tokens.
- Learning #41 stays correct: agentUser subtype blocks all interactive credentials for `/authorize` sign-in.
- BUT the combined research conclusion narrows: "OIDC federation is blocked; SAML federation has a Microsoft-documented preview path that is the Hop-4 equivalent we were looking for."
- Feature request to Microsoft refocuses on the asymmetry: productize the SAML primitive + add the OIDC equivalent.

**Pending validation (Phase 0C spike):**
- Register a SAML helper app + dummy target SAML enterprise app in werner.ac
- Run the 4 hops, inspect the returned SAML assertion (issuer, audience, NameID, signature, conditions)
- Test whether the emitted bare `<Assertion>` can be packaged into a GitHub-acceptable `<samlp:Response>` envelope (InResponseTo-absent per Microsoft caveat)
- Decide whether to migrate GitHub EMU from OIDC to SAML (disruptive — GHEC docs say it suspends managed user accounts and requires re-provisioning) OR stand up a disposable EMU enterprise for end-to-end validation
**Prevention (for next time):** (1) When concluding "a feature doesn't exist," search Microsoft docs for the feature across *all* token-type shapes, not just the one the thesis is built around. OIDC and SAML are distinct doc trees in `learn.microsoft.com/entra/` and features often exist in one but not the other. (2) Cross-model review (ChatGPT, Codex, or another LLM with fresh context) is specifically valuable for catching this kind of over-generalization — a second model with no investment in the original framing will surface adjacencies the primary author missed. (3) When the research finding is "X is impossible," phrase it as narrowly as the evidence supports. "OIDC federation is impossible" is defensible; "all federation is impossible" is a stronger claim that requires wider evidence. **(4) When a user pushes back on a "definitive" finding, take the push-back seriously; over-confidence is a leading indicator of unexamined assumptions.**
**Evidence/references:** [Microsoft Learn: Manage assignment of agent identities to an application (Preview)](https://learn.microsoft.com/entra/identity/enterprise-apps/assign-agent-identities-to-applications#assigning-to-saml-based-applications) — full 4-hop protocol description and required tenant artifacts; [OBO SAML assertion response](https://learn.microsoft.com/entra/identity-platform/v2-oauth2-on-behalf-of-flow#saml-assertions-obtained-with-an-oauth20-obo-flow) — response shape + InResponseTo caveat; `~/.gstack/projects/brandwe-entraclaw-identity-research/brandonwerner-main-design-20260423-183328.md` "Phase 0C: SAML Path Identified" section for the full correction and Phase 0C spike plan.
**FOLLOW-UP — see Learning #43:** Phase A/B/C were empirically executed 2026-04-24. Phase A (OBO-SAML mint) succeeded. Phase B (GitHub EMU SAML gallery app + claim mapping via Graph) succeeded. Phase C (GitHub ACS session establishment) is blocked by a protocol incompatibility between Microsoft OBO-SAML's InResponseTo-less assertion shape and GitHub EMU's Web SSO InResponseTo requirement. Learning #43 documents the empirical confirmation.

---

### Learning #43: Microsoft OBO-SAML and GitHub EMU SAML Are Protocol-Incompatible on InResponseTo

**Date:** 2026-04-24 (same evening as #42, post-empirical execution)
**Status:** **DEFINITIVE — empirically proven via Phase A/B/C end-to-end execution against werner-co GitHub Enterprise + werner.ac Entra tenant.**
**Context:** After Learning #42 identified the preview OBO-SAML flow as the missing "Hop 4," we executed the full spike path: Phase A (emit SAML assertion against dummy target), Phase B (GitHub EMU SAML gallery app configuration via Graph, including entity ID, signing cert, and claimsMappingPolicy for NameID = UPN), Phase C (inject assertion into GitHub's SAML ACS and verify session establishment). Phase A and B succeeded cleanly. Phase C hit a fundamental protocol gap that defines the limits of the Microsoft OBO-SAML preview primitive.
**Problem:** The Microsoft OBO-SAML flow emits a SAML assertion where the signed `<SubjectConfirmationData>` contains NO `InResponseTo` attribute, because the OBO request has no AuthnRequest context to reference. The Microsoft OBO-SAML reference documentation explicitly warns: *"the target app must be able to accept a SAML assertion without an InResponseTo value."* GitHub EMU's SAML ACS is NOT such a target — it requires `InResponseTo` inside the signed `<SubjectConfirmationData>` to bind the assertion to an active SP-initiated session. When we inject an OBO-derived assertion (with InResponseTo only on the outer `<samlp:Response>` envelope), GitHub's ACS silently rejects it: the assertion is accepted at the surface level (consent page "Signed in with Werner Co" renders, `logged_in=yes` + `saml_csrf_token` cookies set), GitHub's `js-auto-replay-enforced-sso-request` JavaScript fires the expected auto-replay form submit, but the subsequent POST returns 302 to `/enterprises/werner-co/sso` without issuing `user_session` or `dotcom_user` cookies. Zero login events appear in GitHub's enterprise audit log, confirming the rejection happens in a pre-session-creation validation step.
**Investigation done:**
1. Phase A empirically executed: `/tmp/phase_a_saml_spike.py` with SAML helper app, dummy target. Hop 4 returned base64url SAML assertion (5208 bytes), signed RSA-SHA256 by Entra. Verified Issuer=`sts.windows.net/<tenant>/`, Audience=target entity ID, NameID=Agent User UPN (after claimsMappingPolicy).
2. Phase B: instantiated GitHub Enterprise Managed User (SAML) gallery app template `3b5ca639-0790-480e-9b24-9625375a05e7` via `/applicationTemplates/.../instantiate`. Configured identifierUris (overrode the HostNameNotOnVerifiedDomain check via SPN), added `addTokenSigningCertificate`, set `preferredTokenSigningKeyThumbprint`, wired oAuth2PermissionGrants, created claimsMappingPolicy with NameID source = `user.userprincipalname`, attached to SP, set `api.acceptMappedClaims=true` on the application. Hop 4 against the real GitHub app produced an assertion with Audience=`https://github.com/enterprises/werner-co`, NameID=`entraclaw-agent-sati-agent@werner.ac`, Format=`urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress`.
3. Phase C attempted four variants: (a) IdP-init POST to ACS with consent-page Continue replay — looped back to /sso; (b) SP-initiated flow with matching InResponseTo on outer envelope via httpx — same loop; (c) browser header spoofing (Sec-Fetch-User=?1 etc.) — no change; (d) Playwright-driven SP-init with `page.route()` intercepting the browser's GET on Entra `/saml2`, injecting an auto-submitting HTML that POSTs our OBO-derived envelope with matching InResponseTo to GitHub's ACS — same terminal state: 200 → consent page → 302 → /sso, no user_session. GitHub's audit log confirmed zero login events across all attempts.
4. The blocker is the signed `<SubjectConfirmationData>` inside the assertion: Entra signs it, we cannot modify it without breaking the signature, we don't have Entra's signing key. The OBO flow explicitly does not include InResponseTo there (Microsoft's docs warn about this). GitHub EMU explicitly requires it (behavior confirmed: SP-init → inject → 302 loop).
**Root cause:** Protocol incompatibility between two standards-compliant SAML dialects. Microsoft OBO-SAML was designed for SAML consumers that do programmatic bearer-assertion validation without the Web SSO AuthnRequest/Response binding (backend-to-backend SAML, some legacy WS-Federation endpoints, explicit API-consumer patterns). GitHub EMU SAML was designed for browser-mediated Web SSO where assertions are bound to specific AuthnRequests via InResponseTo in SubjectConfirmationData. The two dialects don't compose for the "Agent User signs into GitHub as a first-class user" scenario.
**The sharpened research contribution:** Microsoft already ships the SAML-shape primitive that the OIDC side lacks — but the SAML primitive only works for InResponseTo-agnostic RPs. For InResponseTo-requiring RPs (Web SSO SaaS like GitHub EMU, most gallery apps), Microsoft's OBO-SAML cannot establish browser sessions. This narrows the research recommendation: Microsoft needs to (a) add an OIDC-shaped OBO for external audiences (Recommendation A), AND/OR (b) extend OBO-SAML to accept an optional in_response_to / authn_request_id parameter (Recommendation B) so the emitted assertion can carry InResponseTo in the signed SubjectConfirmationData when a downstream RP needs it. Either closes the gap for browser-SSO SaaS.
**Prevention (for next time):** (1) When evaluating SAML interop for a new target, explicitly verify whether the RP requires InResponseTo in SubjectConfirmationData before building around a Microsoft OBO-SAML flow. The Microsoft docs flag this constraint; take the flag seriously. (2) Web SSO SaaS (GitHub, Salesforce, Slack-SAML, most gallery apps) generally require InResponseTo binding. Backend-to-backend SAML APIs generally do not. The distinction matters. (3) Empirical Phase A+B success (clean signed assertion with correct audience/NameID) does NOT imply Phase C success. The last mile of Web SSO is a strict protocol-binding step; assertion correctness is necessary but not sufficient. (4) GitHub's audit log is an authoritative signal: if zero login events appear despite the assertion being accepted at the surface level, the RP is failing validation in a pre-session-creation step — usually InResponseTo or signature-placement mismatch.
**Evidence/references:** `/tmp/phase_a_saml_spike.py` (Phase A spike, emits valid assertion); `/tmp/phase_c_playwright_intercept.py` (Phase C Playwright + route() interception, most advanced variant tried); `/tmp/phase_a_saml_assertion.xml` (raw Entra-signed assertion for dummy target); `/tmp/phase_b_saml_assertion.xml` (raw assertion for real GitHub EMU target); `/tmp/phase_c_envelope.xml` (decoded injection envelope); `~/Documents/entra-agent-user-oidc-federation-findings.docx` v3 for the full research narrative. Microsoft docs: [agent-identities-to-applications SAML flow](https://learn.microsoft.com/entra/identity/enterprise-apps/assign-agent-identities-to-applications#assigning-to-saml-based-applications), [OBO SAML response](https://learn.microsoft.com/entra/identity-platform/v2-oauth2-on-behalf-of-flow#saml-assertions-obtained-with-an-oauth20-obo-flow) (note the InResponseTo caveat). GitHub audit log (werner-co enterprise) — zero login events for the Agent User across all Phase C attempts.

---

### Learning #44: Parent-Directory Rename Orphans the venv (Shebangs + `.pth` Hardcode Absolute Paths at Install Time)

**Date:** 2026-04-24
**Status:** **CONFIRMED — reproduced and fixed in ~2 min; terminal-slowness symptom confirmed as side-effect.**
**Context:** After PR #39 merged (code-level `openclaw → entraclaw` rename on 2026-04-23), the repo directory on disk was also renamed from `/Volumes/Development HD/openclaw-identity-research` to `/Volumes/Development HD/entraclaw-identity-research`. The code rename was clean; the directory rename silently orphaned the `.venv/`.
**Problem:** Overnight, every MCP launch of `.venv/bin/entraclaw-mcp` failed instantly with `/Volumes/Development HD/openclaw-identity-research/.venv/bin/python3: No such file or directory`. Claude Code's MCP client entered a crash-reconnect loop on the stdio server, and the loop itself was the cause of "terminal is very slow" — not Claude, not the network, not persona-sati.
**Root cause:** `python -m venv <path>` bakes `<path>` into `.venv/pyvenv.cfg` as `command = ... -m venv /Volumes/Development HD/openclaw-identity-research/.venv`, and every console-script in `.venv/bin/` (`pip`, `python3`, `entraclaw-mcp`, etc.) gets a shebang or `exec` line with the interpreter's absolute path. An editable-install `.pth` file in `site-packages/` also hardcodes the source-tree path. Renaming the parent directory invalidates all three simultaneously — pyvenv.cfg, every script shebang, AND the editable-install source pointer. The venv looks intact (files present, executable bit set) but every invocation dies on the stale interpreter path.
**Investigation done:**
1. Ran `.venv/bin/entraclaw-mcp` directly — surfaced the stale shebang in one line: `/Volumes/Development HD/openclaw-identity-research/.venv/bin/python3: No such file or directory`.
2. `.venv/bin/python3 -c "import entraclaw"` raised `ModuleNotFoundError` — confirmed the editable `.pth` was also pointing at the old path (or the interpreter itself was unreachable; in this case the interpreter was broken first, so import didn't even get that far).
3. `cat .venv/pyvenv.cfg` showed `command = /opt/homebrew/opt/python@3.12/bin/python3.12 -m venv /Volumes/Development HD/openclaw-identity-research/.venv` — the smoking gun.
4. Fix: `rm -rf .venv && python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"`. Total time ~90 seconds.
5. Post-fix verification: shebang now `/Volumes/Development HD/entraclaw-identity-research/.venv/bin/python3.12`, `import entraclaw` resolves to `.../entraclaw-identity-research/src/entraclaw/__init__.py`, MCP reconnected clean.
**Prevention (for next time):** (1) A repo-directory rename is a **three-part operation**, not one: code rename (git-tracked), directory rename (filesystem), **and venv recreation** (untracked side-effect). If you forget the third, the venv dies silently at next MCP launch. (2) If Claude Code suddenly feels slow and an MCP server is listed as stdio, assume the MCP crash-loop first — `/mcp` → "Reconnect <server>" surfaces the failure reason in the status. Don't chase Claude-harness or network theories until the MCP launch is known-green. (3) A one-line debug shortcut for any suspected Python-venv path corruption: `head -3 .venv/bin/<script> && cat .venv/pyvenv.cfg | grep command`. Both outputs should contain the **current** repo path. If either contains an old path, recreate the venv. (4) This is the sibling failure mode of Learning #36 (sub-agent worktree installs re-pointing the parent venv); both are "venv paths become stale without surfacing a friendly error." A healthy reflex: after any directory-level rename/move/symlink, immediately run `.venv/bin/python -c "from entraclaw import config; print(config.__file__)"` as a one-call sanity check. If it prints the expected path, you're clean. If it errors or prints a path you don't expect, stop and fix before moving on.
**Evidence/references:** Fix executed during this session — pyvenv.cfg before/after captured in session transcript. Related: Learning #36 (worktree venv shadowing, the sibling failure mode). Commit that triggered this: c0bea8d (PR #39, `refactor: rename openclaw → entraclaw across repo`, merged 2026-04-23 17:34 PDT).

---

### Learning #45: Wrapper Scripts Bypass `_is_self_referential_peer` and Reintroduce the Self-Spawn Cascade

**Date:** 2026-04-24
**Status:** **CONFIRMED — root cause for the Apr 24 BrokenPipeError storm; wrapper-marker fix shipped in this PR.**
**Context:** While debugging the morning's MCP-disconnect symptom, the entraclaw stderr was redirected through `scripts/entraclaw-mcp-debug.sh` (a thin wrapper that tees stderr to `/tmp/entraclaw-debug.log` then `exec`s `.venv/bin/entraclaw-mcp`). `.mcp.json`'s `command` was changed from `.venv/bin/entraclaw-mcp` to the wrapper for the duration of the debug session.
**Problem:** Every entraclaw boot started spawning a duplicate entraclaw-mcp ~2 seconds in, with the duplicate dying ~5 seconds later via `BrokenPipeError: [Errno 32] Broken pipe` on `stdout.flush()` inside `mcp/server/stdio.py::stdout_writer`. Wrapper-start markers in `/tmp/entraclaw-debug.log` consistently appeared in pairs ("twin spawn"). Each boot pair did 2× the API work — two three-hop token acquisitions, two Teams chat registrations, two background polling loops, two persona-sati prompt fetches — burning login.microsoftonline.com round-trips and Graph API calls pointlessly. A fresh Claude Code session boot today produced four wrapper starts in 19 seconds.
**Root cause:** This is the same self-spawn cascade originally fixed by PR #36 / commit `8a00939` ("kill efferent-copy self-spawn cascade"), reintroduced by changing the peer command. `_is_self_referential_peer` resolved the peer's `command` (the wrapper script path) and compared it against `sys.argv[0]` (the Python entry point at `.venv/bin/entraclaw-mcp`). The wrapper script's resolved path did not match the running binary, so the check returned False, the peer was NOT skipped, and `discover_sinks` opened a stdio_client to it — spawning a child entraclaw-mcp via the wrapper. The child completed its full init (prompt load, three-hop, polls), responded to `tools/list`, parent saw no `observe` tool, parent tore down the stdio_client → child's stdout closed → BrokenPipeError. Confirmed reproduction:

```python
sys.argv = ["/.../.venv/bin/entraclaw-mcp"]
peer_wrapper = {"type": "stdio", "command": "/.../scripts/entraclaw-mcp-debug.sh"}
peer_direct  = {"type": "stdio", "command": "/.../.venv/bin/entraclaw-mcp"}
_is_self_referential_peer(peer_wrapper)  # False — bypasses the check
_is_self_referential_peer(peer_direct)   # True  — correctly skipped
```

The April 22 fix (commit `8a00939`) addressed direct self-reference (peer command = our entry point). It did not anticipate wrapper indirection. PR #36's `EFFERENT_COPY_DISABLE=1`-in-child-env belt prevented infinite recursion (only 1 cascade level instead of N), so the bug presented as "double init" rather than "subprocess explosion" — quieter symptom, longer time to detection.
**Investigation done:**
1. Read `/tmp/entraclaw-debug.log` (the wrapper's output) — first explicit traceback found across all today's drops: `Exception Group → mcp/server/stdio.py:81 in stdout_writer → BrokenPipeError on stdout.flush()`.
2. Counted wrapper-start markers: 14 in 6.5 hours, all in pairs 2-3s apart. New session at 11:25 PDT produced 4 starts in 19 seconds.
3. Verified rename was NOT the cause (Brandon's initial hypothesis): `pyvenv.cfg`, `.pth` files, and `.venv/bin/entraclaw-mcp` shebang all clean per Learning #44; `python3 -c "from entraclaw import config; print(config.__file__)"` resolves to the parent src tree (no Learning #36 contamination).
4. Confirmed propagate=False fix from PR #40 is active in the running code (no rich-format duplication of entraclaw events; only httpx/msal still go through root's RichHandler).
5. Ran `_is_self_referential_peer` in a Python repl with the wrapper command — returned False, confirming the check bypass.
6. Counted log doubling pattern: every entraclaw event line appeared twice with identical microsecond timestamps (two processes writing to the same `/tmp/entraclaw-debug.log` via separate `tee` instances spawned by separate wrapper invocations).
**Fix (this PR):**
1. **Hot fix (applied immediately):** Reverted `.mcp.json` command from `scripts/entraclaw-mcp-debug.sh` back to `.venv/bin/entraclaw-mcp`. Stops the cascade. Cost: lose stderr capture.
2. **Durable fix (this PR):** Extended `_is_self_referential_peer` to detect wrapper scripts via an opt-in marker comment. Wrappers add `# entraclaw-self-ref-target: <path>` (path resolved relative to script's directory). The check reads up to 16KB of the script, looks for the marker line, and compares the declared target against `sys.argv[0]` / `sys.executable`. Matching wrappers are skipped at factory-build time, never reaching `stdio_client`. Arbitrary shell parsing is explicitly avoided — wrappers using `$(cd ... && pwd)` or other dynamic targets are too fragile to parse, so the marker is the wrapper telling us where it execs.
3. Updated `scripts/entraclaw-mcp-debug.sh` to include the marker. The wrapper can now be safely activated in `.mcp.json` without re-triggering the cascade.

**Prevention (for next time):** (1) When changing `.mcp.json`'s `command` for any peer that is or wraps the running MCP server, verify `_is_self_referential_peer` still detects the new command — easiest test is the repl snippet above. (2) `_is_self_referential_peer` is the ONLY guard against the cascade; treat it as a security-relevant invariant and write a test for any new wrapper variant before deploying. (3) Wrappers should always include the marker if they exec into a known MCP entry point. (4) Stderr capture is valuable but cheap — prefer wrappers that declare their target via the marker over wrappers that build the target dynamically. (5) When the symptom is "MCP keeps disconnecting" and the wrapper change is recent, suspect this regression first; check for paired wrapper-start timestamps (the twin-spawn signature) and `BrokenPipeError` in the captured stderr. (6) Track twin-spawn as a metric — `grep -c "wrapper start" /tmp/entraclaw-debug.log` over a known window should equal the number of Claude Code MCP reconnects (one wrapper per reconnect), not 2× that.
**Evidence/references:** `/tmp/entraclaw-debug.log` lines 8891-8942 (the BrokenPipeError traceback); commit `8a00939` (the original self-spawn cascade fix); commit `9c74cd1` (PR #40 — adjacent change but unrelated to this regression); `tests/test_efferent_copy.py` `TestDiscoverSinks::test_wrapper_with_self_ref_marker_is_skipped` and the two unit tests for `_is_self_referential_peer` wrapper-marker behavior.

### Learning #46: Raw Teams HTML in Channel Notifications Clean-Closes Claude Code MCP

**Date:** 2026-04-24
**Status:** **CONFIRMED + FIXED — commit `f0d29ea` (`fix: sanitize Teams channel notification HTML`).**
**Context:** Entraclaw MCP (stdio child of Claude Code) appeared to become progressively slower and disconnect after 2-10 minutes of sustained activity. Earlier same-day work fixed two real amplifiers: PR #40 stopped `entraclaw` records double-rendering through FastMCP's root `RichHandler`, and PR #41 fixed wrapper-script self-reference detection so the debug wrapper no longer retriggered the self-spawn cascade. The drop still reproduced.
**Problem:** The remaining failure was deterministic, not general stdio backpressure. The first inbound Teams push containing raw Graph HTML in `notifications/claude/channel` params clean-closed Claude Code's MCP stream. Example payload shape: `<attachment id="1777053221965"></attachment>\n<p>As...</p>`. The Teams push path passed `message.get("content", "")` directly into the notification's top-level `content` field.
**Root cause:** Claude Code's channel notification parser is sensitive to angle-bracket content in notification params. This was the same class of bug already fixed on the email push path: `mcp_server.py` documented that sender text rendered as `Name <addr>` looked like an unknown HTML tag and clean-EOFed the MCP stream. The Teams path never got the same sanitization.
**Fix:** `src/entraclaw/mcp_server.py::_push_channel_notification` now uses `_summarize_content(...)` for the top-level notification `content`, and also sanitizes fetched quote-reply metadata before appending to `meta["quoted_messages"]`:

```python
"content": _summarize_content(message.get("content", ""))
quoted.append({**r, "content": _summarize_content(r.get("content", ""))})
```

Regression tests in `tests/test_mcp_server_integration.py` cover both raw Teams HTML in top-level channel content and raw HTML in `quoted_messages[*].content`.
**Verification:** The focused regression tests passed; full suite passed with `ENTRACLAW_KEEP_MEMORY_LOCAL=true` (`652 passed`); `ruff check .` passed. A 65-minute real Claude Code channel soak survived the exact raw HTML attachment push that previously killed the server at ~25 seconds. A follow-up 30-minute quote-reply soak exercised real `quoted_messages` metadata and stayed connected. Brandon then restarted and confirmed the MCP server works in normal use.
**What was ruled out / demoted:** Parent Claude CLI stdio-drain backpressure and blob-write-on-push hot path were plausible amplifiers, but not the root cause of the clean close. Keep PR #40 and PR #41 — they fixed real adjacent problems — but do not treat this symptom as unresolved backpressure unless raw notification content has first been ruled out.
**Prevention:** (1) Never pass raw Teams Graph HTML into `notifications/claude/channel` params. Sanitize any human-visible string payload first. (2) Preserve outgoing Teams HTML for Graph sends; the sanitizer is for inbound MCP channel notifications only. (3) When adding new notification metadata fields, inspect nested content too — top-level sanitization is not enough if nested dicts carry raw `content`. (4) If MCP clean-closes after an inbound message with no Python traceback, check the exact notification payload before chasing transport-level theories.
**Evidence/references:** `src/entraclaw/mcp_server.py` (`_push_channel_notification`), `tests/test_mcp_server_integration.py::TestPushChannelNotificationObservability`, `docs/runbooks/mcp-disconnect-investigation.md`, commit `f0d29ea`, and the Apr 24 soak logs in the Copilot session artifacts.

---

### Learning #47: Per-Tool Observe Mirroring Must Be Opt-In and File Logs Must Rotate

**Date:** 2026-04-24
**Status:** **CONFIRMED + FIXED — efferent-copy now opt-in; JSON file log now rotates.**
**Context:** After the MCP disconnect was fixed, Brandon asked whether entraclaw was still wrapping every call with logging and flagged disk-fill risk. The answer was yes for efferent-copy: when sinks were discovered, `install_into_fastmcp()` wrapped every MCP tool with pre/post `observe(...)` calls. Separately, `setup_logging()` used a plain `logging.FileHandler` for `~/.entraclaw/logs/entraclaw.log`, which can grow forever.
**Problem:** Per-call observer mirroring is useful diagnostic/cognition plumbing, but it is too expensive as a default. Any sink that persists observations can turn normal MCP usage into unbounded write amplification. The main JSON log also had no size cap, so long-running MCP sessions could fill disk even without observer sinks.
**Fix:** Efferent-copy discovery is now off by default and only runs when `EFFERENT_COPY_ENABLE=1` is set. `EFFERENT_COPY_DISABLE=1` remains a hard override, including for spawned stdio peers. `setup_logging()` now uses `RotatingFileHandler` for `entraclaw.log` instead of an unbounded `FileHandler`.
**Prevention:** (1) Treat whole-tool-call mirroring as an explicit diagnostic or cognition feature, never a default runtime behavior. (2) Any log written by a daemon-style MCP process must have a bounded retention policy. (3) Keep security/audit logging separate from optional observer mirroring; disabling efferent-copy must not disable audit events or interaction logging required for product behavior.
**Evidence/references:** `src/entraclaw/efferent_copy.py::discover_sinks`, `src/entraclaw/logging_config.py::setup_logging`, `tests/test_efferent_copy.py::TestDiscoverSinks::test_default_disabled_does_not_contact_peers`, and `tests/test_logging_config.py::TestSetupLogging::test_file_handler_rotates_to_cap_disk_usage`.

### Learning #48: Copilot CLI / Claude Code Inject MCP Tool Descriptions, NOT FastMCP `instructions=`

**Date:** 2026-04-28
**Status:** **CONFIRMED — probe-verified in live Copilot CLI session.**
**Context:** While building autonomous Teams reply for Copilot CLI, we needed a reliable way to teach the model when to use `wait_for_sponsor_dm`. Existing `mcp_server.py:_load_agent_instructions` builds the body+persona prompt and passes it to `FastMCP(..., instructions=...)`, on the assumption that the host CLI surfaces it as a system message.
**Problem:** Probe showed it does NOT. We registered two tools whose descriptions contained sentinel `PROBE_SENTINEL_TOOLDESC_*` strings, and a third sentinel `PROBE_SENTINEL_INSTR_*` was placed in the FastMCP `instructions=` field. In live Copilot CLI: the tool-description sentinels appeared in the model's system prompt; the `instructions=` sentinel did not. Same behavior was previously observed in Claude Code — see CLAUDE.md "Session-Start Protocol" comment that Claude Code only surfaces `instructions=` in MCP debug UI.
**Fix:** When you need a behavior rule to be visible to the LLM in Copilot CLI / Claude Code, embed it in (a) `AGENTS.md` / `CLAUDE.md` / `.github/copilot-instructions.md` (host-injected automatically), or (b) the docstring of the relevant `@mcp.tool()` (becomes part of `tools/list` and reaches the model). Do NOT rely on FastMCP `instructions=` for behavior contracts — keep that field for human-facing debug context only. The `wait_for_sponsor_dm` tool's docstring is the reference example.
**Prevention:** When adding any new doctrine that must reach the model: write the canonical paragraph once in `prompts/anatomy/*.md`, then mirror a one-line summary into AGENTS.md + CLAUDE.md + `.github/copilot-instructions.md`, AND embed the operational rule in the docstring of any tool that enforces it. Treat `instructions=` as advisory only.
**Evidence/references:** Probe tools/results in session checkpoint 2026-04-28; `src/entraclaw/tools/wait_tool.py`; `src/entraclaw/mcp_server.py::wait_for_sponsor_dm` docstring; CLAUDE.md "Session-Start Protocol" section explaining the same gap in Claude Code.

### Learning #49: Long-Blocking `@mcp.tool()` with `asyncio.sleep` Is Cancellable in Copilot CLI — PTY Hijack Was Always the Wrong Tool

**Date:** 2026-04-28
**Status:** **CONFIRMED — empirically reproduced; closes PR #42 as superseded.**
**Context:** The first attempt at autonomous Copilot CLI Teams reply was a PTY supervisor (PR #42) that hijacked the user's terminal to inject sponsor messages as if they were keystrokes. It produced repeated "screen blanks then terminal locks" failures — Ctrl+C did not recover. The next attempt was a headless `copilot -p` daemon, which lost the operator's interactive session entirely and broke the underlying use case ("I'm at lunch, ping me when the build's green, then let me ask follow-ups in this same CLI").
**Problem:** Both approaches treated MCP as a one-shot tool boundary instead of as a long-lived call site. The PTY approach also fought with bracketed-paste mode, terminfo mismatches, and Copilot CLI's own input handler — none of which had clean failure modes. Screen blanking after first interception was a symptom of wrestling Copilot's TTY back into a state it didn't expect.
**Fix:** Probe-verified in live Copilot CLI: a `@mcp.tool()` that does `asyncio.sleep(N)` (or polls Graph in a loop) blocks the host LLM turn until it returns, AND propagates Ctrl+C as `CancelledError` cleanly — Copilot prints "Operation aborted by user" and returns control to the prompt without screen damage. This is the foundation of `wait_for_sponsor_dm`: sleep INSIDE the tool, return the sponsor's message as the tool's return value, and the model sees the message as next-turn input. No PTY, no second process, no daemon. Use case ("ping me when done, then answer follow-ups") is preserved exactly.
**Prevention:** For any "agent should wait for an external event before continuing" pattern in MCP-host CLIs, the correct shape is a long-blocking `@mcp.tool()` with internal polling/await. Do NOT spawn helper processes, do NOT hijack the host TTY, do NOT use background daemons that push notifications out-of-band. The host CLI's own MCP transport is the channel.
**Evidence/references:** Live probe in session 2026-04-28 (`PROBE_SENTINEL_WAITTOOL_AXOLOTL_2026C` round-trip; `seconds=30` then Ctrl+C → clean abort, no blank screen); `src/entraclaw/tools/wait_tool.py::wait_loop`; PR #42 (PTY supervisor — superseded); `tests/tools/test_wait_for_sponsor_dm.py::test_wait_loop_cancellation_propagates`.

### Learning #50: Federated B2B Guests Have Two Email Aliases — Match Both via `identities[].issuerAssignedId`

**Date:** 2026-04-28
**Status:** **CONFIRMED — fixes `wait_for_sponsor_dm` no-reply for cross-tenant sponsors.**
**Context:** A sponsor was added as a B2B guest with invitation email `brandwe@microsoft.com` (the alias used at invite time), but their actual chat-member identity in Teams uses their home-tenant primary SMTP `Brandon.Werner@microsoft.com`. The sponsor gate compared the chat member's `email` field against `sponsor.mails`, missed the match, dropped the inbound DM, and `wait_for_sponsor_dm` silently never returned.
**Problem:** B2B guest user records in Graph carry the invitation alias on `mail` / `userPrincipalName`, but the home-tenant SMTP only appears inside `identities[]` as a `signInType: "federated"` entry whose `issuerAssignedId` is the home SMTP. Same human, two email aliases, gate only knew about one.
**Fix:** In `AgentIdentitySponsor.from_graph_user`, extract every `issuerAssignedId` that contains `@` from the `identities` array into a new `federated_emails` field, and include it in `email_identifiers()`. The chat-members API already returns the home-tenant SMTP as `email`, so the existing `with_chat_members` intersection now matches without an operator override file. Graph queries already requested `identities` in `$select`, so no API changes were needed.
**Prevention:** Whenever sponsor or principal matching depends on email/UPN identity comparison across tenants, treat `identities[].issuerAssignedId` as a first-class alias source — never as metadata. Federated B2B is the default shape for cross-org collaboration; assuming one canonical email per user will silently break.
**Evidence/references:** `src/entraclaw/identity/sponsors.py::_federated_email_identifiers`; `tests/identity/test_sponsor_federated_identities.py` (5 tests); diagnosed via `~/.entraclaw/logs/entraclaw.log` showing chat-member email `Brandon.Werner@microsoft.com` mismatching all three sponsor email sets in agent tenant.

---

### Learning #51: Any Proactive 1:1 Teams DM Requires `wait_for_sponsor_dm` — Long-Running Was Just the Canonical Case

**Date:** 2026-04-28
**Status:** **CONFIRMED — empirically reproduced in Copilot CLI (bouncing-cat ASCII task).**
**Context:** After shipping `wait_for_sponsor_dm` (Learning #49), the body prompt and the tool docstring both gated the "wait for sponsor reply" pattern on the canonical worked example: the human asks for *long-running work* and *promises a Teams ping when it's done* (e.g. "ping me when the build's green"). The model treated that wording as a literal trigger filter rather than as an example of a broader rule. When Brandon ran a quick task ("write a bouncing-cat ASCII script and message me in Teams when done"), the model classified the task as not-long-running, sent the completion DM via `send_teams_message`, said "Done" in the terminal, and ended the turn. Brandon's three Teams replies arrived in `~/.entraclaw/logs/entraclaw.log` as channel pushes but were never picked up — no `wait_for_sponsor_dm` call was blocking to receive them.
**Problem:** The narrow trigger missed the actual structural property that makes Teams replies land in Teams: **the agent proactively opened a 1:1 conversation channel.** Once the agent sends a DM to a 1:1 sponsor chat, the human's natural next-turn reaction lives in Teams, not the host CLI's terminal — regardless of whether the prior task was 5 seconds or 5 hours. Ending the turn after the proactive DM strands the human in Teams with no listener, exactly the failure mode `wait_for_sponsor_dm` was built to prevent.
**Fix:** Broadened the trigger language in *both* injection vectors. (1) `prompts/anatomy/channel-discipline.md` "Sponsor DM wait state" section now says: "Any time you proactively send a Teams DM to a 1:1 sponsor chat as part of completing the operator's request… immediately call `wait_for_sponsor_dm`. This applies even to short tasks." The long-running example is now framed as the *canonical worked example*, not the trigger. (2) The `wait_for_sponsor_dm` tool docstring in `src/entraclaw/mcp_server.py` carries the same wording — critical because Learning #48 established that Copilot CLI does NOT inject FastMCP `instructions=` into the LLM system prompt; only MCP tool descriptions reach the model reliably. Also wired `wait_animation_frame()` into the tool's heartbeat so the operator sees a cycling ASCII frame ("(•ᴗ•) zZz... listening for Teams DM [30s] (Ctrl+C to break)") via `Context.report_progress(message=...)` while the agent is parked, making the listening state visible instead of a silent terminal.
**Prevention:** When writing trigger language for an MCP tool, frame the rule on the *structural side effect* of the action ("you opened a conversation channel"), not on the narrative shape of the user's request ("they asked for long-running work"). Models pattern-match on examples; bury the example beneath the rule. Always update both the prompt anatomy file AND the tool docstring — the docstring is the only vector that reliably reaches Copilot CLI per Learning #48.
**Evidence/references:** Live failure in session 2026-04-28 (bouncing-cat task; three Teams replies stranded in `~/.entraclaw/logs/entraclaw.log`); fix shipped as `fix/wait-protocol-broadened-trigger`; new test classes `TestWaitAnimationFrame` and `TestBroadenedWaitDoctrine` in `tests/tools/test_wait_for_sponsor_dm.py`; `src/entraclaw/tools/wait_tool.py::wait_animation_frame`; `prompts/anatomy/channel-discipline.md` "Sponsor DM wait state".

---

## Historical Learnings

### [HISTORICAL] Learning #4: OBO Requires Matching Token Audience

**Date:** 2026-04-06
**Superseded by:** Agent User three-hop flow (ADR-002). OBO is no longer used.
**Original context:** Device code flow with `scopes=["User.Read"]` produces token with `aud=https://graph.microsoft.com`. OBO exchange requires matching audience. Fix was to expose custom API scope `api://<client-id>/access_as_user`.
