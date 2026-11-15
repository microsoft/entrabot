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

A sponsor whose user record the agent can't read (Graph answers 403 for that user) is known
by user ID only, so mail to that sponsor passes only as a home-tenant address and is otherwise
refused like any outside address. The usual cause is an install set up before 0.2.0, whose
setup could write the agent's Microsoft Graph permissions onto another resource's grant: run
setup again. `--diagnose` warns about this ("Agent sponsor").

### The daily summary goes to the wrong person, or to nobody

The daily summary goes to every sponsor the directory lists on the Agent Identity whose user
record gives an address. `.env`'s first `ENTRABOT_HUMAN_USER_MAILS` address is used only when
the directory gives none. The result's `recipient_source` says which was used (`directory` or
`env`). The recipient policy still checks each recipient.

Run `./scripts/setup.sh --diagnose` (Windows: `scripts\setup-windows.cmd -Diagnose`) and read
the "Agent sponsor" line:

- **`.env` names a sponsor who is not among the Agent Identity's sponsors.** Usually the
  sponsors changed after setup. Make `ENTRABOT_HUMAN_USER_ID`, `ENTRABOT_HUMAN_UPN` and
  `ENTRABOT_HUMAN_USER_MAILS` name the directory's sponsor, or change the agent's sponsors in
  the Microsoft Entra admin center.
- **The agent cannot read a sponsor's user record.** That sponsor is matched by user ID only,
  mail to them passes the recipient policy only as a home-tenant address, and when no
  sponsor's record can be read the summary falls back to `.env`. Run setup again: it grants
  the agent's Microsoft Graph permissions, which let it read user records.
- **Could not read the Agent Identity's sponsors.** The summary falls back to `.env`. Run the
  check again; if it persists, check the agent in the Microsoft Entra admin center.

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

### Connections to Application Insights or settings.sdk.monitor.azure.com

Entrabot never enables the distro's Azure Monitor export, and it switches off the distro's
own usage statistics and settings lookup by setting `MICROSOFT_OTEL_SDKSTATS_DISABLED`,
`APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL` and `APPLICATIONINSIGHTS_CONTROLPLANE_DISABLED`
to `true` before the distro starts. A value already in the environment is kept, so if you see
these connections, check that none of the three is set to `false` in the shell or `.env`.

### Console spans

`ENTRABOT_A365_CONSOLE_ENABLED=true` prints spans to the server's standard error (never
standard output, which is the MCP protocol channel). Look for them in the MCP host's server log.
