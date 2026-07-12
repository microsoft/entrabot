# Clients Overview

Entrabot is a stdio [MCP](https://modelcontextprotocol.io/) server. Any MCP client that can launch a local stdio server can use it — the client just needs to spawn the `entrabot-mcp` process and speak the MCP protocol over stdin/stdout.

The product names below identify the hosts Entrabot has been tested against and documents behavior for. They are not endorsements, rankings, or claims about relative quality — they describe how each host currently handles Entrabot's two delivery mechanisms: the `notifications/claude/channel` push extension and the `send_teams_message` auto-wait fallback.

## Supported hosts

| Host | Channel push | Sponsor-reply delivery | Page |
|---|---|---|---|
| Claude Code | Yes (`notifications/claude/channel`) | `send_teams_message` returns immediately after sending; the sponsor's reply arrives as a next-turn system reminder | [Claude Code](claude-code.md) |
| GitHub Copilot CLI | No | `send_teams_message` auto-blocks after sending and returns the sponsor's reply inline as `sponsor_reply` | [GitHub Copilot CLI](copilot-cli.md) |
| Other MCP hosts | Not recognized by default | Unrecognized `clientInfo.name` values default to the safer auto-block behavior — sends return `sponsor_reply` | [Other MCP Hosts](other-hosts.md) |

## Sponsor DM wait pattern

When a sponsor says something like "ping me when this is done" or "let me know when you're back," the pattern is the same everywhere:

1. Confirm the request in Teams with `send_teams_message`.
2. Do the work.
3. Send the completion update, also with `send_teams_message`.

What happens after step 3 depends on the host:

- **Claude Code** — end the turn after sending. The background poll delivers the sponsor's reply as a next-turn `notifications/claude/channel` push. Do not call `wait_for_sponsor_dm` here; it blocks the session and freezes the conversation.
- **Non-channel-push hosts** (Copilot CLI and others) — `send_teams_message` auto-blocks after sending and returns the sponsor's reply inline as `sponsor_reply`. No extra wait call is needed.
- **`wait_for_sponsor_dm`** is reserved for the rare case where the operator explicitly asks the agent to block until a reply arrives, independent of a send. Never poll it in a loop.

This behavior is determined server-side by host detection (the connected client's `clientInfo.name`), not by a parameter the model can set. See [Security Boundaries](../architecture/security-boundaries.md) for why behavioral switches like this are not exposed as tool parameters.

## Setup

1. **Quickstart** — provision the identity chain and write local configuration. See [Quickstart](../getting-started/quickstart.md).
2. **Client configuration** — the setup script registers Entrabot in `.mcp.json` (Claude Code and other project-local MCP hosts) and in the equivalent user-level config for your client (for example, `~/.copilot/mcp-config.json` for Copilot CLI). See your host's page above for the exact path.
3. **Optional persona bootstrap** — if a persona-sati MCP server is attached, call `bootstrap_session()` before your first substantive answer or external tool call. See [Persona-Sati Host Bootstrap](persona-sati-host-bootstrap.md).
