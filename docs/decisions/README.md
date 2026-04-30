# Architecture Decision Records

Every significant architectural choice in this project is recorded here as a numbered ADR. Newer decisions supersede older ones when a header row says so; the superseded file is kept for history.

| # | Decision | Status | Summary |
|---|----------|--------|---------|
| 001 | Use OBO flows for device-local agent identity | **Superseded by 002** | Original approach — agents authenticate via human device-code → OBO exchange |
| 002 | Agent User over OBO | **Accepted** | Agents authenticate autonomously via three-hop Agent User flow — no human in the loop |
| 003 | Certificate auth over client secrets | **Accepted** | Blueprint signs a JWT assertion with a private key stored in the OS keystore; no secrets on disk (ADR-003) |
| 005 | Cloud-hosted memory via Azure Blob Storage | **Accepted — Phases 1, 2, 5, 6a shipped** | Operational memory (interaction log, daily summaries, watched chats, email cursor) routes through a `MemoryBackend` protocol with `LocalBackend` and `BlobBackend` implementations. Blob is opt-in via `setup.sh --use-cloud-memory`. Remaining phases: 3 (interaction-log migration to ETag-concurrent writes), 4 (email cursor + watched chats migration), 6b (end-to-end persona continuity test), 7 (governance + retention) — see `005-cloud-hosted-memory.md` for the full phase plan. |

> **Note on numbering.** ADR 004 was reserved during planning but never landed as a separate decision — its scope was folded into ADR 005 Phase 5. The number is kept out of circulation to avoid confusion.
