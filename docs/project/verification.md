# Verification Record

What has been checked, how, and what hasn't. "Run for real" means against a real tenant and a
real agent, on the system named. "Unit-tested" means covered by the test suite
(`pytest -v && ruff check .`, which every commit must pass) with Graph, Entra and the operating
system faked. Nothing here names a tenant, account or machine.

Last updated: 2026-11-15, for 0.3.0.

## What 0.3.0 rests on

0.3.0 is 0.2.0 with the Agent 365 integration (pull request #162) and two fixes carried with
it: #153, and the scripts' output on Windows. The MCP server's code is the same as in 0.2.0:
what changed is setup, teardown, the health check and the provisioning scripts. The rest of this record, from "What 0.2.0 rests on"
on, is what 0.2.0 was checked against, and still holds for that code.

- **The Agent 365 registry through setup** was run for real on 2026-11-15 on Linux (Ubuntu
  24.04) and Windows (Windows 11 Pro 25H2), with throwaway agents. Those runs were made on the
  pull request's branch before it was merged; `main` after the merge holds the same code. The
  Linux runs came before the last change, the grant a takeover run makes on its own, which was
  run on Windows only. See [0.3.0: the Agent 365 registry through
  setup](#030-the-agent-365-registry-through-setup) below.
- **The Work IQ "already exists" fix (#153)** was run for real on 2026-11-15 on Windows 11,
  from the release branch, with a throwaway agent:
  - Setup with `-ConfigureA365WorkIq` switched Word, Excel and OneDrive files on. The agent
    had no Microsoft 365 Copilot licence, so setup ended with that as a to-do (exit `3`).
  - With Graph's read of the Blueprint's inheritable permissions made to lag once (the Work IQ
    Word entry hidden from the script's first read; every request, the create included, went
    to Microsoft Graph), Graph answered the create with `400` "already exists". The fixed
    script read the entry back and reported it written, exit `0`, and the entry was
    unchanged. 0.2.0's script failed on the same answer.
  - Not run on Linux or macOS.
- **Scripts run on their own with redirected output** (Windows, a code page without "✅"):
  the failure was seen live on Windows 11 in the #153 run; the fix is unit-tested.
- **The release branch itself** (the version, the merges, the documentation): the full test
  suite and `ruff` on macOS; on Windows 11, the #153 run above. The Windows CI job
  (`test-windows`) runs on the release's pull request; this record was written before it ran.
- **Not run:** macOS; this step on Windows as a standard (non-administrator) user; a second
  computer joining an agent that is already registered.

## What 0.2.0 rests on

Two runs on macOS 27 (Apple Silicon) with one real agent, and a run on a Linux and a Windows
virtual machine with a second agent made for the purpose:

1. **An uncontained run of the 0.2.0 release branch itself.** Its code is what 0.2.0 ships. It
   sent nothing: it covered start-up, sign-in, reads, the recipient-policy refusals, console
   telemetry, the background polls and a clean stop.
2. **An earlier end-to-end run on a combined branch.** That build held everything in 0.2.0
   **and** the process containment now planned for 0.4.0, and the server ran **inside the sandbox**
   for the whole run. The real sends, the inbound pushes, token re-acquisition, audit
   fail-closed and the export to Agent 365 were proven there, not on the release branch.
   Apart from the sandbox launcher, its proxy and the proxy-aware Graph transport, the code
   those checks exercise is the same in 0.2.0. Two later 0.2.0 changes were not in that build:
   the daily summary's choice of recipient and the switches for the telemetry distro's Azure
   Monitor calls. Run 1 covers both.
3. **The release branch on Linux and Windows, end to end (run 3).** Real sends, inbound Teams
   messages from the sponsor, Word through Work IQ, the refusals, audit order, the export to
   Agent 365 and a clean stop, on each platform, then a short check of the final release head on
   both. The bugs it found were fixed on the release branch, each with a test, before the final
   check.

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

### Run 3: Linux and Windows, end to end, 2026-11-15

Two Azure virtual machines and a new agent made for the test, licensed for Teams, mail and
Microsoft 365 Copilot, with local memory and Agent 365 telemetry switched on:

- **Linux:** Ubuntu 24.04.5 (x86_64), no desktop. The agent's key is in GNOME Keyring, run inside
  a D-Bus session with the keyring unlocked for each command, as
  [platform-linux](../platform-docs/platform-linux.md) describes for a headless server. The checkout
  is a `git clone` of the release branch.
- **Windows:** Windows 11 Enterprise 24H2 (x64) with a virtual TPM. The agent's key is
  non-exportable, in the TPM. The install is the branch's source ZIP (no Git). Everything that
  uses the key ran in the logged-on desktop session through a scheduled task, **which ran
  elevated**: a standard user's session was not tested. An SSH logon has no access to the user's
  keys.

On Linux the agent was set up new; the Windows PC then joined it by its Blueprint App ID. A
small MCP client drove the real `entrabot-mcp` over stdio, the way an assistant starts it,
identifying itself as Claude Code (the host with channel push). Every connection that honours
`HTTPS_PROXY` went through a local proxy that recorded host names. Every message was labelled as
an automated test and went only to the sponsor or the agent itself.

| What | Linux | Windows |
|---|---|---|
| `--diagnose` (`-Diagnose`) | All checks pass | All checks pass |
| `send_teams_message`, `post_thinking_placeholder` then `resolve_placeholder`, `send_card` in a new 1:1 chat with the sponsor (`create_chat`) | Run for real; each read back through Graph | Run for real; each read back through Graph |
| `send_email` to the sponsor | Run for real; in Sent Items and the sponsor's inbox | Run for real; in Sent Items and the sponsor's inbox |
| Word through Work IQ: `create_word_document`, `read_word_document` | Run for real (one failure, below) | Run for real |
| `share_file` of that document with the sponsor | Run for real at the final release head, after the sponsor's message (it needs one in the same server session); the share is in the file's permissions and the sponsor received the notice | Run for real, after the sponsor's message; the share is in the file's permissions and the sponsor received the notice |
| Deleting the document | Run for real (Graph answers 423 Locked for a few minutes after Work IQ creates a document) | Run for real |
| Inbound Teams message from the sponsor | Run for real: pushed outside the envelope, sender matched by directory ID | Run for real: pushed outside the envelope, sender matched by directory ID |
| Inbound email from the sponsor | Run for real at the final release head: pushed inside the envelope | **Not run on this platform** (run on Linux, and on macOS in run 2) |
| The agent's own mail | Run for real: the poll fetched the agent's email to itself and, by design, didn't push it | Run for real: the poll fetched the agent's sent mail and didn't push it |
| Recipient policy: `send_email` and `share_file` to an address at a reserved domain | Run for real: both refused, a `denied` audit event each, no Graph request | Run for real: both refused, a `denied` audit event each, no Graph request |
| Audit event before each send | Run for real: every `pending` event before Graph's time for the message | Run for real: every `pending` event before Graph's time for the message |
| Export to Agent 365 | Run for real: every span `sent` to all three sinks, none rejected, no message text | Run for real: every span `sent` to all three sinks, none rejected, no message text |
| The telemetry distro's Azure Monitor calls | Run for real: no Azure Monitor or Application Insights host | Run for real: no Azure Monitor or Application Insights host |
| Background polls for 25 minutes with a call every minute | Run for real | Run for real |
| Clean stop | Run for real | Run for real |
| The final release head: setup again (the upgrade path), `--diagnose`, `whoami` (version 0.2.0), a Work IQ create, read and delete, a refusal, the export, a clean stop | Run for real | Run for real |

One `create_word_document` on Linux failed: the request to Work IQ got no answer, and the tool
returned no error to the caller. Its tokens were all issued (HTTP 200); the cause was not
established. It didn't happen again in 18 later creates inside the server on either platform. The
tool now reports such a failure as a Work IQ error.

Bugs run 3 found, fixed on the release branch with a test:

- Joining an agent by its Blueprint App ID with no Agent User named created a second agent under
  the Blueprint instead of joining the one it has.
- On Windows, setup didn't find an Azure CLI that winget had just installed, and installed a
  private copy as well.
- The version read "unknown" on every install (`whoami`, `--diagnose`, the status report).
- A Work IQ request that failed before an answer escaped the Word and Excel tools as a raw
  exception.
- Setup's review named the Agent User a state file last recorded, not the one the run was for.

It also showed that two computers running one agent with local memory both push the same message
(each keeps its own cursors). That behaviour is unchanged and now documented in the
[storage guide](../guides/storage-configuration.md#one-agent-on-more-than-one-computer-with-local-memory).

## Not proven

- Work IQ beyond Word: Excel and OneDrive files through Work IQ (run 3 covered Word only).
- Inbound email on Windows (run on Linux and macOS; the code is the same).
- Windows setup and the server in a standard user's session (run 3 ran elevated).
- Inbound email from someone who isn't a sponsor (the test account had no mailbox). Email is
  always wrapped, as the sponsor's email showed.
- Sends from the 0.2.0 release branch on macOS; they were run on the combined branch (run 2), and from the release branch on Linux and Windows (run 3).
- The daily summary actually sent: run 1 checked who it would go to, without sending.
- Whether the exported telemetry shows in Defender or the Microsoft 365 admin center was not
  checked for 0.2.0 itself. Both showed it in the 0.3.0 registry runs: see the registry section
  below.
- Disabling the agent and watching a running server stop.

## By platform

| What | macOS | Linux | Windows |
|---|---|---|---|
| The MCP server against a real tenant | Run for real (runs 1 and 2) | Run for real (run 3, Ubuntu 24.04, headless keyring) | Run for real (run 3, Windows 11 24H2, elevated desktop task) |
| Setup wizard against a real tenant | Repair run and health check | Run for real: new agent, repair, health check | Run for real: join, repair, health check |
| Telemetry setup step and the `--diagnose` check | Run for real | Run for real | Run for real |
| Agent 365 registry through setup (0.3.0): the setup app's permission, the registration, its `--diagnose` line, teardown's delete | Unit-tested | Run for real (2026-11-15, Ubuntu 24.04): the permission and its consent check, the registration, `--diagnose`, the admin center listing, the script's delete, teardown's delete. Not run: a second computer joining a registered agent | Run for real (2026-11-15, Windows 11 Pro 25H2, built-in Administrator): the permission, the registration, `-Diagnose`, the admin center listing, telemetry, teardown's delete. Run for real: the takeover run's grant, on a first run. Not run: a standard user |
| `status.sh` and `setup_delegated.sh` read `.env` as data | Run for real (the old scripts fail the tests, the new ones pass) | Unit-tested | n/a |
| The full test suite | Every commit | Not run | CI (`test-windows`) on each pull request |
| Process containment | Not in 0.2.0 or 0.3.0: planned for 0.4.0 | Not in 0.2.0 or 0.3.0 | Not in 0.2.0 or 0.3.0 |

## Never run

- The Copilot SDK harness against a real Copilot runtime.
- Registering the agent with 0.2.0's own `register_agent_instance.py --apply`, which needs a
  permission the 0.2.0 setup app doesn't hold. Setup in 0.3.0 does it; see below.

## 0.3.0: the Agent 365 registry through setup

Run on the Agent 365 integration branch (pull request #162) before it was merged into `main`.

### Linux

Run for real on 2026-11-15 on Ubuntu 24.04, with a throwaway agent:

- Setup with `--configure-a365` registered the agent. `--diagnose` reported it "registered",
  and Microsoft Graph returned the registration.
- Before registration the agent was not listed in the Microsoft 365 admin center (Agents > All
  agents > Registry). About a minute after, it was, with platform "Entrabot", the sponsor as
  owner, and the Entra agent ID. Its Activity tab showed sessions from Agent 365 telemetry,
  including telemetry sent before the agent was registered.
- Graph accepted the request body as setup builds it (application IDs). For an Entra agent
  identity, the registration ID Graph returned was the agent identity's ID.
- The first run found a bug: admin consent answered success about two seconds after the
  permission was added but granted nothing, so setup ticked the permission and the
  registration then failed for want of it (setup exited `3`). Running consent again a few
  minutes later granted it, and the next setup run registered the agent at once. Setup now
  checks that the grant is recorded and consents again, up to six times, until it is.
- The fix, run for real after the permission and the registration had been removed again: the
  first two consents answered success and recorded nothing; the grant showed after the third,
  about 50 seconds in. Setup then ticked the permission, registered the agent on the first try
  in the same run, and exited `0`.
- Defender's advanced hunting (`CloudAppEvents`) showed the agent's tool-call rows from its
  first telemetry, sent before it was registered.
- `register_agent_instance.py --delete --apply`: Graph deleted the registration, the recorded
  ID was cleared, and `--diagnose` then reported "not registered".
- Teardown with `--delete-agent`: it said which registration it would delete, deleted the agent
  and the registration (a tick for each), and exited `0`. About a minute after the delete the
  admin center's registry still listed the agent; a few minutes after, it no longer did (a
  search for it found none). "Deleted agents" stayed empty.

### Windows

Run for real on 2026-11-15 on Windows 11 Pro 25H2 with a TPM, with a throwaway agent. Setup
ran in the console session of an automatic logon (no remote desktop), as the computer's
built-in Administrator, elevated, not as a standard user. The computer was new to a tenant
that already had the setup app.

- The first run took the setup app over on an administrator's earlier approval, created the
  agent with a TPM-held key and switched telemetry on, but did not register the agent: a
  takeover run consents nothing, so the setup app did not get the permission, and setup
  exited `3`. A takeover run now grants that one permission on its own, with an app role
  assignment, and still runs no admin consent. Run for real on a third run,
  after teardown had made the computer new to the setup app again: setup granted the
  permission on its own about 10 seconds in, ran no admin consent, created the agent and
  registered it, and exited `0`.
- The second run (no longer a takeover): admin consent was recorded on the second attempt,
  about 20 seconds in, and the agent was registered in the same run. Setup exited `0`.
- `-Diagnose` reported "Agent 365 registry: registered", and Microsoft Graph returned the
  registration. The Microsoft 365 admin center listed the agent.
- Agent 365 accepted telemetry sent as that agent from Windows.
- `teardown-windows.ps1 -DeleteAgent -Yes` deleted the agent and its registration, and exited
  `0`.
- The Windows CI job (`test-windows`) passes on the pull request.

### Not run

macOS, a second computer joining an agent that is already registered, and this step on
Windows as a standard (non-administrator) user.
