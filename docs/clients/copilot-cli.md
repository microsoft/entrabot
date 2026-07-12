# GitHub Copilot CLI

## Connect Entrabot

Setup writes the same `entrabot` stdio server entry into `$COPILOT_HOME/mcp-config.json`, which defaults to `~/.copilot/mcp-config.json` when `COPILOT_HOME` is unset. Copilot CLI runs the identical `entrabot-mcp` binary used by every other host — there is no Copilot-specific build or code path. See [Quickstart](../getting-started/quickstart.md) for setup and [Configuration Reference](../guides/configuration.md) for the full list of environment variables setup writes.

## Incoming messages and sponsor replies

Copilot CLI does not receive Entrabot's `notifications/claude/channel` push as a new model turn — it has no equivalent mechanism for a server to inject content into the conversation out of band. Background polling still runs; it just doesn't reach the model directly.

For `send_teams_message`, server-side host detection (the connected client's `clientInfo.name`) auto-blocks the call after sending until a verified sponsor reply arrives in the same watched chat, then returns it inline as `sponsor_reply`, including the originating `chat_id`. Use that `chat_id` for any follow-up `send_teams_message` call — no separate wait call is needed, and the loop continues automatically each time you send and the sponsor replies.

Lack of channel push changes only how a message reaches the model turn; the underlying Teams poll (5s) and, in Agent User mode, the email poll (60s) keep running exactly as they do on any other host. See [Clients Overview](overview.md) for how host detection decides between channel-push and auto-wait behavior.

## Optional persona-sati

If a persona-sati MCP server is configured, call `bootstrap_session()` before answering the user's first substantive question or making external tool calls — Copilot CLI does not reliably inject FastMCP's `instructions` field into the model's system prompt either, so the same bootstrap requirement applies here. See [Persona-Sati Host Bootstrap](persona-sati-host-bootstrap.md) for the full protocol.
