# `teardown-windows.ps1`

Windows PowerShell teardown. Reverses the **local** effects of
`setup-windows.ps1` on the current machine.

## Purpose

`teardown-windows.ps1` removes the Blueprint certificate and local state that
Windows setup leaves on a device: the Blueprint certificate(s) in the current
user's certificate store, the local data directory, the certificate lines in
`.env`, the local MSAL token cache, and the entrabot MCP server registration.

It is deliberately **local-only**. It does **not** contact the tenant and does
**not** delete the Entra app registrations (Blueprint, Agent Identity, or Agent
User). Those persist until you remove them from a macOS / Linux host or the
Entra admin portal (see [Related commands](#related-commands)).

## Requirements

- **PowerShell 7+ (`pwsh`).** The script refuses to run under Windows PowerShell
  5.1 and prints guidance on how to launch `pwsh` instead.
- Windows. The script throws if run on a non-Windows host.
- Run it from the repository so it can resolve `.env` and `.mcp.json` at the
  project root (the script derives the project root from its own path).

## Usage

```powershell
# Interactive — prompts before removing local state
.\scripts\teardown-windows.ps1

# Skip the confirmation prompt
.\scripts\teardown-windows.ps1 -Force

# Show detailed help
.\scripts\teardown-windows.ps1 -Help
```

## Options

| Option | Default | Effect |
| --- | --- | --- |
| `-Force` | off | Skip the `Remove all local entrabot state on this machine?` confirmation and proceed. |
| `-Help` | off | Print the script's detailed help and exit. |

## Effects

Unless `-Force` is set, the script first prompts for confirmation; any answer
other than `y`/`Y` prints `aborted.` and exits without changes.

When it proceeds, it removes, on the local machine only:

1. **Blueprint certificate(s).** Every certificate in `Cert:\CurrentUser\My`
   whose subject is `CN=entrabot-blueprint` is deleted (matched by subject
   rather than thumbprint, so a stale cached thumbprint cannot leave a cert
   behind).
2. **Local data directory.** `%LOCALAPPDATA%\entrabot\` is removed recursively.
   This is where the local MSAL token cache lives, so the cached tokens are
   cleared with it.
3. **Certificate lines in `.env`.** The `ENTRABOT_BLUEPRINT_CERT_THUMBPRINT`,
   `ENTRABOT_BLUEPRINT_CERT_SHA1`, and `ENTRABOT_BLUEPRINT_KSP` lines are
   stripped; the rest of `.env` is preserved.
4. **MCP registration.** The `entrabot` entry is removed from the project's
   `.mcp.json` and from Copilot's `mcp-config.json` (under `%COPILOT_HOME%`, or
   `%USERPROFILE%\.copilot\mcp-config.json` when that variable is unset).

## Safety

!!! warning "Entra app registrations are NOT deleted"
    This script only cleans the local device. The Blueprint, Agent Identity, and
    Agent User remain in the tenant. To remove the directory objects, use
    [`deprovision_entra_agent_identity.py`](deprovision-entra-agent-identity-py.md)
    (cross-platform) on any supported host,
    [`teardown.sh`](teardown-sh.md) for full teardown on macOS / Linux, or delete them
    in the Entra admin portal. Leaving them behind means the identity and its
    licenses continue to exist.

- Certificate removal is scoped strictly to the `CN=entrabot-blueprint` subject
  in the current user's store; other certificates are not touched.
- `.env` edits are line-scoped: only the three Blueprint certificate keys are
  removed, and all other configuration is kept.

## Exit behavior

- `1` — launched under Windows PowerShell 5.1 (the script exits before doing any
  work and tells you to use `pwsh`).
- `0` — the confirmation was declined, or teardown completed successfully.
- A non-Windows host causes the script to throw (non-zero) before any cleanup.

## Related commands

- [`deprovision_entra_agent_identity.py`](deprovision-entra-agent-identity-py.md) — delete the Entra app registrations this script leaves behind.
- [`teardown.sh`](teardown-sh.md) — full macOS / Linux teardown, including the tenant resources.
- [`cleanup-orphans.sh`](cleanup-orphans-sh.md) — remove Agent Identity / Blueprint orphans by object ID.
- [Teardown reference index](../index.md#teardown)
- [Identity Lifecycle and Deprovisioning](../../../guides/identity-lifecycle.md)
- [Getting started on Windows](../../../getting-started/windows.md)
- [Platform: Windows](../../../platform-docs/platform-windows.md)
