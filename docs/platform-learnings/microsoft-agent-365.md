# Microsoft Agent 365 — Platform Research

**Date:** 2026-05-04 (3 days post-GA)
**Status:** **GA as of 2026-05-01** — Microsoft's enterprise control plane for AI agents.
**Why this exists:** Entraclaw built its Agent Identity stack from Graph primitives. Agent 365 is Microsoft's blessed productization of that same identity model, plus a tooling/observability/governance plane on top. We need to know precisely where entraclaw and A365 overlap, where A365 supersedes our work, and what the gaps are for the immediate Work IQ Word pivot (Learning #60).

---

## 1. What Agent 365 is

**Agent 365 is the control plane for AI agents in Microsoft 365.** It packages four distinct capabilities sold as a unified per-user license:

1. **Identity** — Entra Agent ID (the same primitive entraclaw uses: Blueprint → Agent Identity → Agent User).
2. **Observability** — OpenTelemetry tracing for every inference call, tool call, and lifecycle event, surfaced in M365 admin center + Microsoft Defender Advanced Hunting.
3. **Tooling** — Work IQ MCP servers (Microsoft-hosted MCP servers wrapping Mail, Calendar, Teams, SharePoint, OneDrive, Word, User, Copilot search, Dataverse) + the MCP Management Server (build your own custom MCP servers).
4. **Identity-as-a-teammate** ("AI teammate") — agents with their own M365 mailbox, Teams presence, directory entry. Frontier-preview-only at GA.

**Sold as part of Microsoft 365 E7 (the "Frontier Suite")** = M365 E5 + Copilot + Entra Suite + Agent 365. CSP transactability went live May 1, 2026. Per-user licensing.

**Three integration surfaces:**
- **Pro-code** — Agent 365 SDK (Python / Node / .NET) + CLI (`a365`, `dotnet tool install --global Microsoft.Agents.A365.DevTools.Cli`).
- **Low-code** — Microsoft Copilot Studio.
- **Pro-code orchestration** — Microsoft Foundry (Azure AI Foundry).

---

## 2. The two agent types

A365 distinguishes **two** agent shapes; the distinction is architectural and load-bearing.

| Type | Identity model | Where it lives | Frontier preview gate |
|------|----------------|----------------|----------------------|
| **Agent** | Acts on behalf of a user (delegated/OBO) **or** as an app (S2S). Registered as either an Entra application or an Agent Identity Blueprint. | Wherever the developer hosts it. Reachable via API or chat. | No — GA. |
| **AI teammate** | Operates as **its own user identity** in M365. Has mailbox, Teams presence, directory entry, manager relationship. Always built on a blueprint. | Inside Microsoft 365 — @mentioned in Teams, emailed directly, added to channels. | **Yes** — Frontier preview only. |

**Critical insight:** Entraclaw's `agent_user` mode IS the AI-teammate pattern, just self-built. We provisioned an Agent User UPN (`entraclaw-agent-sati-agent@werner.ac`), it has its own mailbox, posts as itself in Teams chats, and shows up in directory. Microsoft's GA AI-teammate is the same architecture with managed lifecycle + admin center surface. The work entraclaw did to bootstrap this from Graph primitives is precisely what A365 sells as a product.

---

## 3. The four capability tiers (incremental adoption)

A365 is explicitly designed for incremental adoption — you don't have to buy the whole thing at once. Each tier builds on the previous.

| Tier | Capability | What entraclaw has today |
|------|-----------|---------------------------|
| **1. Register** | Agent appears in M365 admin center inventory. Inherits Entra ID governance, Purview, and Defender if blueprint-based. | ✅ Provisioned — but via direct Graph `POST /servicePrincipals` extending `Microsoft.Graph.AgentIdentity`, NOT via the A365 blueprint flow. Brandon's M365 admin center shows 173 agents in the registry; the Sati Agent is one of them. |
| **2. Observability** | OpenTelemetry tracing on every inference + tool call + agent lifecycle event. Visible in admin center, Purview, Defender. | ❌ Not wired. Entraclaw has its own audit log (`tools/audit.py` + `~/.entraclaw/audit/`) but it's local JSONL, not OTel. |
| **3. Work IQ tools** | Agent calls Microsoft-hosted MCP servers (Mail/Calendar/Teams/SharePoint/OneDrive/Word/User/Copilot/Dataverse). Permissions admin-controlled. | ❌ Not connected. We've built parallel Graph-based implementations for Teams, email, Files; Work IQ Word is the **headline gap** that motivated the Learning #60 pivot. |
| **4. AI teammate** | Mailbox, Teams presence, directory entry. Frontier preview only. | 🟡 Self-built equivalent — Bot Gateway (`src/entraclaw/bot/`), Teams Graph integration, Bot Framework SDK. Functional but not under MS-managed lifecycle. |

**Where entraclaw and A365 overlap-vs-supersede:**

- Tiers 1 & 4 — entraclaw built equivalents from scratch; A365 productizes them. **Migration value is governance/observability, not new functionality.**
- Tier 2 — entraclaw has a local audit log; A365 provides tenant-wide visibility that's wired into Purview + Defender. **Migration value is high — IT/security teams get a unified view.**
- Tier 3 — entraclaw has none of this; A365 provides it ready-made. **Migration value is highest — fixes the Word-comments dead-end and unlocks 8 other Work IQ surfaces.**

---

## 4. Work IQ MCP catalog

**Two URL forms exist** (verified live 2026-05-04 via M365 admin center detail pane):

- **Server-scoped (canonical, what M365 admin center shows):** `https://agent365.svc.cloud.microsoft/agents/servers/{serverName}`
- **Tenant-scoped (used in coding-agent `.mcp.json` examples):** `https://agent365.svc.cloud.microsoft/agents/tenants/{tenantId}/servers/{serverName}`

Both route to the same backend. Detail-pane URLs in this table use the canonical server-scoped form (with `…/` shorthand for the host prefix `https://agent365.svc.cloud.microsoft`).

| Server ID | Display name | What it covers | URL |
|-----------|--------------|----------------|-----|
| `mcp_CopilotTools` (a.k.a. `searchtools`) | Work IQ Copilot | Chat with M365 Copilot, multi-turn threads, file-grounded responses | `…/agents/servers/mcp_CopilotTools` |
| `mcp_CalendarTools` | Work IQ Calendar | Create/list/update/delete events, accept/decline, conflict resolution | `…/agents/servers/mcp_CalendarTools` |
| `mcp_MailTools` | Work IQ Mail | Create/update/delete messages, reply, reply-all, semantic search | `…/agents/servers/mcp_MailTools` |
| `mcp_SharePointTools` | Work IQ SharePoint | Upload, get metadata, search, list management | `…/agents/servers/mcp_SharePointTools` |
| `mcp_OneDriveTools` | Work IQ OneDrive | Personal OneDrive file/folder management | `…/agents/servers/mcp_OneDriveTools` |
| `mcp_TeamsTools` | Work IQ Teams | Chat CRUD, members, post messages, channel ops | `…/agents/servers/mcp_TeamsTools` |
| `mcp_UserTools` (a.k.a. `me`) | Work IQ User | Manager, direct reports, profile, search users | `…/agents/servers/mcp_UserTools` |
| `mcp_WordServer` | Work IQ Word | **Create/read documents, add comments, reply to comments** — the headline tool for Learning #60's Hirsch use case. Verified Available in werner.ac at version 1.0.3 (M365 admin center, 2026-05-04). | `…/agents/servers/mcp_WordServer` |
| `mcp_DataverseTools` | Dataverse / D365 | CRUD + domain-specific actions | `…/agents/servers/mcp_DataverseTools` |

**Word IQ tools (verbatim):**
- `WordCreateNewDocument(fileName, contentInHtml)` — create in user's OneDrive root
- `WordGetDocumentContent(url)` — fetch DOCX content + **all comments** by SharePoint/OneDrive sharing URL
- `WordCreateNewComment(driveId, documentId, newComment)` — top-level comment
- `WordReplyToComment(commentId, driveId, documentId, newComment)` — **the one we need for Hirsch defense**

**Catalog-level facts:**
- Each MCP server is fronted by an OAuth-protected gateway. The gateway audience is a per-server Entra app (e.g., Mail's audience is `api://05879165-0320-489e-b644-f72b33f3edf0`).
- Early docs used a scope-name pattern of `McpServers.{ServerName}.All` (for example, `McpServers.Mail.All` / `McpServers.Word.All`). The current generated manifest observed in this repo uses the shared `Tools.ListInvoke.All` scope plus a server-specific `audience`.
- Catalog values (`url`, `audience`, and `scope`) are populated automatically by `a365 develop add-mcp-servers` into a project-local `ToolingManifest.json`. Runtime must prefer the generated manifest over hard-coded catalog defaults.

**`ToolingManifest.json` shape:**
```json
{
  "mcpServers": [
    {
      "mcpServerName": "mcp_MailTools",
      "mcpServerUniqueName": "mcp_MailTools",
      "url": "https://agent365.svc.cloud.microsoft/agents/servers/mcp_MailTools",
      "scope": "Tools.ListInvoke.All",
      "audience": "c2d0c2b6-8013-4346-9f8b-b81d3b754a29",
      "publisher": "Microsoft"
    }
  ]
}
```

---

## 5. Auth model (the load-bearing details)

A365 has **three** distinct caller patterns. They share the underlying Entra primitives but differ in client setup. **Entraclaw's pivot lands cleanly in pattern A.**

### Pattern A — Agent built on a blueprint (agentic auth)

This is the standard A365 pattern and the one entraclaw will use.

1. Provision an **Agent Identity Blueprint** via `a365 setup all` (or AI-guided setup at `aka.ms/agent365enable`). Entraclaw is the exception: it already provisions the Blueprint with Graph beta, so setup must point `a365.config.json` at the existing `BLUEPRINT_APP_ID` instead of using A365 config-free `--agent-name` mode.
2. The blueprint = an Entra application + `agentIdentityBlueprint` resource. Has API permissions list.
3. Add Work IQ servers: `a365 develop add-mcp-servers mcp_WordServer` — writes to local `ToolingManifest.json`.
4. **A Global Administrator** runs `a365 setup permissions mcp` — patches the blueprint with the new OAuth permissions, requires admin consent.
5. At runtime the agent's SDK call (`add_tool_servers_to_agent` / `addToolServersToAgent` / `AddToolServersToAgentAsync`) loads the manifest, resolves OAuth tokens against each server's audience, and exposes the MCP tools to the orchestrator.

**One-time tenant setup:** A365 needs first-party resource service principals for Agent Tools (`ea9ffc3e-8a23-4a7d-836d-234d7c7565c1`) and Work IQ Word MCP (`c2d0c2b6-8013-4346-9f8b-b81d3b754a29`). On fresh tenants the CLI can fail to create or resolve them, then print `OAuth2 grants failed` while still exiting 0. Entraclaw setup now runs `scripts/ensure_a365_work_iq_permissions.py` before `a365 setup permissions mcp` to create those service principals and Blueprint-wide grants for `McpServersMetadata.Read.All` and `Tools.ListInvoke.All`.

**Config-free trap:** `a365 setup permissions mcp --agent-name "EntraClaw Code Agent"` derives a blueprint display name of `EntraClaw Code Agent Blueprint`. Entraclaw's actual blueprint display name is `EntraClaw Code Agent`, with the real app ID in `.entraclaw-state.json`. Use generated `a365.config.json` with `agentBlueprintId`, `agentIdentityDisplayName`, and `deploymentProjectPath`, then run `a365 setup permissions mcp` without `--agent-name`.

**Existing Blueprint + multiple Agent Users trap:** `--use-blueprint` must also know which Agent User chain to reuse. If local state is missing and no suffix/UPN is provided, provisioning falls back to the unsuffixed `entraclaw-agent@...` UPN. Pass `--agent-user-upn=entraclaw-agent-sati-agent@werner.ac` (or `--with-upn-suffix=sati-agent`) so A365 config points at the intended Agent Identity and Agent User.

### Pattern B — Agent acting on behalf of a user (OBO auth)

For agents that need user delegated access. Agent receives a delegated user token, exchanges it for the MCP gateway audience. No agent identity required.

Use case fit: NOT what entraclaw needs for the Hirsch Word reply (the agent is acting as itself, not on behalf of Brandon).

### Pattern C — Coding-agent client (public-client OAuth)

For Claude Code, Copilot CLI, VS Code consuming Work IQ MCP servers from a developer's machine.

1. Register an Entra **public client app** (any name, e.g., `WorkIQ-PublicMCPClient`).
2. Add API permissions for each Work IQ server you want — the M365 admin center exposes them as `WorkIQ-MailServer`, `WorkIQ-WordServer`, etc.
3. Add redirect URI `http://localhost:8080/callback` (or `vscode://...` etc.).
4. Drop a `.mcp.json` like:
   ```json
   {
     "mcpServers": {
       "WorkIQ-WordServer": {
         "type": "http",
         "url": "https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer",
         "oauth": {
           "clientId": "{app-id}",
           "callbackPort": 8080
         }
       }
     }
   }
   ```
5. Standard MCP-protocol HTTP transport with OAuth.

**Use case fit:** Claude Code consuming Work IQ Word directly during a conversation with Brandon. Could be a useful adjunct to entraclaw's agent path.

### Critical license requirement

> *"You must have a Microsoft 365 Copilot license to use Work IQ MCP servers."*

The Agent User identity (`entraclaw-agent-sati-agent@werner.ac`) needs M365 Copilot assigned. `setup.sh` / `setup-windows.ps1` call `create_entra_agent_ids.py`, which now checks the Agent User's existing SKUs and assigns Microsoft 365 Copilot separately from the Teams-capable M365 SKU used for Teams presence. Copilot is an add-on requirement for Work IQ; it does not replace the base Teams/M365 license. In `/subscribedSkus`, the Copilot SKU may appear as `Microsoft_365_Copilot` rather than the older all-caps `MICROSOFT_365_COPILOT` spelling; setup recognizes both.

---

## 6. SDK packages (what's actually shippable)

### Python (PyPI)

| Package | Purpose |
|---------|---------|
| `microsoft-agents-a365-runtime` | Auth scope resolution, Power Platform API discovery, environment config. The "boot" package. |
| `microsoft-agents-a365-tooling` | Core MCP tooling — `McpToolServerConfigurationService`, manifest parsing. |
| `microsoft-agents-a365-tooling-extensions-{agentframework,openai,semantickernel,azureaifoundry}` | Orchestrator-specific `add_tool_servers_to_agent` registration. |
| `microsoft-agents-a365-observability-core` | OpenTelemetry tracing + spans for agent invocation, tool execution, LLM inference. |
| `microsoft-agents-a365-observability-extensions-{agent-framework,openai,langchain,semantic-kernel}` | Auto-instrumentation per orchestrator. |
| `microsoft-agents-a365-notifications` | Routing for Teams, Outlook, **Word comments**, email notifications. |

### JavaScript (npm)

| Package | Purpose |
|---------|---------|
| `@microsoft/agents-a365-runtime` | Boot |
| `@microsoft/agents-a365-tooling` | Core MCP |
| `@microsoft/agents-a365-tooling-extensions-claude` | **Claude SDK integration** — only language with explicit Claude support |
| `@microsoft/agents-a365-tooling-extensions-{openai,langchain}` | Other orchestrators |
| `@microsoft/agents-a365-observability` | OTel tracing with Azure Monitor integration |
| `@microsoft/agents-a365-notifications` | Type-safe notification handling for email, Word comments, etc. |

### .NET (NuGet)

Same surface area as Python. `Microsoft.Agents.A365.{Runtime,Tooling,Observability,Notifications}` plus orchestrator extension packages.

**Strategic note:** Python is entraclaw's primary language. The Python SDK has all the runtime/tooling/observability surface but **no Claude orchestrator extension** (the Claude extension only ships in JS). For entraclaw's case this doesn't matter — entraclaw is itself the Claude tool surface (an MCP server consumed by Claude Code), so we'd be using the **MCP-client** side of `microsoft-agents-a365-tooling`, not an orchestrator extension.

---

## 7. CLI workflow (the reference implementation)

```bash
dotnet tool install --global Microsoft.Agents.A365.DevTools.Cli  # one time
a365 -h                                                          # verify

# AI-guided path (recommended):
# Open project in VS Code with GitHub Copilot Chat (Agent mode), prompt:
# "Follow the steps at aka.ms/agent365enable to enable my agent for Agent 365."

# Manual path:
a365 develop list-available                  # show all MCP servers in catalog
a365 develop add-mcp-servers mcp_WordServer  # write to ToolingManifest.json
a365 develop list-configured                 # verify
a365 setup permissions mcp                   # (Global Admin) PATCH blueprint with new MCP scopes — requires admin consent
a365 develop start-mock-tooling-server       # local dev — MCP_PLATFORM_ENDPOINT=http://localhost:5309
```

The CLI is .NET 8. Cross-platform but not natively Python — entraclaw would invoke it as a subprocess if needed for any provisioning step.

---

## 8. Connect-existing-agents path (entraclaw's path)

> Source: `https://learn.microsoft.com/microsoft-agent-365/connect-existing-agents`

Microsoft explicitly documents the path for agents built outside the M365 ecosystem. Five steps:

1. **Sync to registry** — for agents on Vertex AI / Bedrock, Microsoft has automated registry sync. For agents like entraclaw built directly on Graph, the registry entry comes from the Agent Identity blueprint (already done). Visibility in admin center confirmed (Brandon's screenshot: Sati Agent is in the 173 agents).
2. **Integrate with Agent 365 SDK** — adds the SDK packages to the agent code; gives it Work IQ access + observability.
3. **Apply policies and access controls** — admin configures via M365 admin center → Agents → Settings (`/admin/manage/agent-settings`).
4. **Manage tooling via CLI** — `a365 develop add-mcp-servers` + `a365 setup permissions mcp`.
5. **Apply security/governance best practices** — Purview labels, Defender real-time protection, Entra Conditional Access. Most of this is automatic for blueprint-based agents.

**Entraclaw's specific path:** steps 2 + 4 are the actionable code+admin work. Step 1 is already done; step 3 is admin work; step 5 is policy work.

---

## 9. Ecosystem partner agents (GA launch list, May 1)

23 partners across categories:

| Category | Partners (✓ at GA, ¹ coming soon) |
|----------|----------------------------------|
| Creative & design | Adobe¹, Box, Canva, Figma, Lucid, Miro |
| HR / talent / learning | Achievers, Cornerstone, Coursera, Gloat, Zensai (AI teammate) |
| Customer support / CRM | Zendesk (AI teammate), Zoho, Rox |
| Workflow automation | n8n (agent factory) |
| Agent factories | Kasisto (financial), Kore (LOB), Nvidia¹ (NeMo) |
| Process intelligence | Celonis¹ |
| Security | Sophos |
| AI productivity | Genspark (AI teammate), Manus¹ (AI teammate) |
| Content governance | Egnyte¹ (AI teammate) |

**Two integration shapes:**
- **Agent** — single deployable agent, surfaces in Teams/M365.
- **Agent factory** — platform that provisions multiple agents, each gets its own Entra Agent ID automatically (Kasisto, Kore, Nvidia, n8n).

**Strategic relevance for entraclaw:** entraclaw is an **agent**, not an agent factory. The closest peer in posture is probably Genspark (its own M365 access, participates in Teams 1:1/group, human-in-the-loop authorization, Word collaboration) — same surface area entraclaw targets.

---

## 10. MCP Management Server (build-your-own MCP)

For organizations needing custom workflows beyond the Work IQ catalog, Agent 365 ships a **meta-MCP server** that creates other MCP servers:

- URL: `https://agent365.svc.cloud.microsoft/mcp/environments/{environment-id}/servers/MCPManagement`
- Tools: `CreateMCPServer`, `CreateToolWithConnector`, `UpdateTool`, `DeleteMCPServer`, `PublishMCPServer`
- Connector library: 1500+ connectors (ServiceNow, JIRA, etc.) + Microsoft Graph APIs + Dataverse custom APIs + arbitrary REST endpoints.
- Tenant admin–only at GA (developer self-publish coming).

**Strategic note:** entraclaw's surface (Teams DMs, email triage, sponsor-routed shares, audit log) overlaps with Work IQ Mail + Work IQ Teams. We could in principle:
- (a) Migrate to Work IQ servers and retire entraclaw's Graph-direct implementations.
- (b) Publish entraclaw as a custom MCP server via MCP Management Server.
- (c) Keep entraclaw as-is, add Work IQ Word as an additional MCP-client integration (Learning #60 pivot).

(c) is the right choice for now — minimal change, fixes the immediate Hirsch problem, leaves migration as a longer-term option.

---

## 11. Observability — what you actually get

When the SDK observability packages are installed and instrumented, every:

- **Agent invocation** (request in / response out)
- **Tool call** (which tool, what args, what result)
- **LLM inference** (prompt, response, model, token counts)

…gets captured as OpenTelemetry spans, with context propagation across the chain. Sinks:

- **M365 admin center** — agent activity dashboards.
- **Microsoft Defender** Advanced Hunting — KQL queries over agent traces. *"Inspect trace logs of tool calls made by agents. Monitor execution details, including which tools were invoked, parameters passed, and outcomes. Detect anomalies or unauthorized usage patterns."*
- **Microsoft Purview** — DLP/sensitivity classification visibility.

**Comparison to entraclaw's audit:**
- Entraclaw's `audit_log` writes JSONL to `~/.entraclaw/audit/`. Local-only, single-process, never sees other agents in the tenant.
- A365 observability is tenant-wide, multi-agent, queryable from Defender. Operationally a different category — for any production deployment we'd want both (entraclaw's tight pre/post-event audit framing for security-sensitive ops + A365's tenant-wide telemetry).

---

## 12. Notifications system

A365 has a **first-class notifications package** (`microsoft-agents-a365-notifications` / `@microsoft/agents-a365-notifications` / `Microsoft.Agents.A365.Notifications`) that handles inbound events from:

- Teams (chat messages, channel posts, @mentions)
- Outlook (emails to the agent's mailbox)
- **Word comments** (someone leaves a comment in a doc the agent has access to — this is the *exact* event the Hirsch use case turns on)
- Email lifecycle events

This is essentially a productized version of entraclaw's "background channel" architecture (`mcp_server.py:_push_channel_notification`, `tools/teams_poll.py`, email poll, chat auto-discovery). **A365 ships push-based notifications** rather than entraclaw's polling — when Brandon comments in a Word doc shared with the agent, the notifications package can deliver that event without us implementing a comment-poll loop.

**Migration consideration:** entraclaw's polling is structurally limiting (5s Teams poll, 60s email poll, 120s auto-discovery). A365's push model is a real architectural improvement. If we adopt A365 SDK observability + tooling, adopting notifications too is a natural next step.

---

## 13. Implications for entraclaw

### 13.1. The Work IQ Word pivot (Learning #60)

**Path A (recommended, minimal change):** Add Work IQ Word MCP as a sub-tool of entraclaw, keeping everything else as-is.

Concrete steps:
1. Verify the Sati Agent User has an M365 Copilot license (or get one assigned).
2. Run the one-time `New-Agent365ToolsServicePrincipalProdPublic.ps1` against werner.ac.
3. Provision an Agent Identity **Blueprint** for entraclaw via `a365 setup all` (this may need to be done from scratch since our Agent Identity was created via Graph not via blueprint — check whether `a365` can adopt an existing AgentIdentity SP).
4. `a365 develop add-mcp-servers mcp_WordServer` to write `ToolingManifest.json`.
5. Brandon (Global Admin) runs `a365 setup permissions mcp` to patch the blueprint with `McpServers.Word.All` scope + audience grant.
6. Add `microsoft-agents-a365-runtime` + `microsoft-agents-a365-tooling` to `pyproject.toml`.
7. Wrap Work IQ Word's 4 tools (`WordCreateNewDocument`, `WordGetDocumentContent`, `WordCreateNewComment`, `WordReplyToComment`) as 4 entraclaw `@mcp.tool()` wrappers in `mcp_server.py`. Pass-through with our existing audit logging.
8. Update README + CHANGELOG.

**Estimated effort:** 1–2 weeks of focused work, biggest unknowns being (a) does step 3 work for an existing Graph-provisioned AgentIdentity, (b) does step 1 succeed for an Agent User identity (license assignability), (c) does the Python SDK's `McpToolServerConfigurationService` work outside an "Agent 365 SDK"-shaped agent project (or does it need scaffolding from `a365 setup all` first).

**Headline use case:** Hirsch leaves an inflammatory comment in a Word doc. The agent gets the notification (Word-comments push), reads the doc (`WordGetDocumentContent`), drafts a reply per `user_hirsch_singhal.md` engagement rules, posts via `WordReplyToComment`. Replaces the entire dead-end of the Learning #60 plan.

### 13.2. Larger-scope opportunities (not in immediate scope)

- **Observability migration** — replace `audit_log`'s local JSONL with A365 OTel spans. Big lift in instrumenting every tool, but unlocks Defender Advanced Hunting + Purview visibility. Worth scheduling as a separate ADR.
- **Notifications migration** — replace polling-based `_push_channel_notification` with `microsoft-agents-a365-notifications` push. Operationally cleaner; eliminates Learning #56 (singleton-lock contention from poll loops).
- **Work IQ migration of Mail + Teams + SharePoint** — entraclaw's current implementations would be replaced by Work IQ servers. Each is a meaningful refactor, but we'd retire substantial Graph-direct code in `tools/teams.py`, `tools/email.py`, `tools/files.py`. Open question whether the cross-tenant + sponsor-allowlist + audit semantics survive the migration (some are entraclaw-specific that Work IQ may not preserve).
- **AI teammate via Frontier preview** — apply for the Frontier program, replace entraclaw's self-built Bot Framework + Teams provisioning with the MS-managed AI teammate lifecycle. Architecturally identical; admin overhead goes down.

### 13.3. Where entraclaw still has unique value post-A365

Even after a hypothetical full Work IQ migration, entraclaw retains:

- **Sponsor-allowlist + chat-membership two-gate authorization** for `share_file` (Learning #59) — Work IQ doesn't appear to have an equivalent and it's a defense-against-LLM-fabrication primitive worth keeping.
- **Body-prompt non-overridability** — A365 doesn't define a security/voice contract layered on top of orchestrator prompts; that's a deliberate entraclaw choice.
- **`wait_for_sponsor_dm` close-the-loop** — A365's notifications package solves the inbound-push problem, but the *wait-for-specific-human-DM* synchronous primitive is entraclaw-specific (Learning #54).
- **Singleton lock + cross-host coordination** (Learning #56) — A365 manages agent lifecycle, but if we run entraclaw locally on multiple machines, the flock-singleton matters.

**Strategic framing:** A365 is a control plane. Entraclaw is an *agent body* that opts into A365's control plane progressively. The decisions are which capabilities to delegate to MS and which to keep. Today: delegate Word comments to A365 (Work IQ Word), keep everything else. Six months from now, probably more delegation.

---

## 14. Open questions / unknowns

These need spike-level investigation before commits, in priority order:

1. **Can `a365 setup all` adopt an existing AgentIdentity SP** (provisioned via Graph), or does it require provisioning a fresh one? If the latter, migrating breaks the existing identity continuity (audit history, Teams chats, sponsor relationships).
2. **Can the Sati Agent User have an M365 Copilot license assigned?** Microsoft documents Copilot licensing as per-user, but Agent User identities are a non-human identity sub-type. Verify before committing.
3. **Does `microsoft-agents-a365-tooling` work standalone** (just MCP-client functionality) or does it require the full Agent 365 SDK runtime + manifest project structure?
4. **Is werner.ac in a region where M365 admin center "Agents and Tools" can grant Work IQ permissions to non-Microsoft-built agents?** Brandon's screenshot confirms the UI shows up, but per Microsoft's docs *"the ability to allow or disallow tooling and MCP servers in Microsoft 365 admin center might not be available in your region yet."*
5. **What does `a365 setup permissions mcp` actually write?** If it writes a per-app oauth2PermissionGrant record, it's PATCHable like our existing `grant_files_consent.py`. If it writes something else (admin consent at the SP-tenant scope?), we need to understand what operation we're authorizing.
6. **Does Word IQ Word's `WordReplyToComment` actually work on docs the agent doesn't own?** The reply use case is on Brandon's MS-tenant Word doc (`tenant.sharepoint.com`), which the agent doesn't have access to even after the share invite. Cross-tenant + Work IQ is its own scenario worth verifying.


### Discovery update: Work IQ Word local manifest

Task 0's local CLI discovery reached Agent 365 device-code authentication before
manifest generation. `a365 develop list-available` printed a device-code prompt,
but this non-interactive implementation environment cannot complete the browser
sign-in, so `a365 develop add-mcp-servers mcp_WordServer` did not run and no
`ToolingManifest.json` was written. Runtime code should still treat the manifest
as the source of truth instead of hard-coding Word-specific audience values once
a human-authenticated CLI session can generate it.

---

## 15. Recommended next-step ladder

| # | Action | Owner | Reversibility |
|---|--------|-------|---------------|
| 1 | Verify M365 Copilot license can be assigned to `entraclaw-agent-sati-agent@werner.ac`. Try assignment in M365 admin center. | Brandon | Reversible |
| 2 | In M365 admin center → **Agents and Tools** → **Tools** → search "Word" → click into **Work IQ Word MCP Server (Preview)** → screenshot the detail pane (permissions, scopes, agents that can grant). | Brandon | Read-only |
| 3 | Confirm whether `a365 setup` can adopt the existing Sati AgentIdentity (`appId=eba51655-...`) or requires fresh blueprint provisioning. Read `a365 setup all` source / docs if not already clear. | Claude (research) | Reversible |
| 4 | If the answers to (1)–(3) are favorable, write a follow-up plan `docs/superpowers/plans/2026-05-04-work-iq-word-pivot.md` with the 8 concrete steps from §13.1. | Claude | Plan only |
| 5 | If any answer is blocking (no Copilot license, can't adopt SP, region not ready), surface to Brandon and consider alternative paths (OOXML manipulation as fallback per Learning #60 §3, or pivot Hirsch defense to Teams reply instead of in-doc). | Both | Reversible |

---

## 16. Provenance (what I read for this doc, 2026-05-04)

- [Overview of Microsoft Agent 365](https://learn.microsoft.com/microsoft-agent-365/overview) — GA confirmation, prerequisites.
- [Agent 365 Identity](https://learn.microsoft.com/microsoft-agent-365/developer/identity) — Blueprint / Agent Instance / Agent User model + auth flows.
- [Get started with Agent 365 development](https://learn.microsoft.com/microsoft-agent-365/developer/get-started) — two agent types, four capability tiers, AI-guided setup.
- [Microsoft Agent 365 SDK Overview](https://learn.microsoft.com/microsoft-agent-365/developer/agent-365-sdk) — package list across Python/JS/.NET.
- [Agent 365 CLI](https://learn.microsoft.com/microsoft-agent-365/developer/agent-365-cli) — installation, commands.
- [Add and manage tools](https://learn.microsoft.com/microsoft-agent-365/developer/tooling) — `ToolingManifest.json` structure, scope/audience semantics, registration extensions.
- [Work IQ MCP overview](https://learn.microsoft.com/microsoft-agent-365/tooling-servers-overview) — catalog, Copilot Studio + Foundry + VS Code clients, public-client OAuth pattern.
- [Work IQ Word reference](https://learn.microsoft.com/microsoft-agent-365/mcp-server-reference/word) — 4 tools + parameters.
- [Connect existing agents](https://learn.microsoft.com/microsoft-agent-365/connect-existing-agents) — registry sync, SDK integration, policies.
- [Ecosystem partner agents](https://learn.microsoft.com/microsoft-agent-365/third-party-agents) — 23-partner GA list.
- [Partner Center: May 2026 announcements](https://learn.microsoft.com/partner-center/announcements/2026-may) — GA confirmation.
- [n8n MCP Server scopes example](https://learn.microsoft.com/entra/agent-id/integrate-n8n-agent#understand-the-mcp-server-scopes) — reference for what `MCP.*` scope grants look like in oauth2PermissionGrants.

Cross-references in this repo:
- `docs/runbooks/hard-won-learnings.md` Learning #60 (Graph beta /comments dead-end → Work IQ Word pivot)
- `docs/platform-learnings/entra-agent-users.md` (the precursor identity research entraclaw built on)
- `docs/platform-learnings/mcp-messaging-servers.md` (the predecessor MCP messaging-server survey — A365 changes the landscape)
- `docs/architecture/DESIGN-persona-sati-integration.md` (mind-body split — A365's "agent identity" is the body half)
- `docs/decisions/001-obo-flows-for-device-agents.md` (the OBO investigation that ultimately led to entraclaw's three-hop + agent identity stack)

### Expansion note

The first runtime integration is Work IQ Word, but the provider is intentionally
surface-neutral. Future Work IQ surfaces should add adapter modules on top of
`WorkIqProvider`; they should not fork token acquisition, manifest loading, or
MCP transport. Teams is the exception and remains Graph-native.

Work IQ expansion beyond Word should wait until the current Word path's
interactive setup/live smoke blocker is resolved.
