# Quickstart

**Source:** <https://github.com/microsoft/entrabot>

Entrabot gives an autonomous agent its own Microsoft Entra Agent Identity and Agent User. The setup script provisions the identity chain, creates a certificate-backed credential, writes local configuration, and registers the MCP server.

> Entrabot is a research project. Use an isolated development tenant and review the generated permissions before relying on it with production data.

## macOS or Linux

Setup needs Python 3.12 or newer and nothing else first: no Azure CLI and no `az login`. Clone the latest release (or download its source code ZIP from <https://github.com/microsoft/entrabot/releases/latest>), then run setup from a terminal in it:

```bash
git clone --branch v0.3.1 https://github.com/microsoft/entrabot.git
cd entrabot
./scripts/setup.sh
```

This clones the latest release, v0.3.1. `main` is development and may be ahead of it. Git then prints a note about a "detached HEAD": that is normal when you check out a release tag, not an error. To upgrade an existing install, see [Upgrading to a new release](../troubleshooting/migrations-and-upgrades.md#upgrading-to-a-new-release).

Setup builds `.venv`, then asks a few questions (each has a suggested answer), signs you in to Microsoft, and creates the agent. To answer up front instead:

```bash
# Replace "workstation" with a short unique label for this Agent User.
./scripts/setup.sh --new --with-upn-suffix=workstation --yes
./scripts/setup.sh --use-blueprint=<blueprint-app-id> --yes
```

Use `--agent-user-upn=<existing-upn>` or `--with-upn-suffix=<label>` when the Blueprint has multiple Agent Users and auto-discovery would be ambiguous. Run `./scripts/setup.sh --help` for every option.

The agent's signing key is kept in your login keychain on a Mac, or a Secret Service keyring on Linux; setup checks that it can use it before it changes anything. On Ubuntu 24.04 the wizard has been run end to end against a real tenant; on macOS only a repair run and the health check have. See [macOS and Linux installation](macos-linux.md) for what setup asks, what it installs, and its limits.

## Windows

Nothing needs installing first. Download the latest release's source code ZIP or clone it, then double-click `scripts\setup-windows.cmd`, or run it from a terminal:

```bat
git clone --branch v0.3.1 https://github.com/microsoft/entrabot.git
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

The setup script registers Entrabot with supported MCP hosts. For Claude Code channel notifications, on macOS or Linux:

```bash
./start-entrabot.sh
```

It runs `claude --dangerously-load-development-channels server:entrabot` in the Entrabot folder. On Windows, double-click `start-entrabot.cmd` in the Entrabot folder. It runs the same command.

Then ask the host to call `whoami`. It should report the Agent User identity, not your human account.

## What runs by default

- On macOS, and on Linux with `bwrap` installed, the MCP server starts inside a sandbox that limits what it can read, write and connect to. Windows isn't contained. See [Process Containment](../guides/containment.md).
- The agent can email, invite or share files only with its sponsors, the people in the chat a request came from, and your own organization. Anyone else needs `ENTRABOT_OUTBOUND_ALLOW`. See [Configuration](../reference/configuration.md).
- Agent 365 integration (the agent's entry in the Agent 365 registry, and its telemetry) is off unless you switch it on at setup. The telemetry needs a Microsoft 365 E7 or Agent 365 licence in your organization.

## Next steps

- [Prerequisites](prerequisites.md)
- [macOS and Linux installation](macos-linux.md)
- [Windows installation](windows.md)
- [Setup script options](../reference/scripts/setup/setup-sh.md)
- [Token flow](../reference/token-flows.md)
- [Troubleshooting](../troubleshooting/index.md)
