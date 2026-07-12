# Engineering TODOs

**Last reviewed:** 2026-07-10
**Current status:** [`docs/project/status.md`](docs/project/status.md)

Keep this file limited to actionable work. Completed design history belongs in ADRs, changelog entries, or architecture plans marked **Shipped**.

## In progress / merge queue

- [ ] **MXC sandbox integration (PR #86).** Rebase against current `main`, resolve conflicts, rerun the full test/lint gate in a worktree-local virtual environment, and update runtime docs only after merge.
- [ ] **Windows command-injection hardening (PR #76).** Re-evaluate the diff against current Windows process-launch helpers and either update and merge it or close it with a superseding reference.
- [ ] **Long-session MCP disconnect.** Continue from `docs/runbooks/mcp-disconnect-investigation.md`; do not restart the investigation without incorporating the existing evidence.

## Reliability backlog

- [ ] **Script-toolkit documentation closeout.** Keep Unix and Windows setup/status/teardown references synchronized with script help and actual cleanup behavior.
- [ ] **Blob environment test isolation.** Prevent host `ENTRABOT_BLOB_*` values from leaking into tests while preserving fail-closed half-configuration checks.
- [ ] **MCP server orphan cleanup.** Ensure abnormal host termination does not leave duplicate background pollers or stale child processes.
- [ ] **Daily-summary scheduler fixes.** Make timezone/day-boundary behavior deterministic and keep retries idempotent.
- [ ] **Email cursor precision.** Advance cursors without skipping same-timestamp messages or replaying already-delivered mail.
- [ ] **Long-running persona-sati authentication.** Replace restart-based recovery after bearer expiry without trying to make an Agent Blueprint act as an OAuth public client.
- [ ] **Reconcile `ENTRABOT_MODE` with auth selection.** `_init_auth` selects the auth path from credential presence and `ENTRABOT_SKIP_PROVISIONING`, not from `ENTRABOT_MODE`. Either make `ENTRABOT_MODE` drive session-type selection (with tests covering `agent_user`, `delegated`, and `auto`) or remove/deprecate the setting and its documentation.

## Platform and security follow-ups

- [ ] **Broaden Windows acceptance coverage.** Exercise Intel x64, non-TPM fallback, certificate rotation, teardown, and long-running polling in addition to the existing Windows 11 ARM64 path.
- [ ] **Automated live E2E design.** Only enable hosted smoke tests after provisioning a dedicated isolated tenant, non-human federated CI identity, deterministic cleanup, and explicit cost/permission ownership.
- [ ] **Conditional Access and agent-governance validation.** Expand the reference tenant matrix for Agent User CA, ID Protection, and least-privilege permission policies.
- [ ] **Reassess the Windows CNG ctypes signer after operational use.** Replace it with a small managed helper only if ABI maintenance becomes a demonstrated problem.

## Recently shipped

- [x] Rename-safe self/peer matching with canonical `ENTRABOT_AGENT_UPN` and object-ID fallback.
- [x] Boundary-owned XPIA wrapping for Teams, email, Files, and Work IQ content, including forged-envelope regression coverage.
- [x] Bot Gateway removal and Graph-native Teams architecture (ADR-006).
- [x] Windows setup, CNG signing, status, teardown, and deterministic cross-platform tests.
- [x] ADR-005 storage phases 1, 2, 5, and 6a: backends, Blob provisioning, migration, and persona integration.
- [x] Provisioning-secret migration to certificate credentials with legacy password cleanup.

## Maintenance rule

When a change materially moves work between backlog, in progress, and shipped, update this file and `docs/project/status.md` in the same pull request. Avoid hard-coded total test or learning counts in evergreen instructions.
