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

## Known gaps / next steps (first cut)

1. **Token provider** — `auth.py` honors `ENTRABOT_GRAPH_TOKEN`; wire entrabot's three-hop
   for production.
2. **TUI parity** — the Textual UI covers log + input + status + inline confirm, but not yet
   the .NET TUI's slash-autocomplete popup, paste-staging, or true character streaming.
3. **Runtime slash commands** — built-ins are implemented; forwarding unknown `/cmd` to the
   SDK's command registry (the .NET `Rpc.Commands` path) is not wired yet.
4. **Active-caller scoping** — the bridge tracks a single "active caller" (latest message);
   concurrent multi-chat turns would want per-turn caller binding.
5. **Smoke-tested, not battle-tested** — imports/config/scheduler/permissions/MCP/CLI are
   unit-smoke-tested against the real SDK; a live end-to-end run against Teams + Copilot is
   the next milestone.
