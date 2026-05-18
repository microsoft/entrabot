# Planning Prompt: Multi-Tenant Lightweight Teams Chat

**For:** A Copilot CLI instance (or any planning LLM) that will design the implementation plan for the multi-tenant-lightweight-chat phase.
**Produced by:** EntraClaw Agent (Claude Opus 4.6) + the user, 2026-04-09
**Branch:** `feature/multi-tenant-lightweight-chat`

---

## Your Mission

Read the existing spec, research the external dependencies, and produce a **detailed, TDD-first implementation plan** for the multi-tenant lightweight Teams chat feature. You're not implementing it yet — you're planning it thoroughly enough that the implementation can start with high confidence.

The feature is already approved (Alice Example: "I'm supportive of this direction"). Your job is to take the high-level spec and turn it into actionable, sequenced, testable steps with all the unknowns resolved.

---

## Project Context — Read This First

**EntraClaw** is a research project exploring how AI agents should authenticate and operate in Microsoft Entra ID. It's a Python MCP server that gives an AI agent its own Teams identity via the Entra "Agent User" pattern — a real directory user object specifically for autonomous agents, parented to an Agent Identity (service principal), authenticated via a three-hop machine-to-machine token flow (Blueprint → Agent Identity → Agent User). No human in the loop after initial provisioning.

The project currently works end-to-end: the agent can send/receive Teams messages, @mention people, add members cross-tenant, and participate in group chats as a real Teams user. The gap: per-user setup requires 10-15 minutes of provisioning + admin work. The multi-tenant lightweight chat feature closes that gap with a progressive identity approach — start with the human's delegated token for instant UX, background-provision the Agent User, then seamlessly swap to it.

**Key players you should know about:**
- **the user** — project owner, product lead. Strong opinion: Agent User is the right long-term identity, not OBO.
- **Dave Fixture** — building a similar agent (CoClaw) that has proven the pattern in a different architecture.
- **Alice Example** — PM leader who drove this ask. Wants WhatsApp-like UX in Teams. Approved this direction.
- **Carol Sample** — identity PM. Pushed for the progressive model. Concerned about directory scale.
- **Henry Placeholder** — identity architect. Originally skeptical, now converged on IC3/Teams federation as a long-term scale path.
- **Bob Tester** — security architect. Flagged device code flow as insecure for security-sensitive ops.
- **Iris Sample** — PM. Her team is building a service for Agent User creation without admin permissions.

---

## REQUIRED READING (in this order)

### Primary Spec
1. **`docs/architecture/NEXT-WhatsApp-lightweight-teams-chat.md`** — THE spec. Has the architecture, implementation plan (5 steps), testing plan, and key decisions. This is what you're planning the implementation of.

### Context on WHY
2. **`docs/architecture/SPEC-dual-track-agent-identity.md`** — Broader architecture debate that validated this approach. Answers "why this instead of pure OBO" and "why this instead of just faster Agent User provisioning." Critical for understanding the architectural tradeoffs.
3. **`docs/runbooks/session-2026-04-08-teams-chat-transcript.md`** — Full transcript of the day Eric approved this. Has the conversation that shaped the design.

### Architecture Decision Records
4. **`docs/decisions/001-obo-flows-for-device-agents.md`** — Why we moved away from OBO.
5. **`docs/decisions/002-agent-user-over-obo.md`** — Why Agent User is the target identity.
6. **`docs/decisions/003-certificate-auth-over-client-secrets.md`** — Why the three-hop flow uses certs, not secrets.

### Platform Learnings (avoid re-learning the hard way)
7. **`docs/runbooks/hard-won-learnings.md`** — 29 entries from prior debugging sessions. **READ ALL OF THEM** before designing anything. They cover pitfalls like "never use az rest for Agent Identity APIs" and "always create BlueprintPrincipal explicitly."
8. **`docs/platform-learnings/entra-agent-users.md`** — Deep dive on Agent User concepts, the three-hop flow, licensing constraints, and the directory scale limits you need to respect.
9. **`docs/platform-learnings/msal-entra-agent-ids.md`** — MSAL library behavior with Agent Identity endpoints. Critical for the device code / localhost redirect flow design.
10. **`docs/platform-learnings/mcp-close-the-loop.md`** — The MCP background channel pattern (Claude Code-specific) + fallback to tool-based polling (Copilot-compatible).
11. **`docs/platform-learnings/teams-graph-api.md`** — Graph API quirks for Teams (create chat Example 6 vs Example 7, cross-tenant federation, etc.).

### Current Implementation
12. **`src/entraclaw/mcp_server.py`** — Current MCP server. Shows how tools are wired up, how the background poll works, how state is managed.
13. **`src/entraclaw/tools/teams.py`** — Teams Graph API integration. Three-hop token acquisition, create_chat, send/read/list, member management.
14. **`src/entraclaw/auth/certificate.py`** — Certificate-based JWT assertion for Hop 1.
15. **`scripts/setup.sh` + `scripts/create_entra_agent_ids.py` + `scripts/entra_provisioning.py`** — Current provisioning flow. You'll need to understand this to design the background provisioning for the multi-tenant version.
16. **`CLAUDE.md`** — Project non-negotiables. **READ THIS.** Contains hard rules like "TDD: tests first", "security fails closed", "never use `az rest` or Azure CLI tokens for Agent Identity APIs", etc.

---

## Non-Negotiables (from CLAUDE.md and hard-won experience)

These are hard constraints. Your plan MUST respect them:

1. **TDD is mandatory.** Write failing tests first, then implement. `pytest -v && ruff check .` must pass before any commit.
2. **Security paths fail closed.** If the audit log can't record, the action doesn't proceed.
3. **Every agent resource access must be attributed to an Agent ID**, never the human user.
4. **Secrets never in logs.** Use `__repr__` overrides on sensitive fields.
5. **Never redirect stderr to /dev/null.** Errors must be visible.
6. **Check every token response for `"error"` key before accessing `"access_token"`.** Entra returns error dicts, not exceptions.
7. **Never use `az rest` or Azure CLI tokens for Agent Identity APIs.** They include `Directory.AccessAsUser.All` which causes hard 403. Use direct httpx with Graph tokens.
8. **Always create BlueprintPrincipal explicitly after Blueprint.** It is NOT auto-created.
9. **Agent IDs are service principals, not users.** Never create fake user accounts with passwords.
10. **Parse `az` CLI output as JSON, not TSV.** TSV can be corrupted by warnings.

---

## Research Checklist (web + Microsoft Learn)

You don't know Microsoft's specific APIs and policies. Research these before you plan:

### MSAL Python — Multi-Tenant App Flows
- [ ] Read `msal` Python library docs on multi-tenant app registration and consent
- [ ] Understand the difference between `ConfidentialClientApplication` and `PublicClientApplication`
- [ ] Find the correct pattern for localhost redirect (interactive auth without device code)
- [ ] Check whether MSAL can do `acquire_token_interactive` with a fixed localhost port
- [ ] Understand the MSAL token cache (memory vs persistent) and how to persist it securely

### Microsoft Identity Platform — Multi-Tenant Apps
- [ ] Microsoft Learn: "How to register a multi-tenant application"
- [ ] Microsoft Learn: "Admin consent experience" — what does the admin see?
- [ ] Delegated vs application permissions: when does admin consent apply?
- [ ] `Chat.ReadWrite` delegated permission — does it require admin consent? (Ayse reported yes, even for her personal MS account)
- [ ] OAuth 2.0 authorization code flow with PKCE (the localhost redirect alternative to device code)

### Microsoft Graph — Delegated Token Capabilities for Teams
- [ ] What scopes does the human delegated token need for instant Teams chat?
  - `Chat.ReadWrite` — send/read messages
  - `Chat.Create` — create new chats
  - `User.Read` — identity info
- [ ] Can a delegated token `POST /chats` to create a self-chat? (the "one-to-one chat with yourself" case)
- [ ] When using delegated auth, do messages have a `from.user.displayName` that includes the human? Does it look weird if the agent is acting as the human?

### Background Agent User Provisioning
- [ ] Aashima's team's service for Agent User creation without admin permissions — where is it? Is there a Graph API endpoint or documentation?
- [ ] What's the minimal set of permissions needed to programmatically create Agent Users from a multi-tenant app?
- [ ] Can the multi-tenant app use its application token (not delegated) to create Blueprints and Agent Users in the consenting tenant?

### License Assignment
- [ ] Frontline Worker F1 vs F3 licensing — which includes Teams?
- [ ] Can FLW licenses be assigned to Agent Users via Graph API? (Grace Synthetic confirmed Teams Enterprise works; FLW is cheaper but less tested)
- [ ] Programmatic license assignment via Graph: `POST /users/{id}/assignLicense`

### Security — Device Code vs Localhost Redirect
- [ ] Why is device code flow considered insecure? (Bob Tester flagged this. Research phishing vectors, lack of device binding.)
- [ ] Localhost redirect flow for CLI apps — how do other tools (Azure CLI, gh) handle this?
- [ ] PKCE (Proof Key for Code Exchange) — what does it protect against?

### Teams Graph API Edge Cases
- [ ] The hard-won-learnings doc has 29 entries; pay special attention to ones about chat creation Example 6 vs Example 7, cross-tenant federation, and the 400/404 errors that aren't actual failures.
- [ ] Look up the Graph API `POST /chats` docs — Examples 1-7 cover different chat creation patterns.

---

## What You Need to Produce

A **detailed implementation plan** document at `docs/architecture/PLAN-multi-tenant-lightweight-chat.md` containing:

### 1. Architecture Decisions
For each major design choice, document:
- The choice
- Alternatives considered
- Why we're picking this
- Trade-offs we're accepting

Key decisions to make:
- **Auth flow:** localhost redirect (PKCE) vs device code. Diana flagged device code; plan should default to localhost redirect and only fall back to device code for headless scenarios.
- **MSAL client type:** public (desktop app) vs confidential (secret-holding). For a CLI tool on user's machine, public is usually correct.
- **Token storage:** in-memory only vs persistent cache. MSAL's default cache is in-memory; for persistence across restarts, where do we store it securely? (Keychain? File + encryption?)
- **Multi-tenant app registration ownership:** Brandon's tenant (werner.ac) or a new shared tenant?
- **Phase 1 message identity:** how does the agent identify itself when using the human's token? (Prefix like `[EntraClaw]` per the spec, but what does this look like in practice?)
- **Phase 2 trigger:** when does the Agent User provisioning START? Immediately on first auth? On user command? Lazy?
- **Phase 2 swap:** how is the token swap communicated to the user? Silent? Notification?
- **MCP client compatibility:** the existing channel push is Claude Code-specific. Should Phase 1 also work for Copilot CLI via tool-based polling? (Yes — the plan should support both.)

### 2. Detailed Implementation Steps

For each step from the spec's implementation plan, break it down into:
- Files to create/modify
- Functions to add (with signatures)
- Tests to write FIRST (the TDD requirement)
- Dependencies on other steps
- Estimated complexity (not time — complexity)

### 3. State Machine for Background Provisioning

The provisioning has multiple async states: `not_started → auth_in_progress → auth_complete → blueprint_creating → blueprint_ready → agent_id_creating → agent_user_creating → license_assigning → teams_provisioning → ready → failed`. Document each transition, what can fail, and how to recover.

### 4. Test Strategy

- Unit tests for each module
- Integration tests for the full flow (with mocked Graph API)
- Manual testing steps (the actual end-to-end sanity check)
- Edge cases to cover (network failure mid-provisioning, token expiry during swap, admin withdraws consent, etc.)

### 5. Rollout Plan

- How do you verify Phase 1 works before building Phase 2?
- What's the minimum viable Phase 1 that Eric could demo?
- How does this get tested without requiring a fresh MS tenant for every test run?
- What breaks if the admin doesn't approve the multi-tenant app?

### 6. Open Questions

List everything you couldn't answer from research. These become questions for Brandon / the team before implementation starts.

### 7. Risks

- Admin consent friction — some tenants may never approve
- License availability — FLW may not be available in all tenants
- MSAL behavior on macOS vs Linux vs Windows (this is a research project spanning all three)
- Cert-based flow vs device-code flow for the background provisioning (they use different identity mechanisms)
- Cross-tenant federation quirks (see Learning #29 and hard-won-learnings)

---

## What You Should NOT Do (Yet)

- Don't write code. This is a planning task.
- Don't refactor existing code unless it's part of the plan's scope.
- Don't propose architectural changes to the spec (you can flag gaps or issues, but the spec is approved).
- Don't add the dual-track OBO decorated token model — that's a separate long-term direction, not part of this build.
- Don't modify `docs/architecture/NEXT-WhatsApp-lightweight-teams-chat.md` — write your plan as a new doc that REFERENCES the spec.

---

## Format for Your Plan

Use the existing `docs/architecture/NEXT-*.md` docs as a style reference. Markdown. Clear headings. Tables where helpful. Code snippets for key API calls. Keep it scannable — this plan will be consulted during implementation.

Start each section with the WHY before the WHAT. Future-you will thank you.

---

## When You're Done

1. Commit the plan to `docs/architecture/PLAN-multi-tenant-lightweight-chat.md` on the `feature/multi-tenant-lightweight-chat` branch.
2. Run `pytest -v && ruff check .` to confirm no regressions (you shouldn't have changed code, but double-check).
3. Summarize the plan's key decisions in 5 bullet points for Brandon's review.
4. List the top 3 risks you identified and the top 3 open questions.

Brandon will review the plan, ask follow-up questions, and when he's satisfied, greenlight the implementation phase. Then a different agent (or you) will execute the plan step-by-step with TDD.

---

## Final Note

The existing EntraClaw codebase is working, tested, and well-documented. Your job is to extend it thoughtfully, not to rewrite it. When in doubt, read the existing code first. The patterns are already there — you just need to add a new auth path (multi-tenant + delegated) without disrupting the existing path (certificate + three-hop Agent User).

Good luck. Ping the team in Teams if you get stuck.
