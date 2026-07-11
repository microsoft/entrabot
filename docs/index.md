# Entrabot Identity Research

**Source:** <https://github.com/microsoft/entrabot> · **License:** MIT

Entrabot is a Python MCP server that gives a device-local agent its own Entra **Agent ID** and **Agent User**. The agent signs in autonomously, sends and receives Teams messages from its own account, uses its mailbox and Microsoft 365 files, and writes audit events against its own object ID. It runs on macOS, Linux, and Windows and works with Claude Code, Copilot CLI, or any MCP-speaking client.

**All you need to get started is:**

- A Microsoft 365 development tenant where you can create app registrations and grant admin consent
- A license that includes Teams and Outlook (E3 or E5 dev tenant licenses work)
- Python 3.12 installed locally

The scripts take care of the rest: provisioning the Agent Identity Blueprint, Agent Identity, and Agent User in Entra; uploading a self-signed certificate; assigning the license; and configuring the local MCP server.

**Microsoft Entra Agent ID** and **Microsoft Agent 365** — which enable these experiences — went GA on 2026-05-01. Entrabot is the reference implementation that pulls those primitives together on a real device, today.

## Where to Start

- **New to the project?** Start with the [Quickstart](getting-started/quickstart.md)
- **Current status / what's shipped / what's next?** [Engineering Status](engineering-status.md)
- **Customizing the agent's prompt?** Read [Customizing the body prompt](guides/customizing-the-body-prompt.md) — the `prompts/agent_system.md` body + `prompts/anatomy/*.md` modules
- **Mind-body split (what's where)?** See [DESIGN: persona-sati integration](architecture/DESIGN-persona-sati-integration.md)
- **Local vs. cloud storage?** Read [Storage configuration](guides/storage-configuration.md)
- **MCP tool reference?** See [MCP tools](reference/api/mcp-tools.md)
- **`setup.sh` flags?** See [setup reference](reference/scripts/setup.md)
- **Script + API reference?** Browse [Reference: Scripts](reference/scripts/operations.md) and [Reference: API](reference/api/mcp-tools.md)
- **Understanding the design?** Read [System Overview](architecture/system-overview.md)
- **Why was Bot Gateway mode removed?** Read [ADR-006: Remove the Teams Bot Gateway Auth Mode](decisions/006-remove-bot-gateway-mode.md)
- **Delegated mode / multi-tenant chat?** Read [Lightweight Teams Chat](architecture/NEXT-WhatsApp-lightweight-teams-chat.md) (landed)
- **Cloud memory work?** See [ADR-005: Cloud-Hosted Memory](decisions/005-cloud-hosted-memory.md) (Phases 1, 2, 5, 6a shipped)
- **How tokens flow?** See [Token Flows](reference/token-flows.md)
- **Debugging?** Check [Hard-Won Learnings](runbooks/hard-won-learnings.md)
- **Why we made a decision?** Browse [Architecture Decision Records](decisions/README.md)
- **Agent User deep dive?** See [Platform Learnings: Agent Users](platform-learnings/entra-agent-users.md)
- **Platform constraints (post-GA Agent Blueprints / Users)?** Read [Agent ID Blueprints and Users](platform-learnings/agent-id-blueprints-and-users.md) — required reading before any OAuth or Agent Identity work.
- **OS-level agent sandboxing (Build 2026)?** Read [Microsoft Execution Containers (MXC)](platform-learnings/mxc-windows-sandbox.md) — platform research for the open MXC integration; it is not shipped on `main`.
- **Security migration history?** See [Provisioner credential migration record](SECURITY-DEBT-PROVISIONER-SECRET.md) (resolved in code)

## Open Research Questions

- What M365 license tier is optimal for Agent Users? (E3 vs E5 vs Teams Enterprise)
- How do you track agent actions across OSes with a universal audit store?
- Conditional Access for Agent Identities — how does device-local enforcement work without a Layer 4 anchor?
- Will Entra add a `registration_endpoint` (RFC 7591) so MCP servers can stop maintaining a two-app-registration workaround? Tracked in `docs/platform-learnings/agent-id-blueprints-and-users.md`.
- What's the right ceiling on per-tenant Agent User count before directory-quota pressure forces a federation model?
