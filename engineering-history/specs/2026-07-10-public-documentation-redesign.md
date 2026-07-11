# Public Documentation Redesign

**Status:** Approved design
**Date:** 2026-07-10
**Scope:** Rebuild the published MkDocs site around shipped Entrabot functionality.

## Goal

The public documentation must read like a maintained product and engineering reference. It must explain what Entrabot does today, how to install and operate it, how its shipped architecture works, and how to troubleshoot it.

Plans, specs, implementation prompts, raw research notes, review transcripts, agent-authored working documents, and resolved investigation dossiers are not public documentation. Useful history will remain in the repository under `engineering-history/`, outside MkDocs' `docs_dir`, so it is neither published nor indexed by the documentation site.

## Problem

The current site mixes current documentation with historical implementation artifacts. Examples include:

- `architecture/PLAN-windows-port.md`, although Windows support is shipped;
- `architecture/next-mcp-server-design.md`, although the MCP server has long been implemented and materially changed;
- `claude-copilot-cli-channel-port.md`, which is an agent-authored porting proposal rather than neutral client documentation;
- `PLAN-*`, `DESIGN-*`, `SPEC-*`, and `NEXT-*` pages exposed as architecture;
- a "Research Snapshots" navigation group containing more plans and specs;
- "Runbooks" containing troubleshooting, migrations, raw investigation logs, and resolved issues;
- grouped script pages instead of one reference page per supported command.

Removing these pages from navigation alone is insufficient. MkDocs still publishes and indexes unlisted Markdown files under `docs/`.

## Chosen Approach

Use a curated public `docs/` tree and a non-published `engineering-history/` tree.

This is preferred over:

1. **Navigation-only cleanup:** rejected because stale pages remain published, searchable, and directly addressable.
2. **Deleting all history:** rejected because some implementation rationale and incident evidence remains useful to maintainers.

Pure duplicates, obsolete generated prompts, and superseded artifacts with no unique value may be deleted rather than archived.

## Target Public Navigation

### Home

- Product overview
- Current capabilities
- Supported platforms and hosts
- Project status and limitations
- Clear entry points into setup and reference documentation

### Getting Started

- Quickstart
- Prerequisites
- macOS and Linux installation
- Windows installation
- First identity and health verification

### Guides

- Configuration
- Teams and chat workflows
- Email workflows
- Files and Work IQ Word
- Storage configuration and migration
- Body and persona customization
- Identity lifecycle and deprovisioning

### Clients

- Host behavior overview
- Claude Code
- GitHub Copilot CLI
- Other MCP hosts
- Persona-sati integration

Client pages must be neutral and current. They must not contain author credits, competitor framing, personal names, raw planning dialogue, or dated capability matrices presented as current truth.

### Architecture

- System Overview
- Layers / Components
- Identity and Token Flow
- MCP Runtime
- Messaging and Channel Delivery
- Storage and Memory
- Security Boundaries
- Cross-Platform and Windows Architecture

Every architecture page describes the shipped system in present tense and is derived from current code and tests. Architecture pages may include a concise "Why this works this way" section, but must not expose raw plans or review transcripts.

### Platform Docs

Rename "Platform Learnings" to "Platform Docs". Keep only current external platform documentation directly relevant to Entrabot:

- Microsoft Entra Agent ID
- Agent Users
- Microsoft Agent 365 and Work IQ
- Microsoft Graph Teams
- Microsoft Graph Files
- Delegated MSAL authentication
- macOS, Linux, and Windows credential storage
- MCP host and transport constraints where they affect Entrabot

Historical Bot Framework, old OBO experiments, generic toolkit comparisons, and superseded platform research move to `engineering-history/`.

### Reference

- MCP tool catalog
- Configuration and environment variables
- Token flow
- Python API surfaces
- Script reference index
- One page per supported operator-facing command

### Troubleshooting

Replace the "Runbooks" label with task-oriented troubleshooting:

- Setup and authentication
- Teams and email
- Windows
- Storage
- MCP lifecycle and connectivity
- Migrations and upgrades

Public troubleshooting contains only current symptoms, causes, resolutions, and support status. Raw investigation timelines and resolved dossiers move to engineering history.

### Project

- Current status
- Changelog

Plans, specs, ADR indexes, developer QA logs, and documentation-maintenance notes are not top-level public navigation.

## New Canonical Pages

The redesign will create current pages rather than rename old plans:

| Canonical page | Source material to verify |
|---|---|
| `docs/architecture/identity-and-token-flow.md` | `src/entrabot/auth/`, `src/entrabot/identity/`, token tests, current platform docs |
| `docs/architecture/mcp-runtime.md` | `src/entrabot/mcp_server.py`, lifecycle tests, registered tool catalog |
| `docs/architecture/messaging-and-delivery.md` | Teams/email pollers, channel push, interaction log, host-gated waiting |
| `docs/architecture/storage-and-memory.md` | storage backends, operational state, persona boundary, ETag behavior |
| `docs/architecture/security-boundaries.md` | audit-first behavior, XPIA wrapping, sponsor gates, secret handling |
| `docs/architecture/windows-and-platforms.md` | Windows CNG signer, TPM/software KSP fallback, Unix credential stores |
| `docs/clients/overview.md` | current host-detection and delivery behavior |
| `docs/clients/claude-code.md` | current Claude Code setup and channel push |
| `docs/clients/copilot-cli.md` | current Copilot CLI setup and inline sponsor replies |
| `docs/clients/other-hosts.md` | portable MCP behavior and limitations |
| `docs/troubleshooting/mcp-connectivity.md` | current failure symptoms and resolved causes, not investigation transcript |

Existing pages may be retained only if they already satisfy the new public content standard.

## Historical Artifact Migration

Move useful artifacts outside `docs/` while retaining recognizable categories:

```text
engineering-history/
  architecture/
  decisions/
  investigations/
  plans/
  prompts/
  research/
  specs/
```

### Required migrations

| Current public artifact | Public replacement | Historical disposition |
|---|---|---|
| `architecture/PLAN-windows-port.md` | `architecture/windows-and-platforms.md` | Archive original plan |
| `architecture/next-mcp-server-design.md` | `architecture/mcp-runtime.md` | Archive original design |
| `claude-copilot-cli-channel-port.md` | `clients/overview.md`, host-specific client pages | Archive original porting plan |
| `architecture/NEXT-WhatsApp-lightweight-teams-chat.md` | current delegated-mode guide and identity architecture | Archive original plan |
| `architecture/PLAN-agent-identity-by-upn.md` | identity architecture and configuration reference | Archive original plan |
| `architecture/PLAN-xpia-content-wrapping.md` | `architecture/security-boundaries.md` | Archive original plan |
| `architecture/DESIGN-persona-sati-integration.md` | storage/memory architecture and persona-sati client guide | Archive original design |
| `architecture/DESIGN-multi-instance-cursor-consistency.md` | messaging architecture and troubleshooting | Archive original design |
| "Research Snapshots" pages | no direct public category | Archive |
| `AGENT-PROMPT-*`, `TODO-*`, Claude/OpenAI/Codex working notes | no public replacement unless functionality needs documentation | Delete duplicate prompts or archive unique history |
| ADR files | concise rationale in functional architecture pages | Archive originals under `engineering-history/decisions/` |
| `runbooks/mcp-disconnect-investigation.md` | current connectivity troubleshooting, if still useful | Archive investigation dossier |
| `runbooks/hard-won-learnings.md` | extracted current troubleshooting guidance | Archive append-only log |
| resolved security-debt documents | current security architecture or migration note, if applicable | Archive |

## Redirect Policy

Use `mkdocs-redirects` so previously published URLs lead to canonical current documentation rather than old planning text or unexplained 404 pages.

Required redirects include:

- `/architecture/PLAN-windows-port/` to the Windows/platform architecture page;
- `/architecture/next-mcp-server-design/` to the MCP runtime architecture page;
- `/claude-copilot-cli-channel-port/` to the client overview;
- `/architecture/NEXT-WhatsApp-lightweight-teams-chat/` to the delegated-mode guide or identity architecture;
- `/architecture/PLAN-agent-identity-by-upn/` to identity/configuration docs;
- `/architecture/PLAN-xpia-content-wrapping/` to security architecture.

Artifacts with no meaningful successor redirect to the nearest useful landing page.

## Script Documentation Policy

Public script documentation covers supported operator-facing commands only.

A command is operator-facing when it is intended for direct invocation and at least one of these is true:

- it is part of installation, status, deployment, teardown, or migration;
- README, setup output, or troubleshooting guidance tells operators to run it;
- it is a supported administrative or diagnostic command with a stable CLI.

Helper modules called only by another script, test fixtures, hooks, one-off spikes, and internal migrations are not public commands.

Each supported command receives its own page containing:

- purpose and support status;
- exact invocation and prerequisites;
- complete arguments and environment variables;
- side effects and resources touched;
- idempotency and retry behavior;
- output and exit codes;
- common failures and recovery;
- platform limitations;
- related commands.

Wrapper commands such as `setup-windows.cmd` receive a short dedicated page if they are directly supported entry points.

The implementation must audit every file in `scripts/` plus root-level operator commands such as `status.sh` and `status-windows.ps1`, then produce an explicit supported-command inventory. No grouped page may substitute for an individual supported script page.

## Public Content Standard

Every published page must:

- describe current behavior in present tense;
- use neutral repository voice;
- avoid personal names and agent-author attribution;
- avoid raw review comments, planning decisions, and competitor framing;
- use canonical terminology from current code;
- contain runnable, verified commands;
- distinguish shipped, optional, preview, and unsupported behavior;
- link to canonical current pages rather than historical artifacts;
- avoid brittle source line numbers and evergreen counts.

## Automated Safeguards

Add repository tests that fail when:

- a public Markdown filename starts with `PLAN-`, `SPEC-`, `DESIGN-`, `NEXT-`, `TODO-`, or `AGENT-PROMPT-`;
- public docs contain agent-attribution markers such as `author: Claude`, `written by Codex`, or `OpenAI:` working-note labels;
- a MkDocs navigation target does not exist;
- a documented script path does not exist;
- a supported operator command lacks its dedicated page;
- a required legacy redirect is missing;
- removed historical files remain under the public `docs/` tree.

The implementation may use existing Python, pytest, MkDocs, and PyYAML dependencies. It must not introduce a separate documentation framework.

## Validation

Before merge:

1. Run focused documentation-structure tests.
2. Run `mkdocs build --strict`.
3. Run the repository pytest and Ruff gates.
4. Inspect the generated navigation and search index for historical artifacts.
5. Verify required redirects locally.
6. After merge, confirm GitHub Pages deployment succeeds.
7. Crawl the major live landing pages and the named legacy URLs.

## Acceptance Criteria

- The public site contains current functional documentation only.
- No planning/spec/research artifact appears in navigation, search, or sitemap.
- Architecture pages explain the shipped system rather than how it was planned.
- Client pages are neutral, current, and free of personal or agent-author attribution.
- "Platform Docs" replaces "Platform Learnings".
- "Troubleshooting" replaces "Runbooks".
- Each supported operator-facing script has a dedicated reference page.
- Historical engineering material is preserved outside the published tree or deleted when duplicative.
- Legacy public URLs redirect to useful canonical replacements.
- Automated checks prevent the old structure from returning.
