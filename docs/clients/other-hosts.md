# Other MCP Hosts

Entrabot can run under any MCP client that supports stdio tools — Codex, Cursor, and other hosts not documented individually can register the same `entrabot-mcp` binary in their MCP configuration and get the full Teams, email, Files, and identity toolset.

## Default behavior

An unrecognized `clientInfo.name` defaults to non-channel-push, auto-block behavior: `send_teams_message` blocks after sending until a verified sponsor reply arrives, then returns it inline as `sponsor_reply`. This is a deliberate fail-safe — assuming push support for a host that doesn't have it would silently drop inbound messages, while assuming auto-block for a host that does have push only costs an extra tool call. When in doubt, Entrabot chooses the safer option.

## Channel-push integration

Entrabot's background poll emits `notifications/claude/channel` push notifications unconditionally, regardless of the connected host — but only a small, explicitly recognized set of channel-push host names suppresses `send_teams_message`'s auto-wait. That set is a hardcoded allowlist in `mcp_server.py`, not an open-ended capability check.

If a host you use actually surfaces `notifications/claude/channel` (or a future equivalent extension) into the model's turn, it still needs a code change to be recognized: its `clientInfo.name` must be added to the recognized channel-push host set in `mcp_server.py` before `send_teams_message` will stop auto-waiting for it. Until that change ships, the host gets the safer auto-block behavior described above even if it happens to support push at the transport level.

## Optional persona-sati

The persona-sati bootstrap protocol is host-agnostic: any host that can call `bootstrap_session()` should do so before the first substantive answer or external tool call, for the same reason it applies to Claude Code and Copilot CLI — FastMCP's `instructions` field is not reliably surfaced into the LLM system prompt. See [Persona-Sati Host Bootstrap](persona-sati-host-bootstrap.md) for the full protocol.

## Related

- MCP Runtime reference — planned; not yet published.
