# MCP Messaging Server Patterns — Research

**Date:** 2026-04-06
**Purpose:** Inform the bidirectional Teams loop design with patterns from existing MCP servers that handle messaging (Slack, iMessage, Discord, Teams).

---

## Key Finding: Nobody Polls at the MCP Layer

Every production MCP messaging server uses **stateless request-response**. The LLM decides when to fetch messages. No server maintains a background polling loop or tracks "last seen message ID" between invocations. Our `watch_teams_replies` tool would be the first to do this.

The one exception is `tolgasumer/discord-mcp` (Go), which uses Discord's WebSocket gateway to push events via JSON-RPC notifications. Teams has no equivalent — Graph API is REST-only for messaging.

---

## Servers Studied

### Slack

| Server | Lang | Stars | Polling | Dedup | Token Refresh |
|--------|------|-------|---------|-------|---------------|
| [Official (mcp.slack.com)](https://docs.slack.dev/ai/slack-mcp-server/) | — | — | On-demand | None | OAuth 1hr, NO refresh token — [known pain point](https://github.com/anthropics/claude-code/issues/29257) |
| [korotovsky/slack-mcp-server](https://github.com/korotovsky/slack-mcp-server) | Go | 9k+ | On-demand + "unreads" shortcut | Slack `ts` as cursor | Static env var tokens |
| [jtalk22/slack-mcp-server](https://github.com/jtalk22/slack-mcp-server) | TS | — | On-demand | None | 4-layer fallback: env -> file -> Keychain -> Chrome extraction, mutex-locked refresh |

**Key pattern — korotovsky's "unreads" shortcut:** Single API call to `ClientUserBoot` returns all channels with `LastRead`/`Latest` metadata, then only fetches history for channels where `Latest > LastRead`. The Teams equivalent would be checking `lastMessagePreview` on chat objects before fetching full messages.

**Key lesson — token refresh is the #1 pain point:** The official Slack server's 1-hour expiry without refresh tokens caused 18 re-authentications in 5 days. Our three-hop flow is even more complex — eager refresh is essential.

### iMessage

| Server | Lang | Polling | Dedup |
|--------|------|---------|-------|
| [photon-hq/imessage-kit](https://github.com/photon-hq/imessage-kit) | TS | Timer polling (2s default) of SQLite DB | Map of ROWIDs + 1s timestamp overlap window |
| [steipete/imsg](https://github.com/steipete/imsg) | Swift | FSEvents + `--since-rowid` cursor | ROWID watermark (monotonic) |
| [carterlasalle/mac_messages_mcp](https://github.com/carterlasalle/mac_messages_mcp) | Python | On-demand query | None (stateless) |

**Key pattern — timestamp overlap + Map dedup (imessage-kit):**
```
overlap = min(1s, pollInterval)
query messages WHERE created_at >= lastCheck - overlap
filter out IDs already in seen_map
```
This prevents message loss at timestamp boundaries due to clock precision and write ordering. The overlap means you re-fetch some messages, so the Map filters duplicates.

**Key pattern — bounded seen-set cleanup:** When the Map exceeds 10,000 entries, prune to last hour's records. Prevents memory leaks in long-running processes.

**Key pattern — ROWID cursor (imsg):** Monotonic IDs beat timestamps. `WHERE rowid > last_seen` has no clock precision issues. Graph API message IDs aren't monotonic, but `$deltaLink` tokens serve the same purpose.

### Discord

| Server | Lang | Polling | Dedup | Rate Limiting |
|--------|------|---------|-------|---------------|
| [tolgasumer/discord-mcp](https://github.com/tolgasumer/discord-mcp) | Go | WebSocket gateway events | Event-driven (no dedup needed) | 30 req/min config |
| [barryyip0625/mcp-discord](https://github.com/barryyip0625/mcp-discord) | TS | On-demand | None | discord.js built-in |

**Key pattern — event streaming via JSON-RPC notifications:** `tolgasumer/discord-mcp` pushes events proactively. Event types are individually configurable to control noise. Not available for Teams (no WebSocket gateway).

**Key pattern — write rate caps:** 10 messages/min global, 3/min per channel, 5s minimum between sends. Good safety model.

### Teams

| Server | Lang | Polling | Token Refresh |
|--------|------|---------|---------------|
| [floriscornel/teams-mcp](https://github.com/floriscornel/teams-mcp) | TS | On-demand | MSAL auto-refresh with file cache |
| [InditexTech/mcp-teams-server](https://github.com/InditexTech/mcp-teams-server) | Python | On-demand | Client credentials re-request |

**Key findings from floriscornel/teams-mcp:**
- Graph API for chat messages only supports descending datetime order; ascending returns an error
- `$filter` is unreliable for chat messages — must sort/filter client-side
- HTML-to-Markdown conversion needed (Graph returns HTML)
- MSAL `ICachePlugin` handles token persistence: read-on-demand, write-on-change
- 100-page safety cap when `fetchAll` is true

---

## Graph API Pitfalls for Teams Polling

These are critical for our implementation:

1. **Chat message endpoints don't support `$orderby` or `$filter` reliably.** Sort and filter client-side after retrieval.

2. **Delta query for chat messages has ~8 month lookback limit.** Older history requires full pagination.

3. **Pagination can be cut off** — the API may stop returning `@odata.nextLink` to preserve service stability.

4. **Throttling is inconsistent.** HTTP 429 can occur without warning. Not all endpoints return `Retry-After` headers. Always implement exponential backoff as fallback.

5. **Webhooks require a public HTTPS endpoint** — non-starter for local MCP servers. Polling + delta query is the only option.

6. **Delta queries return unexpected change types** — deleted items, read-state changes — that don't match your original filter. Must handle `@removed` entries.

7. **Microsoft recommends polling the `x-ms-throttle-limit-percentage` response header** to detect approaching rate limits before hitting 429.

---

## MCP Protocol Patterns for Long-Running Operations

From the MCP spec and community:

1. **The "two-tool pattern" is canonical:** `start_X()` returns job ID, `check_X(job_id)` polls status. Most production MCP servers use this today.

2. **The Tasks primitive (experimental, spec 2025-11-25)** formalizes this: `tools/call` with `task` field returns `taskId` + `pollInterval`, client calls `tasks/get` to check status. Not yet broadly supported by clients.

3. **A single blocking tool that polls internally also works** for Claude Code's stdio transport. This is simpler but blocks the LLM while polling.

4. **Server-guided poll intervals:** Include recommended wait time in tool output so the LLM can pace itself.

5. **Rate limiting is critical:** Unthrottled MCP servers can generate 1,000+ API calls/minute from retry loops.

6. **Resource subscriptions exist in the spec but Claude Desktop doesn't support them.** Polling is the pragmatic choice.

---

## Auth Lifecycle Comparison

| Platform | Token Type | Expiry | Refresh Strategy |
|----------|-----------|--------|-----------------|
| Discord | Bot token | Never | None needed |
| Slack (official) | OAuth | 1hr | None (broken — re-auth via browser) |
| Slack (jtalk22) | Session | Variable | Mutex-locked 4-layer fallback |
| Teams (floriscornel) | OAuth delegated | ~1hr access, ~90d refresh | MSAL auto-refresh with file cache |
| Teams (InditexTech) | Client credentials | ~1hr | Re-request on expiry |
| **Teams (Entraclaw)** | **OBO chain (3 hops)** | **~1hr per hop** | **Must refresh each hop independently** |

Our three-hop flow is the most complex token lifecycle of any MCP messaging server studied. Eager refresh (55-min threshold) + lazy retry (catch 401) is the right strategy, confirmed by the pain points seen across all implementations.

---

## Design Implications for Entraclaw

### Changes from original design

1. **Use delta queries instead of raw timestamp polling.** Graph API's `/chats/{id}/messages/delta` returns a `$deltaLink` token that acts like imsg's `--since-rowid` — monotonic, no clock precision issues. Store the delta token as cursor instead of `last_seen_timestamp`.

2. **Add timestamp overlap as fallback.** If delta query fails or isn't available, fall back to timestamp-based polling with 1-second overlap + message ID dedup set (imessage-kit pattern).

3. **Bounded seen-set with cleanup.** Cap at 1,000 message IDs (our volume is much lower than iMessage), prune on threshold.

4. **Client-side filtering is mandatory.** Don't trust Graph API `$filter` for chat messages. Always filter human-vs-agent messages in Python after retrieval.

5. **Exponential backoff on 429.** Check `Retry-After` header first, fall back to exponential backoff with jitter. Monitor `x-ms-throttle-limit-percentage` header.

6. **Handle `@removed` entries from delta queries.** Don't crash on deleted messages or read-state changes in delta responses.

7. **Single-instance guard.** Log a warning if concurrent polling is detected (can't enforce, but can detect).
