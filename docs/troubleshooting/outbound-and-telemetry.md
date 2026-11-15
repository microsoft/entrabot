# Outbound Controls and Telemetry

Failures that come from the server's own safety checks: the recipient policy, the audit log,
and Agent 365 telemetry. Each of these fails closed on purpose, so the fix is
usually configuration, not turning the check off.

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
user the server runs as, and that the disk isn't full.

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
