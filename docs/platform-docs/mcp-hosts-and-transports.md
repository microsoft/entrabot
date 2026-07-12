# MCP Hosts and Transports

Entrabot is a local [MCP](https://modelcontextprotocol.io/) server built on
FastMCP. It runs on the same machine as the host that launches it, exposes its
capabilities as tools, and speaks the MCP protocol over standard input and
output. This page is the platform reference for how hosts launch Entrabot, how
requests and responses flow, and how inbound Teams and email content reaches the
model despite the constraints of a local stdio server.

## Transport: local stdio child process

The host launches `entrabot-mcp` as a child process and communicates with it
over the process's stdin/stdout. There is no network listener, no HTTP endpoint,
and no shared broker — the transport is the pipe between the host and the
Entrabot process. Any MCP client that can spawn a local stdio server can use
Entrabot; the client only needs to start the process and speak MCP over the
pipe.

Every host-facing capability — Teams, email, Files, cards, promises, the daily
summary, and A365 Work IQ — is registered as an MCP tool and returns a JSON
string, so every tool has the same wire shape regardless of what it does
internally. See [MCP Runtime](../architecture/mcp-runtime.md) for the server's
boot and tool-dispatch lifecycle.

## Request/response and the idle-wake gap

MCP is request/response: the host's model drives every interaction by calling a
tool and reading its result. This is a good fit for actions the model
initiates — send a Teams message, read email, create a chat.

It does not, on its own, let the server wake an idle model. A local stdio MCP
server has no standard mechanism to inject a new conversational turn into a host
that is sitting idle. If the model does not call a tool, the server has no
standard channel to hand it new data. This matters for Entrabot because Teams
replies and inbound email arrive asynchronously, after the model has finished a
turn.

Entrabot bridges this gap in two ways, and which one applies depends on the
host.

## Channel push: `notifications/claude/channel`

Entrabot declares an experimental `claude/channel` capability during MCP
initialization. When a host understands this capability — Claude Code does — it
registers a handler for `notifications/claude/channel`. The background poll then
emits that notification whenever new inbound Teams or email content is detected,
and the host surfaces it to the model as a next-turn system reminder, including
metadata such as the originating `chat_id` so the model can reply in the same
conversation.

The notification is emitted unconditionally, regardless of which host is
connected. Hosts that do not register the `claude/channel` capability simply
ignore the notification, per the MCP specification — it reaches them on the
transport but has nowhere to go. This experimental capability is specific to
Entrabot and the hosts that opt into it; it is not a standard MCP primitive.

See [Claude Code](../clients/claude-code.md) for the host-side behavior and the
launch flag it requires.

## Non-push hosts: server-side auto-wait

Hosts that do not surface the channel push rely on a server-side fallback.
`send_teams_message` decides whether to block after sending by checking the
connected client's `clientInfo.name` against a small, hardcoded set of
recognized channel-push host names. For any host not in that set, the tool
auto-blocks after sending and waits for a verified sponsor reply across every
watched chat, then returns it inline as `sponsor_reply` (including the reply's
own `chat_id`).

This detection lives in the server, not in a tool parameter. There is no
argument the model can set to skip or force the wait; changing the behavior for a
new host is a server-side allowlist change. `wait_for_sponsor_dm` is a separate,
explicit tool for the same underlying wait, reserved for the rare case where the
operator asks the agent to block until a reply arrives outside of a send.

See [GitHub Copilot CLI](../clients/copilot-cli.md) and
[Other MCP Hosts](../clients/other-hosts.md) for per-host behavior, and
[Messaging and Delivery](../architecture/messaging-and-delivery.md) for the full
delivery mechanics.

## Host configuration: `.mcp.json`

Hosts discover Entrabot through their MCP configuration. A project-local
`.mcp.json` (or the host's equivalent user-level config) registers `entrabot` as
a stdio server:

```json
{
  "mcpServers": {
    "entrabot": {
      "type": "stdio",
      "command": ".venv/bin/entrabot-mcp",
      "args": []
    },
    "persona-sati": {
      "type": "sse",
      "url": "http://localhost:8100/sse"
    }
  }
}
```

Entrabot and the optional persona-sati mind server are **independent peers** in
this file. When persona-sati is listed, the host can call its tools directly
alongside Entrabot's; when it is absent, Entrabot runs standalone as a generic
Teams tool. See [Clients Overview](../clients/overview.md) for the per-host
configuration paths setup writes.

## Two side paths that are not the resource transport

Two Entrabot behaviors talk to other MCP servers directly, and neither is part
of the normal host-facing tool transport:

- **Boot-time persona prompt fetch.** When `PERSONA_SATI_MCP_URL` and
  `PERSONA_SATI_MCP_TOKEN_COMMAND` are set, Entrabot mints a token and opens its
  own SSE client session to the remote persona-sati server to fetch the system
  prompt once at boot. This is an outbound SSE client path configured by
  environment variables, separate from the stdio transport the host uses to call
  Entrabot's tools. Any failure falls back to the body prompt alone.
- **Efferent-copy observer dispatch.** When `EFFERENT_COPY_ENABLE=1` is set,
  Entrabot fires a fire-and-forget `observe(tool_name, args[, result])` call to
  any peer whose advertised schema matches. Discovery is schema-based, not
  name-based, and the mechanism is off by default. This is an optional
  side-channel, not a resource path a host depends on.

See [MCP Runtime](../architecture/mcp-runtime.md) for both mechanisms in detail.

## Microsoft Graph and the webhook constraint

Inbound Teams and email content reaches Entrabot by **polling** Microsoft Graph:
a Teams chat poll every 5 seconds and, in Agent User mode, an email poll every 60
seconds. Polling is a deliberate choice forced by the transport.

Microsoft Graph can deliver change notifications through two platform
mechanisms, but neither fits a local stdio server as a runtime transport:

- **Change-notification subscriptions (webhooks)** require Graph to POST to a
  public HTTPS endpoint. A local stdio server has no such endpoint, so it cannot
  receive webhooks without standing up external inbound HTTPS infrastructure.
  Chat-message subscriptions also expire after at most 60 minutes and require
  renewal.
- **Delta queries** (`/chats/{id}/messages/delta`) return a cursor token that
  can be replayed to fetch only what changed, which is useful for efficient
  polling, but delta is a query pattern the server still initiates — not a push.

Entrabot therefore polls and filters client-side rather than subscribing. Graph
also throttles inconsistently and does not reliably support `$filter`/`$orderby`
for chat messages, so filtering and deduplication happen in Entrabot after
retrieval. See [Teams Graph API](teams-graph-api.md) for those query
constraints and [Messaging and Delivery](../architecture/messaging-and-delivery.md)
for the poll loop and cursor handling.

## See also

- [MCP Runtime](../architecture/mcp-runtime.md) — server boot, tool dispatch,
  and the background task matrix.
- [Messaging and Delivery](../architecture/messaging-and-delivery.md) — the
  background poll, channel push, and sponsor-wait behavior.
- [Clients Overview](../clients/overview.md) — per-host delivery behavior and
  configuration paths.
- [Claude Code](../clients/claude-code.md),
  [GitHub Copilot CLI](../clients/copilot-cli.md),
  [Other MCP Hosts](../clients/other-hosts.md) — individual host pages.
