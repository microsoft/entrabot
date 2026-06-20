# ENTRABOT harness

A single-agent **Copilot harness** that routes Microsoft Teams traffic through a Copilot
session and **gates tools/CLI permissions per caller**. Ported from the .NET `teammate`
harness (copilot-team); the multi-agent MQTT "workspace fabric" is intentionally dropped —
the channel/steering transport is the Teams layer that already lives in
`entrabot.tools.teams` + `entrabot.identity`.

## Run it

```bash
pip install -e .      # installs deps + the `entrabot` command
entrabot init         # guided setup: tenant → az login → prereqs → provision → test
entrabot              # launch the harness
```

`entrabot init` walks through the cross-platform setup (running `scripts/prereqs-*` and
`scripts/setup*` for your OS, `az login --allow-no-subscription`, then a three-hop connection
test), surfaces doc links when a step needs manual attention, and offers to launch when done.

**Config location:** `~/.entrabot/` by default. Pass a path (`entrabot init myagent` /
`entrabot myagent`) to use `myagent/.entrabot/`, or run inside a directory that already has a
`./.entrabot/` to use that one. Bare `entrabot` from anywhere uses the home config.

(`python -m entrabot.harness …` works identically if the `entrabot` script isn't on PATH.)

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
4. ✅ **TUI parity** — live character streaming (`#live`), slash-command autocomplete (Tab),
   command history recall (↑/↓), and multi-line paste staging (`⎘`, sent with the next message).
5. ✅ **Interrupt** — Esc (or Ctrl+C) aborts the running turn (`session.abort()`); the status
   line shows "working — esc to interrupt". Console UI interrupts on Ctrl+C best-effort.
6. ✅ **Live end-to-end (verified against the real runtime)** — everything except the literal
   Teams transport is exercised live: `CopilotClient` connect+auth, a streamed turn, the
   **per-caller gate** (deny-caller → gated tool blocked; allow-caller → permitted; this
   surfaced + fixed a real bug — the SDK calls the handler with `(request, context)`), runtime
   **command forwarding** (`rpc.commands.list`/`invoke` → text result rendering), and the
   **steering echo-binding** (the injected prompt matches the `USER_MESSAGE` echo, so caller/chat
   bind to the turn). Run `entrabot-harness doctor` to check runtime + auth + token.
7. ✅ **Windows UTF-8** — stdout/stderr are reconfigured to UTF-8 so the banner / `●` / em-dashes
   don't crash cp1252 consoles.

Tests: `pytest tests/harness` — 26 unit tests (config, scheduler, permissions incl. the
two-arg handler + yolo/ask/deny semantics, MCP loader, banner).

Still open:

- **Live Teams round-trip** — the Copilot/session/permission half is verified live; an actual
  message in→out over Teams needs a tenant with the Agent-User creds (or `ENTRABOT_GRAPH_TOKEN`).
