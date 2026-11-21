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
- no sandbox is available (Windows; Linux without `bwrap` or without `xdg-dbus-proxy`, with a
  `bwrap` the system refuses, see below, or on a machine that is neither x86-64 nor arm64);
- the running server was started by a version without containment. Restart the MCP host.

Set `ENTRABOT_CONTAINMENT=required` to make the server refuse to start uncontained.

## Linux: `xdg-dbus-proxy` is not installed

The server says it runs without containment because `xdg-dbus-proxy is not installed`. The
sandbox reaches the keyring through that program and never gets the session bus itself, so
without it there is no sandbox. Install it and restart the MCP host:

```bash
sudo apt install xdg-dbus-proxy      # Debian, Ubuntu
sudo dnf install xdg-dbus-proxy      # Fedora, RHEL
```

## A token command or tool can't find an environment variable

The contained server gets its own settings (`ENTRABOT_*`, `PERSONA_SATI_*`), `PATH`, the
language settings and a few more from the environment it was started in, and nothing else.
If a persona-sati token command, or anything else the server starts, needs another variable,
name it in `ENTRABOT_SANDBOX_EXTRA_ENV` (comma-separated names) in the MCP host's `env` for
the server.

## Linux: `bwrap` is installed but the server is not contained

The server says, on standard error and in `--diagnose`:

```text
running WITHOUT containment: bubblewrap (bwrap) is installed but this system does not let it
create a sandbox: unprivileged user namespaces are restricted (on Ubuntu 24.04, by AppArmor).
```

Ubuntu 24.04 and later stop programs without an AppArmor profile from using unprivileged user
namespaces, and the `bubblewrap` package ships no profile. Run by hand, `bwrap --ro-bind / / true`
prints `bwrap: setting up uid map: Permission denied`. Entrabot tries `bwrap` once at each
start, and falls back to running uncontained when that fails (or refuses to start, with
`ENTRABOT_CONTAINMENT=required` or `bwrap`).

To let `bwrap` create sandboxes, an administrator gives it an AppArmor profile. This needs
`sudo`, and it applies to every use of `bwrap` on the computer, not only Entrabot's. Create
`/etc/apparmor.d/bwrap` with:

```text
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
```

Then load it and check:

```bash
sudo apparmor_parser -r /etc/apparmor.d/bwrap
bwrap --ro-bind / / true && echo works
python -m entrabot.sandbox.selftest
```

Restart the MCP host afterwards. This was run for real on Ubuntu 24.04.5: without the profile
the server ran uncontained and said so; with it, the server ran contained.

## The audit directory is outside `~/.entrabot`

When the server is contained, an audit directory outside `~/.entrabot`
(`ENTRABOT_AUDIT_DIR`) is added to the sandbox's writable paths automatically. If sends are
refused because the audit event could not be recorded, the cause is the directory itself
(missing, not writable, or a full disk), not the sandbox.
