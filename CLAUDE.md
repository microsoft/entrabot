# CLAUDE.md — Openclaw Identity Research

> Root working context. Durable architecture lives in `docs/`.

## Non-Negotiables

- Security paths fail closed — if audit can't record, the action doesn't proceed
- Every agent resource access must be attributed to an Agent ID, never the human user
- Secrets and tokens never appear in logs — use `__repr__` overrides on sensitive fields
- Test before committing — `pytest -v && ruff check .`
- Token flows are separated by type — never mix OBO, device-code, and client-credentials logic

## Current Runtime Model

- Python 3.12+ research project — no deployed service yet
- Four modules: `platform/` (OS shim) → `auth/` (OBO/Agent ID) → `audit/` (tracking) → `teams/` (Agent User)
- External dependencies: Microsoft Entra ID (identity), Microsoft Teams (communication via Graph API)
- Auth via `msal` library — OBO token exchange is the core flow
- All structured data uses `dataclasses` or `pydantic` — no raw dicts

## Read These First

- `docs/index.md`
- `docs/getting-started/quickstart.md`
- `docs/architecture/system-overview.md`
- `docs/reference/obo-flows.md`
- `docs/decisions/001-obo-flows-for-device-agents.md`

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Test + lint
pytest -v && ruff check .

# Single test
pytest tests/auth/test_obo.py::test_token_exchange -v

# Format
ruff format .

# Docs preview
pip install mkdocs-material && mkdocs serve
```

## High-Value Repo Areas

- `src/openclaw/platform/`: OS-specific agent identity — `AgentIdentityProvider` protocol with Mac/Linux/Windows implementations
- `src/openclaw/auth/`: OBO token exchange, Agent ID registration, consent — one module per flow type
- `src/openclaw/audit/`: Audit-first enforcement — events emitted before actions execute
- `src/openclaw/teams/`: Bidirectional Teams communication via Graph API
- `docs/decisions/`: ADRs — every significant architectural choice is recorded here

## gstack

This project uses gstack for enhanced AI workflows. **Use `/browse` for all web browsing — never use `mcp__claude-in-chrome__*` tools.**

### Available skills

`/office-hours`, `/plan-ceo-review`, `/plan-eng-review`, `/plan-design-review`, `/design-consultation`, `/design-shotgun`, `/design-html`, `/review`, `/ship`, `/land-and-deploy`, `/canary`, `/benchmark`, `/browse`, `/connect-chrome`, `/qa`, `/qa-only`, `/design-review`, `/setup-browser-cookies`, `/setup-deploy`, `/retro`, `/investigate`, `/document-release`, `/codex`, `/cso`, `/autoplan`, `/plan-devex-review`, `/devex-review`, `/careful`, `/freeze`, `/guard`, `/unfreeze`, `/gstack-upgrade`, `/learn`

### Troubleshooting

If gstack skills aren't working, rebuild:

```bash
cd .claude/skills/gstack && ./setup
```

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
- Save progress, checkpoint, resume → invoke checkpoint
- Code quality, health check → invoke health
