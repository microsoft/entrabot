# Prerequisites

Entrabot requires:

- A Microsoft 365 development tenant where you can create app registrations and grant admin consent
- A license that includes Teams and Outlook (E3 or E5 dev tenant licenses work)
- On macOS and Linux: Python 3.12 or newer, and `git`

On Windows you install nothing first. Skip this page and go to [Windows](windows.md): the setup script finds or downloads Python and builds the environment itself.

## Clone and install

### macOS and Linux

```bash
git clone https://github.com/microsoft/entrabot.git
cd entrabot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

### Windows

Nothing to do here. `scripts\setup-windows.cmd` creates `.venv` and installs Entrabot into it. See [Windows](windows.md).

To work on Entrabot's code on Windows, add the development tools to that environment after setup has run once:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Verify the install

On macOS and Linux:

```bash
pytest -v --tb=short
ruff check .
```

Both commands must pass before you provision an Agent Identity.

## Next step

Continue to platform-specific setup: [macOS and Linux](macos-linux.md) or [Windows](windows.md).
