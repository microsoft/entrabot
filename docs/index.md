# Openclaw Identity Research

Openclaw brings cloud-style identity tracking to device-local agents. When an autonomous agent runs on your Mac, Linux, or Windows machine, it gets its own **Agent ID** and **Agent User** in Microsoft Entra — so audit logs always distinguish agent actions from human actions.

## Key Concepts

| Concept | What It Does |
|---------|-------------|
| **Agent Identity** | A service principal representing a specific agent instance, parented by a Blueprint |
| **Agent User** | A purpose-built Entra user account (1:1 with an Agent Identity) that can have a mailbox, Teams presence, and M365 license |
| **Three-Hop Flow** | Blueprint token → Agent Identity token (FIC) → Agent User token (`user_fic` grant) — fully autonomous, no human in the loop |
| **Platform Abstraction** | OS-specific credential storage (Keychain, Credential Manager, Secret Service) |
| **Digital Worker** | The agent as a first-class team member — mailbox, Teams, org chart, @mentionable |

## Where to Start

- **New to the project?** Start with the [Quickstart](getting-started/quickstart.md)
- **Customizing the agent's prompt?** Read [Customizing the body prompt](guides/customizing-the-body-prompt.md)
- **Local vs. cloud storage?** Read [Storage configuration](guides/storage-configuration.md)
- **MCP tool reference?** See [MCP tools](reference/mcp-tools.md)
- **`setup.sh` flags?** See [setup.sh reference](reference/setup-script.md)
- **Understanding the design?** Read [System Overview](architecture/system-overview.md)
- **Bot Gateway design?** Read [DESIGN: Teams Bot Gateway](architecture/DESIGN-teams-bot-gateway.md)
- **Delegated mode spec?** Read [Lightweight Teams Chat](architecture/NEXT-WhatsApp-lightweight-teams-chat.md)
- **Current status?** See [Engineering Status](engineering-status.md)
- **Cloud memory work?** See [ADR-005: Cloud-Hosted Memory](decisions/005-cloud-hosted-memory.md) (Phases 1, 2, 5, 6a shipped)
- **How tokens flow?** See [Token Flows](reference/token-flows.md)
- **Debugging?** Check [Hard-Won Learnings](runbooks/hard-won-learnings.md)
- **Why we made a decision?** Browse [Architecture Decision Records](decisions/README.md)
- **Agent User deep dive?** See [Platform Learnings: Agent Users](platform-learnings/entra-agent-users.md)

## Open Research Questions

- What M365 license tier is optimal for Agent Users? (E3 vs E5 vs Teams Enterprise)
- How do you track agent actions across OSes with a universal audit store?
- Conditional Access for Agent Identities — how does Layer 4 enforcement work device-locally?
