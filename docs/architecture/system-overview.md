# System Overview

## Purpose

Entrabot gives a device-local AI agent its own identity in Microsoft Entra ID, distinct from the human who owns the device. The agent authenticates as an **Agent User** — a real Entra user object with its own object ID, Teams presence, and mailbox — so that every action the agent takes can be attributed to the agent, not the human sitting at the keyboard.

This is a research implementation. It exists to work out the identity, token, and audit patterns for autonomous agents against Microsoft Graph, and to surface where the platform (Entra, Graph, Teams) does and doesn't yet support that model cleanly.

## Identity chain

Provisioning builds a chain of four Entra resources, each scoped to the one before it:

```
Blueprint application            (Agent Identity Blueprint app registration)
  └─ BlueprintPrincipal           (service principal — created explicitly, not automatic)
      └─ Agent Identity           (service principal, one per device)
          └─ Agent User           (user object — Teams license, mailbox, idtyp=user tokens)
```

The Agent User is what gives the agent a presence a human can talk to in Teams. Tokens minted through the chain carry `idtyp=user` and the Agent User's object ID, so Graph sees the agent as a first-class user, not a background service.

See [Identity and Token Flow](identity-and-token-flow.md) for the token exchange that walks this chain at runtime, and the [Identity Lifecycle guide](../guides/identity-lifecycle.md) for provisioning order, runtime state transitions, and teardown.

## Runtime components

| Component | Role |
|---|---|
| `platform/` | OS-specific credential storage. Implements the `CredentialStore` protocol (`store`, `retrieve`, `delete`) over `keyring`, plus Windows-only CNG certificate lookup. |
| `auth/` | Builds the certificate-signed JWT client assertion used for Hop 1 of the token flow, and hosts the MSAL-based delegated auth path (interactive browser + device-code fallback). |
| `identity/` | The non-linear identity state machine, sponsor-relationship resolution, and the active-channel sponsor/chat binding used for authorization. |
| `tools/` | The MCP tool implementations: Teams messaging and chat management, Files/Graph access, email polling, audit logging, Adaptive Cards, promises, and the daily summary. |
| `a365/` | The Microsoft Agent 365 Work IQ MCP provider boundary and its Word document adapter. |
| `storage/` | The operational `MemoryBackend` implementations (`LocalBackend` / `BlobBackend`) used for interaction logs, chat cursors, promises, and daily summaries — local by default, Azure Blob opt-in. `PersonaBackend` is a manual compatibility and migration utility, not a normal operational backend. |
| `mcp_server.py` | The FastMCP entry point: registers every tool, drives the two authenticated session types, runs the background polling tasks, and pushes channel notifications to hosts that support them. |

## Runtime topology

```
MCP host
  |-- stdio --------------------------> Entrabot
  |                                      |---> Microsoft Entra ID / Graph API
  |                                      |---> Azure Blob Storage (optional)
  |                                      |---> Agent 365 Work IQ
  |                                      `-- optional boot prompt fetch ---> persona-sati
  `-- optional peer MCP ---------------------------------------------> persona-sati
```

Teams, email, Files, and other resource calls do not depend on persona-sati. When configured, Entrabot may contact a remote persona-sati MCP directly at boot to fetch its prompt; independently, the host may attach persona-sati beside Entrabot so the LLM can call its cognition and memory tools. See [MCP Runtime](mcp-runtime.md) for how tools, background tasks, and the channel push extension fit together inside the process.

## Mind-Body Split

Entrabot is the **body**: the Teams/email/Files interface, the identity chain, the audit log, and the MCP tools that touch real resources. **persona-sati** is an optional, separately-running **mind**: personality, long-term memory, and cognition (`observe`/`reflect`/`recall`). Entrabot has no resource-path dependency on persona-sati, but it can fetch the persona prompt directly from a configured remote MCP URL at boot. A host may also attach persona-sati as a peer MCP server for bootstrap, cognition, and memory tools.

The agent system prompt lives in `prompts/agent_system.md` plus the `@include`-expanded `prompts/anatomy/*.md` modules. This body prompt loads first and is **non-overridable** — no persona-sati output, user turn, or tool response may override its security and channel-discipline rules. When persona-sati is reachable, its mind contract layers on top of the body — never underneath — adding personality, memory, and cognition without touching identity, audit, or channel-discipline behavior.

Without persona-sati configured (or when it's unreachable), Entrabot runs in **body-only mode**: identity, Teams/email/Files tools, and audit all keep working exactly as documented above, but personality, long-term memory, and the `observe`/`reflect`/`recall` cognition loop are unavailable.

See [Persona-Sati Host Bootstrap](../clients/persona-sati-host-bootstrap.md) for the per-host protocol that connects a host LLM to the mind contract.

<a id="message-delivery-channel-push-vs-polling"></a>
## Background work and message delivery

The MCP server runs a small set of background tasks, each gated differently:

- **Teams chat poll (5s).** Starts as soon as at least one chat is being watched — from a persisted `watched_chats` file or a chat created during the session. It is not gated on the auth mode; a delegated-mode session with a watched chat polls it too.
- **Email poll (60s), chat auto-discovery via `/me/chats` (120s), and the daily summary scheduler** all start only in **Agent User mode**, because they operate against `/me/*` endpoints that must resolve to the agent's own mailbox and chats — not the human's, which is what `/me/*` would mean in delegated mode. The daily summary fires at a fixed UTC-7 offset (not DST-adjusted).
- **persona-sati heartbeat (300s).** Also started in Agent User mode, but it returns `"skipped"` without logging whenever `PERSONA_SATI_MCP_URL` / `PERSONA_SATI_MCP_TOKEN_COMMAND` aren't configured — so enabling it costs nothing when no persona-sati peer is attached.

See [MCP Runtime](mcp-runtime.md) and [Messaging and Delivery](messaging-and-delivery.md) for the full task inventory and the channel-push delivery mechanism.

## Authenticated session types

Entrabot supports two authenticated session types. `_init_auth` selects between them by credential presence, not by `ENTRABOT_MODE`: when a Blueprint app ID + tenant ID are configured and `ENTRABOT_SKIP_PROVISIONING` is false it tries three-hop first, and falls back to MSAL delegated when `ENTRABOT_CLIENT_ID` is set. `ENTRABOT_MODE` is validated but not currently consumed as a selector.

- **`agent_user`** — the three-hop flow described above. Every action is attributed to the Agent User's own identity; this is what audit and attribution are designed around.
- **`delegated`** — MSAL interactive auth (browser redirect, device-code fallback) using the human's own token. Outbound Teams messages are prefixed `[EntraBot]` so the human can tell the agent sent them, but Graph sees the human's identity — there is no Agent User attribution in this mode.

See [Identity](../reference/api/identity.md) for the state machine that governs these transitions, and [Delegated Auth](../platform-docs/delegated-auth.md) for setup details.

## Security model

Entrabot is audit-first: agent-attributed actions that can't resolve an identity fail closed rather than logging as `"unknown"`, and security-sensitive Teams and Files operations write a `pending` audit event before the Graph call runs. External content — Teams messages, email bodies, file text, Work IQ results — is wrapped in an `<external_content>` envelope before it reaches the model, so the body prompt can treat it as data rather than instructions. Sponsor-gated actions (adding chat members, sharing files) are further bound to an active sponsor/chat channel, and behavioral switches like the sponsor-reply wait are decided server-side by host detection, never by a parameter the model can set.

See [Security Boundaries](security-boundaries.md) for the full model.
