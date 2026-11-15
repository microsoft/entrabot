# Verification Record

What has been checked, how, and what hasn't. "Run for real" means against a real tenant and a
real agent, on the system named. "Unit-tested" means covered by the test suite
(`pytest -v && ruff check .`, which every commit must pass) with Graph, Entra and the operating
system faked. Nothing here names a tenant, account or machine.

Last updated: 2026-11-15, for 0.2.0.

## What 0.2.0 rests on

Two runs, both on macOS 27 (Apple Silicon) with the same real agent:

1. **An uncontained run of the 0.2.0 release branch itself.** Its code is what 0.2.0 ships. It
   sent nothing: it covered start-up, sign-in, reads, the recipient-policy refusals, console
   telemetry, the background polls and a clean stop.
2. **An earlier end-to-end run on a combined branch.** That build held everything in 0.2.0
   **and** the process containment planned for 0.3.0, and the server ran **inside the sandbox**
   for the whole run. The real sends, the inbound pushes, token re-acquisition, audit
   fail-closed and the export to Agent 365 were proven there, not on the release branch.
   Apart from the sandbox launcher, its proxy and the proxy-aware Graph transport, the code
   those checks exercise is the same in 0.2.0. Two later 0.2.0 changes were not in that build:
   the daily summary's choice of recipient and the switches for the telemetry distro's Azure
   Monitor calls. Run 1 covers both.

### Run 1: the 0.2.0 release branch, uncontained, 2026-11-15

The server was started from the release branch (`python -m entrabot.mcp_server`) and driven over
stdio by an MCP client for about five minutes. It used the agent's real `.env`, read only, with
its own data, log and audit directories, so it took its own single-instance lock beside the
agent's running server. That agent keeps its watched-chat list and poll cursors in cloud
storage, which the two servers shared for those five minutes. The test server pushed nothing,
so it took no message from the running one (each email is claimed by one server only). Every
connection that honours `HTTPS_PROXY` went through a local proxy that allowed everything and
recorded host names, to see what the telemetry distro contacts.

| What | Result |
|---|---|
| Start-up, uncontained | Run for real: 43 tools; no containment code or report (`whoami` has no `containment` field) |
| Agent signs in (certificate key from the login keychain, three-hop flow) | Run for real: `whoami` reports `agent_user`, attribution `agent` |
| Reads: `whoami`, `list_recent_files`, `read_interactions` | Run for real: each returned without error |
| Recipient policy: `send_email` and `share_file` to an address at a reserved domain | Run for real: both refused ("recipient not allowed: nothing was sent"), one `denied` audit event each, no success event |
| `--diagnose`'s "Agent sponsor" check | Run for real: pass (one directory sponsor; `.env` names it) |
| The daily summary's recipient | Run for real, without sending: the directory gives the sponsor's address, so the summary would go there (`recipient_source` "directory") |
| Console telemetry | Run for real: an `invoke_agent` span and 13 `execute_tool` spans on standard error, content suppressed; the MCP stream on standard output stayed intact |
| The telemetry distro's own Azure Monitor calls switched off | Run for real: the recording proxy saw only Microsoft's sign-in host, Microsoft Graph and the agent's blob storage, and no Azure Monitor or Application Insights host |
| Background polls for four minutes, with a tool call every 30 seconds | Run for real: every call succeeded; no inbound message arrived in that window |
| Clean stop | Run for real: the server process was gone when the client closed |

### Run 2: the combined branch, contained, 2026-11-15

The MCP server was started through the normal launch path (`entrabot-mcp`), with the sandbox
on by default and nothing set, and driven over stdio by an MCP client for about 25 minutes
across four server starts. Every message it sent was labelled as an automated test and went
only to the agent's one sponsor or to the agent itself. Each result was read back through
Microsoft Graph, and the sponsor's own mailbox and Teams client showed what arrived.

| What | Result |
|---|---|
| Agent signs in (certificate key from the login keychain, three-hop flow) | Run for real |
| `send_teams_message` (after a thinking placeholder), `send_card`, `post_thinking_placeholder` and `update_placeholder` into the sponsor's 1:1 chat | Run for real; each message read back through Graph |
| `send_email` to the sponsor | Run for real; in the agent's Sent Items and the sponsor's inbox |
| `write_text_file`, `read_file`, `share_file` with the sponsor, in the agent's OneDrive | Run for real; the share appears in the file's permissions and the sponsor received the notice. The file was deleted afterwards. |
| Audit event written before each send | Run for real: each `pending` event is timestamped before Graph's own creation time for the message |
| Recipient policy: `send_email` and `share_file` to an address at a reserved domain | Run for real: both refused, a `denied` audit event each, and no Graph request made |
| Audit fail-closed: audit directory read-only | Run for real: `post_thinking_placeholder` and `send_teams_message` refused (`PermissionError` writing the event), no Graph request, nothing in the chat |
| Inbound Teams message from the sponsor | Run for real: pushed to the MCP client over the channel, outside the envelope (a verified sponsor's Teams message is the one exception) |
| Inbound Teams message from a tenant user who isn't a sponsor, in a new chat | Run for real: chat auto-discovery registered the chat and the message was pushed inside the `[external_content ...]` envelope |
| Inbound email from the sponsor | Run for real: pushed inside the envelope |
| The agent emailing itself | Run for real: the email poll claimed it in the shared cursor and, by design, didn't push the agent's own mail |
| Token re-acquired at least every `ENTRABOT_TOKEN_MAX_AGE_SECONDS` | Run for real at 60 seconds: 11 re-acquisitions in 11 minutes, with every read succeeding |
| Setup's telemetry step for a real agent (permission grant with the setup app, token check as the agent, `.env` switches written and read back) | Run for real: the agent's token carried `Agent365.Observability.OtelWrite` within seconds |
| `--diagnose` after the telemetry step | Run for real: "Agent 365 telemetry: switched on; the agent's token carries Agent365.Observability.OtelWrite" |
| Export to Agent 365 (an inbound Teams message from the sponsor and a few tool calls) | Run for real: every export request to `agent365.svc.cloud.microsoft` answered HTTP 200 with each span's status `sent` for all three of Agent 365's sinks and no span rejected. (A 200 alone isn't proof: Microsoft also answers 200 when it drops spans for an unlicensed tenant.) The exported spans carried no message text (`gen_ai.output.messages` is `[content suppressed]`). The tenant had Agent 365 licences assigned. |
| Console telemetry | Run for real: `invoke_agent` spans for inbound messages and `execute_tool` spans for each tool call, written to standard error |
| Clean stop | Run for real |

Bugs that run found in what 0.2.0 ships, fixed before this record, each with a test:

- With console telemetry on, spans were written to standard output, which is the MCP protocol
  channel.
- `read_file` didn't follow Graph's redirect to the file's download URL, so text files read back
  empty and PDF and Word files failed.

It also showed the Microsoft OpenTelemetry Distro calling Azure Monitor on its own
(`settings.sdk.monitor.azure.com` and an Application Insights host) with telemetry on. The
sandbox refused both there. 0.2.0 switches both calls off (run 1 above).

## Not proven

- Work IQ reads and writes: Word, Excel and OneDrive files weren't switched on for the test agent.
- Inbound email from someone who isn't a sponsor (the test account had no mailbox). Email is
  always wrapped, as the sponsor's email showed.
- Sends from the 0.2.0 release branch itself; they were run on the combined branch (run 2).
- The daily summary actually sent: run 1 checked who it would go to, without sending.
- The agent in the Agent 365 registry (`register_agent_instance.py --apply` needs a permission the
  setup app doesn't hold), and whether the exported telemetry shows in Defender or the Microsoft
  365 admin center (a portal check, not done here).
- Disabling the agent and watching a running server stop.

## By platform

| What | macOS | Linux | Windows |
|---|---|---|---|
| The MCP server against a real tenant | Run for real (above) | Not run for 0.2.0 | Not run for 0.2.0 |
| Setup wizard against a real tenant | Repair run and health check | Run for real (Ubuntu 24.04) | Run for real (Windows 11) |
| Telemetry setup step and the `--diagnose` check | Run for real | Unit-tested | Unit-tested |
| `status.sh` and `setup_delegated.sh` read `.env` as data | Run for real (the old scripts fail the tests, the new ones pass) | Unit-tested | n/a |
| The full test suite | Every commit | Not run | CI (`test-windows`) on each pull request |
| Process containment | Not in 0.2.0: planned for 0.3.0 | Not in 0.2.0 | Not in 0.2.0 |

## Never run

- The Copilot SDK harness against a real Copilot runtime.
- The agent in the Agent 365 registry.
