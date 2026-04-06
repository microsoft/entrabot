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

### Learning #23: FastMCP Context Object Has Untapped Capabilities

**Date:** 2026-04-06
**Context:** Researching mechanisms for server-to-LLM communication within a tool call
**Problem:** We needed to understand what FastMCP provides beyond basic tool return values.
**Discovery:** FastMCP's `Context` object exposes: `ctx.sample()` (ask the LLM to generate text mid-tool), `ctx.elicit()` (request structured input), `ctx.report_progress()`, `ctx.set_state()`/`ctx.get_state()` (session state persistence), and `ctx.send_notification()` (for spec-defined notification types).
**Implications:** `ctx.sample()` could theoretically let `watch_teams_replies` re-engage the LLM when a reply arrives — but this is untested with Claude Code's MCP client and likely unsupported. `ctx.set_state()`/`ctx.get_state()` could replace our manual `_state` dict for cursor and seen-set management in a future refactor.
**Prevention:** Before building custom infrastructure, always check what the framework provides. FastMCP's Context is much richer than we initially used.

---

## Historical Learnings

### [HISTORICAL] Learning #4: OBO Requires Matching Token Audience

**Date:** 2026-04-06
**Superseded by:** Agent User three-hop flow (ADR-002). OBO is no longer used.
**Original context:** Device code flow with `scopes=["User.Read"]` produces token with `aud=https://graph.microsoft.com`. OBO exchange requires matching audience. Fix was to expose custom API scope `api://<client-id>/access_as_user`.
