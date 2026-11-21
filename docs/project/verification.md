# Verification Record

What has been checked, how, and what hasn't. "Run for real" means against a real tenant and a
real agent, on the system named. "Unit-tested" means covered by the test suite
(`pytest -v && ruff check .`, which every commit must pass) with Graph, Entra and the operating
system faked. Nothing here names a tenant, account or machine.

Last updated: 2026-11-20, for 0.3.1 and the unreleased process containment (planned for 0.4.0).

## Process containment (unreleased, planned for 0.4.0)

Run for real on macOS only, in run 2 below, where the server ran contained for the whole run. That run
was made on the containment branch before it was brought up to date with 0.3.1 (the Agent 365
registry, the per-certificate keychain items, Teams reactions); **it has not been run for real
again since**.
**The first Linux sandbox was escaped when it was attacked for real; it was rebuilt and
attacked again (below).** On Linux it has been run for real with a throwaway agent that had
no Teams licence or mailbox: sign-in, the server's start and polls, and the refusals, but no
Teams, mail or Files traffic. The Microsoft eXecution Container backend has never been run, and Windows is
not contained.**

| What | macOS | Linux | Windows |
|---|---|---|---|
| Server contained by default; `whoami`, `containment.json` and the check behind `./status.sh` and `--diagnose` report it | Run for real (Seatbelt) | Run for real (`bwrap`), without and with an agent: `whoami` said `bwrap`, the status record and the check said contained | Not contained; reported as such |
| Agent signs in inside the sandbox | Run for real (certificate key from the login keychain) | Run for real: the certificate key read from GNOME Keyring over the session's D-Bus socket (a path under `/tmp`, mounted in), the three-hop sign-in, and `GET /me` answering as the agent. A `dbus-run-session` shell only; a desktop session's keyring and KWallet never run | n/a |
| Every connection through the allowlisting proxy (sends, reads, polls, token re-acquisition, the Agent 365 export) | Run for real | Run for real for the sign-in's token requests, `GET /me`, and the server's own polls over about 90 seconds, with nothing refused. No send, no read of a message or file, no Agent 365 export: the agent had no Teams licence or mailbox (the mail poll got Graph's `404`) | n/a |
| Refusals from inside the live server's sandbox: reading the home directory, `/Users/Shared` or another volume; writing to the repository, the home directory or `/tmp`; a non-Microsoft host | Run for real (checked against the live server process and with real attempts under its profile) | Run for real with a probe in the server's sandbox, not against the live server: the home directory showed only the allowed folders, the repository was read-only, `example.com` was refused by the proxy (`403`, and logged), a direct connection had no route, DNS did not resolve. Writes to the home directory and `/tmp` succeed there but land in a throwaway tmpfs | n/a |
| The telemetry distro's Azure Monitor calls (`settings.sdk.monitor.azure.com`, an Application Insights host) | Refused by the proxy (before 0.2.0 switched them off) | Never run | n/a |
| Clean stop: no process left, status record and generated profile removed | Run for real | Run for real: after standard input closed, no `bwrap`, bridge or server process was left, the launcher exited `0`, and the status record and proxy socket were gone | n/a |
| A `bwrap` the system refuses (unprivileged user namespaces restricted) | n/a | Run for real on stock Ubuntu 24.04.5: `auto` ran uncontained and said why; `required` exited `2` | n/a |
| Microsoft eXecution Container backend | Unit-tested; never run | Unit-tested; never run | Not supported |
| Reading the status record never signals the process it checks | Unit-tested | Unit-tested | Unit-tested in CI (`test-windows`) |

Bugs found on the containment branch, fixed with a test each: the Seatbelt profile let the server read
outside the home directory (other users' folders, other volumes); Teams, mail and Files calls
didn't use the sandbox's proxy, so they failed inside it; and on Windows the status record's
liveness check sent Ctrl+C to the process it checked (signal 0 is `CTRL_C_EVENT` there), found
by the Windows CI run.

### Linux, run for real, 2026-11-20

A new Azure VM (Ubuntu 24.04.5, kernel 7.0, Python 3.12.3, `bubblewrap` 0.9.0), the branch at
`80c65ab` plus the fix below. First with no `.env` and no agent, then with a throwaway agent.

Without an agent:

- **The test suite:** `pytest` passed 6546 and skipped 263, with no failures; `ruff check .`
  found nothing.
- **Found: on stock Ubuntu 24.04 the server did not start.** AppArmor restricts unprivileged
  user namespaces and the `bubblewrap` package ships no profile, so `bwrap` exited `1`
  ("setting up uid map: Permission denied") and `entrabot-mcp` exited `1` with it. Fixed with
  tests: the launcher tries `bwrap` before it uses it. Run for real after the fix, on the
  stock system: in `auto` the server started, answered `initialize`, and said on standard
  error that it runs without containment and why; with `ENTRABOT_CONTAINMENT=required` it
  exited `2` with the same reason; the self-test reported `"contained": false`.
- **With an AppArmor profile for `bwrap`** (the one in the containment troubleshooting page):
  the server started contained (launcher, `bwrap`, the bridge, the server), answered
  `initialize` and `whoami` over stdio (`"containment": "bwrap"`), wrote the status record
  with its hosts and paths, and the "Server containment" check passed. When standard input
  closed, every process was gone, the launcher exited `0`, and the status record and the
  proxy's socket were removed.
- **The limits, from a probe in exactly the server's sandbox:** see the table above.

With a throwaway agent, set up on the VM by `./scripts/setup.sh --yes --new` with a device-code
sign-in, in a `dbus-run-session` shell with GNOME Keyring 46.1 unlocked (the recipe in the
Linux platform page). Setup created the agent and its key in the keyring and ended with one
thing to check (exit `3`): the tenant had no free Teams licence for it.

- **Sign-in inside the sandbox:** `python -m entrabot.sandbox.selftest` exited `0`. Inside
  `bwrap` the agent's key was read from the keyring over the session's D-Bus socket, the
  three-hop sign-in went through the proxy, `GET /me` answered with the agent's address, and
  the four refusals held in the same run.
- **The server as the agent, contained, for about 90 seconds:** `whoami` over stdio answered
  `authenticated`, `agent_user` and `"containment": "bwrap"`. The proxy refused nothing in
  that time. The mail poll reached Microsoft Graph and got its `404` (the agent has no
  mailbox). When standard input closed, every process was gone and the launcher exited `0`.
- **Not run with this agent:** a Teams message, mail or file sent or read from inside the
  sandbox (it had no licence; see the next run), inbound pushes, token re-acquisition after
  expiry, and the Agent 365 export (telemetry was off); a desktop session's keyring, KWallet,
  an abstract D-Bus socket, another distribution, and a `bwrap` that is setuid.

### Teams, mail and Files from inside the Linux sandbox, 2026-11-20

At commit `0d1b1b3`, after the rebuild described below, on a new Ubuntu 24.04.5 VM (x86-64,
`bubblewrap` 0.9.0 with the AppArmor profile, `xdg-dbus-proxy` 0.1.5, GNOME Keyring unlocked
in a `dbus-run-session` shell). The VM was joined to an existing agent that has a Microsoft
365 E3 licence, as a second computer (`./scripts/setup.sh --yes --use-blueprint=... --agent-user-upn=...`
with a device-code sign-in). Setup exited `0` and its end-to-end test passed. The server was
then started contained and driven over stdio; `whoami` answered `"containment": "bwrap"`.

| From inside the sandbox | Result |
|---|---|
| `create_chat` with the sponsor | Returned the existing 1:1 chat |
| `send_teams_message` without a placeholder | Refused by the server's own placeholder rule, as designed |
| `post_thinking_placeholder`, then `send_teams_message` | Both sent; message IDs returned |
| `read_teams_messages` | Returned the message just sent, inside the external-content envelope |
| `delete_teams_message` (the placeholder) | Deleted (`204`) |
| `send_email` to the sponsor | Accepted by Microsoft Graph |
| `list_recent_files` | Returned the agent's recent files |
| Background Teams poll, 80 seconds | 16 requests, all `200` |
| Background mail poll | `GET /me/messages` answered `200` |

- The proxy refused nothing, the only hosts reached were `login.microsoftonline.com` and
  `graph.microsoft.com`, and when standard input closed every process was gone and the
  launcher exited `0`.
- **Inbound pushes, in two later contained runs:** each arrived as
  `notifications/claude/channel`, with the proxy refusing nothing.

  | Inbound | Result |
  |---|---|
  | A message in a group chat the agent belongs to (found by chat discovery) | Pushed inside the external-content envelope |
  | A Teams reply from the sponsor in the 1:1 chat | Pushed 14 seconds after it was sent, as the sponsor's own words (no envelope, as designed for a verified sponsor) |
  | A mail from the sponsor to the agent | Pushed 53 seconds after it was sent, inside the external-content envelope |
  | A mail the agent sent to its own address | Not pushed, as designed: six polls answered `200` and the poll skips the agent's own mail |

  The sponsor's reply and mail were sent from Teams and Outlook on the web, signed in as the
  sponsor. The mail the agent had sent earlier was seen in the sponsor's inbox.
- Afterward the VM was disconnected (`./scripts/teardown.sh --yes`, which took its certificate
  off the agent's Blueprint and left the agent) and deleted.
- **Not run:** `read_email`, reading or sharing a file, Work IQ, token re-acquisition after expiry, and
  the Agent 365 export.

### The Linux sandbox was escaped, then rebuilt, 2026-11-20

Everything above in this section was run on the first Linux sandbox. It showed that the
documented limits held when a probe tried them. It did not show that the sandbox could not
be left, and nobody had tried.

**The escape, run for real on the same VM.** From inside exactly the server's sandbox:

- `systemd-run --user`, pointed at `/run/user/<uid>/bus`, exited `0`. The command it started
  ran outside the sandbox: it wrote a file in the real home directory, read
  `~/.ssh/authorized_keys`, and fetched `https://example.com` (`200`). The sandbox mounted
  the whole system read-only, and a read-only mount does not stop `connect()` on a socket.
- Also from inside: another user's world-readable file was read, `/mnt` was listed, a
  variable set only in the launching shell was present, and the system bus, systemd's
  private socket and the GPG and SSH agent sockets accepted connections.
- Refused even then: the real `/tmp`, `/root`, `mount`, `sudo`, `crontab`, a direct
  connection.

**The rebuild** follows what Flatpak and Microsoft eXecution Container's bubblewrap backend
do: only named folders are mounted and the root is remounted read-only; the session bus is
never mounted, and the key store is reached through `xdg-dbus-proxy --filter`; the
environment is a named list; all capabilities are dropped; `--disable-userns`; and a
system-call filter modelled on Flatpak's.

**Run for real after the rebuild, on the same VM** (x86-64, `bubblewrap` 0.9.0,
`xdg-dbus-proxy`, the launcher started from the login session, whose bus has systemd on it):

| Attempt from inside the sandbox | Before | After |
|---|---|---|
| `systemd-run --user` over the session bus | Ran a command outside | "Failed to connect to bus": the socket does not exist; no file was written outside |
| The same through the bus the sandbox is given (the proxy) | n/a | Refused; with the key store's own D-Bus library, systemd answers on the real bus and gets an error through the proxy, and no name but the key store's is visible |
| Connect to the session bus, systemd's private socket, the system bus | Connected | No such file |
| Unix sockets visible anywhere outside the sandbox's own two | Twelve under `/run/user` | None |
| Another user's file under `/home`; `/mnt` | Read; listed | No such file |
| A variable from the launching shell | Present | Not present |
| A new user namespace (`unshare`), a mount, the kernel keyring (`keyctl`) | Not tried | Refused |
| Capabilities held | Not checked | None, and "no new privileges" set |

With the same throwaway agent, after the rebuild: `python -m entrabot.sandbox.selftest`
exited `0` (the key was read from GNOME Keyring through the proxy, the three-hop sign-in and
`GET /me` answered as the agent, the four refusals held). The server ran contained as the
agent for about 90 seconds: `whoami` answered `authenticated`, `agent_user`, `bwrap`; the
proxy refused nothing; every process, the D-Bus proxy included, was gone after it stopped.

**Tests that attack a real sandbox** now exist (`tests/sandbox/test_bwrap_for_real.py`,
`test_dbusproxy_for_real.py`): 12 tests, run on that VM, all passing. They skip where `bwrap`
cannot create a sandbox, which includes macOS, Windows and a stock GitHub Ubuntu runner. Since
2026-11-21 a CI job (`test-linux-sandbox`) sets an Ubuntu 24.04 runner up so that they run (the
AppArmor profile for `bwrap`, `xdg-dbus-proxy`, and the user's systemd with its session bus),
runs them for each pull request, and fails if any of them skips. The system-call filter is also run through an interpreter in
`tests/sandbox/test_seccomp.py`, for x86-64 and arm64, on every system.

**Not run after the rebuild:** arm64; another distribution or a bubblewrap older than 0.8.0
(where only the filter stops new namespaces); a desktop session's keyring; KWallet; an
abstract session bus socket (the proxy connects from outside, so it should work; not tried);
Teams, mail and Files traffic; and no one other than the author has attacked it.

### macOS, run for real without an agent, 2026-11-20

On macOS 27.2.1 (Apple Silicon), the repository on a second volume, no agent set up. A probe
in exactly the server's Seatbelt sandbox:

- **Held:** the two Microsoft hosts answered through the proxy; another host, a direct
  connection, `curl` without the proxy and name lookup were refused; the home directory,
  `~/.ssh`, `~/Documents`, `/Users/Shared`, `/Volumes` and the repository's own volume root
  could not be listed; writes to the repository, the home directory and `/tmp` were refused.
- **Escape attempts refused:** `launchctl submit`, `launchctl bootstrap`, `crontab`, `at`,
  `sudo`, a second permissive `sandbox-exec`.
- **Found:** `open -a Calculator` started the app; an app started by Launch Services runs
  outside the sandbox. A script written to the state folder and opened the same way did not
  run, for a reason that was not established. The shell's environment was inherited.
- **After denying Launch Services and Apple Events in the profile, and the named
  environment:** `open` failed, `osascript` to another app failed with a permission error,
  the shell's variable was not present, and the limits above still held.
- **Readable, as documented:** `/etc/passwd`, a file in the real `/tmp`, and the login
  keychain's item list (not the secrets).
- **Not run:** the agent's sign-in on macOS with the new profile rules, or at all since the
  branch was brought up to date with 0.3.1.

## What 0.3.1 rests on

0.3.1 is 0.3.0 with two fixes, `revoke_consent.py` changing only one resource's grant (pull
request #164) and the scripts finding setup's private Azure CLI (#165), and two docs
changes: #164's page on calling other APIs as the agent and #166's video tour. The MCP
server's code is the same as in 0.3.0. What changed is the scripts in `scripts/`, the
"Agent 365 registry" line of the health check, and how the harness's `entrabot init` finds
the Azure CLI. The rest of this record, from "What 0.3.0 rests on" on, is what 0.3.0 and
0.2.0 were checked against, and still holds for that code.

The two fixes were run for real on their pull requests' own branches before they were
merged, not on this release branch:

- **`revoke_consent.py` on one resource (#164)**, run for real on Windows 11 from #164's
  branch: with grants on four resources, `--all --resource-app-id` deleted only the one
  named and left the agent's Microsoft Graph, Azure Storage and Agent 365 grants in place.
  The guide #164 adds was run for real on Windows 11 against a stand-in API that is not part
  of this repository: Entra refused the token with `AADSTS65001` before the grant, and the
  API answered `200` after it.
- **The scripts and setup's private Azure CLI (#165)**, run for real on Ubuntu 24.04 from
  #165's branch, on the computer where the bug was found: `--diagnose` reported the registry
  as registered and `register_agent_instance.py --check` answered "present", with no `az` on
  `PATH`.

The Windows CI job (`test-windows`, the full test suite on Windows) and CodeQL passed on each
of the pull requests #164, #165 and #166.

**The release branch itself** (the version, the CHANGELOG, the install commands and the
documentation) was checked on macOS 27 (Apple Silicon) only, with the gate from
`RELEASING.md`: `pytest -v --tb=short` passed 6173 tests and skipped 258, with no failures,
`ruff check .` found nothing, and `mkdocs build --strict` built the site with no warnings.

**One run for real from the release branch**, on Ubuntu 24.04, with a real agent in a real
tenant. It only read; it changed nothing. The checkout was a second copy of an agent that was set
up on that computer earlier, not a fresh setup, and there was no `az` on `PATH`:

- `./scripts/setup.sh --diagnose` reported version 0.3.1 and passed the three-hop sign-in,
  the Graph identity, the Teams scope, Agent 365 telemetry and the "Agent 365 registry" line
  ("registered", and Microsoft Graph returns it). Its two MCP configuration lines did not
  pass, because that second checkout was never set up as the agent's own; this release does
  not change them.
- `scripts/register_agent_instance.py --check` answered "present" and exited `0`.

That is the private Azure CLI fix (#165) on the release's own code, on Linux.

**Not run:**

- `revoke_consent.py`'s fix on macOS or Linux, or from the release branch on any system.
- The private Azure CLI fix on Windows or macOS.
- A fresh setup from the release branch, on any system.
- The full test suite on Windows on the release branch. It runs as the `test-windows` job of
  the release's pull request.

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
  the failure was seen live on Windows 11 in the #153 run. The first fix passed its unit tests
  and still failed there, because a redirected stdout on Windows uses the `surrogateescape`
  error handler, not `strict`. With that handled, run for real on Windows 11:
  `ensure_a365_work_iq_permissions.py` with its output redirected to a file exited `0` and
  printed "?" for the check mark.
- **The release branch itself** (the version, the merges, the documentation): the full test
  suite and `ruff` on macOS; on Windows 11, the #153 run above. The Windows CI job
  (`test-windows`, the full test suite on Windows) and CodeQL passed on the release's pull
  request.
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
| Process containment | Unreleased (planned for 0.4.0): run for real with an agent before the latest profile rules; without an agent after them (above) | Unreleased: escaped, rebuilt, then run for real with an agent and attacked, then with Teams, mail and Files traffic (above); the attack tests run in CI (`test-linux-sandbox`) | Not contained |

## Never run

- Teams, mail and Files traffic from inside the Linux sandbox, and the MXC backend anywhere.
- Containment on Windows (not implemented).

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
