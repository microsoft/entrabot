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
- **Which client do I use?** See [Clients Overview](clients/overview.md)
- **Current status / what's shipped / what's next?** [Project Status](project/status.md)
- **Customizing the agent's prompt?** Read [Customizing the Body Prompt](guides/customizing-the-body-prompt.md)
- **How the system fits together?** Read [System Overview](architecture/system-overview.md)
- **Local vs. cloud storage?** Read [Storage Configuration and Migration](guides/storage-configuration.md)
- **MCP tool reference?** See [MCP Tools](reference/mcp-tools.md)
- **Script reference?** Browse [Scripts Overview](reference/scripts/index.md)
- **How tokens flow?** See [Identity and Token Flow](architecture/identity-and-token-flow.md)
- **Something not working?** Check [Troubleshooting](troubleshooting/index.md)
- **Platform deep dives (Agent IDs, Agent 365, Teams/Files Graph, OS keystores)?** Browse [Platform Docs](platform-docs/agent-id-blueprints-and-users.md)

## Supported Platforms and Clients

- **Operating systems:** macOS, Linux, Windows
- **MCP clients:** Claude Code, GitHub Copilot CLI, and any other MCP-speaking host — see [Clients Overview](clients/overview.md)
- **Microsoft 365 surfaces:** Teams chat, Outlook mail, OneDrive/SharePoint files (via Microsoft Agent 365 Work IQ)
