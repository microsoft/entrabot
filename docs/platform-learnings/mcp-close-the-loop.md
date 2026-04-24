# MCP "Close the Loop" Problem — Research

**Date:** 2026-04-06
**Purpose:** Document the fundamental gap in MCP bidirectional communication and our position relative to the industry.

---

## The Problem

After an MCP tool sends a message (e.g., `send_teams_message`), the LLM has no way to know when a reply arrives unless it explicitly calls another tool to check. The MCP protocol is request-response — the LLM drives all interaction. If the LLM doesn't ask, it doesn't get data.

We built `watch_teams_replies` as a blocking polling tool, and it works when explicitly called. But the LLM doesn't automatically call it after sending a message — it says "done" and stops. The human's reply goes into the void.

This is not a bug in our implementation. It is a **fundamental architectural constraint of the MCP protocol**.

---

## What We Found: Nobody Has Solved This

### Every MCP Messaging Server Is Fire-and-Forget

| Server | Platform | Sends Messages? | Auto-polls for replies? |
|--------|----------|----------------|------------------------|
| Official Slack MCP | Slack | Yes | No |
| korotovsky/slack-mcp-server | Slack | Yes | No |
| floriscornel/teams-mcp | Teams | Yes | No |
| carterlasalle/mac_messages_mcp | iMessage | Yes | No |
| barryyip0625/mcp-discord | Discord | Yes | No |
| **Entraclaw** | **Teams** | **Yes** | **Yes (watch_teams_replies)** |

We are the only MCP messaging server that even provides a polling tool for replies.

### Discord's Push Pattern Doesn't Work

tolgasumer/discord-mcp sends JSON-RPC notifications via stdout when Discord gateway events fire:
```json
{"jsonrpc": "2.0", "method": "discord/messageCreated", "params": {...}}
```

**Neither Claude Code nor Claude Desktop processes these.** The notifications are written to the transport and silently ignored. Custom notification methods have no handler in any major MCP client.

### MCP Resource Subscriptions Are Dead on Arrival

The spec defines `resources/subscribe` and `notifications/resources/updated`. Claude Code support was requested in issue #7252 — **closed as "not planned"** on January 5, 2026. Claude Desktop also doesn't support resource subscriptions.

### MCP Tasks Don't Solve It Either

The Tasks primitive (SEP-1686) lets a tool return a task ID and report progress while running in the background. But:
- No major client (Claude Code, Claude Desktop) supports `tasks/get` or `tasks/subscribe`
- Even if they did, Tasks solve timeout issues for **finite operations**, not indefinite waiting
- Push notifications for task completion are "best-effort" and deferred to future work

### The Root Cause

> LLMs are request-response systems. Even with perfect MCP notifications, something must inject a new "turn" into the conversation to wake the LLM up. That "something" is currently the application layer (the chat UI, the CLI, or a scheduled task), not the MCP protocol itself.

Standard message roles ("user", "assistant", "system") don't accommodate events triggered by external systems. There is no "tool_push" role.

---

## The Industry Response: Triggers & Events Working Group

The MCP project chartered a **Triggers & Events Working Group** on March 24, 2026:
- Led by Clare Liguori (AWS) and Peter Alexander (Anthropic)
- Mission: "define how MCP servers proactively notify clients of state changes"
- Active RFC: "Events in MCP v1" — targeting end of April 2026
- Reference implementations planned for Tier-1 SDKs

The existing notification primitives (`notifications/resources/updated`, `notifications/tools/list_changed`) are explicitly listed as OUT of scope — confirming they are not considered sufficient for push notifications.

**This means the proper solution doesn't exist yet and won't until at least mid-2026, after client adoption of whatever the WG produces.**

---

## What CAN Be Done Today

Ranked by reliability:

### 1. LLM-Initiated Polling (What We Built)
`watch_teams_replies` blocks for up to N seconds. Works perfectly when called. The problem is getting the LLM to call it.

### 2. PostToolUse Hook with `additionalContext`
After `send_teams_message`, a Claude Code hook injects a system reminder: "You just sent a Teams message. Call watch_teams_replies to wait for a response."

```json
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "mcp__entraclaw__send_teams_message",
      "hooks": [{
        "type": "command",
        "command": "echo '{\"hookSpecificOutput\":{\"hookEventName\":\"PostToolUse\",\"additionalContext\":\"Message sent. You should now call watch_teams_replies to wait for the human reply.\"}}'"
      }]
    }]
  }
}
```

This is **influence, not invocation** — the LLM reads the hint and usually follows it. It's probabilistic, not deterministic. But it's the standard Claude Code pattern for tool chaining.

### 3. Stop Hook (Agent-Based)
A Stop hook spawns a subagent before Claude stops. The subagent can check for pending replies:
```json
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "agent",
        "prompt": "Check if there are unread Teams replies using watch_teams_replies. If yes, report them.",
        "timeout": 60
      }]
    }]
  }
}
```

### 4. Desktop Scheduled Task
A recurring task that runs the full send→watch→act loop as an autonomous session. Has full MCP access. But runs in a **separate session** — can't feed data back into an ongoing conversation.

### 5. FastMCP `ctx.sample()` (Experimental)
FastMCP's Context object exposes `ctx.sample()` — ask the client's LLM to generate text from within a tool. Theoretically, `watch_teams_replies` could call `ctx.sample("The human replied: {message}. What should I do?")` to re-engage the LLM mid-tool. Untested, likely not supported by Claude Code's MCP client.

---

## Our Position

We are ahead of the industry:

1. **We have a working polling tool** — nobody else provides `watch_teams_replies`
2. **We have token auto-refresh** — our three-hop refresh is more complex than any other MCP server's auth lifecycle
3. **We have dedup** — timestamp overlap + seen-set, informed by iMessage server research
4. **We identified the protocol gap** — and the MCP working group is actively addressing it

The proper solution will come from the Triggers & Events WG. When it ships, we'll be the first to adopt it because we already have the polling infrastructure — we just need to swap the trigger mechanism from "LLM decides to poll" to "server pushes event."

---

## Sources

- [MCP Triggers & Events WG Charter (March 2026)](https://modelcontextprotocol.io/community/triggers-events/charter)
- [Claude Code resource subscription request — closed "not planned" (Issue #7252)](https://github.com/anthropics/claude-code/issues/7252)
- [Claude Code hook tool chaining request — closed "not planned" (Issue #4992)](https://github.com/anthropics/claude-code/issues/4992)
- [MCP Discussion #1192: Server Notification Best Practices](https://github.com/modelcontextprotocol/modelcontextprotocol/discussions/1192)
- [MCP Issue #982: Long-Running Operations](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/982)
- [tolgasumer/discord-mcp: JSON-RPC notification pattern](https://github.com/tolgasumer/discord-mcp)
- [FastMCP Tasks Documentation](https://gofastmcp.com/servers/tasks)
- [FastMCP Context Object](https://gofastmcp.com/servers/context)
- [Roman Gelembjuk: MCP Push Notifications in Custom Agent](https://gelembjuk.com/blog/post/using-mcp-push-notifications-in-ai-agents/)
- [2026 MCP Roadmap](https://blog.modelcontextprotocol.io/posts/2026-mcp-roadmap/)
