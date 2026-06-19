# ENTRABOT harness

A single-agent **Copilot harness** that routes Microsoft Teams traffic through a Copilot
session and **gates tools/CLI permissions per caller**. Ported from the .NET `teammate`
harness (copilot-team); the multi-agent MQTT "workspace fabric" is intentionally dropped —
the channel/steering transport is the Teams layer that already lives in
`entrabot.tools.teams` + `entrabot.identity`.

## Run it

```bash
pip install -e .            # installs the github-copilot-sdk dependency + the entry point
entrabot-harness init my-bot "What this agent does"
entrabot-harness            # start a session in that directory
```

- `ENTRABOT_GRAPH_TOKEN` — set to enable the Teams bridge (ingress polling + outbound).
  Without it the harness runs **console-only** (you can chat with the agent; it just won't
  listen to / post on Teams). Production should wire entrabot's three-hop token in
  `auth.py` (`make_token_provider`, marked INTEGRATION POINT).
- `ENTRABOT_AGENT_USER_ID` — the agent's own Teams user id (so it doesn't echo itself).
- `ENTRABOT_TUI=1` — use the full-screen Textual UI (`pip install -e '.[tui]'`); the
  console UI is the default.

## Per-caller permissions (the point)

`.entrabot/harness.json` carries a `permissions` block:

```json
{
  "permissions": {
    "default": { "mode": "ask", "deny": ["shell:rm*", "shell:sudo*"] },
    "callers": {
      "boss@contoso.com": { "mode": "allow" },
      "guest@partner.com": { "mode": "deny", "allow": ["read", "mcp:docs.*"] }
    }
  }
}
```

The active Teams caller (resolved by `teams_comms.TeamsBridge`) is matched against the
policy in `permissions.py`, which feeds the SDK's `on_permission_request` hook. Tokens are
`<kind>` (`shell`/`write`/`read`/`url`/`mcp`/`custom`) or `<kind>:<glob>`. An explicit
`allow`/`deny` is authoritative; only the undecided ("ask") case is affected by `--yolo`
(skips the prompt) — so `--yolo` can never blow past a caller the policy explicitly denies.

## Module map (port of the .NET harness)

| Module | Ports from | Status |
|--------|-----------|--------|
| `cli.py` | Program.cs | run / init / version / help |
| `session.py` | Session/InteractiveSession.cs | client+session, event→UI streaming, slash cmds, steering inject |
| `permissions.py` | the permission model (+ per-caller extension) | complete |
| `teams_comms.py` | Session/ChannelConnection.cs (MQTT→Teams) | poll ingress + egress; tracks active caller |
| `teams_tools.py` | Session/ChannelTools.cs (`channels_*`→`entrabot_*`) | send / read / list |
| `scheduler.py` | Session/Scheduling.cs + SelfScheduler.cs | interval/oneshot/cron, persisted |
| `mcp_loader.py` | Session/McpConfigLoader.cs | `.mcp.json` / `.vscode/mcp.json` |
| `config.py` | Config/TeammateConfig.cs + ConfigStore.cs | `.entrabot/harness.json` |
| `scaffold.py` | Bootstrap/Scaffolder.cs | AGENT.md + copilot-instructions.md |
| `banner.py` / `ansi.py` | Cli/Banner.cs + Ansi.cs | ENTRABOT wordmark (ENTRA blue / BOT pink) |
| `ui/console.py` | Ui/ConsoleUi.cs | complete |
| `ui/tui.py` | Ui/TuiUi.cs (Terminal.Gui → Textual) | functional; line-buffered (not char-streamed) |

## Status

Closed in this branch:

1. ✅ **Token provider** — `auth.py` honors `ENTRABOT_GRAPH_TOKEN`, else wires entrabot's
   three-hop (`acquire_agent_user_token`) with JWT-`exp` caching + pre-expiry refresh.
   Returns `None` (console-only) if neither is available.
2. ✅ **Runtime slash commands** — unknown `/cmd` is forwarded to the SDK command registry
   (`session.rpc.commands.list` / `.invoke`); results render as text / agent-prompt-turn /
   message / subcommand list. Runtime commands also show in `/help` and TUI autocomplete.
3. ✅ **Per-turn caller binding** — the caller + chat travel with each injected Teams
   message and are bound to the turn it starts (promoted on the `USER_MESSAGE` echo, cleared
   on `SESSION_IDLE`). The permission policy resolves the caller of the *running* turn, not
   "latest message wins".
4. ✅ **TUI: character streaming + autocomplete** — the Textual UI now streams the assistant
   line character-by-character (live `#live` widget) and offers slash-command autocomplete
   (Tab to complete).

Still open:

- **TUI paste-staging** — multi-line paste isn't staged like the .NET TUI (single-line Input).
- **Live end-to-end** — exercised against the real SDK via smoke tests (imports, config,
  scheduler, per-caller permissions, MCP, command API, tool construction, init flow); a live
  round-trip against Teams + Copilot is the remaining milestone.
