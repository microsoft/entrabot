# TODOS

## P1

### ~~Token auto-refresh in teams_send~~ ✅ DONE
Implemented as `_with_token_retry()` in `mcp_server.py` and `_ensure_valid_token()` (proactive refresh at 55 min). All tools use it.

### AppContainer sandbox production implementation
Tonight's spike proves feasibility. Production version needs: filesystem allowlist, network filtering (Graph API only), process spawn restrictions, MCP server integration. May require Win32 C extension from Python.
- **Effort:** L (CC: ~1-2 days)
- **Depends on:** AppContainer spike results
- **Source:** CEO review, refined premise (sandbox co-equal with identity)

## P2

### Move provisioner to standalone service for production
Extract the background provisioner from the MCP server process into a separate service that handles Agent User creation server-side. Shipping `Application.ReadWrite.All` client_credentials to end-user machines is a trust boundary issue for production. Embedded provisioner is acceptable for research (single developer machine).
- **Effort:** L (CC: ~M)
- **Depends on:** PR #2 (embedded provisioner ships first as proof of concept)
- **Source:** CEO review + Codex cross-model review, tension point #2

### ~~Graph API 429 rate limit handler~~ ✅ DONE
Implemented as `RetryOn429Transport` in `tools/rate_limit.py`. Wraps httpx async transport — all Graph calls (send, read, create_or_find_chat) auto-retry up to 3 times with Retry-After backoff. 7 tests.

### Persist sent-message IDs across restarts
Serialize the in-memory sent-message-ID set to keyring or local file, reload on startup. Currently the set is lost on restart, meaning prior agent-sent messages in delegated mode could be re-processed as human instructions. The `[EntraClaw]` prefix provides a secondary defense (filter messages starting with prefix after restart), but persistence eliminates the gap entirely. ~50 LOC + corruption handling.
- **Effort:** S (CC: ~S)
- **Depends on:** PR #1 (sent-message tracking must ship first)
- **Source:** Eng review + Codex outside voice, tension point #3

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
