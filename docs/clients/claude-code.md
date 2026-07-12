# Claude Code

## Connect Entrabot

Setup writes a project-local `.mcp.json` that registers the `entrabot` stdio server. Launch Claude Code with the exact flag below:

```bash
claude --dangerously-load-development-channels server:entrabot
```

The flag is required even when `.mcp.json` already lists `entrabot` as an MCP server — it's not a substitute for registration, and registration alone isn't a substitute for it. Without the flag, Claude Code loads the tools normally but does not route Entrabot's `notifications/claude/channel` push into next-turn context, so inbound Teams messages and emails never reach the model as channel pushes. `server:entrabot` is the MCP server name from `.mcp.json`, not a package or publication identifier — the double dash matters, since a single dash is silently treated as prompt text.

## Incoming messages

The MCP server runs background polling independent of any client: a Teams chat poll every 5 seconds, and in Agent User mode, an email poll every 60 seconds. When either detects new inbound content, the server emits a channel notification. Claude Code surfaces it as a next-turn `<channel source="entrabot">` system reminder, including chat or message metadata (for example `chat_id`) so you can reply in the same conversation without an extra lookup.

## Sponsor replies

Send the confirmation or completion message with `send_teams_message`, then end the turn. The background poll delivers the sponsor's reply as the next channel push — you do not need to wait for it explicitly. Do not call `wait_for_sponsor_dm` as part of this pattern; reserve it for cases where the operator explicitly asks the agent to block until a reply arrives. See [Sponsor DM wait pattern](overview.md#sponsor-dm-wait-pattern) for the full decision tree.

## Optional persona-sati

If a persona-sati MCP server is configured, call `bootstrap_session()` before answering the user's first substantive question or making external tool calls. FastMCP's `instructions` field does not reliably reach the LLM system prompt in Claude Code, so the persona only reaches the model if the body calls for it explicitly. See [Persona-Sati Host Bootstrap](persona-sati-host-bootstrap.md) for the full protocol.

## Related

- [Configuration Reference](../guides/configuration.md)
- Troubleshooting Teams and email delivery — planned; not yet published.
