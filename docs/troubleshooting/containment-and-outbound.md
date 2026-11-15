# Containment, Outbound Controls and Telemetry

Failures that come from the server's own safety checks: the sandbox, the recipient policy, the
audit log, and Agent 365 telemetry. Each of these fails closed on purpose, so the fix is
usually configuration, not turning the check off.

## Containment

### A tool fails with a DNS or proxy error

Symptoms: a tool returns `ConnectError` with `nodename nor servname provided, or not known`
(macOS) or `Temporary failure in name resolution` (Linux), or `ProxyError: 403 Forbidden`.

Inside the sandbox there is no DNS and the only way out is the launcher's proxy.

- `ProxyError: 403` means the proxy refused the host. Check
  `~/.entrabot/logs/containment-refusals.jsonl` for the host name. If it is one the agent
  legitimately needs, add it to `ENTRABOT_SANDBOX_EXTRA_HOSTS` (comma-separated;
  `*.example.com` covers subdomains) and restart the MCP host.
- A DNS error means the code made the request without the proxy. Every Graph call in Entrabot
  goes through the proxy; if you see this from Entrabot's own code, it is a bug. Report it with
  the tool name. As a stopgap, `ENTRABOT_CONTAINMENT=off` confirms that containment is the
  cause (see [Process Containment](../guides/containment.md#turning-it-off) for what you give
  up).

### The agent can't sign in when contained, but can when it isn't

On macOS the certificate key is read from the login keychain inside the server's process, so
the sandbox must allow `~/Library/Keychains`. It does by default. If the keychain lives
somewhere else, add that folder with `ENTRABOT_SANDBOX_EXTRA_READ`. Run
`python -m entrabot.sandbox.selftest`: its `whoami` result shows whether sign-in works in
exactly the server's sandbox.

On Linux the Secret Service is reached over the D-Bus session bus. The sandbox mounts the bus
socket in when `DBUS_SESSION_BUS_ADDRESS` is a path (`unix:path=...`). An abstract socket
(`unix:abstract=...`) can't be reached from the sandbox's network namespace, and sign-in
fails closed.

### A file the agent needs can't be read

`PermissionError: [Errno 1] Operation not permitted` on a path means the sandbox refused it.
The contained server reads only its repository, virtual environment, state and sign-in cache.
Add other read-only paths with `ENTRABOT_SANDBOX_EXTRA_READ` (separated by `:`).

### Status says the server is not contained

`./status.sh` or `--diagnose` warns "the running server ... is NOT contained" when:

- `ENTRABOT_CONTAINMENT=off` is set;
- no sandbox is available (Windows; Linux without `bwrap`);
- the running server was started by a version without containment. Restart the MCP host.

Set `ENTRABOT_CONTAINMENT=required` to make the server refuse to start uncontained.

## Recipient policy

### A send or share is refused: "recipient not allowed: nothing was sent"

The server refuses `send_email`, `create_chat`, `add_teams_member`, `share_file` and the daily
summary for any address that is not:

- one of the agent's sponsors, as the directory lists them on its Agent Identity;
- a member of the chat the request came from (while that request is recent);
- in the agent's home tenant (a domain in `ENTRABOT_HOME_DOMAINS`, or one that Microsoft's
  sign-in discovery resolves to the home tenant).

The reply lists each refused address with its reason, and a `denied` audit event is written.
Nothing reaches Microsoft Graph. To allow an outside recipient, an administrator adds the exact
address, or its domain (`fabrikam.com`), to `ENTRABOT_OUTBOUND_ALLOW` in `.env` and restarts the
MCP host. A domain entry covers that domain only, not its subdomains.

A sponsor that the agent can't read in the directory (Graph answers 403 for that user) has no
known address, so mail to that sponsor is refused like any outside address. Allowlist it, or
grant the agent read access to that user.

## Audit

### A send is refused: "the audit event could not be recorded"

`send_email`, `send_teams_message`, `send_card`, `post_thinking_placeholder` and `create_chat`
write their audit event before they act. If that write fails, they don't act. Check that the
audit directory (`ENTRABOT_AUDIT_DIR`, default `~/.entrabot/audit`) exists, is writable by the
user the server runs as, and that the disk isn't full. When the server is contained, an audit
directory outside `~/.entrabot` is added to the sandbox's writable paths automatically.

## Agent 365 telemetry

### Telemetry is on but nothing appears in Agent 365

Microsoft accepts the spans (HTTP 200) and then drops them as `tenant_not_licensed` unless at
least one user in the tenant has a Microsoft 365 E7 or Agent 365 licence assigned. Setup and
`--diagnose` can't check this. Assign the licence to a user, then check again.

Also check, in `--diagnose`, that the "Agent 365 telemetry" line shows the agent's token
carrying `Agent365.Observability.OtelWrite`. If it doesn't, run setup again with
`--configure-a365-observability` (Windows: `-ConfigureA365Observability`).

### Console spans

`ENTRABOT_A365_CONSOLE_ENABLED=true` prints spans to the server's standard error (never
standard output, which is the MCP protocol channel). Look for them in the MCP host's server log.
