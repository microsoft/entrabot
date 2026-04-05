# Copilot Instructions — openclaw-identity-research

## Project Overview

Openclaw is a research project for securing agentic workflows on local devices (Mac/Linux/Windows) using Microsoft Entra Agent IDs and on-behalf-of (OBO) token flows. The goal is to bring cloud-style identity tracking to device-local agents — so sign-in and access logs distinguish **agent actions** from **human actions**, even when the agent operates with the user's permissions.

Key concepts:
- **Agent ID**: An identity issued to an autonomous agent (e.g., Copilot CLI) that distinguishes it from the human user
- **OBO (On-Behalf-Of)**: A token exchange flow where a human consents to let an agent act on their behalf; the resulting token is attributed to the agent
- **Platform abstraction**: OS-level integration (macOS, Linux, Windows) for agent identity lifecycle — creation, consent, token acquisition, and audit
- **Teams integration**: Agents connect to Teams as "Agent Users" with **bidirectional communication** — the agent uses Teams to message the human, and the human steers the agent back through Teams. Analogous to `gh copilot --remote`, which lets you steer a local Copilot session through GitHub's web UI.
- **Digital worker**: Another term for the agent identity. The local session holds a token for the digital worker, distinct from the human user's token.

Open research questions:
- What identity system replaces Live ID for agent-to-Teams auth at scale?
- How do you track agent actions across OSes with a universal audit store?
- Teams Graph API gaps: if the Graph API can't do something the Teams UX can, report it (Office is obligated to close gaps within 30 days).

## Tech Stack

- **Language**: Python 3.12+
- **Auth libraries**: `msal` (Microsoft Authentication Library) for token flows
- **Testing**: `pytest`
- **Linting**: `ruff`

## Commands

```bash
# Install dependencies
pip install -e ".[dev]"

# Run all tests
pytest

# Run a single test
pytest tests/test_foo.py::test_bar -v

# Lint
ruff check .

# Format
ruff format .
```

## Architecture

```
src/
  openclaw/
    platform/       # OS-specific agent identity (mac.py, linux.py, windows.py)
    auth/           # OBO token flows, Agent ID registration, consent
    audit/          # Action tracking / audit log abstraction
    teams/          # Teams "Agent User" integration
tests/              # Mirrors src/ structure
docs/               # Research notes, protocol designs, threat models
```

### Key patterns

- **Platform dispatch**: `platform/` modules expose a common interface (`AgentIdentityProvider`) with OS-specific implementations. Use `platform.system()` to select at runtime.
- **Token flow separation**: Auth code is split by flow type (device-code, OBO, client-credentials) — never mix flow logic in a single function.
- **Audit-first design**: Every agent action that touches a resource must emit an audit event before returning. Audit is not optional or deferred.

## Conventions

- Use `dataclasses` or `pydantic` models for all structured data — no raw dicts for tokens, audit events, or identity objects.
- Type-annotate all function signatures. Run `pyright` or `mypy` if available.
- Test files mirror source structure: `src/openclaw/auth/obo.py` → `tests/auth/test_obo.py`.
- Secrets and tokens never appear in logs. Use `repr` overrides on sensitive model fields.
- Research questions and design decisions go in `docs/`, not code comments.
