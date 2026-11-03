# Current Status

**Last updated:** November 2, 2026

!!! note "Research implementation"
    Entrabot is a research implementation, not a hosted production service. Run it against an isolated development tenant, and review every permission it requests before granting consent.

## Available today

### Identity and authentication

- Two authenticated session types: certificate-backed Agent User and MSAL delegated.
- Autonomous four-resource provisioning chain: Agent Identity Blueprint → BlueprintPrincipal → Agent Identity → Agent User.
- Blueprint, BlueprintPrincipal, and Agent Identity are created through the dedicated Microsoft Graph v1.0 subtype endpoints; Agent User creation uses Graph beta.
- Certificate private keys are stored in the OS keystore (Keychain, Windows CNG (via the certificate store), or Linux Secret Service).
- Several machines can share a Blueprint: attaching one adds its certificate and keeps the others. `setup.sh --remove-all-existing-certs` is the only way to replace them all. Windows setup registers its certificate the same way, and re-running it registers a certificate an earlier version left unregistered.

### Microsoft 365

- Microsoft Teams messaging with explicit chat IDs, plus background chat discovery and polling.
- Outlook read and send, background polling, and daily summaries.
- Direct Microsoft Graph Files access alongside Agent 365 Work IQ integrations for Word, Excel, and OneDrive/SharePoint (ODSP).
- Bounded, read-only Excel worksheet and range reads through the Graph Workbook API (`list_workbook_worksheets`, `read_workbook_range`). Run against a live tenant on an `.xlsx` in the Agent User's own OneDrive; not yet exercised on a file shared by someone else or on `.xlsm`.

### Security and runtime

- Audit-first, fail-closed actions: if an action can't be recorded, it doesn't proceed.
- An authoritative external-content boundary wraps untrusted Teams, email, Files, and Work IQ content before it reaches the model.
- Host-gated sponsor reply behavior adapts to the calling client.
- A body-first prompt governs core behavior, with an optional persona layered on top.

### Storage and platforms

- Local operational storage by default, with Azure Blob Storage available as an opt-in.
- With Blob Storage, all shared operational state, including the watched-chat list and email cursor, lives in the container. Instances on one container share it, and exactly one instance pushes a given email.
- Setup and teardown flows for macOS, Linux, and Windows.
- Windows support is acceptance-tested on Windows 11 ARM64.

### Documentation and operations

- Public site with nine task-oriented sections and source-audited references for all registered MCP tools and all 45 supported operator commands.
- Legacy URLs redirect to current pages; plans, specs, investigations, and research are retained only under non-published `engineering-history/`.

## Active development

- MXC sandbox integration remains under review and is not part of the current `main` runtime.

## Known limitations

- Entrabot is a research implementation, not a production-ready tenant service.
- Agent User provisioning still depends on Microsoft Graph beta.
- Live smoke tests provision real Entra, Azure, and Microsoft 365 resources and send a real Teams message, so they run manually rather than in CI until an isolated tenant and non-human CI identity are available.
- Windows end-to-end coverage is narrower than macOS and currently centered on Windows 11 ARM64.
- Legacy Graph file comments are not the same as Word UI comments; use the Work IQ Word tools for document comments.
- Work IQ Excel (`mcp_ExcelServer`) is in the live Agent 365 catalog but not on Microsoft Learn, and the CLI warns of a "legacy ATG audience", so its tools may change. It requires `McpServers.Excel.All` on the Blueprint.
- `ENTRABOT_FILES_DENIED_SITES` is checked against Graph's reported site only for the workbook and Work IQ Excel tools. The Work IQ Word and ODSP tools don't apply it, and `read_file` and `add_file_comment` check the `site_id` the caller supplies.
- Excel writes, table reads, and workbook sessions aren't implemented ([#117](https://github.com/microsoft/entrabot/issues/117)).
- External-content wrapping is defense in depth. It reduces instruction-injection risk but is not proof against every possible model jailbreak.
- On a cloud backend, the email cursor starts at "now" when its key is first created (the first boot on a container, or after the key is deleted), so mail received before then isn't pushed; it stays readable through `read_email`. Once the key exists, a restart reuses it and mail received during downtime is pushed as normal. See [Upgrading to shared poll state](../troubleshooting/migrations-and-upgrades.md#upgrading-to-shared-poll-state).
- An instance on a host that ignores channel notifications can still claim Teams and email deliveries, so with mixed hosts on one container an item can go to the host that can't show it.
- Two emails with the same timestamp, where the second arrives after the cursor has moved past it, won't be pushed. Both stay readable through `read_email`.
- The shutdown save stops waiting after 10 seconds, but a storage call already in flight can still delay process exit until the storage client's own timeout.
- A batch of email held because its claim couldn't be confirmed lives only in memory, so restarting before the claim settles loses it. The mail stays readable through `read_email`.
- Chats are polled one at a time and each read sees the latest 10 messages, so a very busy chat can have messages pushed past that window between visits. They stay readable through `read_teams_messages`.
- Delegated sessions have no discovery sweep, so with a cloud backend their chat list only grows through `create_chat`.
- A wait keeps the sponsor gate it started with, so a gate built during a brief storage or Graph failure can miss a federated sponsor until the next wait.
- Failed watched-list writes retry on the next discovery sweep and at shutdown, so in a delegated session a failed write can wait until shutdown. Discovery re-adds the chat in an Agent User session.
- If the sponsor lookup fails, a sponsor's Teams push arrives in the XPIA envelope like anyone else's until a later lookup succeeds, so the agent may ask them to restate an instruction. Someone removed as a sponsor can keep speaking outside the envelope for up to 15 minutes, until the cached list refreshes.
- Adding a Blueprint certificate reads the list and then writes it, and those two steps aren't atomic. Two machines attaching in the same moment can lose one of the new certificates; the affected machine's setup reports it, and re-running setup there adds it again. A per-user lock keeps two setups on the same machine from overlapping, but it can't cover two machines.
- `ENTRABOT_MODE` is validated but does not currently select the auth path in `_init_auth`; credential presence and `ENTRABOT_SKIP_PROVISIONING` determine which path (three-hop Agent User or MSAL delegated) is attempted.

## Project links

- Changelog: [`changelog.md`](changelog.md)
- GitHub issues: <https://github.com/microsoft/entrabot/issues>
- GitHub pull requests: <https://github.com/microsoft/entrabot/pulls>
- [Security policy](https://github.com/microsoft/entrabot/blob/main/SECURITY.md)
