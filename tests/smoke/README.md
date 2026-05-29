# EntraBot E2E Smoke Test

`smokeit` is an opt-in, destructive integration smoke test. It provisions a fresh
Agent Identity chain and cloud memory storage, optionally exercises Teams or A365
Work IQ license paths, then tears all test resources down and verifies deletion.

It is intentionally outside normal `pytest` because it creates real Entra,
Azure, and Microsoft 365 resources.

## What It Exercises

1. `scripts/setup.sh --new` with an explicit test Agent User UPN.
2. Azure Blob storage provisioning with a test storage account/container.
3. `scripts/setup.sh --status --health-only` and `--json`.
4. Agent User token acquisition through the three-hop flow.
5. Optional Teams message send, using either an existing chat ID or a sponsor UPN.
6. Optional Teams reply polling, which waits for a human response after send.
7. Optional A365 Work IQ/Copilot license assignment path.
8. `scripts/teardown.sh --agent-user-upn=...` targeted teardown.
9. Graph deletion verification for Agent User, Agent Identity, and Blueprint.
10. Azure storage account deletion and verification.
11. Local `.env`, `.entrabot-state.json`, and `entrabot/blueprint-private-key`
   restoration after the run.

## Required Flag

The runner refuses to start unless you pass:

```bash
--confirm-destroy-test-resources
```

By default, the smoke run tests identity provisioning and storage only. It does
not assign Teams-capable or Microsoft 365 Copilot licenses unless the matching
feature gate is enabled:

- `--test-teams` — assign/use a Teams-capable license and send a Teams message.
- `--wait-for-teams-reply` — with `--test-teams`, poll until a human reply
  arrives. This is intentionally opt-in because it blocks until someone answers
  or `--teams-reply-timeout` expires.
- `--test-a365` — enable the Work IQ/Copilot license assignment path.

## Unix/macOS

Identity + storage only, with no Teams-capable or Copilot license requirement:

```bash
tests/smoke/smokeit.sh \
  --confirm-destroy-test-resources \
  --agent-user-upn smoketest-agent@yourtenant.onmicrosoft.com
```

Teams validation with a sponsor-created 1:1 chat:

```bash
tests/smoke/smokeit.sh \
  --confirm-destroy-test-resources \
  --agent-user-upn smoketest-agent@yourtenant.onmicrosoft.com \
  --test-teams \
  --sponsor-upn human@yourtenant.com
```

Teams send plus reply polling, useful when a human is available to answer the
smoke message:

```bash
tests/smoke/smokeit.sh \
  --confirm-destroy-test-resources \
  --agent-user-upn smoketest-agent@yourtenant.onmicrosoft.com \
  --test-teams \
  --sponsor-upn human@yourtenant.com \
  --wait-for-teams-reply \
  --teams-reply-timeout 300
```

Or send into a known chat instead of creating/discovering a 1:1 chat:

```bash
tests/smoke/smokeit.sh \
  --confirm-destroy-test-resources \
  --agent-user-upn smoketest-agent@yourtenant.onmicrosoft.com \
  --test-teams \
  --chat-id '19:...'
```

## Windows

```powershell
.\tests\smoke\smokeit.ps1 `
  --confirm-destroy-test-resources `
  --agent-user-upn smoketest-agent@yourtenant.onmicrosoft.com `
  --test-teams `
  --sponsor-upn human@yourtenant.com
```

## Logs

Every run writes a self-contained log bundle under:

```text
tests/smoke/logs/<UTC timestamp>/
```

Useful files:

- `smoke-inputs.json` — resolved CLI inputs.
- `smoke-summary.json` — step list, command logs, cleanup status, failure text.
- `NN-<step>.log` — full stdout/stderr for each command.
- `state-after-setup.json` — resource IDs captured before teardown.
- `teams-message.json` — chat/message IDs from the Teams send step.
- `teams-reply.json` — reply polling result when `--wait-for-teams-reply` is enabled.
- `graph-deletion-verification.json` — post-teardown Graph verification.

On failure the runner prints the failing log path and the last 80 log lines.
That is meant to be enough context for a human or LLM agent to resume debugging
without manually navigating the repo.

## Failure Cleanup

By default, the runner attempts best-effort teardown and storage deletion after a
failure, then restores local state. To keep cloud resources for forensic triage:

```bash
--keep-resources-on-failure
```

Local state is still restored so your normal development environment is not left
pointing at the smoke-test identity chain.
