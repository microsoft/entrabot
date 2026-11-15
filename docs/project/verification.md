# Verification Record

What has been checked, how, and what hasn't. "Run for real" means against a real tenant and a
real agent, on the system named. "Unit-tested" means covered by the test suite
(`pytest -v && ruff check .`, which every commit must pass) with Graph, Entra and the operating
system faked. Nothing here names a tenant, account or machine.

Last updated: 2026-11-15.

## End-to-end run, macOS, 2026-11-15

The MCP server was started through the normal launch path (`entrabot-mcp`, containment on by
default, nothing set) on macOS 27 (Apple Silicon), with a real agent in a real tenant, and driven
over stdio by an MCP client for about 25 minutes across four server starts. Every message it sent
was labelled as an automated test and went only to the agent's one sponsor or to the agent itself.
Each result was read back through Microsoft Graph, and the sponsor's own mailbox and Teams client
showed what arrived.

| What | Result |
|---|---|
| Server contained by Seatbelt; `whoami`, `containment.json` and the check behind `./status.sh` and `--diagnose` report it | Run for real |
| Agent signs in inside the sandbox (certificate key from the login keychain, three-hop flow) | Run for real |
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
| Export to Agent 365 from the contained server (an inbound Teams message from the sponsor and a few tool calls) | Run for real: every export request to `agent365.svc.cloud.microsoft` through the sandbox's proxy answered HTTP 200 with each span's status `sent` for all three of Agent 365's sinks and no span rejected. (A 200 alone isn't proof: Microsoft also answers 200 when it drops spans for an unlicensed tenant.) The exported spans carried no message text (`gen_ai.output.messages` is `[content suppressed]`). The tenant had Agent 365 licences assigned. |
| Agent 365 telemetry, console exporter only | Run for real: `invoke_agent` spans for inbound messages and `execute_tool` spans for each tool call, written to standard error; the MCP stream on standard output stayed intact |
| Refusals from inside the live server's sandbox: reading the home directory, `/Users/Shared` or another volume; writing to the repository, the home directory or `/tmp`; a non-Microsoft host | Run for real (checked against the live server process and with real attempts under its profile) |
| Clean stop: no process left, status record and generated profile removed | Run for real |

Bugs the run found, fixed before this record, each with a test:

- The Seatbelt profile let the server read outside the home directory (other users' folders,
  other volumes).
- Teams, mail and Files calls didn't use the sandbox's proxy, so they failed inside the sandbox.
- With console telemetry on, spans were written to standard output, which is the MCP protocol
  channel.
- `read_file` didn't follow Graph's redirect to the file's download URL, so text files read back
  empty and PDF and Word files failed.

Also seen: the Microsoft OpenTelemetry Distro tries to reach Azure Monitor
(`settings.sdk.monitor.azure.com` and an Application Insights ingestion host) when telemetry is
on. The sandbox's proxy refused both; without containment those requests would go out.

## Not proven by that run

- Work IQ reads and writes: Word, Excel and OneDrive files weren't switched on for the test agent.
- Inbound email from someone who isn't a sponsor (the test account had no mailbox). Email is
  always wrapped, as the sponsor's email showed.
- The agent in the Agent 365 registry (`register_agent_instance.py --apply` needs a permission the
  setup app doesn't hold), and whether the exported telemetry shows in Defender or the Microsoft
  365 admin center (a portal check, not done here).
- Disabling the agent and watching a running server stop.

## By platform

| What | macOS | Linux | Windows |
|---|---|---|---|
| Containment | Run for real (above) | Unit-tested (`bwrap` command, network bridge); not run | Not contained |
| Microsoft eXecution Container backend | Unit-tested | Unit-tested | Not supported |
| Setup wizard against a real tenant | Repair run and health check | Run for real (Ubuntu 24.04) | Run for real (Windows 11) |
| Telemetry setup step and the `--diagnose` check | Run for real | Unit-tested | Unit-tested |
| `status.sh` and `setup_delegated.sh` read `.env` as data | Run for real (the old scripts fail the tests, the new ones pass) | Unit-tested | n/a |

## Never run

- Containment on a real Linux machine, and the MXC backend anywhere.
- Containment on Windows (not implemented).
- The Copilot SDK harness against a real Copilot runtime.
- The agent in the Agent 365 registry.
