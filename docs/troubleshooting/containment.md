# Containment

Failures that come from the sandbox the MCP server runs in on macOS and Linux (see
[Process Containment](../guides/containment.md)). The sandbox fails closed on purpose, so the
fix is usually configuration, not turning it off. For a refused recipient, a send refused
because the audit log can't be written, and telemetry, see
[Outbound Controls and Telemetry](outbound-and-telemetry.md).

## A tool fails with a DNS or proxy error

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

## The agent can't sign in when contained, but can when it isn't

On macOS the certificate key is read from the login keychain inside the server's process, so
the sandbox must allow `~/Library/Keychains`. It does by default. If the keychain lives
somewhere else, add that folder with `ENTRABOT_SANDBOX_EXTRA_READ`. Run
`python -m entrabot.sandbox.selftest`: its `whoami` result shows whether sign-in works in
exactly the server's sandbox.

On Linux the Secret Service is reached over the D-Bus session bus. The sandbox mounts the bus
socket in when `DBUS_SESSION_BUS_ADDRESS` is a path (`unix:path=...`). An abstract socket
(`unix:abstract=...`) can't be reached from the sandbox's network namespace, and sign-in
fails closed.

## A file the agent needs can't be read

`PermissionError: [Errno 1] Operation not permitted` on a path means the sandbox refused it.
The contained server reads only its repository, virtual environment, state and sign-in cache.
Add other read-only paths with `ENTRABOT_SANDBOX_EXTRA_READ` (separated by `:`).

## Status says the server is not contained

`./status.sh` or `--diagnose` warns "the running server ... is NOT contained" when:

- `ENTRABOT_CONTAINMENT=off` is set;
- no sandbox is available (Windows; Linux without `bwrap`);
- the running server was started by a version without containment. Restart the MCP host.

Set `ENTRABOT_CONTAINMENT=required` to make the server refuse to start uncontained.

## The audit directory is outside `~/.entrabot`

When the server is contained, an audit directory outside `~/.entrabot`
(`ENTRABOT_AUDIT_DIR`) is added to the sandbox's writable paths automatically. If sends are
refused because the audit event could not be recorded, the cause is the directory itself
(missing, not writable, or a full disk), not the sandbox.
