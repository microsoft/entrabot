# Teams Bot Framework

> **Last updated:** July 2025
> **Status:** Active research — Bot Framework SDK deprecated Dec 2025; successor is M365 Agents SDK

## Overview

The **Azure Bot Framework** is Microsoft's platform for building conversational bots that connect to channels like Microsoft Teams, Web Chat, Slack, and more. Bots are web applications that receive "activities" (messages, events) from the **Azure Bot Service** cloud relay and respond via the same channel.

### Relevance to Entraclaw

Entraclaw's scenario — autonomous agents on devices that get Agent IDs and use OBO flows — maps onto the Bot Framework model in an interesting but imperfect way:

- **Bot = Agent**: Each Entraclaw agent could register as a bot, getting a Microsoft App ID (Entra ID app registration) that serves as its identity.
- **Teams = Human Interface**: The bot can communicate with humans via Teams — sending status updates, receiving commands, presenting structured data via Adaptive Cards.
- **Proactive Messaging = Agent-Initiated Communication**: Agents need to push information to humans without waiting for a prompt. Bot Framework supports this via stored conversation references.
- **Public Endpoint Requirement = Challenge**: Bot Framework requires a publicly accessible HTTPS endpoint. A device-local agent would need tunneling (ngrok, Dev Tunnels) or a cloud relay.

### SDK Lifecycle Warning

> **⚠️ CRITICAL:** The Bot Framework SDK for Python (`botbuilder-python`) is **deprecated** as of January 2026 and will not receive support after **December 31, 2025**. Microsoft's recommended successor is the **Microsoft 365 Agents SDK** (`microsoft-agents-*` packages). New projects should use the Agents SDK. Existing bots should plan migration.

---

## Key APIs / Interfaces

### SDK Packages (Bot Framework — Legacy but still functional)

```
pip install botbuilder-core botbuilder-dialogs botbuilder-azure botbuilder-integration-aiohttp
```

| Package | Purpose |
|---------|---------|
| `botbuilder-core` | Core bot logic: `ActivityHandler`, `TurnContext`, `CardFactory` |
| `botbuilder-schema` | Activity schema definitions |
| `botbuilder-dialogs` | Multi-turn dialog management (waterfall, prompts) |
| `botbuilder-azure` | Azure storage for bot state |
| `botbuilder-ai` | LUIS / QnA Maker integration |
| `botbuilder-integration-aiohttp` | aiohttp adapter for hosting |

### SDK Packages (M365 Agents SDK — Successor)

```
pip install microsoft-agents-hosting-core microsoft-agents-activity \
  microsoft-agents-hosting-aiohttp microsoft-agents-hosting-teams \
  microsoft-agents-authentication-msal microsoft-agents-storage-blob
```

| Bot Framework Package | Agents SDK Replacement |
|----|-----|
| `botbuilder-core` | `microsoft-agents-hosting-core` |
| `botbuilder-schema` | `microsoft-agents-activity` |
| `botbuilder-azure` | `microsoft-agents-storage-blob`, `microsoft-agents-storage-cosmos` |
| `botbuilder-integration-aiohttp` | `microsoft-agents-hosting-aiohttp` |

### Core Classes & Patterns

#### ActivityHandler (Bot Framework)

The base class you subclass to handle incoming activities:

```python
from botbuilder.core import ActivityHandler, TurnContext

class MyBot(ActivityHandler):
    """Handles incoming activities from Bot Framework."""

    async def on_message_activity(self, turn_context: TurnContext):
        """Called when user sends a message."""
        user_text = turn_context.activity.text
        await turn_context.send_activity(f"You said: {user_text}")

    async def on_members_added_activity(self, members_added, turn_context: TurnContext):
        """Called when bot is installed or users join."""
        for member in members_added:
            if member.id != turn_context.activity.recipient.id:
                await turn_context.send_activity("Welcome! I am your Entraclaw agent.")

    async def on_conversation_update_activity(self, turn_context: TurnContext):
        """Called on conversation lifecycle events."""
        await super().on_conversation_update_activity(turn_context)

    async def on_teams_card_action_invoke(self, turn_context: TurnContext):
        """Called when user interacts with an Adaptive Card action."""
        pass
```

**Key `ActivityHandler` methods:**
- `on_message_activity(turn_context)` — User sent a message
- `on_members_added_activity(members_added, turn_context)` — New members (including bot install)
- `on_members_removed_activity(members_removed, turn_context)` — Members left
- `on_message_reaction_activity(turn_context)` — Reactions added/removed
- `on_event_activity(turn_context)` — Custom events
- `on_typing_activity(turn_context)` — User is typing

#### TurnContext

The context object for a single "turn" (request/response cycle):

```python
# Key properties and methods:
turn_context.activity          # The incoming Activity object
turn_context.activity.text     # Message text
turn_context.activity.from_property  # Who sent it (ChannelAccount)
turn_context.activity.conversation   # Conversation info

await turn_context.send_activity("text")           # Send a text response
await turn_context.send_activity(activity_obj)     # Send a rich Activity
await turn_context.update_activity(activity)       # Edit a previous message
await turn_context.delete_activity(activity_id)    # Delete a message

# Get conversation reference (needed for proactive messaging)
ref = TurnContext.get_conversation_reference(turn_context.activity)
```

#### Hosting with aiohttp

```python
from aiohttp import web
from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings
from botbuilder.integration.aiohttp import ConfigurationBotFrameworkAuthentication

SETTINGS = BotFrameworkAdapterSettings(
    app_id="<MicrosoftAppId>",
    app_password="<MicrosoftAppPassword>"
)
ADAPTER = BotFrameworkAdapter(SETTINGS)
BOT = MyBot()

async def messages(req: web.Request) -> web.Response:
    body = await req.json()
    activity = Activity().deserialize(body)
    auth_header = req.headers.get("Authorization", "")
    response = await ADAPTER.process_activity(activity, auth_header, BOT.on_turn)
    if response:
        return web.json_response(data=response.body, status=response.status)
    return web.Response(status=201)

app = web.Application()
app.router.add_post("/api/messages", messages)
web.run_app(app, host="0.0.0.0", port=3978)
```

### Activity Types

| Activity Type | Description |
|---------------|-------------|
| `message` | User sent text, attachment, or card |
| `conversationUpdate` | Members added/removed, conversation created |
| `messageReaction` | User reacted to a message |
| `typing` | User is typing |
| `invoke` | Card action, messaging extension, task module |
| `event` | Custom event from client |
| `installationUpdate` | Bot installed/uninstalled |

---

## Auth & Identity Model

### Bot Registration & Identity

When you create an **Azure Bot** resource, Azure creates a **Microsoft Entra ID (Azure AD) app registration**:

- **MicrosoftAppId** (GUID) — The bot's unique identity
- **MicrosoftAppPassword** (client secret) — Used by the bot to authenticate to Azure Bot Service

This Entra ID app registration **is** the bot's identity. It's analogous to an Entraclaw Agent ID — a unique identity tied to a specific application instance.

### Authentication Flow

```
┌──────────┐     ┌──────────────────┐     ┌──────────────┐
│  Teams   │────▶│ Azure Bot Service │────▶│  Bot Backend │
│  Client  │◀────│   (cloud relay)   │◀────│  (your app)  │
└──────────┘     └──────────────────┘     └──────────────┘
                         │
                   JWT token in
                 Authorization header
                   (verified by SDK)
```

1. **Service-to-service auth**: Bot Framework SDK validates incoming requests using JWT tokens. The bot authenticates outbound requests using its App ID + Password to get access tokens.
2. **Token validation is automatic**: The `BotFrameworkAdapter` handles all token validation — you don't write auth code for basic scenarios.

### User Authentication (OAuth / SSO)

If the bot needs to act **on behalf of a user** (e.g., access their calendar via Graph), a separate auth flow is needed:

1. **Separate Entra ID app for user auth** (best practice — don't reuse the bot's app registration)
2. **OAuth connection** configured in Azure Bot Service
3. **OAuthPrompt** dialog in bot code to trigger sign-in

### OBO (On-Behalf-Of) Flow in Teams

**This is directly relevant to Entraclaw's OBO pattern.**

Teams supports **Single Sign-On (SSO)** for bots:

1. User interacts with bot in Teams
2. Teams sends an SSO token (representing the user) to the bot
3. Bot exchanges this SSO token for a downstream access token using the **OBO flow** with Entra ID's `/token` endpoint
4. Bot calls downstream APIs (e.g., Microsoft Graph) as the user

```python
# Conceptual OBO token exchange (using MSAL)
from msal import ConfidentialClientApplication

app = ConfidentialClientApplication(
    client_id="<bot-app-id>",
    client_credential="<bot-app-secret>",
    authority="https://login.microsoftonline.com/<tenant-id>"
)

# Exchange the SSO token for a Graph token
result = app.acquire_token_on_behalf_of(
    user_assertion=sso_token_from_teams,
    scopes=["https://graph.microsoft.com/.default"]
)
access_token = result["access_token"]
```

### Identity Mapping to Entraclaw

| Bot Framework Concept | Entraclaw Equivalent | Notes |
|----|----|----|
| MicrosoftAppId (Entra ID app) | Agent ID | 1:1 mapping possible. Each agent device gets its own app registration. |
| MicrosoftAppPassword | Agent credential | Client secret or certificate |
| OBO token exchange | Agent acting on behalf of user | Bot receives SSO token, exchanges for Graph token |
| Bot user in Teams | Agent presence in Teams | Bot appears as a contact/app in Teams |

### Key Identity Question for Entraclaw

**Can multiple device agents share one Bot registration, or does each need its own?**

- Bot Framework assumes **one bot = one App ID = one endpoint URL**
- Multiple agent instances could share one registration if they share a backend, but each agent can't have its own independent endpoint under one registration
- For truly independent device agents, each would need its own Entra ID app registration — which could be automated via Graph API

---

## Proactive Messaging

Proactive messaging is **critical** for Entraclaw — agents need to push status updates, alerts, and results to humans without waiting for a prompt.

### How It Works

1. **Capture the conversation reference** when the bot first interacts with a user (on install, first message, etc.)
2. **Store the reference** persistently
3. **Later, use the reference** to send a message outside of a normal turn

### Requirements

- The bot must be **installed** for the target user/team/channel
- You need the `conversationId`, `serviceUrl`, and `tenantId`
- The bot's App ID and Password are required

### Python Example — Storing and Using Conversation References

```python
from botbuilder.core import ActivityHandler, TurnContext, CardFactory
from botbuilder.schema import (
    Activity, ActivityTypes, ConversationReference, ChannelAccount,
    ConversationParameters
)
from botframework.connector import ConnectorClient
from botframework.connector.auth import MicrosoftAppCredentials

# Global store (use a database in production)
CONVERSATION_REFERENCES: dict[str, ConversationReference] = {}


class ProactiveBot(ActivityHandler):
    async def on_conversation_update_activity(self, turn_context: TurnContext):
        self._add_conversation_reference(turn_context.activity)
        await super().on_conversation_update_activity(turn_context)

    async def on_message_activity(self, turn_context: TurnContext):
        self._add_conversation_reference(turn_context.activity)
        await turn_context.send_activity("Message received. I'll notify you proactively.")

    def _add_conversation_reference(self, activity: Activity):
        ref = TurnContext.get_conversation_reference(activity)
        CONVERSATION_REFERENCES[ref.user.id] = ref


# --- Called from an external trigger (API endpoint, timer, event) ---
async def send_proactive_message(user_id: str, message: str):
    ref = CONVERSATION_REFERENCES.get(user_id)
    if not ref:
        raise ValueError(f"No conversation reference for user {user_id}")

    credentials = MicrosoftAppCredentials(APP_ID, APP_PASSWORD)
    connector = ConnectorClient(credentials, base_url=ref.service_url)

    await connector.conversations.send_to_conversation(
        ref.conversation.id,
        Activity(
            type=ActivityTypes.message,
            text=message,
            channel_id=ref.channel_id,
            from_property=ref.bot,
            recipient=ref.user,
            conversation=ref.conversation,
        )
    )
```

### Alternative: Using `continue_conversation` (SDK method)

```python
async def notify_user(user_id: str, message: str):
    ref = CONVERSATION_REFERENCES[user_id]
    await ADAPTER.continue_conversation(
        ref,
        lambda turn_context: turn_context.send_activity(message),
        APP_ID,
    )
```

### Creating a New Conversation (No Prior Interaction)

If you need to message a user who hasn't interacted with the bot yet, you can **proactively install the app via Graph API** and then create a conversation:

```python
credentials = MicrosoftAppCredentials(APP_ID, APP_PASSWORD)
connector = ConnectorClient(credentials, base_url=SERVICE_URL)

conversation_params = ConversationParameters(
    members=[ChannelAccount(id=user_aad_object_id)],
    channel_data={"tenant": {"id": TENANT_ID}},
    is_group=False,
)
conversation = await connector.conversations.create_conversation(conversation_params)

await connector.conversations.send_to_conversation(
    conversation.id,
    Activity(type=ActivityTypes.message, text="Hello from your agent!")
)
```

### Service URLs by Environment

| Environment | Service URL |
|-------------|------------|
| Public | `https://smba.trafficmanager.net/teams/` |
| GCC | `https://smba.infra.gcc.teams.microsoft.com/teams` |
| GCC High | `https://smba.infra.gov.teams.microsoft.us/teams` |
| DoD | `https://smba.infra.dod.teams.microsoft.us/teams` |

### Proactive Messaging Gotchas

- **403 Forbidden with `MessageWritesBlocked`**: User has blocked or uninstalled the bot
- **The bot must be installed** in the user's personal scope or the target team to send proactive messages
- **`userId` is bot-specific**: A user's ID is unique per bot registration — you can't reuse IDs across bots
- **`aadObjectId` is universal**: Use this (Entra ID Object ID) for cross-bot user identification
- **Rate limiting**: Teams throttles bot messages. Batch carefully for org-wide notifications.

---

## Adaptive Cards

Adaptive Cards provide rich, structured UI in Teams messages — perfect for presenting agent status, task results, and interactive controls.

### Sending an Adaptive Card (Python)

```python
import json
from botbuilder.core import ActivityHandler, TurnContext, CardFactory
from botbuilder.schema import Activity, ActivityTypes


class AgentBot(ActivityHandler):
    async def on_message_activity(self, turn_context: TurnContext):
        card_json = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": [
                {
                    "type": "TextBlock",
                    "text": "🤖 Agent Status Report",
                    "size": "Large",
                    "weight": "Bolder",
                    "wrap": True
                },
                {
                    "type": "FactSet",
                    "facts": [
                        {"title": "Agent ID", "value": "agent-xyz-001"},
                        {"title": "Status", "value": "✅ Running"},
                        {"title": "Last Task", "value": "Data sync completed"},
                        {"title": "Uptime", "value": "4h 23m"}
                    ]
                }
            ],
            "actions": [
                {
                    "type": "Action.Submit",
                    "title": "🔄 Run Diagnostics",
                    "data": {"action": "run_diagnostics"}
                },
                {
                    "type": "Action.Submit",
                    "title": "⏹️ Stop Agent",
                    "data": {"action": "stop_agent"}
                }
            ]
        }

        card_attachment = CardFactory.adaptive_card(card_json)
        await turn_context.send_activity(
            Activity(
                type=ActivityTypes.message,
                attachments=[card_attachment]
            )
        )
```

### Handling Card Action Submissions

When a user clicks an `Action.Submit` button, the bot receives an invoke activity:

```python
async def on_message_activity(self, turn_context: TurnContext):
    if turn_context.activity.value:
        # This is a card action submission
        action_data = turn_context.activity.value
        action = action_data.get("action")
        if action == "run_diagnostics":
            await turn_context.send_activity("Running diagnostics...")
        elif action == "stop_agent":
            await turn_context.send_activity("Stopping agent...")
    else:
        # Regular text message
        await turn_context.send_activity(f"You said: {turn_context.activity.text}")
```

### Useful Libraries

- **[PyAdaptiveCards](https://pyadaptivecards.readthedocs.io/)** — Build cards programmatically instead of writing JSON
- **[teams-bot-ui](https://pypi.org/project/teams-bot-ui/)** — Pre-made card templates
- **[Adaptive Cards Designer](https://adaptivecards.io/designer/)** — Visual drag-and-drop card builder (export JSON)

### Teams-Specific Card Tips

- Always set `"wrap": true` on TextBlocks for full content visibility
- Teams supports Adaptive Cards up to version **1.5**
- Maximum card payload size: **28 KB**
- Use `Action.Execute` (Universal Actions) for cross-platform card actions

---

## Integration Patterns

### Architecture: Entraclaw Agent as a Teams Bot

#### Option A: Cloud-Hosted Bot Backend (Recommended)

```
┌─────────────────┐      ┌──────────────────┐      ┌──────────────────┐
│  Device Agent    │─────▶│  Cloud Bot API    │◀────▶│  Azure Bot Svc   │◀───▶ Teams
│  (on-device)     │ gRPC │  (Azure App Svc)  │ HTTP │  (cloud relay)   │
│                  │ MQTT │                    │      │                  │
└─────────────────┘      └──────────────────┘      └──────────────────┘
```

- Device agent communicates with a cloud-hosted bot backend via a private protocol (gRPC, MQTT, WebSocket)
- Cloud bot backend handles Bot Framework protocol (public HTTPS endpoint)
- Bot backend relays messages between Teams users and the device agent
- **Pro**: Clean separation, no tunneling needed
- **Con**: Requires cloud infrastructure, agent isn't truly self-contained

#### Option B: Device-Local Bot with Tunnel

```
┌─────────────────┐      ┌──────────────────┐      ┌──────────────────┐
│  Device Agent    │      │  ngrok / Dev      │◀────▶│  Azure Bot Svc   │◀───▶ Teams
│  + Bot Server    │◀────▶│  Tunnel           │      │  (cloud relay)   │
│  (localhost:3978)│      │                   │      │                  │
└─────────────────┘      └──────────────────┘      └──────────────────┘
```

- Bot server runs directly on the device alongside the agent
- ngrok or Microsoft Dev Tunnels expose the local endpoint publicly
- **Pro**: Fully self-contained agent, no cloud dependency for bot logic
- **Con**: Tunnel reliability, dynamic URLs (need reserved subdomain or re-registration), security exposure

#### Option C: Hybrid — Graph API for Notifications, No Bot

```
┌─────────────────┐      ┌──────────────────┐
│  Device Agent    │─────▶│  Microsoft Graph  │────▶ Teams Channel Post
│  (on-device)     │ HTTP │  API              │
└─────────────────┘      └──────────────────┘
```

- Agent uses Graph API directly to post messages to a Teams channel
- No Bot Framework needed, no public endpoint needed
- **Pro**: Simplest, no bot registration needed for basic notifications
- **Con**: No conversational interaction, messages posted as app not as bot, limited interactivity

### Public Endpoint Requirement

**Bot Framework requires a publicly accessible HTTPS endpoint** at `/api/messages`. This is the single biggest architectural constraint for device-local agents.

**Solutions:**
1. **ngrok** (development): `ngrok http 3978` → `https://abc123.ngrok-free.app/api/messages`
2. **Microsoft Dev Tunnels** (integrated with Teams Toolkit)
3. **Azure App Service** (production): Host bot in Azure
4. **Azure Functions** (serverless production alternative)
5. **Cloudflare Tunnels / Tailscale Funnel** (alternative tunneling)

---

## Bot Framework vs Graph API

| Capability | Bot Framework | Graph API |
|-----------|--------------|-----------|
| **Conversational bot in Teams** | ✅ Primary use case | ❌ Not supported |
| **Proactive messaging as bot** | ✅ Full support | ❌ Cannot send as bot |
| **Post to channel** | ✅ As bot identity | ✅ As user or app |
| **Rich cards (Adaptive Cards)** | ✅ Full support | ⚠️ Limited |
| **Receive user messages** | ✅ Real-time | ❌ No real-time receive |
| **Channel/team management** | ❌ | ✅ Full CRUD |
| **User management** | ❌ | ✅ Full CRUD |
| **Chat history access** | ❌ | ✅ Read messages |
| **File operations** | ❌ | ✅ OneDrive/SharePoint |
| **Calendar, mail access** | ❌ | ✅ Full access |
| **Requires public endpoint** | ✅ Yes | ❌ No |
| **Message identity** | Bot (app) identity | User or app identity |

### When to Use Which

- **Bot Framework**: Interactive agents that converse with humans, respond to commands, send proactive notifications with rich cards
- **Graph API**: Backend automation — provisioning teams/channels, accessing data, posting simple notifications
- **Both together**: Bot Framework for the conversational experience + Graph API for data access (user info, files, calendar) using OBO tokens

### Combining Both (Common Pattern)

```python
# Bot receives a message, uses OBO to call Graph for user data
async def on_message_activity(self, turn_context: TurnContext):
    if turn_context.activity.text == "my profile":
        # Get OBO token for Graph
        graph_token = await self._get_graph_token(turn_context)

        # Call Graph API
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {graph_token}"}
            async with session.get(
                "https://graph.microsoft.com/v1.0/me",
                headers=headers
            ) as resp:
                profile = await resp.json()

        await turn_context.send_activity(
            f"Name: {profile['displayName']}\nEmail: {profile['mail']}"
        )
```

---

## Ecosystem Landscape (Mid-2025)

The Microsoft bot/agent ecosystem is in transition. Understanding the relationships is critical for making the right technology choice.

### SDK / Tool Lineage

```
Bot Framework SDK v4 (2018-2025)
  ├── botbuilder-python ──────▶ DEPRECATED Dec 2025
  ├── botbuilder-js ──────────▶ DEPRECATED Dec 2025
  └── botbuilder-dotnet ──────▶ DEPRECATED Dec 2025

Microsoft 365 Agents SDK (2025+)     ◀── SUCCESSOR
  ├── microsoft-agents-*-python
  ├── microsoft-agents-*-js
  └── microsoft-agents-*-dotnet

Teams SDK (formerly Teams AI v2)     ◀── Teams-specific
  └── For Teams-only bots

Bot Framework Composer ──────────────▶ ARCHIVED (read-only)
Power Virtual Agents ────────────────▶ REBRANDED → Copilot Studio
Microsoft Copilot Studio ────────────▶ Low-code/no-code successor
```

### Decision Matrix: Which Tool for Entraclaw?

| Scenario | Recommended Tool |
|----------|-----------------|
| New multi-channel agent bot | M365 Agents SDK |
| Teams-only bot | Teams SDK |
| No-code bot for business users | Copilot Studio |
| Device agent → Teams notifications | Graph API (simple) or Bot Framework (interactive) |
| Full autonomous agent with Teams UI | M365 Agents SDK + Graph API |

---

## Community Learnings & Gotchas

### Common Issues (from Reddit, SO, GitHub)

1. **"Works locally, fails in Teams"**
   - Most common issue. Usually caused by mismatched manifest, wrong endpoint URL, or missing App ID/Password in production config.
   - **Fix**: Verify Azure Bot Service endpoint matches your deployed URL exactly. Check Teams app manifest `botId` matches your `MicrosoftAppId`.

2. **Azure subscription required for bot registration**
   - M365 Developer accounts alone are insufficient — you need an active Azure subscription linked to the same tenant.
   - Many developers get stuck here following Microsoft docs.

3. **Permission errors: `botAadApp/create` or `botFramework/create`**
   - Occur during Teams Toolkit provisioning. Usually a tenant permissions issue.
   - **Fix**: Ensure you have Contributor access to the Azure subscription and the tenant allows app registrations.

4. **"App ID already registered to another bot"**
   - Each App ID can only be associated with one Azure Bot resource.
   - **Fix**: Delete the old bot resource or create a new App ID.

5. **Proactive messaging — `ConversationReference` lost**
   - Conversation references must be persisted across bot restarts. In-memory storage loses them.
   - **Fix**: Store references in a database (Cosmos DB, Azure Table Storage, even SQLite).

6. **SSO token exchange fails silently**
   - Common when `connectionName` in manifest doesn't match Azure Bot Service OAuth connection.
   - OBO flow requires admin consent for certain Graph scopes.
   - **Fix**: Triple-check `connectionName`, ensure consent is granted, verify Entra ID app permissions.

7. **Rate limiting on proactive messages**
   - Teams throttles bot messages, especially when sending to many users at once.
   - **Fix**: Implement backoff/retry, batch sends, use Graph API for org-wide installations first.

8. **Python SDK documentation is sparse**
   - Most examples and docs focus on C#/JS. Python samples exist but are less maintained.
   - **Key resource**: `github.com/microsoft/BotBuilder-Samples/tree/main/samples/python`

### Development Experience Tips

- **Use Bot Framework Emulator** for local testing without Teams
- **Use Dev Tunnels** (built into Teams Toolkit) instead of ngrok for easier setup
- **Always set `"wrap": true`** on Adaptive Card TextBlocks
- **Store conversation references persistently** — this is the #1 proactive messaging pitfall
- **Use separate Entra ID apps** for bot identity vs. user auth (best practice, prevents cert rotation issues)

---

## Open Questions

### For the Entraclaw Scenario

1. **Agent-per-device vs. shared bot registration?**
   - Can multiple device agents share one Bot registration with a single cloud endpoint that fans out? Or does each device need its own registration?
   - **Likely answer**: Shared registration with a cloud relay that routes to specific devices. One-bot-per-device would require O(n) Entra ID app registrations.

2. **OBO flow compatibility with Agent IDs?**
   - If an Entraclaw agent has its own Agent ID (Entra ID app), can it use OBO to act on behalf of the user who "owns" it?
   - **Likely answer**: Yes — this maps directly to the Teams SSO + OBO pattern. The agent's App ID is the "bot," and it exchanges the user's SSO token for downstream access.

3. **Can a device agent maintain a stable Teams presence without a public endpoint?**
   - Option: Use a cloud relay / message queue (Azure Service Bus, Event Grid) where the device agent polls for incoming messages rather than exposing an HTTP endpoint.
   - This breaks the standard Bot Framework model but could work with custom plumbing.

4. **What happens to the bot identity when a device goes offline?**
   - Bot Framework has no concept of "online/offline" for bots. If the endpoint is unreachable, messages fail silently (or with errors).
   - Entraclaw may need a "presence" protocol on top of Bot Framework.

5. **M365 Agents SDK readiness for Python?**
   - The Python SDK for M365 Agents is new (2025). How production-ready is it?
   - GitHub: `github.com/microsoft/Agents-for-python`

6. **Multi-tenant vs. single-tenant bot?**
   - Entraclaw agents may operate across tenant boundaries. Bot Framework supports both, but multi-tenant requires additional configuration.

7. **Scalability of proactive messaging?**
   - If 10,000 device agents each need to send hourly updates, what are the Teams rate limits?
   - Teams has per-bot, per-conversation, and per-tenant rate limits. Need empirical testing.

8. **Microsoft Agent Framework (MAF) integration?**
   - MAF provides orchestration patterns (single agent, handoff, reflection, multi-agent). How does this layer on top of the Bot Framework / Agents SDK for device agent scenarios?

---

## Sources

### Official Documentation
- [Bot Framework SDK overview](https://learn.microsoft.com/en-us/azure/bot-service/bot-service-overview?view=azure-bot-service-4.0) — What the SDK is, capabilities
- [Bot Framework SDK docs](https://learn.microsoft.com/en-us/azure/bot-service/index-bf-sdk?view=azure-bot-service-4.0) — Full documentation hub
- [Bot Framework authentication basics](https://learn.microsoft.com/en-us/azure/bot-service/bot-builder-authentication-basics?view=azure-bot-service-4.0) — Auth model, App ID/Password, OBO
- [Authentication types](https://learn.microsoft.com/en-us/azure/bot-service/bot-builder-concept-authentication-types?view=azure-bot-service-4.0) — Service vs. user auth
- [Guide to IDs in Bot Framework](https://learn.microsoft.com/en-us/azure/bot-service/bot-service-resources-identifiers-guide?view=azure-bot-service-4.0) — All the different IDs explained
- [Teams bots overview](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/overview) — How bots work in Teams
- [Send proactive messages](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages) — Full proactive messaging guide
- [Enable SSO in Teams bots](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/authentication/bot-sso-overview) — SSO + OBO flow
- [OBO flow (Entra ID)](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow) — Protocol-level OBO documentation

### Migration & Successor
- [M365 Agents SDK Python migration guide](https://learn.microsoft.com/en-us/microsoft-365/agents-sdk/bf-migration-python) — How to migrate from Bot Framework
- [M365 Agents SDK migration guidance](https://learn.microsoft.com/en-us/microsoft-365/agents-sdk/bf-migration-guidance) — General migration strategy
- [M365 Agents SDK Python reference](https://learn.microsoft.com/en-us/python/api/agent-sdk-python/agents-overview?view=agent-sdk-python-latest) — API reference
- [Introducing the M365 Agents SDK](https://devblogs.microsoft.com/microsoft365dev/introducing-the-microsoft-365-agents-sdk/) — Announcement blog
- [Agents-for-python (GitHub)](https://github.com/Microsoft/Agents-for-python) — SDK source and samples
- [Microsoft AI SDKs Decoded](https://www.candede.com/articles/microsoft-ai-sdks-decoded) — Excellent overview of SDK landscape

### Samples & Code
- [BotBuilder-Samples Python](https://github.com/microsoft/BotBuilder-Samples/tree/main/samples/python) — Official Python samples (archived)
- [16.proactive-messages sample](https://github.com/microsoft/BotBuilder-Samples/blob/main/samples/python/16.proactive-messages/app.py) — Proactive messaging reference
- [07.using-adaptive-cards sample](https://github.com/microsoft/BotBuilder-Samples/blob/main/samples/python/07.using-adaptive-cards/bots/adaptive_cards_bot.py) — Adaptive Cards reference
- [Teams Samples - Proactive Messaging](https://github.com/OfficeDev/Microsoft-Teams-Samples/tree/main/samples/bot-proactive-messaging-teamsfx/python) — Teams-specific proactive sample

### API References
- [ActivityHandler class (Python)](https://learn.microsoft.com/en-us/python/api/botbuilder-core/botbuilder.core.activityhandler?view=botbuilder-py-latest) — Core bot class
- [TurnContext class (Python)](https://learn.microsoft.com/en-us/python/api/botbuilder-core/botbuilder.core.turncontext?view=botbuilder-py-latest) — Turn context API

### Community & Analysis
- [Teams SDK evolution (Fall 2025)](https://www.voitanos.io/blog/microsoft-teams-sdk-evolution-2025/) — Excellent analysis of SDK landscape changes
- [End of Bot Framework SDK?](https://devblogs.dewiride.com/ai/microsoft-365-agents-sdk/end-of-microsoft-bot-framework-sdk) — Deprecation analysis
- [Bot Framework and Graph API (MS Q&A)](https://learn.microsoft.com/en-us/answers/questions/785959/bot-framework-and-graph-api) — When to use which
- [Graph API vs Bot Framework for messaging (MS Q&A)](https://learn.microsoft.com/en-us/answers/questions/1824858/graph-api-vs-bot-framework-api-for-sending-message) — Comparison
- [Teams Bot not working when deployed](https://techcommunity.microsoft.com/discussions/teamsdeveloper/teams-bot-not-working-when-deployed/4287129) — Common deployment issues
- [SO: Teams bot approach](https://stackoverflow.com/questions/73701719/teams-bot-which-approach-to-go-for) — Bot Framework vs Graph discussion
- [SO: Proactive messaging Python](https://stackoverflow.com/questions/62547610/microsoft-teams-python-botbuilder-proactive-messaging) — Common proactive messaging issues
- [SO: Is Bot Framework Composer dead?](https://stackoverflow.com/questions/77723153/is-bot-framework-composer-project-dead) — Composer deprecation confirmation

### Tools
- [Bot Framework Emulator](https://github.com/microsoft/BotFramework-Emulator) — Local testing tool
- [Adaptive Cards Designer](https://adaptivecards.io/designer/) — Visual card builder
- [PyAdaptiveCards](https://pyadaptivecards.readthedocs.io/) — Python library for building cards
- [teams-bot-ui (PyPI)](https://pypi.org/project/teams-bot-ui/) — Pre-made card templates for Teams bots
