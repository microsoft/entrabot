# Quickstart

**Source:** <https://github.com/microsoft/entrabot>

Entrabot gives an autonomous agent its own Microsoft Entra Agent Identity and Agent User. The setup script provisions the identity chain, creates a certificate-backed credential, writes local configuration, and registers the MCP server.

> Entrabot is a research project. Use an isolated development tenant and review the generated permissions before relying on it with production data.

## macOS or Linux

```bash
# 1. Clone the repository
git clone https://github.com/microsoft/entrabot.git
cd entrabot

# 2. Install platform prerequisites
./scripts/prereqs-macos.sh   # macOS
# Linux: install Python 3.12+, Azure CLI, git, and your Secret Service/keyring

# 3. Create a fresh identity chain
# Replace "workstation" with a short unique label for this Agent User.
./scripts/setup.sh --new --with-upn-suffix=workstation
```

To attach this device to an existing Blueprint instead:

```bash
./scripts/setup.sh --use-blueprint=<blueprint-app-id>
```

Use `--agent-user-upn=<existing-upn>` or `--with-upn-suffix=<label>` when the Blueprint has multiple Agent Users and auto-discovery would be ambiguous. Run `./scripts/setup.sh --help` for storage, Work IQ, migration, and status options.

## Windows

Use PowerShell 7 (`pwsh`), not Windows PowerShell 5.1:

```powershell
git clone https://github.com/microsoft/entrabot.git
cd entrabot
pwsh -File scripts/setup-windows.ps1 -NewChain -UpnSuffix workstation
```

For an existing Blueprint:

```powershell
pwsh -File scripts/setup-windows.ps1 -UseBlueprint <blueprint-app-id>
```

Windows setup prefers a TPM-backed CNG key and falls back to a software-protected key when TPM provisioning is unavailable. See the [full installation guide](https://github.com/microsoft/entrabot/blob/main/INSTALL.md) for prerequisites and troubleshooting.

## Verify the identity

```bash
./status.sh --health-only --strict
```

On Windows:

```powershell
pwsh -File status-windows.ps1 -HealthOnly -Strict
```

A healthy Agent User token has `idtyp=user`, the Agent User's `oid`, and Microsoft Graph as its audience. The Agent Identity and Agent User are separate objects and should both appear in status output.

## Start the MCP host

The setup script registers Entrabot with supported MCP hosts. For Claude Code channel notifications:

```bash
claude --dangerously-load-development-channels server:entrabot
```

Then ask the host to call `whoami`. It should report the Agent User identity, not your human account.

## Next steps

- [Installation and platform details](https://github.com/microsoft/entrabot/blob/main/INSTALL.md)
- [Setup script options](../reference/scripts/setup.md)
- [Token flow](../reference/token-flows.md)
- [Troubleshooting](../runbooks/hard-won-learnings.md)
