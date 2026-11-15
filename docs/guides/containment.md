# Process Containment

On macOS and Linux, Entrabot's MCP server runs inside an operating-system sandbox by default.
The sandbox limits what the server process can read, write and connect to, so a server that
is tricked by a message, a document or a tool argument still can't read your other files or
send data to a host that isn't Microsoft's.

This guide covers what the sandbox allows, how to check it, where refusals are logged, and how
to turn it off. For the design, see [Security Boundaries](../architecture/security-boundaries.md#process-containment).

## Status by platform

| Platform | Sandbox | Status |
|---|---|---|
| macOS | Seatbelt (`sandbox-exec`) with a profile generated at each start | On by default. Verified by running the full server on macOS: sign-in, Graph reads, the background polls and the refusals below. |
| Linux | bubblewrap (`bwrap`) | On by default when `bwrap` is installed. Implemented and unit-tested, not yet run on a real Linux machine. Without `bwrap` the server runs uncontained and says so. |
| Windows | none | Not contained. The server runs with all of its user's rights. |
| Any | Microsoft eXecution Container (MXC) | Used first when its executor is installed and trusted. Implemented and unit-tested, not yet run. |

## What the contained server can do

**Read:** the repository and its virtual environment, Entrabot's state (`~/.entrabot`), the
sign-in cache, and on macOS `~/Library/Keychains` (the certificate key is read from the login
keychain in the server's own process). Nothing else under your home directory. On macOS also
nothing else under `/Users` or `/Volumes`, so other users' folders, `/Users/Shared` and other
disks are out of reach. System folders (`/System`, `/usr`, `/Library`, `/opt`, `/private`,
including `/tmp`) stay readable, because Python and its libraries load from there.

**Write:** Entrabot's state and the sign-in cache only. Not the repository, not the rest of
your home directory, not `/tmp` (the server's `TMPDIR` points inside its state).

**Connect:** only through a proxy that the launcher runs outside the sandbox. The proxy opens
TLS tunnels on port 443 to these hosts and refuses everything else:

- `login.microsoftonline.com`
- `graph.microsoft.com`
- `agent365.svc.cloud.microsoft` (Work IQ, Agent 365 telemetry)
- `*.sharepoint.com` (Graph file downloads redirect there)
- the Azure Blob endpoint, when cloud memory is on
- persona-sati's host, when `PERSONA_SATI_MCP_URL` is set
- anything in `ENTRABOT_SANDBOX_EXTRA_HOSTS`

The proxy sees host names only; TLS stays end to end. DNS isn't reachable from inside the
sandbox. Anything the server starts, such as a persona-sati token command, inherits the same
limits.

Containment does not limit what the agent can do through Microsoft Graph with its own
permissions. The recipient policy, the sponsor gates and Entra handle that; see
[Security Boundaries](../architecture/security-boundaries.md).

## Settings

| Setting | Values | Effect |
|---|---|---|
| `ENTRABOT_CONTAINMENT` | `auto` (default) | Contain when a sandbox is available; otherwise run uncontained, say so on standard error and record it. |
| | `required` | Refuse to start uncontained (exit code 2). |
| | `off` | Never contain. |
| | `seatbelt`, `bwrap`, `mxc` | Use that sandbox or refuse to start. |
| `ENTRABOT_SANDBOX_EXTRA_HOSTS` | comma-separated host names, `*.example.com` for subdomains | Hosts the proxy also allows. |
| `ENTRABOT_SANDBOX_EXTRA_READ` | paths separated by `:` | Extra read-only paths, for example for a token command that reads files elsewhere. |
| `ENTRABOT_MXC_SHA256` | hex digest | When set, the MXC executor is used only if it matches. |

Put them in `.env` or the MCP host's environment for the server.

## Check it

- `./status.sh` and `./scripts/setup.sh --diagnose` include a **Server containment** line:
  pass when the running server is contained (and by what), warn when it is running
  uncontained, skip when no server is running.
- The `whoami` tool returns `"containment": "seatbelt"` (or `bwrap`, `mxc`, `none`).
- `python -m entrabot.sandbox.selftest` runs a probe inside exactly the sandbox and proxy the
  server gets, without starting the server. It signs in as the agent, reads `GET /me`, and
  confirms that a file in your home directory can't be read, the repository can't be written,
  and a non-Microsoft host can't be reached. It sends nothing and changes nothing.
- While the server runs, `~/.entrabot/runtime/containment.json` records the sandbox, the
  allowed hosts and paths, and the process IDs.

## Refusals

A host the proxy refuses is recorded, one JSON line each, in
`~/.entrabot/logs/containment-refusals.jsonl`, and logged as
`containment proxy refused <host>:<port>`. A refused file access surfaces as
`PermissionError: [Errno 1] Operation not permitted` in the tool's error or the server log.
See [Containment, Outbound Controls and Telemetry](../troubleshooting/containment-and-outbound.md) for what to do about each.

## Turning it off

Set `ENTRABOT_CONTAINMENT=off` and restart the MCP host. The server then runs with all of your
user account's rights: it can read every file you can (your SSH keys, other projects, browser
data) and connect anywhere. Do this only to rule containment out while troubleshooting, and
turn it back on afterwards. `--diagnose` and `./status.sh` report an uncontained server as a
warning.
