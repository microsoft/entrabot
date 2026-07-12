# Teams Toolkit & App SDK

> **Last Updated:** 2025-07  
> **Status:** Research Reference  
> **Relevance to Entrabot:** High — Teams is the primary human-to-agent communication channel

## Overview

**Microsoft Teams Toolkit** (rebranded as **Microsoft 365 Agents Toolkit / ATK** in May 2025) is a VS Code / Visual Studio extension that scaffolds, packages, provisions, and deploys apps and agents for Microsoft Teams, Outlook, and Microsoft 365 Copilot.

### Why This Matters for Entrabot

Entrabot autonomous agents on devices need a channel to communicate with humans. Teams is the primary target. The question is whether we should:

1. **Use Teams Toolkit** to scaffold and manage our agent as a Teams app
2. **Use the SDKs directly** (Teams SDK or M365 Agents SDK) and handle packaging ourselves
3. **Use Azure Bot Service** as a channel-agnostic transport and register Teams as a channel

**TL;DR Assessment:** Teams Toolkit is useful for rapid prototyping and handles tedious manifest/packaging/auth plumbing. But for Entrabot's scenario — autonomous agents with their own identity (Agent IDs) communicating proactively — we'll likely use the **Teams SDK (Python)** directly for bot logic and proactive messaging, with the Toolkit mainly for manifest management and sideloading during development.

---

## The SDK Landscape (as of mid-2025)

The Microsoft SDK landscape for Teams is in significant flux. Here's the current state:

| SDK | Status | Use When | Languages |
|-----|--------|----------|-----------|
| **TeamsFx** | **Deprecated** (community support until Sep 2026) | Legacy — do not use for new projects | JS/TS, .NET |
| **Teams SDK** (formerly Teams AI Library v2) | **Active / Recommended** | Teams-only apps: bots, tabs, message extensions, AI agents | JS/TS, C#, **Python (preview)** |
| **M365 Agents SDK** | **Active** | Multi-channel agents (Teams + Outlook + Copilot + Slack/Twilio) | JS/TS, C#, Python |
| **Bot Framework v4** | **Deprecated** (no new features after Dec 2025) | Legacy multi-channel bots — migrate away | JS/TS, C#, Python, Java |
| **Azure AI Foundry Agent SDK** | **Active** | Cross-cloud agent orchestrations with Azure AI | Python, C# |

### Key Naming History

```
Bot Framework v4        →  deprecated Dec 2025
TeamsFx                 →  deprecated Sep 2026
Teams AI Library v1     →  released Nov 2023, built on Bot Framework
Teams AI Library v2     →  released Sep 2025, renamed "Teams SDK" Nov 2025
Teams Toolkit           →  rebranded "M365 Agents Toolkit" May 2025
```

### Which SDK for Entrabot?

**Teams SDK (Python)** is the right choice for Entrabot because:
- Teams-only for now (no Slack/Twilio needed yet)
- Python is our primary language
- Built-in AI/LLM integration patterns
- Proactive messaging support
- SSO + OBO flow support for accessing Graph on behalf of users

---

## Key APIs & Interfaces

### Teams SDK (Python) — `teams-ai` on PyPI

```bash
pip install teams-ai
```

The Python SDK requires Python 3.9+ and builds on the Bot Framework adapter. Key abstractions:

```python
from teams import App

app = App()

@app.on("message")
async def on_message(context, state):
    """Handle incoming messages from Teams users."""
    user_text = context.activity.text
    await context.send_activity(f"You said: {user_text}")

# AI-powered agent with function calling
@app.ai.action("get_device_status")
async def get_device_status(context, state, parameters):
    """LLM can call this function when user asks about device status."""
    device_id = parameters.get("device_id")
    # ... look up device via Entrabot backend
    return {"status": "online", "last_seen": "2025-07-01T12:00:00Z"}
```

### Teams SDK (TypeScript) — Streamlined API

```typescript
import { App } from '@microsoft/teams.apps';

const app = new App();

app.on('message', async ({ api, isSignedIn, send, signin }) => {
  if (!isSignedIn) await signin();
  const me = await api.user.me.get();
  await send(`Hello, ${me.displayName}!`);
});

app.listen(3000);
```

### Teams CLI — Bootstrap Projects

```bash
# Create a new Python bot project
npx @microsoft/teams.cli@latest new python entrabot-agent --template echo

# Create a TypeScript project
npx @microsoft/teams.cli@latest new typescript entrabot-agent --template echo
```

### Key SDK Classes & Concepts

| Class/Concept | Purpose |
|---------------|---------|
| `App` | Main application entry point, handles routing |
| `@app.on("message")` | Decorator-based activity routing |
| `@app.ai.action(name)` | Register functions the LLM can call |
| `Prompt` | Defines the system prompt + conversation context for the LLM |
| `TurnState` | Manages per-turn and per-conversation state |
| `ConversationReference` | Serializable reference to a conversation (needed for proactive messaging) |
| `CloudAdapter` | Bot Framework adapter that handles Teams channel communication |
| MCP Plugin | Model Context Protocol client/server support for multi-agent interop |

---

## Auth & Identity Model

### How Teams App Auth Works

Teams apps authenticate via **Microsoft Entra ID (Azure AD)** using a registered App Registration:

```
┌──────────────┐     SSO Token      ┌─────────────────┐
│ Teams Client │──────────────────→ │ Your Bot Backend │
│ (user signed │                    │ (validates token)│
│  into Teams) │                    └────────┬────────┘
└──────────────┘                             │
                                    OBO Flow │ (exchange token)
                                             ▼
                                  ┌─────────────────────┐
                                  │ Microsoft Entra ID   │
                                  │ (issues new token    │
                                  │  with Graph scopes)  │
                                  └──────────┬──────────┘
                                             │
                                             ▼
                                  ┌─────────────────────┐
                                  │ Microsoft Graph API  │
                                  │ (User.Read, Mail,    │
                                  │  Calendar, etc.)     │
                                  └─────────────────────┘
```

### SSO (Single Sign-On)

- Users already authenticated in Teams get seamless access to the bot
- The bot receives a token scoped to `access_as_user`
- No login prompt for the user in most cases

### OBO (On-Behalf-Of) Flow

- Backend exchanges the SSO token for a token with downstream API permissions
- Always acts with **delegated** permissions (user's context)
- Cannot do app-only operations via OBO

### Key Entra ID App Registration Fields

```json
{
  "appId": "YOUR-APP-GUID",
  "api": {
    "requestedAccessTokenVersion": 2,
    "oauth2PermissionScopes": [
      {
        "value": "access_as_user",
        "type": "User",
        "userConsentDisplayName": "Access as you"
      }
    ]
  },
  "requiredResourceAccess": [
    {
      "resourceAppId": "00000003-0000-0000-c000-000000000000",
      "resourceAccess": [
        { "id": "e1fe6dd8-ba31-4d61-89e7-88639da4683d", "type": "Scope" }
      ]
    }
  ]
}
```

### How This Relates to Agent IDs

**This is a critical design question for Entrabot:**

| Aspect | Teams App Identity | Entrabot Agent ID |
|--------|-------------------|-------------------|
| Identity Provider | Microsoft Entra ID | Our own (backed by Entra ID?) |
| Principal Type | App Registration (service principal) | Per-agent identity |
| Token Type | Entra ID JWT (delegated via OBO) | TBD — could be Entra workload identity |
| Scope | User-delegated or app-only | Per-agent permissions |

**Options for mapping Agent IDs to Teams:**
1. **One Entra App Registration per agent type** — each agent class gets its own app ID
2. **One App Registration, multiple bot instances** — single Teams app, agent ID in metadata
3. **Managed Identity per agent** — use Azure Managed Identities mapped to agent IDs

The most practical for now: **one Teams app registration for all Entrabot agents**, with agent identity tracked in our own system. The Teams bot acts as a gateway/router to the appropriate agent.

---

## Teams AI Library / Teams SDK AI Features

The Teams SDK (formerly Teams AI Library) provides first-class AI integration:

### Core AI Abstractions

```python
from teams.ai import AIOptions, OpenAIModel, PromptManager

# Configure the AI model
model = OpenAIModel(
    api_key=os.environ["OPENAI_API_KEY"],
    default_model="gpt-4o"
)

# Define prompts with system instructions
prompt_manager = PromptManager(prompts_folder="./prompts")

# Wire up to the app
app = App(options=AppOptions(
    ai=AIOptions(
        model=model,
        prompt="default",
        prompt_manager=prompt_manager
    )
))
```

### Function Calling (Tools)

The SDK auto-generates JSON schemas for registered functions:

```python
@app.ai.action("lookup_agent")
async def lookup_agent(context, state, parameters):
    """Look up an Entrabot agent by its Agent ID.
    
    Args:
        agent_id: The unique identifier of the agent
    """
    agent = await entrabot_service.get_agent(parameters["agent_id"])
    return agent.to_dict()
```

### Model Context Protocol (MCP) Support

As of late 2025, the Teams SDK supports MCP natively:

- **MCP Client plugin**: Your Teams bot can consume external MCP servers (tools, prompts, resources)
- **MCP Server plugin**: Your Teams bot can expose its capabilities as an MCP server
- **Agent-to-Agent (A2A)**: Teams agents can communicate with each other

This is directly relevant to Entrabot — an Entrabot agent running on a device could expose an MCP server, and the Teams bot could act as an MCP client to it.

### Prompt Files

Prompts are defined in a folder structure:

```
prompts/
  default/
    skprompt.txt          # System prompt text
    config.json           # Model parameters (temperature, max_tokens)
    actions.json          # Available function definitions
```

---

## App Manifest & Sideloading

### Manifest Structure

The Teams app manifest (`manifest.json`) declares what the app can do:

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/teams/v1.19/MicrosoftTeams.schema.json",
  "manifestVersion": "1.19",
  "version": "1.0.0",
  "id": "YOUR-APP-ID-GUID",
  "packageName": "com.entrabot.agent",
  "developer": {
    "name": "Entrabot",
    "websiteUrl": "https://entrabot.dev",
    "privacyUrl": "https://entrabot.dev/privacy",
    "termsOfUseUrl": "https://entrabot.dev/terms"
  },
  "name": {
    "short": "Entrabot Agent",
    "full": "Entrabot Autonomous Agent Interface"
  },
  "description": {
    "short": "Communicate with your Entrabot agents",
    "full": "Interface for humans to interact with Entrabot autonomous agents on devices"
  },
  "icons": {
    "color": "color.png",
    "outline": "outline.png"
  },
  "accentColor": "#6264A7",
  "bots": [
    {
      "botId": "YOUR-BOT-APP-ID",
      "scopes": ["personal", "team", "groupChat"],
      "supportsFiles": false,
      "isNotificationOnly": false,
      "commandLists": [
        {
          "scopes": ["personal"],
          "commands": [
            { "title": "status", "description": "Check agent status" },
            { "title": "agents", "description": "List your agents" }
          ]
        }
      ]
    }
  ],
  "permissions": ["identity", "messageTeamMembers"],
  "validDomains": ["entrabot.dev", "*.azurewebsites.net"]
}
```

### Key Manifest Fields for Entrabot

| Field | Purpose | Entrabot Relevance |
|-------|---------|-------------------|
| `bots[].botId` | Entra App Registration ID | Our bot's app registration |
| `bots[].scopes` | Where bot works (personal/team/groupChat) | Likely `personal` for 1:1 agent chat |
| `bots[].isNotificationOnly` | If true, can't receive messages | `false` — we need bidirectional |
| `permissions` | App permissions | `identity` for SSO, `messageTeamMembers` for proactive messaging |
| `webApplicationInfo` | SSO configuration | Links to our Entra app registration |

### Packaging

The app package is a ZIP file at the root level:
```
entrabot-agent.zip
├── manifest.json
├── color.png        (192×192)
└── outline.png      (32×32)
```

### Sideloading for Development

1. Enable in tenant: Teams Admin Center → Teams apps → Setup policies → Allow uploading custom apps
2. In Teams: Apps → Manage your apps → Upload a custom app → Upload for me/my org
3. Or use the Agents Toolkit in VS Code which automates this

### Common Sideloading Gotchas

- All URLs must be **HTTPS** (no HTTP)
- Icons must be exact sizes (192×192 color, 32×32 outline)
- `validDomains` must include all domains your app accesses
- ZIP must have files at root (no nested folders)
- Manifest can pass validation but still fail on sideload due to subtle issues
- Teams web client has intermittent sideloading bugs — try desktop client as fallback
- Clear Teams cache if you see stale app versions

---

## Integration Patterns for Entrabot

### Architecture: Entrabot Agent as a Teams Bot

```
┌──────────────────────────────────────────────────────────────┐
│                    Microsoft Teams                            │
│  ┌──────────┐                                                │
│  │  User     │  ← chat messages →  ┌──────────────────────┐ │
│  │  (human)  │                      │ Entrabot Teams Bot   │ │
│  └──────────┘                      │ (Teams SDK / Python) │ │
│                                     └──────────┬───────────┘ │
└──────────────────────────────────────────────────┼────────────┘
                                                   │
                        Azure Bot Service          │
                        (channel registration)     │
                                                   │
                    ┌──────────────────────────────┤
                    │                              │
                    ▼                              ▼
          ┌─────────────────┐          ┌─────────────────────┐
          │ Entrabot Backend│          │ Proactive Messaging  │
          │ (Agent Router)  │          │ Service              │
          │                 │          │ (stores ConvRefs,    │
          │ - Agent Registry│          │  sends notifications)│
          │ - OBO token mgmt│          └─────────────────────┘
          │ - Agent dispatch│                    │
          └────────┬────────┘                    │
                   │                             │
          ┌────────▼────────┐                    │
          │ Entrabot Agents  │ ← notifications ──┘
          │ (on devices)     │
          │ - Agent ID auth  │
          │ - Task execution │
          │ - MCP servers    │
          └─────────────────┘
```

### Pattern 1: Reactive Messaging (User → Agent)

1. User sends message in Teams
2. Teams SDK bot receives the activity
3. Bot identifies target agent (from conversation context or user selection)
4. Bot forwards request to Entrabot backend
5. Agent processes and responds
6. Bot sends response back to Teams

### Pattern 2: Proactive Messaging (Agent → User)

This is the more important pattern for Entrabot — agents need to notify humans.

```python
from botbuilder.core import CloudAdapter, ConversationReference

# Store conversation reference when user first interacts
conversation_references: dict[str, ConversationReference] = {}

@app.on("conversationUpdate")
async def on_install(context, state):
    """Capture conversation reference when bot is installed."""
    ref = context.activity.get_conversation_reference()
    user_id = context.activity.from_property.aad_object_id
    conversation_references[user_id] = ref
    # Persist to database for durability

# Later, when an agent needs to notify a user:
async def notify_user(adapter: CloudAdapter, user_aad_id: str, message: str):
    """Send a proactive message from an agent to a user."""
    ref = conversation_references.get(user_aad_id)
    if not ref:
        raise ValueError("User hasn't installed the bot yet")
    
    async def callback(turn_context):
        await turn_context.send_activity(message)
    
    await adapter.continue_conversation(
        ref,
        callback,
        bot_app_id="YOUR-BOT-APP-ID"
    )
```

**Key constraint:** Proactive messaging only works if the user has installed the bot app (or an admin has installed it for them). You can use Graph API to programmatically install the app for users.

### Pattern 3: MCP Bridge (Agent ↔ Teams via MCP)

With Teams SDK's MCP support:
1. Entrabot agent on device exposes an MCP server
2. Teams bot connects as an MCP client
3. User asks a question in Teams
4. Bot invokes MCP tools on the agent
5. Results flow back through Teams

---

## Declarative Agents vs Coded Agents

Two paradigms exist in the Microsoft 365 ecosystem:

### Declarative Agents
- **No/low code** — define behavior via configuration, instructions, and connectors
- Built in **Copilot Studio** or with the Agents Toolkit
- Leverage Microsoft's orchestrator and LLM
- Inherit M365 compliance and security automatically
- **Not suitable for Entrabot** — we need full control over orchestration, model choice, and external system integration

### Custom Engine (Coded) Agents
- **Full code** — you implement orchestration, model integration, and business logic
- Built with Teams SDK or M365 Agents SDK
- Choose your own LLM (OpenAI, Azure OpenAI, local models, etc.)
- Full control over data flow and external integrations
- **This is Entrabot's path** — custom engine agent with our own backend

### Decision Matrix

| Requirement | Declarative | Custom Engine |
|------------|-------------|---------------|
| Choose own LLM | ❌ | ✅ |
| Custom orchestration | ❌ | ✅ |
| External system integration | Limited | ✅ |
| Proactive messaging | Limited | ✅ |
| Agent-to-agent comms | ❌ | ✅ (via MCP/A2A) |
| Rapid prototyping | ✅ | ❌ |
| M365 compliance built-in | ✅ | Manual |

---

## Community Learnings & Gotchas

### Developer Pain Points (from Reddit, SO, GitHub Issues)

1. **SDK Confusion**: The biggest complaint is which SDK to use. Microsoft has 3+ overlapping SDKs (Teams SDK, M365 Agents SDK, Azure AI Foundry Agent SDK) with unclear boundaries. The Voitanos blog post captures this frustration well — even MVPs struggle to advise developers.

2. **Steep Learning Curve**: Non-Microsoft-ecosystem developers find the manifest schema, Entra ID app registration, Azure Bot Service setup, and token flows overwhelming. Each layer adds complexity.

3. **Sideloading Fragility**: The sideloading experience is buggy, especially in the Teams web client. Nondescript "Something Went Wrong" errors are common. Fixes include:
   - Try desktop client instead of web
   - Clear Teams cache
   - Re-upload the ZIP
   - Check that admin policies allow custom apps

4. **Documentation Churn**: Docs frequently change as SDKs are renamed/deprecated. TeamsFx docs still appear in search results but point to deprecated patterns. The rename from "Teams AI Library" to "Teams SDK" happened Nov 2025.

5. **Python Support is Preview**: The Python SDK is functional but still in developer preview. APIs may change. TypeScript and C# have more mature support and more samples.

6. **Proactive Messaging Complexity**: Storing and managing `ConversationReference` objects requires infrastructure (database). References can go stale if the Teams app is uninstalled/reinstalled. The `serviceUrl` in the reference can change.

7. **Dev Tunnel Flakiness**: The Toolkit uses dev tunnels for local debugging, which can be unreliable. Some developers prefer ngrok.

### What Works Well

- **Scaffolding**: The Toolkit/CLI gets you from zero to a running bot in under 5 minutes
- **Manifest management**: Toolkit handles environment variable substitution in manifests
- **SSO integration**: Once configured, SSO + OBO flow works smoothly
- **Adaptive Cards**: Rich UI rendering in Teams is well-supported
- **Hot reload**: Dev experience with the Toolkit's F5 debugging is good

---

## Open Questions for Entrabot

1. **Agent Identity Mapping**: How do we map Entrabot Agent IDs to the Teams bot's single app registration? Options:
   - Agent ID as metadata in the conversation state
   - Multiple bot registrations (one per agent — probably overkill)
   - Agent ID as a "sub-account" within our bot

2. **Proactive Messaging Scale**: If 1000 agents need to notify users simultaneously, what's the throttling behavior of Azure Bot Service / Teams?

3. **Python SDK Stability**: Is the `teams-ai` Python package stable enough for production? Or should we use TypeScript for the Teams layer and call our Python backend via API?

4. **MCP Integration Path**: Can Entrabot device agents expose MCP servers that the Teams bot connects to? What about NAT traversal for on-premise devices?

5. **Multi-Tenant Deployment**: If Entrabot serves multiple M365 tenants, do we need separate bot registrations per tenant or can a single multi-tenant app registration work?

6. **Admin Consent**: For enterprise deployment, how do we handle admin consent for our bot app? Can we avoid per-user consent via organization-wide app installation?

7. **Message Format**: Should agent→human notifications use plain text, Adaptive Cards, or both? Adaptive Cards allow structured data + action buttons but add complexity.

8. **Conversation Continuity**: When an agent sends a proactive message and the user replies, how do we maintain context across the reactive/proactive boundary?

---

## Sources

| Source | Notes |
|--------|-------|
| [M365 Agents Toolkit Overview — Microsoft Learn](https://learn.microsoft.com/en-us/microsoftteams/platform/toolkit/agents-toolkit-fundamentals) | Official overview, rebranding from Teams Toolkit |
| [Teams SDK Welcome Page](https://microsoft.github.io/teams-sdk/welcome/) | New unified SDK docs, CLI commands, language support |
| [Teams SDK Python Guides](https://microsoft.github.io/teams-sdk/python/in-depth-guides/) | Python-specific in-depth guides (preview) |
| [Teams SDK GitHub Repo](https://github.com/microsoft/teams-sdk) | Source code, samples, issues |
| [TeamsFx SDK Docs — Microsoft Learn](https://learn.microsoft.com/en-us/microsoftteams/platform/toolkit/teamsfx-sdk) | Deprecated SDK reference (still useful for understanding auth patterns) |
| [Voitanos: Teams SDK Evolution 2025](https://www.voitanos.io/blog/microsoft-teams-sdk-evolution-2025/) | Best independent analysis of the SDK confusion landscape |
| [Announcing Teams SDK (formerly Teams AI Library)](https://devblogs.microsoft.com/microsoft365dev/announcing-the-updated-teams-ai-library-and-mcp-support/) | Official announcement with MCP support details |
| [Teams Manifest Schema](https://learn.microsoft.com/en-us/microsoftteams/platform/teams-sdk/teams/manifest) | Manifest structure reference |
| [SSO Setup — Teams SDK](https://microsoft.github.io/teams-sdk/teams/user-authentication/sso-setup/) | SSO configuration guide |
| [OBO Flow — Microsoft Identity Platform](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow) | Official OBO flow documentation |
| [Proactive Messages — Microsoft Learn](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages) | Proactive messaging patterns and requirements |
| [Proactive Messaging — Teams SDK](https://microsoft.github.io/teams-sdk/typescript/essentials/sending-messages/proactive-messaging/) | New SDK proactive messaging guide |
| [Declarative Agent Tool Comparison — Microsoft Learn](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/declarative-agent-tool-comparison) | When to use declarative vs coded agents |
| [Custom Engine Agent Architecture](https://team400.ai/blog/2026-04-microsoft-365-copilot-custom-engine-agent-architecture) | Deep dive on custom engine agent patterns |
| [teams-ai on PyPI](https://pypi.org/project/teams-ai/) | Python SDK package |
| [Upload Custom Apps — Microsoft Learn](https://learn.microsoft.com/en-us/microsoftteams/platform/concepts/deploy-and-publish/apps-upload) | Sideloading documentation |
| [Microsoft AI SDKs Decoded](https://www.candede.com/articles/microsoft-ai-sdks-decoded) | From Bot Framework to MAF — full history |
| [MCP Server — Teams SDK](https://microsoft.github.io/teams-sdk/typescript/in-depth-guides/ai/mcp/mcp-server/) | MCP server integration guide |
| [GitHub: microsoft/mcp](https://github.com/microsoft/mcp) | Microsoft's official MCP implementations |
| [SO: teams-toolkit tag](https://stackoverflow.com/questions/tagged/teams-toolkit?tab=Frequent) | Community Q&A on common issues |
| [GitHub Issue: Sideloading Bugs](https://github.com/OfficeDev/microsoft-365-agents-toolkit/issues/14688) | Known sideloading issues tracker |
