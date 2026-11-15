# Prerequisites

Entrabot requires:

- A Microsoft 365 development tenant where you can create app registrations and grant admin consent
- A license that includes Teams and Outlook (E3 or E5 dev tenant licenses work)
- On macOS and Linux: Python 3.12 or newer, and a key store for the agent's sign-in key (the login keychain on a Mac; a Secret Service keyring such as GNOME Keyring or KWallet on Linux)

You do not need the Azure CLI, an `az login`, or Git first: setup offers to install a private Azure CLI, signs you in itself, and works from a downloaded ZIP of the repository.

On Windows you install nothing first. Skip this page and go to [Windows](windows.md): the setup script finds or downloads Python and builds the environment itself.

## Get the code

```bash
git clone https://github.com/microsoft/entrabot.git
cd entrabot
```

Or download the ZIP from <https://github.com/microsoft/entrabot> (Code > Download ZIP) and extract it.

### macOS and Linux

Nothing more to install by hand. `./scripts/setup.sh` creates `.venv` and installs Entrabot into it. If it finds no Python 3.12 or newer, it says how to install one: on a Mac, `brew install python@3.12` or [`./scripts/prereqs-macos.sh`](../reference/scripts/setup/prereqs-macos-sh.md); on Debian and Ubuntu, `sudo apt install python3.12 python3.12-venv`; on Fedora and RHEL, `sudo dnf install python3.12`. See [macOS and Linux](macos-linux.md).

### Windows

Nothing to do here. `scripts\setup-windows.cmd` creates `.venv` and installs Entrabot into it. See [Windows](windows.md).

## Development tools

Setup installs what the agent needs, not the test tools. To work on Entrabot's code, add them to the same environment after setup has run once.

On macOS and Linux:

```bash
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -v --tb=short
.venv/bin/python -m ruff check .
```

On Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Without setup, create the environment yourself first (`python3.12 -m venv .venv`).

## Next step

Continue to platform-specific setup: [macOS and Linux](macos-linux.md) or [Windows](windows.md).
