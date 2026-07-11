# Prerequisites

Entrabot requires:

- A Microsoft 365 development tenant where you can create app registrations and grant admin consent
- A license that includes Teams and Outlook (E3 or E5 dev tenant licenses work)
- Python 3.12 or newer
- `git`

## Clone and install

```bash
git clone https://github.com/microsoft/entrabot.git
cd entrabot
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

## Verify the install

```bash
pytest -v --tb=short
ruff check .
```

Both commands must pass before you provision an Agent Identity.

## Next step

Continue to platform-specific setup: [macOS and Linux](macos-linux.md) or [Windows](windows.md).
