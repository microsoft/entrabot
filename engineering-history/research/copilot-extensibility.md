# Microsoft 365 Copilot Extensibility

> **Research date:** 2025-08  
> **Relevance to Entrabot:** Agent identity, OBO flows, device-bound autonomous agents  
> **Status:** Reference document — will evolve as Microsoft ships new capabilities

## Overview

Microsoft 365 Copilot's extensibility model allows organizations to customize and extend Copilot beyond its out-of-the-box capabilities. The model is built on three composable pillars — **Connectors** (knowledge/memory), **Agents** (orchestration/persona), and **Plugins/Actions** (skills/workflows) — unified under a single governance and security framework.

**Key architectural insight for Entrabot:** Microsoft has introduced **Entra Agent ID** — a first-class identity primitive for AI agents in Entra ID, directly analogous to Entrabot's Agent ID concept. This is the most significant finding from this research.

### Extensibility Pillars at a Glance

| Pillar | Purpose | Key Tech | Limitations |
|---|---|---|---|
| **Graph Connectors** | Ingest external data for search/grounding | Microsoft Graph API, ACLs | Read-only; no action triggers |
| **Declarative Agents** | Custom AI assistants with persona + skills | JSON manifest, Copilot Studio | Must use Microsoft's orchestrator/LLM |
| **API Plugins** | Real-time API integration and actions | OpenAPI specs, MCP protocol | Only available inside agents, not base Copilot |
| **Custom Engine Agents** | Full BYO orchestrator/model agents | M365 Agents SDK, any LLM | You own security, scaling, compliance |

### Two Agent Architectures

1. **Declarative Agents** — Low-code/no-code. You define instructions, knowledge sources, and actions. Microsoft's orchestrator, LLMs, and compliance stack do everything else. Fast to deploy, limited flexibility.

2. **Custom Engine Agents** — You bring your own orchestrator, models, and backend logic. Microsoft 365 acts as a channel (Teams, Outlook, etc.) while your service handles all AI reasoning. Maximum flexibility, maximum responsibility.

**Entrabot relevance:** An Entrabot device agent would most likely integrate as a **Custom Engine Agent** — it has its own orchestration and identity, and M365 Copilot would serve as a presentation/channel layer.

---

## Declarative Agents

### What They Are

Declarative agents are custom AI assistants that run natively on Microsoft's Copilot orchestrator. You define *what* the agent does (persona, knowledge, actions) rather than *how* it reasons. The agent surfaces inside M365 apps (Teams, Outlook, Word, etc.) as a first-class Copilot experience.

### Manifest Format

Agents are defined in a `declarativeAgent.json` manifest (schema versions v1.4, v1.5, v1.6+):

```json
{
  "version": "v1.4",
  "name": "Policy Advisor",
  "description": "Answers policy questions using internal docs.",
  "instructions": "You are a corporate policy advisor. Always cite sources...",
  "capabilities": [
    {"name": "OneDriveAndSharePoint.Read"}
  ],
  "knowledge": {
    "sharePointSites": ["https://contoso.sharepoint.com/sites/policies"]
  },
  "actions": [
    {
      "id": "createTicket",
      "pluginId": "com.contoso.servicedesk"
    }
  ],
  "conversation_starters": [
    "What is our remote work policy?",
    "How do I request PTO?"
  ],
  "behavior_overrides": {
    "suppress_model_knowledge": true
  }
}
```

### Core Properties

- **`instructions`** — Up to 8,000 chars of natural language behavioral guidelines (persona, guardrails, tone)
- **`capabilities`** — Which M365 data the agent can access (email, calendar, files, Teams messages)
- **`knowledge`** — SharePoint sites, OneDrive, web data, Teams channels the agent can ground on
- **`actions`** — Plugin references for real-time API calls (create, update, query external systems)
- **`conversation_starters`** — Sample prompts displayed to users
- **`behavior_overrides`** — (v1.4+) Suppress model's built-in knowledge, add disclaimers

### Capabilities and Limitations

**Can do:**
- Access M365 data scoped to user permissions
- Call external APIs via plugins (read + write)
- Ground responses on specific SharePoint sites / knowledge bases
- Surface in any M365 app (Teams, Outlook, Word, etc.)
- Support MCP servers for dynamic tool discovery (2025+)

**Cannot do:**
- Use a custom LLM or orchestrator (that's Custom Engine Agents)
- Operate outside the user's permission boundary
- Be globally added to base Copilot — must be explicitly selected by users
- Handle multi-step autonomous workflows without user confirmation

---

## API Plugins

### How They Work

API Plugins connect Copilot (or a declarative agent) to external APIs for both data retrieval and actions. They are defined using **OpenAPI specifications** and registered with the agent.

**Critical constraint:** Plugins are *not* globally available in base Copilot. They live inside specific agents — you must build a declarative agent to host your plugin.

### Plugin Architecture

1. Developer creates an OpenAPI spec describing available operations
2. Plugin is registered in the Teams Developer Portal or via the M365 Agents Toolkit
3. Plugin is attached to a declarative agent via the manifest
4. At runtime, the Copilot orchestrator selects the appropriate plugin action based on user intent
5. Copilot calls the API endpoint with extracted parameters and presents results

### Authentication Model for Plugins

This is directly relevant to Entrabot's OBO flow architecture.

**Supported auth methods:**

| Method | Description | Use Case |
|---|---|---|
| **None** | No auth required | Public/read-only APIs |
| **API Key** | Static key in header/query | Simple internal APIs |
| **OAuth 2.0 (Authorization Code + PKCE)** | Full OAuth flow via Entra ID | Enterprise APIs requiring user context |
| **SSO (Single Sign-On)** | Leverages existing M365 user token | Seamless user experience |
| **On-Behalf-Of (OBO)** | Plugin exchanges user token for downstream API access | Chained API calls with delegated permissions |

**OAuth/SSO flow for plugins:**
1. User is already authenticated to M365
2. Copilot runtime passes user's access token to the plugin API as a bearer token
3. Plugin validates the token against Entra ID
4. For OBO: plugin exchanges the token via Entra ID's OBO flow to call downstream APIs (e.g., Microsoft Graph) on behalf of the user
5. Redirect URI for plugins: `https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect`

**Entrabot parallel:** This is strikingly similar to Entrabot's OBO model. The key difference is that in M365 Copilot, the *user* is always the authenticated principal — the plugin acts on behalf of the user. In Entrabot, the *agent* has its own identity and can act on its own behalf or on behalf of a user.

### OpenAPI Spec in Plugin Definition

```yaml
securitySchemes:
  OAuth2:
    type: oauth2
    flows:
      authorizationCode:
        authorizationUrl: https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize
        tokenUrl: https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token
        scopes:
          api://{app-id}/access_as_user: "Access API as user"
```

---

## Graph Connectors

### Purpose

Graph Connectors bring **read-only** external data into Microsoft 365's search index and Copilot's grounding context. They make external content (Confluence, Jira, file shares, databases, HR systems) available as first-class knowledge sources.

### How They Work

1. Register an application in Microsoft Entra ID
2. Grant application permissions (`ExternalConnection.ReadWrite.OwnedBy`, `ExternalItem.ReadWrite.OwnedBy`)
3. Connector crawls external system using client credentials flow (no user interaction)
4. Items are pushed to Microsoft Graph with schema and ACLs
5. Microsoft 365 indexes the content for search and Copilot grounding

### Identity and Permissions

- **Connector identity:** Service principal in Entra ID (app registration with client ID + secret)
- **Authentication:** OAuth 2.0 client credentials flow — connector authenticates as the app, not as a user
- **ACL enforcement:** Each ingested item carries an Access Control List mapping external permissions to Entra ID users/groups. Microsoft Graph enforces **security trimming** — users only see content they have permission to access
- **Admin consent:** Required for the connector's application permissions

### Two Connector Models

| Model | Description | Auth at Query Time |
|---|---|---|
| **Synced** (common) | Content indexed into Microsoft Graph | Permissions set at ingestion via ACLs |
| **Federated** (preview) | Live query to external system at search time | OAuth 2.0 or API key; external system checks permissions |

### Relevance to Entrabot

Graph Connectors are primarily a data ingestion mechanism. An Entrabot agent could:
- Use a Graph Connector to surface device-local data in Copilot (e.g., local files, sensor data)
- Be the *source system* that a Graph Connector crawls
- Not directly integrate *as* a connector — connectors don't have agent-like autonomy

---

## Copilot Studio

### What It Is

Copilot Studio is Microsoft's low-code/no-code platform for building, testing, and publishing custom agents. It's the primary tool for creating declarative agents without writing code.

### Capabilities

- Visual agent builder with natural language instruction editing
- Knowledge source configuration (SharePoint, web, files)
- Action builder (Power Automate flows, API plugins, MCP servers)
- Multi-agent orchestration — parent agents can delegate to specialist child agents
- Built-in analytics, testing, and deployment to Teams/SharePoint/Outlook
- Copilot Tuning (Build 2025) — fine-tune models with org-specific data (5,000+ license orgs)

### Relationship to Declarative Agents

Copilot Studio is the **primary authoring tool** for declarative agents. The relationship:

- **Copilot Studio** = GUI for building agents → produces a declarative agent manifest
- **M365 Agents Toolkit** (VS Code) = Pro-code alternative → produces the same manifest format
- Both target the same runtime: the Copilot orchestrator

### Multi-Agent Orchestration

Introduced at Build 2025, Copilot Studio supports:
- **Parent-child delegation:** A router agent delegates subtasks to specialist agents
- **A2A protocol:** Agent-to-agent communication for complex workflows
- **MCP integration:** Agents can dynamically discover and invoke external tools via MCP servers

### Distribution and Governance

- Agents can be published to the M365 Admin Center
- IT admins control availability via the "Agents & Connectors" hub
- Usage analytics, agent lifecycle management, and compliance controls are centralized

---

## Agent Identity in M365 Copilot

This section is the most critical for Entrabot's architecture comparison.

### Entra Agent ID — Microsoft's "Agent ID"

**Announced at Build 2025**, Microsoft Entra Agent ID is a new identity primitive purpose-built for AI agents. This is the closest analog to Entrabot's Agent ID concept in the Microsoft ecosystem.

#### Key Properties

| Property | Description |
|---|---|
| **Unique Identity** | Every AI agent gets a unique Agent ID in the Entra directory |
| **First-class entity** | Agents are treated like users/workloads — not shoehorned into app registrations |
| **Lifecycle management** | Registration, approval, review, revocation — full lifecycle |
| **Metadata** | Purpose, owner, environment, custom security attributes |
| **Conditional Access** | Same Zero Trust policies that apply to users can apply to agents |
| **Short-lived tokens** | Agent tokens are scoped and time-limited |
| **Least privilege** | Agents must declare capabilities; no implicit permissions |
| **Blueprints** | Templates with preconfigured permissions/policies for consistent agent provisioning |
| **Central registry** | Entra Admin Center provides inventory and tracking for all agents |

#### How Agents Are Identified

1. **Entra Agent ID** — Unique identifier in the directory for the agent entity itself
2. **App Registration** — Underlying Entra ID app registration (client ID + secret/cert)
3. **Agent Name/Type** — Human-readable metadata in audit logs
4. **Owner** — The user/admin who created/manages the agent

#### Audit and Attribution

- **Purview Unified Audit Logs** capture:
  - User interactions with Copilot agents (who asked what, when, which app)
  - Admin actions on agents (publish, update, delete, deploy)
  - Agent name, agent type, admin identity for each event
  - Resource references (file IDs, message IDs accessed)
  - Sensitivity labels on accessed content
- **Searchable events:** `UpdateCopilotAgent`, `PublishCopilotAgent`, etc.
- **Retention:** 180 days default, 365 days on E5
- **Access control:** Only Audit Reader role or higher can view logs
- **Current limitation:** Full auditing is available for Copilot Studio agents; prebuilt and third-party agents have limited audit coverage

#### Comparison: Entra Agent ID vs. Entrabot Agent ID

| Aspect | Entra Agent ID | Entrabot Agent ID |
|---|---|---|
| **Scope** | Tenant-wide (Entra directory) | Cross-platform (device-bound) |
| **Identity basis** | Entra ID app registration + Agent ID overlay | Device attestation + cryptographic identity |
| **Auth model** | OAuth 2.0 / managed identity | OBO flow with device-bound keys |
| **Autonomy** | Acts on behalf of user or with delegated permissions | Can act autonomously with own identity |
| **Lifecycle** | Admin-managed via Entra Admin Center | Self-managed with platform attestation |
| **Interop** | M365 ecosystem (Teams, Outlook, etc.) | Cross-platform (any API/service) |
| **Maturity** | Preview (announced Build 2025) | Research/design phase |

### Key Insight for Entrabot

Microsoft has validated the core Entrabot premise: **AI agents need their own identity, not just delegated user tokens.** Entra Agent ID is Microsoft's answer to this, but it's:
- Tenant-scoped (not cross-platform)
- Admin-managed (not self-sovereign)
- Cloud-first (not device-bound)

Entrabot's Agent ID could potentially **register as an Entra Agent ID** within a tenant, bridging the gap between device-bound autonomous agents and the M365 ecosystem.

---

## The Copilot Orchestrator

### How Routing Works

The orchestrator is the core component that processes user requests:

1. **User Input** → natural language query via Teams, Outlook, Word, etc.
2. **Responsible AI Checks** → content safety, policy compliance
3. **Intent Recognition** → LLM analyzes query to determine user intent
4. **Skill Matching** → orchestrator checks available plugins/actions against the intent
5. **Tool Invocation** → selected plugin is called with extracted parameters; auth tokens are managed
6. **Response Assembly** → results integrated into conversational response via LLM
7. **Citation** → sources are cited in the response

### Plugin Selection Logic

The orchestrator uses the plugin's OpenAPI description (operation summaries, parameter descriptions) to match user intent to available actions. Better descriptions = better matching. This is documented at [learn.microsoft.com/microsoft-365/copilot/extensibility/orchestrator](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/orchestrator).

### Security at Every Step

- User permissions enforced throughout (security trimming)
- Plugin auth tokens managed by the runtime
- Explicit user consent required for write operations
- All interactions logged in Purview audit

---

## Protocols: MCP and A2A

### Model Context Protocol (MCP)

MCP is the open standard (originally from Anthropic, adopted by Microsoft) for connecting AI agents to external tools and data:

- **MCP Server** exposes tools/actions with standardized schemas
- **MCP Client** (Copilot) discovers and invokes tools via the protocol
- **Dynamic discovery** — agents can find new tools at runtime without redeployment
- Copilot Studio supports MCP natively as of 2025

**Entrabot opportunity:** An Entrabot device agent could expose an MCP server, making device-local capabilities available to Copilot agents. This is likely the simplest integration path.

### Agent-to-Agent (A2A) Protocol

A2A focuses on inter-agent collaboration:

- Agents can delegate tasks to other agents
- Designed for opaque agent-to-agent communication
- Complementary to MCP (A2A = agent coordination; MCP = tool access)

**Entrabot opportunity:** An Entrabot agent could participate in A2A orchestration, receiving delegated tasks from Copilot agents.

---

## Integration Patterns

### Could an Entrabot device agent be a Copilot extension?

**Yes, via multiple paths:**

#### Pattern 1: Custom Engine Agent (Recommended)

```
User ↔ M365 Copilot (Teams/Outlook) ↔ M365 Agents SDK proxy ↔ Entrabot Agent (device)
```

- Entrabot agent runs its own orchestrator on-device
- M365 acts as a channel — user interacts via Teams/Outlook
- Agent authenticates with Entra Agent ID
- Agent uses OBO flow for user-delegated operations
- Full control over reasoning, tool use, and data access

#### Pattern 2: MCP Server on Device

```
Copilot Agent → MCP Client → Entrabot MCP Server (device) → local tools/data
```

- Entrabot agent exposes device capabilities as an MCP server
- A Copilot declarative agent connects to this MCP server
- Device tools (file access, sensors, local apps) become Copilot actions
- Simpler integration; Copilot handles orchestration

#### Pattern 3: API Plugin

```
Copilot Agent → API Plugin → Entrabot Agent API endpoint → device
```

- Entrabot agent exposes an OpenAPI-described REST API
- Registered as a plugin attached to a declarative agent
- OAuth/SSO auth via Entra ID
- More rigid than MCP; requires static OpenAPI spec

#### Pattern 4: Graph Connector (Data Only)

```
Entrabot Agent → Graph Connector → Microsoft Graph index → Copilot grounding
```

- Entrabot agent pushes device data into Microsoft Graph
- Data becomes available for Copilot search and grounding
- No real-time interaction; batch data sync only
- ACLs map device permissions to Entra ID identities

### Recommended Architecture

For full Entrabot integration with M365 Copilot:

1. **Register Entrabot Agent as Entra Agent ID** — gives the agent a first-class identity in the tenant
2. **Expose device capabilities via MCP server** — for dynamic tool discovery
3. **Implement Custom Engine Agent pattern** — for scenarios requiring autonomous reasoning
4. **Use OBO flow** — when the agent needs to act on behalf of the user in M365
5. **Push data via Graph Connector** — for background data sync and grounding

---

## Community Learnings & Gotchas

### Documented Frustrations (2024-2025)

1. **Actions only work inside agents** — You cannot add plugins globally to base Copilot. This surprises many developers who assume plugins are universally available.

2. **Documentation lag** — The platform evolves faster than the docs. Community blogs, GitHub repos, and podcasts are essential supplements. The official [Copilot Developer Camp](https://microsoft.github.io/copilot-camp/) is the best hands-on resource.

3. **Licensing complexity** — Development environments require specific M365 Copilot licenses. Many developers hit permission/admin-policy roadblocks during setup. See [prerequisites](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/prerequisites).

4. **Declarative vs. Custom Engine confusion** — The distinction is non-trivial and documentation doesn't always make the right choice obvious. Rule of thumb: start declarative, go custom engine only when you need BYO orchestration.

5. **Underwhelming ROI in some deployments** — Enterprise pilots have been scaled back when end-users report needing to verify Copilot's work constantly, negating time savings.

6. **Read/write asymmetry with connectors** — Connectors bring data *in* but can't trigger actions *out*. Developers expecting bidirectional integration are disappointed.

7. **Rapid breaking changes** — Manifest schema versions (v1.4 → v1.5 → v1.6) introduce new properties and deprecate old patterns. Stay on the latest schema.

8. **Audit gaps** — Full audit logging currently covers Copilot Studio agents; prebuilt and third-party agents have limited coverage.

### Best Practices from Early Adopters

- **Start narrow:** Build agents for specific, high-value use cases. Generic "do everything" agents underperform.
- **Invest in instructions:** The `instructions` field is the most impactful part of a declarative agent. Vague instructions = generic behavior.
- **Ground thoroughly:** Connect agents to well-maintained, accurate knowledge sources. Bad data in = bad answers out.
- **Test with real users:** Copilot agent behavior can be surprising. Iterate with actual end-users, not just developers.
- **Monitor with Purview:** Set up audit log monitoring early to catch permission issues and usage patterns.

---

## Open Questions

### For Entrabot's Scenario

1. **Can an Entra Agent ID represent a device-bound agent?** Entra Agent ID is designed for cloud-hosted agents. Can it be extended to represent an agent that lives on a specific device, or would Entrabot need a separate identity layer that maps *into* Entra Agent ID?

2. **OBO flow limitations for agents:** M365's OBO flow assumes the calling principal is a user-consented app. If an Entrabot agent has its own Agent ID, can it participate in OBO without a user being in the loop? Or does the flow always require a user context?

3. **MCP server accessibility:** If an Entrabot agent runs an MCP server on a local device, how would Copilot (running in the cloud) reach it? This implies either a relay/tunnel service or a cloud-hosted proxy for the device agent.

4. **Graph Connector ACL mapping:** If device-local data has its own permission model (not based on Entra ID users), how would ACLs be mapped? Custom identity mapping may be required.

5. **Custom Engine Agent latency:** For device-bound agents, network latency between the M365 cloud orchestrator and the device could impact user experience. Is there a local-first fallback pattern?

6. **Multi-tenant agents:** If an Entrabot agent needs to work across multiple M365 tenants (e.g., a user's personal and work tenants), what are the Entra Agent ID implications?

7. **Agent autonomy limits:** Entra Agent ID enforces admin-managed lifecycle. Entrabot's vision includes self-sovereign agents. Is there a reconciliation path, or are these fundamentally different trust models?

8. **A2A protocol maturity:** How mature is A2A for production use? Could Entrabot agents participate in A2A orchestration today, or is it still experimental?

---

## Sources

### Microsoft Official Documentation
- [Extend Microsoft 365 Copilot](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/) — Main extensibility hub
- [Agents for Microsoft 365 Copilot Overview](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/agents-overview) — Agent types and architecture
- [Declarative Agent Manifest Schema 1.0](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/declarative-agent-manifest-1.0) — JSON schema reference
- [How the Copilot Orchestrator Chooses Actions](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/orchestrator) — Routing and skill matching
- [Configure Authentication for Plugins](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/api-plugin-authentication) — OAuth, SSO, OBO for plugins
- [Copilot Connectors Overview](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/overview-copilot-connector) — Graph Connectors for Copilot
- [Microsoft Entra Agent ID Documentation](https://learn.microsoft.com/en-us/entra/agent-id/) — Agent identity primitive
- [Copilot Extensibility FAQ](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/faq) — Common questions answered
- [Extensibility Prerequisites](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/prerequisites) — Dev environment setup
- [Extensibility Planning Guide](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/planning-guide) — Choosing the right approach
- [Agent Architecture Components](https://learn.microsoft.com/en-us/microsoft-copilot-studio/guidance/architecture/components-of-agent-architecture) — Copilot Studio architecture

### Microsoft Announcements
- [Build 2025: Copilot Tuning, Multi-Agent Orchestration](https://www.microsoft.com/en-us/microsoft-365/blog/2025/05/19/introducing-microsoft-365-copilot-tuning-multi-agent-orchestration-and-more-from-microsoft-build-2025/) — Major agent announcements including Entra Agent ID
- [MCP in Copilot Studio](https://www.microsoft.com/en-us/microsoft-copilot/blog/copilot-studio/introducing-model-context-protocol-mcp-in-copilot-studio-simplified-integration-with-ai-apps-and-agents/) — MCP integration announcement
- [Agent 365 Resources](https://microsoft.github.io/agent-resources/agent365/) — Microsoft's agent governance framework

### Developer Resources
- [Copilot Developer Camp](https://microsoft.github.io/copilot-camp/) — Hands-on labs and tutorials
- [Declarative Agent Auth Labs](https://microsoft.github.io/copilot-camp/pages/extend-m365-copilot/auth/) — OAuth/SSO/OBO walkthrough
- [Build Declarative Agents (GitHub)](https://github.com/MicrosoftDocs/m365copilot-docs/blob/main/docs/build-declarative-agents.md) — Source docs
- [API Plugin Auth (GitHub)](https://github.com/MicrosoftDocs/m365copilot-docs/blob/main/docs/api-plugin-authentication.md) — Auth reference source

### Community & Analysis
- [Bisser.io: Copilot Extensibility — Possibilities and Pitfalls](https://bisser.io/microsoft-365-copilot-extensibility-possibilities-and-pitfalls/) — Excellent analysis of limitations
- [Voitanos: Copilot Extensibility Options](https://www.voitanos.io/blog/microsoft-365-copilot-extensibility-options-declarative-agents-copilot-studio/) — Declarative agents vs Copilot Studio comparison
- [Voitanos: Copilot Developer GA](https://www.voitanos.io/blog/microsoft-365-copilot-generally-available-october-2024/) — GA developer experience
- [Steve Corey: Declarative vs Custom Engine Agents](https://stevecorey.com/breaking-down-copilot-agents-declarative-agents-vs-custom-engine-agents/) — Side-by-side breakdown
- [Schneider.im: Entra Agent ID — A New Era](https://www.schneider.im/microsoft-entra-agent-id-a-new-era-of-identity-for-ai-agents/) — Deep dive on Agent ID
- [AdminDroid: Entra Agent ID](https://blog.admindroid.com/new-microsoft-entra-agent-id-to-secure-and-manage-ai-agents/) — Practical Agent ID guide
- [LazyAdmin: How Agent ID Secures AI Agents](https://lazyadmin.nl/office-365/microsoft-entra-agent-id/) — Entra Agent ID walkthrough
- [Platforms of Power: Managing Entra Agent Identities for Copilot Studio](https://platformsofpower.net/managing-entra-agent-identities-for-copilot-studio/) — Copilot Studio + Agent ID
- [Lewis Does Dev: Extending Copilot with Agents & Graph Connectors](https://www.lewisdoes.dev/blog/extending-microsoft-365-copilot-with-agents-graph-connectors/) — Practical connector guide
- [AI Builders Academy: MCP with Declarative Agents](https://aibuilders.academy/mcp-with-declarative-agents-on-copilot-using-the-m365-agents-toolkit/) — MCP integration tutorial
- [A2A Protocol: A2A and MCP](https://a2a-protocol.org/latest/topics/a2a-and-mcp/) — Protocol comparison
- [Xenoss: Microsoft Copilot Enterprise Limitations](https://xenoss.io/blog/microsoft-copilot-enterprise-limitations) — Candid enterprise assessment
- [M365.fm: Copilot Extensibility Overview](https://www.m365.fm/blog/microsoft-365-copilot-extensibility-overview/) — Podcast/blog overview
- [M365.fm: Copilot Audit Logs Explained](https://www.m365.fm/blog/copilot-audit-logs-explained/) — Audit deep dive
- [PKBullock: What's New in Declarative Agents v1.4/v1.5](https://pkbullock.com/blog/2025/whats-new-with-m365-declarative-agents-v1-4-v1-5) — Schema evolution
