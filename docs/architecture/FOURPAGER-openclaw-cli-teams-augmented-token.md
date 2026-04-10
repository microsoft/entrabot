# Augmenting Delegated Tokens for AI Agent Identity in Teams

**A four-page proposal**
**From:** Brandon Werner (Identity), with input from Henry Placeholder, Carol Sample, Iris Sample, Bob Tester, Alice Example, Dave Fixture, Eve Mock, Grace Synthetic, Frank Demo
**Date:** 2026-04-09
**Audience:** Microsoft Teams Agent ID team, Substrate leadership, Leo Fixture
**Status:** Draft for review

---

## 1. The Problem We Are Solving

AI agents are no longer experimental side-projects. In the last six months, Microsoft has shipped Agent IDs to GA, GitHub has shipped GitHub Copilot CLI, and multiple internal product groups (ClawPilot, M365 Copilot, Openclaw research, the Maya POC team) are building agent runtimes that act on behalf of human users. **Every one of these agents needs to communicate with its human user.** The natural channel for that communication, inside Microsoft, is Teams.

Today the developer experience for agent-to-human chat in Teams is stuck between two unsatisfying choices, and **neither serves the most important customer scenario**.

**Choice A — The agent runs as a separate Entra identity (Agent User).** This is the model Openclaw uses today and what Microsoft shipped GA. The agent is provisioned via Blueprint → Agent Identity → Agent User, gets its own UPN, its own audit trail, its own Teams presence, and its own M365 license. Identity separation is real. Governance, IGA, Conditional Access, and audit all attach correctly. **But the model is heavy:** Teams/Exchange resource provisioning takes 10–15 minutes per identity, every Agent User counts against the tenant directory quota (300K default, 1M+ requires federated-partner coordination), and admin onboarding is required per tenant. This is the right answer for *autonomous* agents — agents that run for days, do work on their own, and need persistent identity. It is the wrong answer for *interactive* agents that run for five minutes to help a human with a quick task. At the scale at which Agent IDs will be adopted (100K-employee enterprises × multiple agent sessions per day per user), the directory simply will not hold them all.

**Choice B — The agent runs as the human (delegated token, plain OAuth).** No new identity is created. The agent uses the human's existing access token to call Graph and post messages. Speed and scale are unbounded — there is nothing to provision — and the agent inherits the human's exact permissions, which is exactly what an interactive helper needs in order to read the human's calendar, check the human's email, or access the human's documents. **But the agent is invisible to governance.** Sender attribution at the directory level is the human; audit logs say "Maya did it" with no indication that an AI mediated the action; Conditional Access cannot apply different policies to agentic activity than to normal user activity; and if Maya's account starts behaving unusually because her agents are running, the only enforcement lever the IT admin has is to clamp down on Maya — which makes her own day-to-day worse. Today, every team that has shipped this pattern has resorted to the same workaround: prepend a string like `[EntraClaw]` or `[CoClaw]` to the message body. That string is forgeable, has no enforcement weight, and Teams renders the messages identically to anything Maya would type herself.

The concrete product manifestation of this gap is the **Openclaw / CLI ↔ Teams chat scenario**: a developer opens a CLI agent on their laptop, asks it to do a quick research task, and expects to chat with it through Teams the same way they would chat with a colleague. Today, that experience is either heavyweight (10-minute provisioning wait, admin call) or fake (string prefix in body, no governance). Neither is shippable. We need a path that gives interactive agents real attribution and governance hooks **without** the directory cost of full Agent Users.

## 2. Why We Are Pursuing Two Paths

After three days of architectural discussion across Identity, Teams, GitHub, M365, and security, the team has converged on a position that initially feels like fragmentation but is actually the only honest answer: **interactive agents and autonomous agents are operationally different enough that one identity construct cannot optimally serve both, and customers will use both for different jobs at different times.**

| Dimension | Scenario 1 — Interactive Agent | Scenario 2 — Autonomous Agent |
|---|---|---|
| Lifetime | Minutes to hours | Days to indefinite |
| Identity persistence | Ephemeral, tied to a CLI session | Persistent, lives in the directory |
| Provisioning latency tolerance | Zero (sub-10-second target) | Acceptable (10–15 minutes) |
| Scale per user | Many concurrent sessions per user per day | One or a small number per agent role |
| Access pattern | Acts on the human's resources (calendar, files, mail, chats) | Has its own access surface, granted explicitly |
| Governance need | Forensic attribution + risk-based CA | Full IGA, lifecycle, access reviews |
| Existing model | Delegated token (no identity separation today) | Agent User (Microsoft GA today) |

**The structural argument for two paths is permissions, not just speed and scale.** Bob Tester made the sharpest version of this point: an Agent User in a separate tenant cannot access the human's personal resources at all — it has no calendar, no mailbox, no relationship to the human's documents in M365. If Maya wants her agent to read her email or schedule meetings on her calendar, the agent fundamentally needs Maya's identity context. That is structural, not solvable by faster provisioning. A purely Agent-User-only world cannot serve interactive helpers that need to act on the human's data.

**The directory scale problem is also real and not solvable by speed alone.** Carol Sample documented the quota table: tenants get 300K objects by default with one verified domain, support 1M with explicit lift, and need RP-propagation coordination with federated partners beyond 1M. Aashima's team is working on faster Agent User provisioning; substrate can almost certainly drive provisioning latency from minutes to seconds. But faster provisioning of an object that still consumes directory quota does not change the math. With one Agent User per session per user, even a mid-sized tenant collapses against the quota wall before the year is out. Soft-deleted objects continue to count for ~30 days, so churning through pooled identities makes the problem worse, not better.

**Brandon's customer-credibility concern remains.** Microsoft has GA'd Agent IDs and made a public promise about them. We cannot ship a story that reads "use Agent IDs unless you are building something at scale, in which case use a different decorated-token flow that we are also calling 'agent' but with different semantics." That is exactly the kind of construct-proliferation that has burned customers on managed identities, federated credentials, and workload identities in the past. **The two paths must be presented as complementary points on the same customer journey**: the interactive helper today, the autonomous agent tomorrow when the work stabilizes. The customer's mental model is "what kind of agent am I building" — not "which token construct am I using." The platform should present that abstraction even if the underlying constructs differ.

## 3. The Proposal: Augmented Delegated Tokens for the Interactive Path

We propose a new token-issuance flow that reuses the existing delegated OAuth surface but adds **Entra-issued, cryptographically signed claims** that mark the token as agentic and carry attribution + scoping metadata. This is the same delegated-user OBO pattern that ships today, with three additions: a recognized issuance flow, a fixed claim schema, and a commitment that the claims are stamped by Entra, not asserted by the client.

### Token Structure

A standard delegated user token issued for an interactive agent session would carry the existing claims plus an `agentic` claim group:

```jsonc
{
  // ----- Existing standard claims -----
  "aud": "https://graph.microsoft.com",
  "iss": "https://login.microsoftonline.com/{tenant}/v2.0",
  "sub": "<user-oid>",                  // Maya
  "oid": "<user-oid>",                  // Maya — same as sub
  "upn": "maya@contoso.com",
  "tid": "<tenant-id>",                 // Maya's home tenant
  "scp": "Chat.ReadWrite User.Read",    // Standard delegated scopes
  "exp": 1781234567,

  // ----- New: agentic claim group -----
  "agentic": {
    "agentic": true,                    // Boolean marker
    "session": "agt-7c4f...",           // Per-session identifier (NEW)
    "owner": "<user-oid>",              // Sponsor — same as sub for interactive
    "client": "<agent-runtime-app-id>", // Which agent runtime issued this
    "scope": ["readonly"],              // High-level scope category
    "constraints": {
      "no_hpa": true,                   // Disallow highly privileged actions
      "resources": ["chat", "user.read"] // Allowed resource categories
    }
  }
}
```

### Critical Design Rules

1. **Entra issues the claims, not the client.** When an agent runtime requests a token via a recognized agentic flow (a new grant type, a specific scope, or a flagged client app), Entra stamps the `agentic` claim group on the token before signing it. The runtime cannot fabricate, omit, or modify those claims. The signed claims are cryptographically verifiable downstream.
2. **The session ID is the per-session attribution anchor.** Each agent session gets a fresh, unique session identifier. The session ID is the unit of governance — Conditional Access policies, CAE revocation, anomaly detection, and audit forensics all key off it. Importantly, **the session ID is not a directory object** — it lives only in the token and in transient session-store records. This is what gives the model unbounded scale: there is no per-session directory cost.
3. **Constraints are user-declared, runtime-enforced.** Maya declares her guardrails at session start ("read-only, no destructive writes, calendar and mail only"). The runtime translates her declaration into the `constraints` block. Because the claims are Entra-issued, the runtime cannot lie about what Maya declared.
4. **Enforcement is at the gateway, not the resource provider.** Resource providers (Graph endpoints, downstream services) do not need to be enlightened about the new claims. A gateway/sidecar (GSA + CAE) intercepts traffic, reads the agentic claims, and applies policy before the request reaches the RP. The RP sees a normal-looking token from the human's identity, exactly as today.
5. **The user gesture distinguishes interactive from autonomous.** Borrowing from Eve Mock's framing: a CLI invocation with no special flag (`maya-helper review my pr`) gets the lightweight delegated-token-with-agentic-claims path; an explicit `/agent create persistent-helper` gesture gets the heavyweight Agent User provisioning path. The customer's choice is "what kind of agent" not "which identity construct" — the platform routes based on the gesture.

### What This Buys Us

- **Forensic attribution.** Audit logs can distinguish Maya-the-human from Maya-via-her-agent for every action, even though both use the same OID. Security incident investigation can separate human-driven and agent-driven activity. This is the "assume breach" telemetry value Alice Example called out — and it is real even before any UI work happens.
- **Risk-based governance.** Conditional Access policies can target the agentic session ID specifically: "block agentic sessions from posting to channels," "require step-up auth for HPA from agentic sessions," "auto-revoke an agentic session after 24 hours." None of these affect Maya's normal sessions.
- **CAE revocation per session.** If risk signals fire on an agent session, CAE can kill *only* that session token without invalidating Maya's normal authentication. The blast radius of a compromised agent stays contained to its session, not Maya's whole account.
- **No new directory objects.** Scale is unbounded because the session is not persisted as a directory entity. Tenants do not pay quota for ephemeral agent sessions.

This is **capability-based security** rather than identity-based security. It does not replace Agent Users for autonomous agents, where identity-based separation is still the right answer. It complements them.

## 4. The Ask of Teams

The architectural pattern above has value the moment Entra ships token issuance for the agentic claims, even with no platform-side enlightenment — the audit, CA, and CAE benefits are real on day one. **But the lightweight scenario only delivers the customer-visible UX promise — "an AI agent chatting with me in Teams that I can clearly tell apart from myself" — if Teams reads the claims and renders the chat differently.**

The specific ask of the Teams team has two parts.

**4.1 Read the agentic claims from the access token at message-send time and render the message visually distinct.** The minimum viable enlightenment is two UI changes:

1. **Sender decoration.** When a message is posted via a token whose `agentic.agentic` claim is true, render the sender display name with an unmistakable AI marker — a bot icon, an "AI" badge, or a colored prefix. The marker makes it visually obvious to every reader (the human user and anyone else in the chat) that this message came from an AI agent acting on the human's behalf, not from the human directly.
2. **Distinct chat entity.** Render agent messages as if they came from a separate participant in the chat, not from the human user themselves. In a 1:1 chat, that means agent messages should appear on the *left* side of the conversation (like another participant) rather than the *right* side (like the user's own messages). In group chats, the agent should appear as a distinct roster entry, not as a duplicate of the human. The display name can still be derived from the human (e.g., "Maya's helper"), but the chat treats it as its own participant.

The token claim is the source of truth for both behaviors. Teams does not need to know about agent runtimes, agent registration, or any new identity construct. It only needs to read a single claim from the token it already accepts and adjust two rendering decisions.

**4.2 Include the agentic session ID in chat audit events.** When an agent message is posted, the `agentic.session` value should flow through to the audit pipeline (M365 Unified Audit Log, Purview, Defender) as a structured field on the chat event. This is what unlocks forensic attribution and anomaly detection at the security tooling layer. Without it, the chat audit looks the same as a human-typed message and the SOC has nothing to filter on.

### Why Teams Should Prioritize This

- **The same enlightenment serves both identity tracks.** A Teams client that reads an `agentic` claim works equally well for delegated tokens (Scenario 1) and for Agent User tokens (Scenario 2). One enlightenment investment, two scenarios served. There is no zero-sum tradeoff between this and the existing Agent User work.
- **Multiple Microsoft product groups need it.** GitHub Copilot CLI (Patrick), ClawPilot (Dave Fixture), the Maya POC team, Openclaw research, and Aashima's digital teammate work are all converging on the same need. Without a Teams-side answer, every team invents its own string-prefix workaround that does not actually solve attribution. With a Teams-side answer, all of them get a consistent customer experience.
- **The customer-facing alternative is worse.** Without enlightenment, every shipping agent product will use forgeable string prefixes in message bodies. Customers who care about attribution (security teams, regulated industries, large enterprise IT) will see an inconsistent, ungovernable mess across products and rightly conclude that Microsoft has not solved this problem. With enlightenment, the platform answer is uniform.
- **The work is small.** The Teams team is not being asked to design a new identity model or build a new chat surface. They are being asked to read one claim from a token they already accept and adjust two existing rendering paths. The engineering cost is low; the scoping cost is the conversation we are now having.

We are happy to provide the token format spec, sample tokens, a working agent runtime that issues the augmented requests (Openclaw, on `feature/multi-tenant-lightweight-chat`), and direct collaboration with whoever owns the chat rendering pipeline. Brandon Werner is driving the bottom-up engagement with the Teams Agent ID team this week and will report back by Monday. If the bottom-up path stalls, we will escalate via Leo Fixture to align with Omar's parallel push and get this prioritized in BUILD scope.
