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

Nothing needs installing first. Download or clone the repository, then double-click `scripts\setup-windows.cmd`, or run it from a terminal:

```bat
git clone https://github.com/microsoft/entrabot.git
cd entrabot
scripts\setup-windows.cmd
```

Setup asks a few questions (each has a suggested answer), signs you in to Microsoft, and creates the agent. To answer up front instead:

```bat
scripts\setup-windows.cmd -NewChain -UpnSuffix workstation -Yes
scripts\setup-windows.cmd -UseBlueprint <blueprint-app-id> -Yes
```

The agent's signing key is created in the PC's TPM when the TPM accepts it, and in the Windows software key store otherwise. Either way it cannot be exported. See [Windows installation](windows.md) for what setup asks, what it installs, and its limits.

## Verify the identity

```bash
./status.sh --health-only --strict
```

On Windows:

```bat
scripts\setup-windows.cmd -Diagnose
```

A healthy Agent User token has `idtyp=user`, the Agent User's `oid`, and Microsoft Graph as its audience. The Agent Identity and Agent User are separate objects and should both appear in status output.

## Start the MCP host

The setup script registers Entrabot with supported MCP hosts. For Claude Code channel notifications:

```bash
claude --dangerously-load-development-channels server:entrabot
```

On Windows, double-click `start-entrabot.cmd` in the Entrabot folder. It runs the same command.

Then ask the host to call `whoami`. It should report the Agent User identity, not your human account.

## Next steps

- [Prerequisites](prerequisites.md)
- [Windows installation](windows.md)
- [Setup script options](../reference/scripts/setup/setup-sh.md)
- [Token flow](../reference/token-flows.md)
- [Troubleshooting](../troubleshooting/index.md)
