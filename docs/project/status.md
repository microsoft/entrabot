# Current Status

**Last updated:** July 12, 2026

!!! note "Research implementation"
    Entrabot is a research implementation, not a hosted production service. Run it against an isolated development tenant, and review every permission it requests before granting consent.

## Available today

### Identity and authentication

- Two authenticated session types: certificate-backed Agent User and MSAL delegated.
- Autonomous four-resource provisioning chain: Agent Identity Blueprint → BlueprintPrincipal → Agent Identity → Agent User.
- Blueprint, BlueprintPrincipal, and Agent Identity are created through the dedicated Microsoft Graph v1.0 subtype endpoints; Agent User creation uses Graph beta.
- Certificate private keys are stored in the OS keystore (Keychain, Windows CNG (via the certificate store), or Linux Secret Service).

### Microsoft 365

- Microsoft Teams messaging with explicit chat IDs, plus background chat discovery and polling.
- Outlook read and send, background polling, and daily summaries.
- Direct Microsoft Graph Files access alongside Agent 365 Work IQ integrations for Word and OneDrive/SharePoint (ODSP).

### Security and runtime

- Audit-first, fail-closed actions: if an action can't be recorded, it doesn't proceed.
- An authoritative external-content boundary wraps untrusted Teams, email, Files, and Work IQ content before it reaches the model.
- Host-gated sponsor reply behavior adapts to the calling client.
- A body-first prompt governs core behavior, with an optional persona layered on top.

### Storage and platforms

- Local operational storage by default, with Azure Blob Storage available as an opt-in.
- Setup and teardown flows for macOS, Linux, and Windows.
- Windows support is acceptance-tested on Windows 11 ARM64.

### Documentation and operations

- Public site with nine task-oriented sections and source-audited references for all registered MCP tools and all 42 supported operator commands.
- Legacy URLs redirect to current pages; plans, specs, investigations, and research are retained only under non-published `engineering-history/`.

## Active development

- MXC sandbox integration remains under review and is not part of the current `main` runtime.

## Known limitations

- Entrabot is a research implementation, not a production-ready tenant service.
- Agent User provisioning still depends on Microsoft Graph beta.
- Live smoke tests provision real Entra, Azure, and Microsoft 365 resources and send a real Teams message, so they run manually rather than in CI until an isolated tenant and non-human CI identity are available.
- Windows end-to-end coverage is narrower than macOS and currently centered on Windows 11 ARM64.
- Legacy Graph file comments are not the same as Word UI comments; use the Work IQ Word tools for document comments.
- External-content wrapping is defense in depth. It reduces instruction-injection risk but is not proof against every possible model jailbreak.
- `ENTRABOT_MODE` is validated but does not currently select the auth path in `_init_auth`; credential presence and `ENTRABOT_SKIP_PROVISIONING` determine which path (three-hop Agent User or MSAL delegated) is attempted.

## Project links

- Changelog: [`changelog.md`](changelog.md)
- GitHub issues: <https://github.com/microsoft/entrabot/issues>
- GitHub pull requests: <https://github.com/microsoft/entrabot/pulls>
- [Security policy](https://github.com/microsoft/entrabot/blob/main/SECURITY.md)
