# Current Status

**Last updated:** July 11, 2026

!!! note "Research implementation"
    Entrabot is a research implementation, not a hosted production service. Run it against an isolated development tenant, and review every permission it requests before granting consent.

## Available today

### Identity and authentication

- Two runtime modes: certificate-backed Agent User mode and MSAL delegated mode.
- Autonomous four-resource provisioning chain: Agent Identity Blueprint → BlueprintPrincipal → Agent Identity → Agent User.
- Blueprint, BlueprintPrincipal, and Agent Identity are created through the dedicated Microsoft Graph v1.0 subtype endpoints; Agent User creation uses Graph beta.
- Certificate private keys are stored in the OS keystore (Keychain, Windows Certificate Store, or Linux Secret Service).

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

## Active development

- MXC sandbox integration remains under review and is not part of the current `main` runtime.

## Known limitations

- Entrabot is a research implementation, not a production-ready tenant service.
- Agent User provisioning still depends on Microsoft Graph beta.
- Live smoke tests provision real Entra, Azure, and Microsoft 365 resources and send a real Teams message, so they run manually rather than in CI until an isolated tenant and non-human CI identity are available.
- Windows end-to-end coverage is narrower than macOS and currently centered on Windows 11 ARM64.
- Legacy Graph file comments are not the same as Word UI comments; use the Work IQ Word tools for document comments.
- External-content wrapping is defense in depth. It reduces instruction-injection risk but is not proof against every possible model jailbreak.

## Project links

- Changelog: [`changelog.md`](changelog.md)
- GitHub issues: <https://github.com/microsoft/entrabot/issues>
- GitHub pull requests: <https://github.com/microsoft/entrabot/pulls>
- [Security policy](../../SECURITY.md)
