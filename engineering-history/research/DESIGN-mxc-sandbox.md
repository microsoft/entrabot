# DESIGN: MXC Execution-Container Integration (`entrabot/sandbox/`)

**Date:** 2026-06-08
**Status:** Planned (engineer review + outside-voice review complete; hardened). Phase 1 approved to build; Phase 2 stubbed + documented. `run_code` is operator-opt-in and **off by default**.
**Author:** plan-eng-review (engineer review)
**Research input:** `docs/platform-learnings/mxc-windows-sandbox.md`
**Supersedes:** the `AppContainer sandbox production implementation` item in `TODOS.md` (the prior CEO-review premise "sandbox co-equal with identity"). MXC's `processcontainer` backend *is* AppContainer/BaseContainer — cross-platform and MS-maintained.

---

## Problem statement

entrabot makes a device-local agent a first-class **identity** (Entra Agent User) with honest **attribution**, but its *local execution* has **no containment** — any code the agent runs inherits the human's full session authority. MXC (Microsoft Execution Containers, Build 2026) is the OS-enforced containment layer that closes this gap. Pairing the two delivers the full Build-2026 secure-agent story: **identity + attribution (entrabot) + containment (MXC)**.

entrabot today runs **no** local untrusted code — every `@mcp.tool()` is a Graph API call; the only `subprocess` is the operator-configured persona-sati token command. So this is not a "wrap what exists" change: it **adds a new, contained local-execution capability** (`run_code`, off by default). Phase-1 "attribution" is entrabot-side audit bookkeeping; OS-level Entra-bound session identity is Phase 2.

---

## Scope (locked in review)

- **Phase 1 — build now (Option A):** contained local execution via MXC **process isolation**, on **macOS** (Seatbelt backend) and **Windows** (`processcontainer`/AppContainer→BaseContainer), driven from Python via the MXC **native binary + JSON policy** (the language-agnostic surface; the MXC SDK is TypeScript-only).
- **Phase 2 — stub + document now, build later (Option C):** Entra-bound, Intune-governed **session isolation**. Not generally available (see *What's not buildable yet*). We ship the seams (enum value, `session.py` stub, identity-binding hook, ADR, this doc) so a future contributor/LLM can complete it when the platform surfaces GA.

### NOT in scope (explicitly deferred)
- **Session isolation / Entra-binding implementation** — Phase 2; platform surfaces not GA (below).
- **Micro-VM, WSL Linux-container, Windows 365-for-Agents backends** — Microsoft roadmap, not shipped.
- **Sandboxing entrabot's own Graph tools** — they're already identity-scoped server-side; containing them adds no value.
- **Full Developer-ID notarization / EV signing** — Phase-1 uses local self-sign (dev runs it themselves); revisit for end-user distribution.

---

## What's not buildable yet (cite for stakeholders)

The richer "session isolation attributed to an Entra agent identity, governed by Intune" story is **announced, not GA**:

- **Entra/Intune binding is future-tense** — Windows Developer Blog, *"Windows platform security for AI agents"* (2026-06-02): "Agent 365's policy-based controls with Microsoft Entra and Intune **will be used** to apply those MXC constraints to a specific agent." And "Our initial release **will support non-interactive sessions** with additional capabilities **targeted for future releases**." URL: `https://blogs.windows.com/windowsdeveloper/2026/06/02/windows-platform-security-for-ai-agents/`
- **Session isolation is Windows-Insider-only** — MXC SDK platform table lists `isolation_session` minimum build **26300.8553 (Insider Preview)**: `https://learn.microsoft.com/en-us/windows-insider/release-notes/experimental/preview-build-26300-8553`
- **Not a security boundary yet** — `github.com/microsoft/mxc` README: "no MXC profiles should be treated as security boundaries currently."
- **Internal verification (2026-06-08):** eng.ms (EngHub) search timed out; ES Chat found no internal record confirming or refining GA of Entra-bound MXC session identity. Status **unresolved internally** — recommend a direct inquiry to the MXC/Windows platform owners. Treat MXC as **defense-in-depth**, never as a control entrabot relies on.

---

## Architecture

New `entrabot/sandbox/` module mirrors the existing `platform/` shape (protocol + per-OS impls + factory), plus one new MCP tool.

```
entrabot/sandbox/
  base.py      SandboxRunner protocol; SandboxPolicy / SandboxResult (dataclasses, no raw dicts);
               Backend enum { PROCESS, SESSION (stub) }; identity_binding seam (no-op in P1)
  policy.py    build_policy() -> MXC JSON (schema 0.6.0-alpha); Python discovery helpers
               (tools/temp/profile paths); default-deny baseline; clamp_to_ceiling()
  binary.py    resolve_binary(): prebuilt (MXC_BIN_DIR / npm bin/) -> build-from-source -> None
  mac.py       SeatbeltRunner   (mxc-exec-mac, containment=seatbelt, --experimental)
  windows.py   ProcessContainerRunner (wxc-exec.exe, containment=processcontainer)
  linux.py     (optional, low priority) bubblewrap/lxc
  session.py   STUB — SessionIsolationRunner raising NotImplementedError; docstring marks the
               Phase-2 Entra-binding seam for a future implementer
  __init__.py  get_sandbox_runner() factory (per-OS, like get_credential_store())

mcp_server.py  NEW @mcp.tool() run_code(...) — audit-first, default-deny, attributed to the
               agent identity; wraps get_sandbox_runner().run()
```

### Data flow (happy path + shadow paths)

```
LLM host (Claude Code / Copilot CLI)
   │ run_code(argv, policy_narrowing?)        [tool registered only if ENTRABOT_ENABLE_RUN_CODE=1]
   ▼
run_code tool
   │ 1. audit "files.run_code … → pending"   (fail-closed: no audit ⇒ no run)
   │ 2. policy = clamp_to_ceiling(operator_ceiling, llm_narrowing)   (positive-allowlist, LLM may only NARROW;
   │      backend-capability-aware → SandboxBackendUnsupportedError if it can't be enforced)
   │ 3. runner = get_sandbox_runner()  → verify binary SHA256  (None ⇒ unavailable; bad hash ⇒ untrusted)
   ▼
SeatbeltRunner / ProcessContainerRunner
   │ canonicalize paths; per-run 0700 private dir; MXC JSON via stdin/0700 file (not argv);
   │ structured argv, no shell → native binary
   ▼
OS kernel enforces policy; runs argv in container (timeout kills whole tree)
   ▼
SandboxResult(stdout, stderr, exit) → size-cap + redact → audit "→ success|failure"
```

Shadow paths: **no binary** → `SandboxUnavailableError`, tool advertises disabled (like blob/persona-sati optional capability). **policy exceeds ceiling** → clamped + logged, never widened. **binary nonzero/crash** → `SandboxExecutionError` with captured stderr; audited failure. **timeout** → policy `timeout` (default 30s) kills the process tree; audited.

### Key decisions (locked in review)

| # | Decision | Rationale |
|---|---|---|
| **D2** | Build Option A now; stub + document Option C | Only A is buildable cross-platform today; C's platform surfaces aren't GA. |
| **D3** | Binary acquisition: prefer **verified** prebuilt → build-from-source (opt-in/dev) → per-OS **self-sign** → runtime-detect fallback | macOS seatbelt has no guaranteed signed prebuilt (experimental); Windows likely ships `wxc-exec.exe` via npm; self-sign clears Gatekeeper/SmartScreen for a dev-run tool without full notarization; runtime-detect keeps entrabot's optional-capability convention. **Binary is verified by pinned commit + SHA256 before exec** (see Security model). |
| **D4** | Policy authority: **operator-set ceiling, LLM may only narrow** (server-side, backend-aware `clamp_to_ceiling`) | Honors Learning #54 — the model can't widen its own containment. Ceiling is **positive-allowlist-only** (no allow-broad+deny), env-driven, default-deny. |
| **D5** | Adopt the hardened design from the outside-voice review (3/10 → resolved) | See Security model below. Net-negative security is the failure mode; this makes it fail-closed and honest. |
| — | **`run_code` is disabled by default** — operator opt-in via `ENTRABOT_ENABLE_RUN_CODE=1` | It's a new model-invocable execution surface inside a sandbox MS says "is not a security boundary yet." Off unless an operator turns it on. |
| — | **No token injection in Phase 1.** `keychainAccess` is hard-banned (non-overridable `false`, with a test) | Least privilege; the sandbox never reaches `~/Library/Keychains` or entrabot's private keys. Revisit only with a stdin/0700-file delivery design if a future need is proven. |
| — | **Structured argv, shell off by default**; absolute interpreter from the operator allowlist | No shell-metachar / PATH-hijack / startup-file escape. |
| — | **Audit-first, default-deny, fail-closed** | Repo prime directives. |

## Security model (hardened — resolves the outside-voice review)

The independent review scored the first draft **3/10**: a new code-execution surface, a clamp that doesn't hold cross-platform, unverified binary, and overclaimed enforcement. The hardened model:

1. **Disabled by default.** No `run_code` tool is registered unless `ENTRABOT_ENABLE_RUN_CODE=1`. Honest threat model: this is an *optional contained executor*, not a fix for an existing entrabot gap.
2. **Positive-allowlist-only, backend-aware ceiling.** The operator ceiling lists what's *allowed* (read roots, write roots, `network: block|allow`). It never relies on `deniedPaths` — which the research doc confirms is **unsupported on Windows**. `clamp_to_ceiling()` is **backend-capability-aware**: if the requested/effective policy needs an enforcement primitive the active backend can't deliver, it **fails closed** (refuses to run), never silently degrades.
3. **No secrets in the sandbox.** No Entra token injection in Phase 1. `keychainAccess=false` is fixed in the ceiling and not narrowable-up. A test asserts LLM narrowing cannot flip it.
4. **Verified binary.** `binary.py` verifies the resolved binary against a **pinned commit + expected SHA256** before exec; build-from-source is **opt-in / dev-labeled**, not automatic. A tampered/unknown binary → `SandboxUntrustedBinaryError`, run refused.
5. **Config via stdin or a 0700 temp file, not `--config-base64`** (argv is observable in process listings). Per-run private working dir, randomized name, `0700`; all `readwritePaths` **canonicalized** server-side; symlinks in writable roots rejected unless explicit.
6. **Structured argv, no shell.** `run_code` takes `argv: list[str]` + interpreter from the operator allowlist; no shell string. Timeout kills the **whole process tree**.
7. **Output is bounded + redacted.** stdout/stderr returned to the LLM is size-capped and run through redaction; raw stderr is not blindly surfaced.
8. **Audit schema (redacted):** agent identity, tool_call_id, backend, ceiling id/version, policy hash, effective-policy summary (path *labels*/hashes, not raw secret paths), cwd, argv hash, timeout, exit code, duration, stdout/stderr truncation metadata, denial reason, binary hash/version. No tokens, no raw secret paths, no raw env.
9. **No overclaiming.** Phase-1 "attributed to the agent identity" means **entrabot-side audit bookkeeping**, not OS-level Entra-bound session identity. OS-level attribution is Phase 2 (session isolation) and is gated on the unreleased platform surfaces.

### macOS enforcement caveats (don't advertise as controls)
- Network: **only `defaultPolicy: block|allow`**. `allowedHosts` is best-effort (Seatbelt has no DNS); `blockedHosts`/proxy are rejected. For real egress control, require an external proxy/firewall outside the sandbox.
- SIP makes system paths unwritable regardless of policy (fine — it only ever tightens).

### Error taxonomy
`SandboxUnavailableError` (no binary), `SandboxUntrustedBinaryError` (hash/provenance), `SandboxBackendUnsupportedError` (policy needs an unenforceable primitive → fail closed), `SandboxPolicyError` (ceiling/clamp violation, dry-run schema invalid), `SandboxExecutionError` (nonzero/crash), `SandboxTimeoutError`. Audit failure → run refused (fail-closed). Each maps to an MCP-safe user message; sensitive stderr is never blindly returned.

### C-readiness seams (so a future LLM can finish Phase 2)

1. `Backend.SESSION` enum value (present, unused in P1).
2. `session.py` `SessionIsolationRunner` stub: raises `NotImplementedError`, with a docstring that names the Entra-binding step, the Insider-build requirement, and the blog/learn URLs.
3. `SandboxRunner.identity_binding(agent_identity)` hook — no-op in P1; Phase-2 attaches the Entra Agent identity to the session here.
4. This design doc's *What's not buildable yet* section + an ADR record the gating and the citations.

---

## Distribution / build pipeline

- `scripts/setup_sandbox.sh` (+ `.ps1`): detect prebuilt → else clone+build the pinned MXC source (Rust 1.93) → self-sign (`codesign -s -` on macOS; local cert on Windows) → record path in `.env` (`ENTRABOT_MXC_BIN`). Idempotent; non-fatal (run_code degrades to unavailable on failure).
- Pin the MXC source commit + schema `0.6.0-alpha`; validate configs with `--dry-run` in CI.
- macOS binary self-signed locally; **full notarization deferred** to any end-user-distribution phase.

---

## Test plan (TDD)

`tests/sandbox/` mirrors the module. The native binary is **mocked** (patch the subprocess boundary) for unit tests; **opt-in adversarial integration tests** (gated behind an env flag like the smoke suite) exercise the real binary against the backend semantic gaps the mocks can't catch.

- `policy.py`: `clamp_to_ceiling()` — LLM narrowing accepted; any widening beyond ceiling clamped (the highest-value Learning-#54 guard); **positive-allowlist-only**; **backend-aware fail-closed** when a primitive is unenforceable; `keychainAccess` cannot be flipped true by narrowing; path canonicalization + symlink rejection; default-deny baseline.
- `binary.py`: prebuilt-found / build-fallback / none → `SandboxUnavailableError`; **bad SHA256 → `SandboxUntrustedBinaryError`**.
- `mac.py` / `windows.py`: correct JSON + flags; config delivered via stdin/0700 file (not argv); result parsing; nonzero/crash → `SandboxExecutionError`; timeout → `SandboxTimeoutError` kills the tree.
- `run_code` tool: **not registered when `ENTRABOT_ENABLE_RUN_CODE` is unset**; audit "pending" before run (fail-closed); audit success/failure with the redacted schema; unavailable/untrusted-binary paths; clamp applied; tool exposes **no** policy-widening parameter; takes structured argv, not a shell string; output size-capped + redacted.
- `session.py`: raises `NotImplementedError` (pins the Phase-2 boundary).
- **Opt-in adversarial integration** (real `mxc-exec-mac`): symlink escape from a writable root is blocked; a denied/secret path is unreadable; no write outside the 0700 private dir; `network: block` blocks egress; sensitive env is not inherited; timeout kills grandchildren; argv/config do not leak into audit.

---

## Implementation Tasks

- [ ] **T1 (P1)** sandbox — `base.py`: `SandboxRunner` protocol (returns capabilities, not just a runner), `SandboxPolicy`/`SandboxResult` dataclasses, `Backend` enum, `identity_binding` no-op seam, full error taxonomy. (TDD)
- [ ] **T2 (P1)** sandbox — `policy.py`: `build_policy()` (schema 0.6.0-alpha JSON), discovery helpers, **positive-allowlist** `clamp_to_ceiling()` (backend-aware, fail-closed) + the Learning-#54 widening guard + `keychainAccess`-cannot-be-enabled + path-canonicalization/symlink tests.
- [ ] **T3 (P1)** sandbox — `binary.py`: three-tier `resolve_binary()` + **SHA256/provenance verification** → `SandboxUntrustedBinaryError`; `SandboxUnavailableError`.
- [ ] **T4 (P1)** sandbox — `mac.py` SeatbeltRunner + `windows.py` ProcessContainerRunner (config via stdin/0700 file, structured argv, no shell, per-run 0700 dir, tree-kill timeout); `get_sandbox_runner()` factory.
- [ ] **T5 (P1)** mcp_server — `run_code` tool: **registered only when `ENTRABOT_ENABLE_RUN_CODE=1`**; audit-first (redacted schema), clamp, structured argv, output size-cap+redact; no widening param.
- [ ] **T6 (P1)** scripts — `setup_sandbox.sh`/`.ps1`: prebuilt→build(opt-in)→self-sign→**record SHA256**; idempotent, non-fatal.
- [ ] **T7 (P2)** sandbox — `session.py` Phase-2 stub + docstring seam (Entra-binding, Insider-build req, URLs); `Backend.SESSION`.
- [ ] **T8 (P2)** docs — ADR (`docs/decisions/007-mxc-execution-containers.md`); update `TODOS.md` (supersede AppContainer item); README "Open" → MXC; roadmap section in the platform-learnings doc.
- [ ] **T9 (P1)** tests — full `tests/sandbox/` suite + **opt-in adversarial** integration tests (symlink/secret-path/no-net/no-env-leak/tree-kill/redaction); `pytest -v && ruff check .` green.
- [ ] **T10 (P3)** sandbox — `linux.py` (bubblewrap/lxc) — optional, lower priority.

---

## Open questions / risks

- **R1 (Phase 2 crux):** Is entrabot's **Entra Agent User** (a cloud Graph identity) the same identity MXC session isolation attributes a session to (blog: "a local ID *or* a cloud provisioned identity backed by Entra")? Unverified. Carry as the Phase-2 gating risk; resolve via an inquiry to MXC owners.
- **R2:** MXC `0.x` schema churn — pin a version, isolate the JSON in `policy.py`, validate in CI.
- **R3:** macOS Gatekeeper on a self-signed binary in CI/headless contexts — confirm ad-hoc signing suffices for non-interactive runs.
- **R4:** Not a security boundary yet — every entrabot gate stays independent; MXC never relaxes one.
