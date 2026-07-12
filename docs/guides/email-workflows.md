# Email Workflows

Entrabot polls the Agent User's mailbox for inbound email, surfaces
substantive messages as channel notifications, and can generate a daily
triage summary. This guide covers the background poll, the read/send tools,
and the daily summary scheduler.

## Background polling

Background email polling starts only in Agent User (`agent_user`) mode —
email tools target the Agent User's own mailbox via `/me/messages`, and
running the poll in `delegated` mode would target the human sponsor's
mailbox instead, which is not the intent.

The poll runs every 60 seconds. On first run it initializes its cursor to
the current time rather than the oldest message in the mailbox, so boot
doesn't flood the agent with historical mail.

Each cycle:

- Filters out known noise: Teams notification mail, M365 marketing senders,
  and no-reply/donotreply addresses.
- Skips Sent Items echoes of the agent's own outbound mail, and deduplicates
  messages so the same email is not surfaced twice.
- Detects Purview-encrypted mail via a `message.rpmsg` attachment and
  reports it as inaccessible without IRM decryption, rather than trying to
  read an encrypted body.

Substantive messages that pass the filters become channel notifications
(delivered the same way as Teams messages) and get an entry in the
interaction log, which the daily summary later reads.

## Reading and sending

- **`read_email`** fetches the full body, recipients, headers, and an
  attachment-present flag for a given message. The body is wrapped in an
  authoritative external-content boundary before it reaches the model, so
  it's treated as data rather than trusted instructions — the same treatment
  given to Teams and file content.
- **`send_email`** supports both new outbound mail and replies — replying
  uses the Graph reply endpoint so the thread is preserved rather than
  starting a new conversation. Sends are attributed to the Agent User's
  identity through the audit/tool layer.
- **`scripts/read_email.py`** is a standalone subject-search utility that
  runs outside the MCP server, independent of the poll/notification
  pipeline. See
  [`read_email.py` reference](../reference/scripts/operations/read-email-py.md).

## Daily summaries

The daily summary scheduler, like the email poll, starts only in Agent User
mode. It wakes once a day at 5:00 PM at a fixed UTC-7 offset (PDT) and
triages the interaction log into three buckets:

- **Needs you** — inbound messages without a same-thread reply from the
  agent yet.
- **Handled** — threads where the agent already replied to an inbound
  message.
- **Heads-up** — outbound messages the agent sent without a prior inbound
  on that thread (the agent reached out first).

The rendered summary (HTML) and a JSON sidecar with per-bucket counts are
archived through the configured storage backend regardless of whether an
email is sent.

Automatic sending requires at least one address configured in
`ENTRABOT_HUMAN_USER_MAILS`; the first address in that list is treated as
the primary sponsor and used as the recipient. If no sponsor mail address is
configured, the summary is still archived but no email goes out. See
[Configuration](configuration.md) for the full list of sponsor-related
environment variables.

## See also

- [MCP Tools Reference](../reference/mcp-tools.md)
- [Troubleshooting: Teams and Email](../troubleshooting/teams-and-email.md)
