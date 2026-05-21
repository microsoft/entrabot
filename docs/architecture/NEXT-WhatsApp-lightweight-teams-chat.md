# NEXT: Lightweight Teams Chat — Multi-Tenant App with Progressive Identity

**Date:** 2026-04-08 (updated 2026-04-09 — architecture debate context added, plan unchanged)
**Status:** Approved — ready to implement
**Branch:** `feature/multi-tenant-lightweight-chat`
**Driver:** PM leadership's request for WhatsApp-like simplicity in Teams; Substrate leadership's standup pushback on heavyweight provisioning
**Priority:** Moved ahead of Windows isolation work (rescheduled to weekend)
**Approval:** PM leadership ("I'm supportive of this direction"), the user, the platform team
**Related:** `docs/architecture/SPEC-dual-track-agent-identity.md` (broader architecture debate that validated this build-time plan)

---

## Problem Statement

The current EntraClaw setup requires per-user admin work:
- Entra provisioner app creation
- Blueprint + BlueprintPrincipal + Agent Identity
- Agent User creation + M365 license (E3/E5 at $25/mo)
- 10-15 minute provisioning delay for Teams/mailbox
- Certificate generation + OS keystore integration

This is heavyweight for the scenario where you just want an agent to chat with you on Teams.

**Goal:** User installs MCP server, signs in once, starts chatting on Teams immediately. No per-user admin involvement. Agent User identity provisioned automatically in the background.

## Agreed Direction

After discussion between Brandon, the agent runtime team, PM leadership, the identity PM, the identity architect, and licensing/federation contacts:

- **Use a multi-tenant app** that an admin approves once per tenant
- **Start with the human's delegated token** for instant Teams access
- **Background-provision Agent User** for eventual identity separation
- **No WhatsApp integration** — the ask is WhatsApp-like UX **in Teams**
- **Agent User is non-negotiable** — Brandon and the agent runtime team disagree with dropping it
- The identity PM clarified: also want to explore sponsor-identity option, but Agent User path comes first
- PM leadership confirmed: "having working code that shows/proves some limitations is more helpful than theorizing"

### Why Not Just Fix Agent User Provisioning?

- A partner PM team is building a service to create Agent Users without admin permissions (per the identity architect)
- A peer M365 claw project already uses Agent Users, wants shorter provisioning times
- But those efforts are still evolving — this proposal works today with existing APIs

### The Admin Consent Problem

- Chat.ReadWrite delegated permission requires admin consent in enterprise tenants
- You can work around this by running your own tenant (like Brandon with werner.ac)
- But for corp tenants (MSIT, etc.), admin approval is unavoidable
- The identity PM confirmed: even using her own MS account hits "requires admin approval" for Teams messaging APIs

## Architecture

### Multi-Tenant App Registration

One app registration, published as multi-tenant. Admin consents once per tenant.

**App permissions needed:**
- `Chat.ReadWrite` (delegated) — for instant messaging via human's token
- `User.ReadWrite.All` (application) — for background Agent User creation
- `Application.ReadWrite.All` (application) — for Blueprint creation
- `DelegatedPermissionGrant.ReadWrite.All` (application) — for consent grants

### Progressive Identity Flow

```
Phase 1: INSTANT (human's delegated token)
┌──────────────────────────────────────────────────────────┐
│ User installs MCP server → device code login             │
│ MCP server gets delegated token → Chat.ReadWrite         │
│ Agent chats on Teams AS the human (prefixed [EntraClaw]) │
│ Works in < 60 seconds                                    │
└──────────────────────────────────┬───────────────────────┘
                                   │ (background, async)
Phase 2: UPGRADE (Agent User)      ▼
┌──────────────────────────────────────────────────────────┐
│ App creates Blueprint + Agent Identity + Agent User      │
│ Assigns Teams-capable license (FLW F1/F3 preferred)      │
│ Waits for Teams provisioning (~10-15 min)                │
│ Generates certificate, stores in OS keystore             │
│ Three-hop token flow: Blueprint → Agent ID → Agent User  │
│ Seamlessly switches to Agent User token                  │
│ Messages now come from agent's own identity              │
└──────────────────────────────────────────────────────────┘
```

### Architecture Diagram

```
[Human on Teams mobile/desktop]
     │
     │ (existing Teams chat UI — no changes)
     │
[Graph API: /chats/{id}/messages]
     │
     │ Phase 1: human's delegated token
     │ Phase 2: Agent User token (idtyp=user)
     │
[EntraClaw MCP Server]
     │
     ├── Device code auth (MSAL, multi-tenant app)
     ├── Background provisioner (Blueprint → Agent ID → Agent User)
     ├── Channel push (notifications/claude/channel)
     └── All existing tools (send, read, watch, members, audit, whoami)
```

## End User Experience

**After admin one-time setup:**

1. `pip install entraclaw` + add to `.mcp.json` (or Claude Code marketplace eventually)
2. Start Claude Code / Copilot CLI
3. MCP server starts → shows device code URL + code
4. User opens URL, enters code, signs in with their Microsoft account (10 seconds)
5. Agent can immediately chat on Teams (using human's identity, prefixed)
6. Background: Agent User provisioning happens automatically
7. Once Agent User is ready: seamless switch to agent's own identity

**What the admin does once:**
- Approves the EntraClaw multi-tenant app in their tenant (standard enterprise app onboarding)

**What the user never has to do:**
- No Blueprint or Agent Identity creation
- No certificate generation
- No license assignment
- No waiting for provisioning before first use
- No `setup.sh`

## Implementation Plan

### Step 1: Multi-Tenant App Registration

Create an app registration in Brandon's tenant (werner.ac) configured as multi-tenant.

- Register app in Azure portal with multi-tenant audience
- Configure redirect URIs for device code flow
- Add required permissions (delegated + application)
- Record app ID and configure in MCP server

### Step 2: Device Code Auth Flow

Add MSAL-based device code authentication to the MCP server.

**Files to create/modify:**
- `src/entraclaw/auth/device_code.py` — new module for device code flow using MSAL
- `src/entraclaw/mcp_server.py` — add device code auth as alternative to certificate auth
- `src/entraclaw/config.py` — add multi-tenant app config (app ID, scopes)

**Flow:**
1. MCP server starts, checks for existing token cache
2. If no cached token: initiate device code flow, print URL + code
3. User authenticates → MSAL caches refresh token
4. Subsequent starts use cached token (silent refresh)

**Scopes:** `Chat.ReadWrite`, `Chat.Create`, `User.Read`

### Step 3: Sponsor-Identity Messaging

Modify the existing tools to work with the human's delegated token (not just Agent User token).

**Changes:**
- `send_teams_message` — works with either token type
- `read_teams_messages` — works with either token type
- Message prefixing: when using human's token, prepend `[EntraClaw]` to distinguish agent messages
- Chat creation: create a "self-chat" or use existing chat

### Step 4: Background Agent User Provisioning

Reuse existing provisioning logic from `scripts/` but run it as a background task within the MCP server.

**Flow:**
1. After device code auth succeeds, kick off background provisioning
2. Use the multi-tenant app's application permissions (not delegated)
3. Create Blueprint → BlueprintPrincipal → Agent Identity → Agent User
4. Assign FLW (F1/F3) license if available, else E3/E5
5. Generate certificate, store in OS keystore
6. Once Agent User has Teams access: acquire Agent User token via three-hop flow
7. Swap token in `_state` — all subsequent tool calls use Agent User

**State tracking:**
- New `_state["identity_mode"]`: `"sponsor"` or `"agent_user"`
- `_state["provisioning_status"]`: `"not_started"`, `"in_progress"`, `"complete"`, `"failed"`

### Step 5: Token Swap and Seamless Upgrade

When Agent User provisioning completes:
1. Acquire Agent User token
2. Update `_state["token"]` and `_state["identity_mode"]`
3. Notify the channel: "Upgraded to Agent User identity — messages now come from EntraClaw Agent"
4. All subsequent messages sent as Agent User, not human

## Testing Plan

- Unit tests for device code flow (mock MSAL)
- Unit tests for sponsor-identity messaging (mock Graph)
- Unit tests for background provisioning state machine
- Unit tests for token swap
- Integration test: full flow from device code → sponsor messaging → Agent User upgrade

## Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| WhatsApp integration? | No | The identity PM clarified: WhatsApp UX **in Teams**, not actual WhatsApp |
| Drop Agent User? | No | Brandon and the agent runtime team: non-negotiable for audit separation |
| Auth flow | Device code | Works headless, no browser redirect needed for CLI |
| Token library | MSAL | Standard for multi-tenant apps, handles caching/refresh |
| License for Agent User | FLW F1/F3 preferred | Cheaper ($2.25/mo vs $23/mo), includes Teams |
| Message prefix in Phase 1 | `[EntraClaw]` | Distinguishes agent messages when using human's identity |

## Conversation Context (Raw Notes)

### Identity PM Clarification of Intent

"There is one scenario with 2 options: One option is to have the agent runtime have its own identity — the AgentUser. The other option is to have the agent runtime have the owner's identity. This would allow a lot more delegation cause it would start with access to everything that the owner has access to (and we'd have to propose a way to scope it down). Whether you or I or anyone else agrees with that direction or not, we also want to explore this option."

### Identity Architect Federation Question

A teammate asked: if the corp admin requires MFA for external users, can the agent in a "rogue wolf" tenant still interact? Answer: federated chat (external access) authenticates in the home tenant, so corp MFA policies for B2B guests don't apply. Corp admin's levers are: disable external access entirely, or block specific domains.

### Agent Runtime Team on Channels

"Practically this also means it can't interact in channels without admin approval across tenants." — federated chat works for 1:1 and group chats, but Teams channels require guest/member access in the target tenant.

### Licensing Contact on Licensing

"I got it working without frontier as well, just Teams enterprise license did the trick." and "UX doesn't allow this license on Agentic Users, API does" — confirming Graph API can assign licenses that the portal blocks.

### Path B: No Admin Needed (Existing Capability)

If the admin doesn't approve the multi-tenant app, users can still set up their own tenant (like Brandon with werner.ac), create an Agent User there, and federate into any Teams chat in any org. This is the existing EntraClaw capability. The multi-tenant app just makes it seamless for users who can get admin approval.

## Open Questions

1. Can MSAL device code flow acquire Chat.ReadWrite without admin consent in some tenants?
2. What is the exact FLW F1/F3 provisioning time for Teams?
3. Can the multi-tenant app's application permissions create Agent Users in the consenting tenant?
4. Should we support both paths (multi-tenant app + standalone tenant) in the same MCP server?
5. What does the "self-chat" look like in Teams when the agent messages using the human's identity?

## 2026-04-09 Update — Architecture Debate Context

Between 2026-04-08 evening and 2026-04-09 early morning, the group (the identity platform team, the user, the agent runtime team) had an extended debate on the broader agent identity architecture. Full synthesis in `docs/architecture/SPEC-dual-track-agent-identity.md`.

**Does this change the build-time plan?** No. The progressive identity approach in this spec was **validated** by the debate as the right bridge. Key points:

1. **The OBO vs Agent User debate is long-term architecture, not a BUILD blocker.** This spec is the short-term bridge regardless of where that lands.

2. **The security architect flagged device code flow as insecure** for security-sensitive operations. Action: use localhost redirect for the initial auth flow instead of pure device code. (Step 2 of the implementation plan — update the MSAL flow choice.)

3. **The identity PM confirmed device code UX works great in practice** (CoClaw proved it, even from her phone on the go). But "great UX" is not "secure enough" — we still need the localhost redirect for prod.

4. **Directory scale is real but solvable.** The identity PM's quota table: 300K default, 1M+ needs coordination. The identity PM had 7-8 concurrent agent sessions at once — this doesn't scale if every session creates an Agent User. Mitigations:
   - Don't pool (OID recycling is a security risk, soft-delete quota makes churn worse)
   - Do push substrate for fast provisioning (Brandon confirmed doable)
   - Long-term: Teams/IC3 federation for chat-only agent identities (the identity architect's idea — virtual agent tenant, session IDs as external OIDs)

5. **Brandon's substrate commitment:** fast Agent User provisioning (sub-minute) is doable. That makes the Phase 1 → Phase 2 swap fast enough for BUILD.

6. **Agent User is NOT replaced by OBO.** Agent User remains the security anchor. OBO with decorated tokens is a UX layer with weaker security (same OID = no real isolation). The progressive model uses delegated tokens as a bridge, not as an architectural endpoint.

### Minor Adjustments to This Spec

- **Step 2 (Device Code Auth Flow):** Change default to **localhost redirect** for security (per the security architect's flag). Keep device code as a fallback for headless environments.
- **Step 4 (Background Agent User Provisioning):** Coordinate with Brandon's substrate contacts for sub-minute provisioning target. If achievable, Phase 2 upgrade happens in seconds, not 10-15 minutes.
- **Step 5 (Token Swap):** No change — the seamless swap logic is the same regardless of wait time.

### Not Changed

- Multi-tenant app registration (Step 1)
- Agent User as the target identity (non-negotiable)
- Phase 1 / Phase 2 flow
- End user experience
- Testing plan

### New Future Direction (Tracked Separately)

The IC3/Teams federation approach (agent identities as native federated users in Teams backend, no directory objects) is a promising long-term direction but is **not part of this build-time spec**. Brandon has a TODO to talk to the Teams team about it on 2026-04-09 morning. If that lands, it would be a follow-up spec, not a change to this one.
