# Openclaw Identity Research

> TSA bug filing file has been configured: [tsaoptions.json](.config/tsaoptions.json). Official builds are required to have TSA bug filing enabled by default. [Learn more](https://aka.ms/OBTSA)

## Introduction

Research project for securing agentic workflows on local devices (Mac/Linux/Windows) using Microsoft Entra Agent IDs and on-behalf-of (OBO) token flows. Agents get their own identity so audit logs always distinguish agent actions from human actions, and communicate bidirectionally with humans through Microsoft Teams.

## Getting Started

### Prerequisites

- Azure CLI (`az`) logged in with admin access to your Entra tenant
- Python 3.12+
- Git

### One-Command Setup

```bash
./scripts/setup.sh
```

This script will:

1. Create an Entra app registration with the correct permissions
2. Set up Graph API scopes for Teams integration
3. Install Python dependencies
4. Save configuration to `.env`
5. Run tests to verify everything works

The script is **idempotent** — safe to re-run at any time. It detects existing resources and skips creation.

### Run the MCP Server

After setup, add Openclaw to your Copilot CLI config:

```jsonc
// Add to ~/.copilot/mcp-config.json
{
  "mcpServers": {
    "openclaw": {
      "command": "python3.12",
      "args": ["-m", "openclaw.mcp_server"],
      "cwd": "/path/to/openclaw-identity-research",
      "env": {}
    }
  }
}
```

The server loads configuration from `.env` automatically.

Then launch Copilot CLI:

```bash
copilot
```

And use the tools:

- `openclaw_bootstrap` — authenticate and get an agent identity
- `openclaw_teams_connect` — connect to Teams
- `openclaw_teams_send` — send a message
- `openclaw_audit_log` — record an audit event

### Manual Setup (without Azure)

If you just want to run the code and tests without an Entra tenant:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -v
```

### Teardown

To remove the Entra app registration and all cached credentials:

```bash
./scripts/teardown.sh
```

### Software Dependencies

- Python 3.12+
- `msal` (Microsoft Authentication Library) for token flows
- `pytest` for testing, `ruff` for linting

### Documentation

```bash
pip install mkdocs-material
mkdocs serve
```

Open http://localhost:8000 — or see [docs/index.md](docs/index.md) for a reading guide.

## Architecture

Four modules handle the agent identity lifecycle on Mac/Linux/Windows:

- **platform/** — OS-specific agent identity (keychain, credential storage, consent UX)
- **auth/** — OBO token exchange with Microsoft Entra, Agent ID registration
- **audit/** — Action tracking — every resource access emits an audit event before executing
- **teams/** — Bidirectional Teams communication (agent ↔ human via Graph API)

## Build and Test

```bash
# Run all tests
pytest -v

# Run a single test
pytest tests/auth/test_obo.py::test_token_exchange -v

# Lint
ruff check .

# Format
ruff format .
```

## Repository Map

| Directory | Purpose |
|-----------|---------|
| `src/openclaw/` | Application source code |
| `tests/` | Test suite (mirrors `src/` structure) |
| `docs/` | Documentation site (MkDocs Material) |
| `docs/platform-learnings/` | Deep research on all integration platforms |
| `docs/proposals.md` | Architecture proposals (9 proposals across 3 OSes) |
| `scripts/` | Setup and teardown automation |
| `.github/` | CI workflows and Copilot instructions |

## Contribute

See [owners.txt](owners.txt) for code owners. All changes to protected branches require approval from at least one listed owner.
