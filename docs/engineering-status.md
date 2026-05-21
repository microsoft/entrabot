# Engineering Status

**Last updated:** 2026-05-21
**Status:** v1 released. Three auth modes (Agent User / Delegated / Bot Gateway) running locally on macOS, Linux, and ARM64 Windows 11. **1,237 tests** across the suite, ruff clean. Body-first prompt architecture loads at boot; persona-sati MCP wires personality and memory when configured. ADR-005 cloud-memory Phases 1, 2, 5, 6a shipped — blob storage is opt-in via `setup.sh --use-cloud-memory`. Work IQ Word migration landed (PR #75) and the `send_teams_message` auto-wait pattern is host-gated and deterministic. README, docs site, and GitHub Pages auto-deploy refreshed 2026-05-21.

---

## In Progress

Source of truth for detail: `TODOS.md` in the repository root. One line each below.

- **Script-toolkit docs closeout** — `./status.sh` is the canonical entry; finish the remaining script-reference polish and smoke verification. See `TODOS.md` P1.
- **Test isolation: blob env leakage** — `tmp_data_dir` fixture in `tests/tools/test_interaction_log.py` doesn't clear `ENTRACLAW_BLOB_ENDPOINT`; 10 tests fail on any machine with blob env configured.
- **MCP server orphans on Claude Code exit** — background poll tasks sit outside FastMCP's lifespan cancel scope; new sessions spawn a second server, both poll Graph independently.
- **Daily summary scheduler — wrong day + double-fire** — UTC-based `target_day` summarizes the brand-new UTC day at 5pm PDT; scheduler fired twice at the same second on 2026-04-17.
- **Email cursor sub-second precision** — cursor file at second precision; an email at the cursor's exact second gets re-pushed once on every server restart.

## Recently Shipped

Last ~30 days. Full diff: `git log --since="2026-04-21"`.

- **README + docs-site refresh** (2026-05-21, ff9a8dd, 9b73dee, b495073) — developer-first README rewrite, GitHub Pages auto-deploy, nav restructure.
- **OSS sanitization passes** (2026-05-21, f2a3c18; 2026-05-18, 6cff243) — PII scrub, personal data and private identifiers removed from repo.
- **Script toolkit refactor + E2E smoke harness** (2026-05-19, PR #77) — `./status.sh` consolidated; `setup.sh --status` delegates to the same implementation.
- **Sponsor DM wait — host-gated fix** (2026-05-19, 905b7d0, 26aa647) — `wait_for_sponsor_dm` no longer blocks Claude Code sessions; channel push is the path on hosts that support it. Learning #66.
- **Targeted agent identity teardown + setup hardening** (2026-05-15, f21cf82, c47552b) — granular teardown without nuking the Blueprint; identity consolidation no longer races on partial state.
- **Work IQ Word migration** (2026-05-15, PR #75) — Word create/read/comment/reply now routes through Microsoft Agent 365's Work IQ Word MCP server; Graph beta `/comments` is no longer the comment-reply path.
- **Persona-sati host bootstrap + Entra sponsor authority** (2026-05-02, PR #72) — `bootstrap_session()` returns the assembled mind contract in one call; sponsor allowlist resolved via Entra.
- **Server-side placeholder + commitment-language discipline hooks** (2026-05-04, PR #74) — outbound Teams text gets server-side commitment detection; placeholders post + resolve cleanly.
- **Files MCP — share gate inverted + PR2 author/upload/share** (2026-04-30, PR #69, PR #64) — sponsor requester required, recipient unrestricted; write + upload + share to sponsor tools land.

## Open Issues

### `add_file_comment` Word/Excel — Graph beta `/comments` returns 404

Work IQ Word migration shipped on `main` (PR #75, 2026-05-15) and live smoke passed; Graph beta `/comments` is no longer the Word-comment reply path. The legacy `add_file_comment` against `.docx` files still 404s — the endpoint family is SharePoint list-item metadata comments, not document-content comments. For `.xlsx` the correct surface is `/workbook/comments`, not addressed.

**Fix tracked in:** `docs/runbooks/hard-won-learnings.md` Learning #60; Work IQ pivot in PR #75 covers the Word path.

### CLI commitment-language detection — unenforced

Server-side commitment detection ships as part of the `send_teams_message` outbound hooks, but only fires on outbound Teams text. Commitments uttered to the operator in the host terminal ("I'll batch this up later") never reach the MCP server and silently drift. Server-side enforcement is host-portable; CLI enforcement would require host-specific hooks. We chose host-portable coverage of the Teams path over Claude-Code-only coverage of both paths.

**Fix tracked in:** `scripts/hooks/README.md` "Known coverage gaps"; body-prompt strengthening in `prompts/anatomy/channel-discipline.md` is the current mitigation.

### Persona-sati 12h MCP refresh bug — PR #47 paused at Blueprint constraint

Every ~12 hours, Claude Code's cached MCP bearer expires and persona-sati tools start returning Zod schema errors until restart. Draft PR `persona-sati#47` (550/550 tests pass) implemented OIDC discovery + PRM shim, but the live OAuth flow at the 12h boundary is blocked: Microsoft's Agent Blueprint app type — which the Persona-Sati Blueprint uses — cannot have public-client redirect URIs and cannot be flipped to fallback-public-client mode. Tenant state reverted; no behavioral change for cert-based three-hop or OBO. Possible resolutions: separate Entra app reg for the MCP client (Phase 2A), persona-sati implements OAuth 2.1 itself (Phase 2B), or land #47 as Phase 1 only.

**Fix tracked in:** `persona-sati#47`; `docs/platform-learnings/agent-id-blueprints-and-users.md` for the platform constraint.

### Agent Identity missing `Application.Read.All` after provisioning

`wait_for_sponsor_dm` and sponsor-gated flows fail with 403 `Authorization_RequestDenied` when calling `/servicePrincipals/{id}/microsoft.graph.agentIdentity/sponsors`. Root cause: `scripts/create_entra_agent_ids.py` doesn't grant `Application.Read.All` to the Agent Identity service principal. Workaround applied manually on the Windows VM via `New-MgServicePrincipalAppRoleAssignment`.

**Fix tracked in:** Add `Application.Read.All` grant to `create_entra_agent_ids.py`. Partially addressed in 45bec0f (Windows port acceptance); full provisioner fix still pending across platforms.

---

## Architecture Snapshot

The agent talks to the MCP server over stdio. The server reads the Blueprint's private key from the OS keystore, walks the three-hop chain to produce a delegated user token, and uses that token for every Graph and Work IQ call. Inbound Teams messages and emails arrive via background polls and push into the client as channel notifications. Operational state lives locally by default or in Azure Blob Storage scoped to the Agent User's object ID when cloud memory is enabled.

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

## What Works (Shipped Capabilities)

- End-to-end: `setup.sh` → MCP server → Teams message delivery
- Three-hop Agent User token flow (Blueprint → Agent Identity → Agent User, `idtyp=user`)
- Agent User creation, license assignment, and consent grant via Graph
- Dedicated provisioner app (avoids Azure CLI token rejection)
- State persisted in `.entraclaw-state.json` (idempotent setup, no secret reset)
- Certificate auth for Blueprint — private key in OS keystore, no secrets on disk (ADR-003)
- Token auto-refresh: eager (55-min) + lazy (401 retry) for all tools
- Bidirectional Teams channel — background polling + `notifications/claude/channel` push, 2s overlap dedup window
- 429 rate-limit handling with `Retry-After` propagation
- Multi-user group chat support and cross-tenant federated B2B chats (auto-detects guest UPN, resolves home tenant via OpenID discovery)
- `add_teams_member` — add users to a chat at runtime
- Three auth modes: `agent_user`, `delegated` (MSAL interactive + device-code fallback), `bot` (M365 Agents SDK + JSONL IPC + Dev Tunnel)
- Progressive identity state machine: `UNAUTHENTICATED → DELEGATED → PROVISIONING → AGENT_USER` with `asyncio.Lock`-protected transitions
- Identity-aware user ID — `_effective_user_id()` returns the right object ID per mode
- Body-first prompt architecture — `@include` expansion of `prompts/anatomy/*.md`, non-overridable security and channel discipline
- Persona-sati MCP integration — `bootstrap_session()` returns the assembled mind contract in one call; clean fallback when not configured
- Adaptive Cards — `send_card` with `tool_activity`, `task_status`, `build_result` templates
- Microsoft Agent 365 Work IQ Word — create, read, comment, reply-to-comment
- Files MCP — SharePoint / OneDrive read, write, upload, share (two-gate sponsor authorization on share)
- Email — background poll with Purview-encrypted detection, daily summary at 5pm PDT
- Promises — `add_promise` / `list_promises` / `resolve_promise` backed by entraclaw blob, ETag concurrency, identity-scoped
- Storage backends — `LocalBackend` (default) and `BlobBackend` (opt-in via `setup.sh --use-cloud-memory`)
- ARM64 Windows 11 acceptance — full CNG signing via TPM-backed cert, three-hop flow live against Entra, Copilot CLI MCP registration, Teams DM round-trip
- 66 hard-won learnings documented in `docs/runbooks/hard-won-learnings.md`

---

## Test + Lint Discipline

1,237 tests collected. `pytest -v && ruff check .` must pass before every commit. Coverage threshold is 80% via `--cov-fail-under=80`. Background poll loops, identity transitions, three-hop token mints, and outbound discipline hooks are covered by integration tests with respx-mocked Graph endpoints.
