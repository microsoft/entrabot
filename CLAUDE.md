# CLAUDE.md — Openclaw Identity Research

> Root working context. Durable architecture lives in `docs/`.

## Non-Negotiables

- **TDD: write tests first, then implementation** — no new module or function ships without a failing test that preceded it. `pytest -v && ruff check .` must pass before every commit
- Security paths fail closed — if audit can't record, the action doesn't proceed
- Every agent resource access must be attributed to an Agent ID, never the human user
- Secrets and tokens never appear in logs — use `__repr__` overrides on sensitive fields
- Never redirect stderr to /dev/null — errors must always be visible for debugging
- Check every token response for `"error"` key before accessing `"access_token"` — Entra returns error dicts, not exceptions
- Never use `az rest` or Azure CLI tokens for Agent Identity APIs — they include `Directory.AccessAsUser.All` which causes hard 403
- Always create BlueprintPrincipal explicitly after Blueprint — it is NOT auto-created
- Agent IDs are service principals, not users — never create fake user accounts with passwords
- Parse `az` CLI output as JSON, not TSV — TSV can be corrupted by warnings

## Current Runtime Model

- Python 3.12+ research project — no deployed service yet
- Five modules: `platform/` (OS shim) → `auth/` (certificate JWT) → `tools/` (MCP tools) → `audit/` (tracking) → `mcp_server.py` (FastMCP + background channel)
- External dependencies: Microsoft Entra ID (identity), Microsoft Teams (communication via Graph API)
- Auth via three-hop Agent User flow: Blueprint (certificate) → Agent Identity (FIC) → Agent User (`user_fic` grant) — `httpx` direct, no MSAL at runtime
- Certificate auth: private key in OS keystore (Keychain/TPM/Keyring), JWT assertion for Hop 1 (ADR-003)
- Background channel: polls Teams every 5s, pushes via `notifications/claude/channel`
- All structured data uses `dataclasses` or `pydantic` — no raw dicts

## Active Work

- **Multi-tenant lightweight chat** — branch `feature/multi-tenant-lightweight-chat`. Full spec: `docs/architecture/NEXT-WhatsApp-lightweight-teams-chat.md`. Multi-tenant app + device code auth + progressive identity (start with human's delegated token, background-provision Agent User). Approved by Alice Example, Brandon, Alex.

## Read These First

- `docs/architecture/NEXT-WhatsApp-lightweight-teams-chat.md` (current active spec)
- `docs/index.md`
- `docs/engineering-status.md`
- `docs/runbooks/hard-won-learnings.md` (29 entries — read before making changes)
- `docs/decisions/001-obo-flows-for-device-agents.md`
- `docs/decisions/003-certificate-auth-over-client-secrets.md`
- `docs/platform-learnings/mcp-close-the-loop.md`

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Test + lint (run before every commit)
pytest -v --tb=short && ruff check .

# Test with coverage
pytest -v --cov=openclaw --cov-report=term-missing --cov-fail-under=80

# Single test
pytest tests/tools/test_teams.py::TestAcquireAgentUserToken::test_success -v

# Format
ruff format .

# Docs preview
pip install mkdocs-material && mkdocs serve
```

## High-Value Repo Areas

- `src/openclaw/platform/`: OS-specific credential storage — `CredentialStore` protocol with Mac/Linux/Windows implementations
- `src/openclaw/auth/`: Certificate-based JWT assertion builder — `build_client_assertion()`, `compute_cert_thumbprint()`
- `src/openclaw/tools/teams.py`: Three-hop token flow + Teams Graph API (send, read, filter, chat creation, add members cross-tenant)
- `src/entraclaw/mcp_server.py`: FastMCP server — 6 tools + background poll + channel push + token refresh
- `docs/decisions/`: ADRs — every significant architectural choice is recorded here
- `docs/runbooks/hard-won-learnings.md`: 29 hard-won learnings — READ THIS before making changes

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
