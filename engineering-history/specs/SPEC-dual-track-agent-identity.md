# SPEC: Dual-Track Agent Identity — OBO Lightweight + Agent User Governed

> **HISTORICAL — Apr 2026 draft synthesized into shipped implementation (three auth modes: `agent_user` / `delegated` / `bot`). Kept for design rationale. See [Engineering Status](../project/status.md).**

**Date:** 2026-04-08 (updated 2026-04-09 with late-night session additions)
**Status:** Draft — synthesized from group chat debate, not yet reviewed
**Driver:** Identity architect's litmus test framework + convergence between identity, security, and PM stakeholders
**Participants:** Identity architect, identity PM, partner PM, security architect, identity governance architect, PM leadership, agent runtime team, the user
**Context:** Teams group chat debate ~19:30-22:20 UTC on 2026-04-08, continued 03:47-04:23 UTC on 2026-04-09
**Build-time Plan:** `docs/architecture/NEXT-WhatsApp-lightweight-teams-chat.md` (progressive identity: human delegated token → background Agent User provision → seamless swap)

---

## Problem Statement

How should an AI agent authenticate and be governed when acting on behalf of a human?

Two extremes exist today, and neither alone is sufficient:

1. **Same identity (OBO)** — instant UX, but no governance anchor, no identity separation
2. **Different identity (Agent User)** — governance anchor, but breaks JIT UX, provisioning latency, directory scale limits

The group agreed: **both are needed**. The debate was about architecture, sequencing, and non-functional requirements.

## Identity Architect's Litmus Test (Evaluation Framework)

All proposals must answer these questions for the "Maya scenario" — an end user running a single-purpose, ad-hoc, urgent local task:

| # | Question | Ideal Answer |
|---|----------|--------------|
| 1 | Does Maya have to think about things **other** than her task? (which identity, what permissions, did I grant enough?) | No |
| 2 | How long does she wait for boilerplate steps? | Zero — or it will be circumvented |
| 3 | What work does IT admin (Derek) need per account? | Nothing — as long as access <= user's access |
| 4 | Does Derek need to act in response to Maya starting a task? | No — unless risk signals fire |

**Personas:**
- **Maya** — end user who owns the CLI session
- **Derek** — IT admin who cares deeply about AI security + guardrails

## Agreed Architecture: Two Tracks

### Track 1: Lightweight OBO (Instant, Session-Scoped)

**For:** Ad-hoc tasks, real-time questions, "5-second" UX bar

**Identity Model:**
- Agent uses Maya's token (same OID) with **decorated claims**
- Token carries: `agentic=true`, `owner/sponsor=Maya`, `session=sid`
- Plus scoping claims: `agentic-scope=readonly`, `no-HPA`, resource-specific constraints
- Maya declares guardrails at session start: "do this, no destructive writes"

**Governance:**
- **No IGA** — lightweight identity has no directory persistence, IGA requires persistence (per the identity PM)
  - The identity governance architect challenged this: IGA can govern non-directory entities via policy rules on `<USER>-<AGENT>,<TARGET>` tuples
  - The identity PM requested a KT session with the governance team to understand how — the partner PM agreed to set up 1 hour
- **Conditional Access** — risk-based policies, fine-grained action-level controls (identity architect + identity PM agreed)
- **GSA + CAE sidecar** as data plane choke point — intercepts traffic, enforces decorated claims
- RP sees Maya's normal token unchanged — enforcement is at the proxy layer, not the resource

**Enforcement Architecture:**
```
Maya's CLI → Agent Runtime → [Decorated OBO Token] → GSA/CAE Proxy → RP
                                                      ↑
                                                Enforces: scope, HPA block,
                                                resource constraints, risk signals
                                                      ↓
                                              RP sees: Maya's normal token
```

**Key Constraints:**
- **1:1 only** — OBO/decorated-token agent communicates exclusively with its owner/sponsor. Multi-user chat (agent talking to people other than its owner) requires full Agent User with Office license (Track 2). Agreed by PM leadership, the security architect, and the identity architect on 2026-04-10. Rationale: OBO claw chatting with non-owners introduces security risks documented in the agent runtime team's "Circles of Trust" (Lobster PoC).
- Proxy choke point only works for in-network traffic (partner PM: "open claw running in AWS accessing Salesforce won't hit the proxy")
- Security architect: choke point might come from sandboxing infra, not necessarily GSA/Entra
- Cannot rely on LLM prompt-level enforcement — LLMs forget instructions over long conversations (identity PM's firsthand experience with Copilot editing a doc it was told not to)

**Litmus Test Answers (OBO Track):**
1. Maya just describes her task + declares scope constraints — no identity thinking
2. Zero wait time — token is her own, decorated inline
3. Derek does nothing per account — CA policies set once for all users
4. Derek only reacts if risk signals fire

### Track 2: Agent User (Governed, Persistent Identity)

**For:** Autonomous agents, long-running tasks, agents that need their own audit trail, sandbox scenarios

**Identity Model:**
- Agent gets its own OID — separate directory object, separate audit trail
- Full identity governance: IGA lifecycle (birth, provisioning, access certification)
- Explicit permission grants — standard delegation gestures (email, calendar, etc.)

**Governance:**
- Full IGA hooks — directory object exists, governance can manage it
- Licensing enforcement per-service (WorkIQ, Teams, EXO check token subject)
- Admin has full visibility and control

**Key Constraints:**
- Provisioning latency: 10-15 min for Teams/mailbox (the partner PM's team building a service to reduce this)
- Directory object scale: 300K default, 1M+ requires coordination with RP propagation (per the identity PM's quota table)
- User must think about permissions: "which identity, have I granted enough?"
- Ceremonial delegation gesture required for personal resources (per the security architect)

**Litmus Test Answers (Agent User Track):**
1. Maya must consider identity + permissions — fails the "just do the task" bar
2. 10-15 min provisioning wait — acceptable for "start and come back later" but not real-time
3. Partner PM: nothing needed by admin (her team's service handles it), but historically requires admin
4. No — once provisioned, agent operates autonomously

## Progressive Model (Identity PM's Synthesis)

Start with Track 1 (OBO), step-up to Track 2 (Agent User) when needed:

1. **Session start** — Maya gets instant OBO access with scope constraints
2. **Task exploration** — Maya discovers the JBTD, understands what the agent needs
3. **Promotion gesture** — Maya explicitly promotes to Agent User for autonomous/long-running work
4. **Governed operation** — Agent User with full IGA, own OID, explicit grants

This controls the scale of Track 2 (not every user creates an Agent User for every ad-hoc task).

## Late-Night Session Additions (2026-04-09 03:47-04:23 UTC)

### The Directory Scale Reality

The identity PM shared live data: she had **8 agent sessions** running simultaneously (7 + CoClaw's CLI). Extrapolating: 300K employees × 7-8 concurrent sessions = 2M+ directory objects — past the point where RP propagation costs show up and federated partner coordination is needed.

### CoClaw as Existence Proof (Agent Runtime Team)

The identity PM used CoClaw and noted two identity modes in practice:
1. **CoClaw's own account** (Agent User) — used for work actions: creating docs, checking Teams messages
2. **Identity PM's token via device code** — used for accessing her personal resources

This is literally the dual-track model running in production. The agent runtime team built custom identity scaffolding to make this work. Identity PM: "I like the fact that it uses its own account for work_iq."

However — CoClaw's device code approach is what the security architect flagged as insecure. Same OID problem: "Derek cannot tell the difference" between the human user and CoClaw when the agent acts on her token.

### Pool Model Rejected

A "connection pool" model (pre-provision Agent Users, check out/return) was proposed and rejected by the identity PM:

- **OID recycling is a security risk** — residual permissions/audit from one session would attach to the next checkout
- **Soft-deleted objects count against quota** — 30 days at full weight, 30 more at partial. Pool churn makes scale worse, not better.
- **Hard delete lifecycle** is ~2 months before quota is freed

### The IC3/Teams Federation Direction (Identity Architect + Brandon convergence)

Brandon and the identity architect — on opposite sides of the debate earlier — converged on a new direction:

> **"What is the point of having hard objects in directory if all we need is distinct chats in teams?"** — the identity architect

The proposal:
- Agent identities live in **IC3** (Teams' backend), not the Entra directory
- Issue tokens that look like **native federation tokens** with external OID = CLI session ID
- Teams sees agents as federated users from a virtual agent tenant
- IC3 already scales to billions of messages — directory quota becomes irrelevant
- Governance hooks on the session ID, not a directory object

Brandon's pitch to substrate: **"A company-wide fast REPL maker for AI"** — use Teams/IC3 as the chat interface AND the identity scale layer. Agent IDs are already GA; Microsoft must back them up at scale. FMIs (Federated Machine Identities) are likely the scaling path.

**Status:** Brandon has confirmed with substrate folks that sub-minute Agent User provisioning is doable. Talk to Teams team about the federation angle (TODO from Brandon — morning of 2026-04-09).

### Implications for BUILD Timeline

The progressive model (Track 1 → Track 2) is still the right build-time bridge. But the long-term architecture may be:

1. **Short-term (BUILD/May):** Multi-tenant app + human delegated token + background Agent User provision. Unchanged from `NEXT-WhatsApp-lightweight-teams-chat.md`.
2. **Medium-term:** Faster Agent User provisioning (seconds not minutes) via the partner PM's service + substrate fast-path.
3. **Long-term:** Agent identities in IC3 via Teams federation. No directory objects for chat-only scenarios. FMIs for workload identity.

This changes nothing about what we should build for BUILD. The bridge plan stands.

## Open Items

| # | Item | Owner | Status |
|---|------|-------|--------|
| 1 | IGA for token-only identities | Identity governance team + partner PM | KT session to be scheduled (partner PM asked for 1 hour, 2026-04-08) |
| 2 | GSA + CAE data plane sidecar feasibility | Identity PM / GSA team | "No small feat" — needs engineering scoping |
| 3 | Sandboxing infra as alternative choke point | Security architect | Exploring — knows more about this than Identity team |
| 4 | Permission categorization for scope claims | Identity PM | Required for Maya to declare "no destructive writes" |
| 5 | Out-of-network enforcement (AWS → Salesforce) | Partner PM | Parked for later — in-network first |
| 6 | Agent User provisioning speed | Partner PM's team + Brandon's substrate contacts | Brandon confirmed "doable" with substrate (2026-04-09 night session) |
| 7 | Directory quota at scale | Brandon → Teams team | Talk to Teams about IC3 federation approach (TODO 2026-04-09 morning) |
| 8 | Device code flow replacement | Security architect | Flagged as security concern — use localhost redirect |
| 9 | IC3/Teams federation for agent identities | Brandon + identity architect | New direction from 2026-04-09 session — agents as native fed users in Teams backend, no directory objects |
| 10 | FMI (Federated Machine Identities) in IC3 | Brandon | Proposed as long-term scale path for Agent IDs |

## Key Agreements

1. **Everyone agrees both tracks are needed** — not either/or (partner PM, corrected by the agent runtime team + Brandon)
2. **Same governance plumbing for both** — provisioning, tagging, risk assessment applies to OBO and Agent User (partner PM)
3. **Proxy/gateway enforcement, not RP changes** — RPs don't need to understand decorated claims (identity architect, identity PM)
4. **No prompt-level enforcement** — must be at identity/token/sandbox layer (identity PM, from her Copilot experience)
5. **No admin setup for ad-hoc tasks** — leadership aligns (partner PM, confirmed 2026-04-08)
6. **May timeline pressure** — identity PM wants an Identity-owned fallback plan for May
7. **Agent IDs are GA, must scale** — Microsoft cannot ship a product that doesn't back up at scale (Brandon, 2026-04-09 night session)
8. **Progressive model is the BUILD-time bridge** — human delegated token → background Agent User provision → seamless swap. Not a parallel architecture.
9. **OBO track is 1:1 only** — agent communicates exclusively with its owner/sponsor. Multi-user chat requires Agent User + Office license (Track 2). Security rationale: OBO agent chatting beyond owner introduces Circles of Trust risks (PM leadership, security architect, identity architect, 2026-04-10).

## Relationship to Existing Work

- **EntraBot (Brandon + agent runtime team):** Currently implements Track 2 (Agent User) end-to-end. Working today with Teams identity, @mentions, cross-tenant federation, multi-chat. Proves Agent User viability.
- **Coclaw (agent runtime team):** Agent in sandbox, does coding directly. Complementary to identity layer.
- **Multi-tenant lightweight chat spec:** `docs/architecture/NEXT-WhatsApp-lightweight-teams-chat.md` — implements the progressive model (start with delegated token, background-provision Agent User).
- **Actor-attribution working group:** Designing the OBO + actor attribution model — non-directory FMI/SPIFFE-like actor identity.
- **Partner PM's provisioning service:** Reduces Track 2 setup — no admin permissions needed for Agent User creation.

## What This Spec Does NOT Cover

- Token format changes (decorated claims design)
- GSA/CAE sidecar implementation
- Windows AppContainer/sandbox integration
- Licensing model for Agent Users at scale
- Cross-tenant OBO flows
