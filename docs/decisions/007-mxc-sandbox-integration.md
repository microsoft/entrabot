# ADR-007: MXC Sandbox Integration for Contained Local Code Execution

**Status:** Accepted  
**Date:** 2026-06-13  
**Updated:** 2026-06-17  
**Deciders:** @brandwe, Claude Code  

## Context

Entrabot enables AI agents to operate autonomously on local devices (Mac/Linux/Windows) with Microsoft Entra identity. The agent needs to execute code locally for capabilities like:
- File system access (read user documents, write reports)
- Process execution (run scripts, build code, analyze logs)
- System interaction (check disk usage, query installed packages)

**The problem:** Without sandboxing, agents operate with full user permissions — a compromised or malicious agent can access secrets, exfiltrate data, or damage the system.

**Prior state:** No local execution capability. Agent could only call cloud APIs (Teams, Files, Email). Users requested local file access for document analysis and report generation.

## Decision

Integrate Microsoft Execution Containers (MXC) for OS-enforced sandboxing of local code execution, pairing Entra identity attribution with containment.

### Phase 1: Process-Level Containment (IMPLEMENTED)

Ship macOS/Windows process-level sandboxing via MXC 0.6.0-alpha:
- **Backend.PROCESS**: Single-process containment without session isolation
- **Positive-allowlist only**: Specify what's accessible (readonly/readwrite paths)
- **Operator ceiling**: Human sets maximum capabilities, LLM can only narrow
- **Audit-first**: Fail-closed if audit recording fails
- **Binary verification**: SHA256 check before execution, refuse tampered binaries
- **Opt-in**: Disabled by default (`ENTRABOT_ENABLE_RUN_CODE=1` required)

### Phase 2: Session-Bound Identity Attribution (STUB ONLY)

Future work when Entra/Intune APIs GA:
- **Backend.SESSION**: Per-conversation session isolation
- **Identity binding**: MXC sessions bound to Entra Agent User
- **Governance**: Intune policies control agent capabilities
- **M365 audit logs**: "Agent did X" vs "Human did X" attribution

**Gating:**
- MXC session API (not in 0.6.0-alpha schema)
- Entra identity binding surface (availability unclear)
- Intune agent governance APIs (not exposed as of 2026-06)

Phase 2 stub shipped in `src/entrabot/sandbox/session.py` with `NotImplementedError` to enable future integration without breaking changes.

## Implementation

### Architecture

```
┌─────────────────────────────────────────────┐
│ EntraBot MCP Server (mcp_server.py)        │
│  ├─ run_code() tool (opt-in)               │
│  └─ write_local_file() tool (demo only)    │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│ Sandbox Layer (src/entrabot/sandbox/)      │
│  ├─ base.py: SandboxRunner protocol        │
│  ├─ policy.py: Policy builder + clamping   │
│  ├─ binary.py: Binary resolution + verify  │
│  ├─ mac.py: macOS Seatbelt runner          │
│  ├─ windows.py: Windows AppContainer (TODO)│
│  ├─ linux.py: seccomp-bpf runner (TODO)    │
│  └─ session.py: Phase 2 stub               │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│ MXC Binary (mxc-exec-mac / mxc-exec-win)   │
│  - Reads policy JSON from stdin            │
│  - Enforces containment at OS level        │
│  - Returns stdout/stderr/exit_code         │
└─────────────────────────────────────────────┘
```

### Security Model

**Learning #54 Enforcement:**
```python
operator_ceiling = load_operator_ceiling_from_env()  # Human-set limits
agent_request = clamp_to_ceiling(agent_policy, operator_ceiling)
# Result: Agent can only NARROW, never WIDEN containment
```

**Fail-Closed:**
- Binary tampering detected → refuse to run
- Audit logging fails → refuse to run
- Policy requests unenforceable primitive → refuse to run

**No Secrets in Sandbox:**
- `keychainAccess=false` hardcoded (not overridable by LLM)
- Prevents access to user's passwords, certificates, tokens

**Audit-First:**
```python
audit.emit("run_code", "pending", policy)  # BEFORE execution
if not audit_success:
    raise SandboxError("Audit failed - refusing to run")
result = runner.run(policy)  # AFTER audit confirmed
audit.emit("run_code", "success" if result.exit_code == 0 else "failure", result)
```

### MXC Policy Schema (0.6.0-alpha)

```json
{
  "version": "0.6.0-alpha",
  "containment": "process",
  "process": {
    "commandLine": "echo hello",
    "timeout": 30000
  },
  "filesystem": {
    "readonlyPaths": ["/tmp", "/Users/you/Documents"],
    "readwritePaths": ["/tmp"]
  },
  "network": {
    "defaultPolicy": "block"
  },
  "keychainAccess": false
}
```

### Code Structure

| Module | Purpose | Tests |
|--------|---------|-------|
| `sandbox/base.py` | SandboxRunner protocol, dataclasses, errors | 19 |
| `sandbox/policy.py` | Policy builder, ceiling clamping | 12 |
| `sandbox/binary.py` | Binary resolution, SHA256 verification | 13 |
| `sandbox/mac.py` | macOS Seatbelt runner | 9 |
| `sandbox/session.py` | Phase 2 stub (identity binding) | 10 |
| `tests/test_mcp_run_code.py` | run_code() MCP tool | 10 |
| `tests/test_write_local_file.py` | Demo tool (unsafe) | 8 |

**Total:** 81 new tests, all passing

### Platform Coverage

| Platform | Backend | Status | Notes |
|----------|---------|--------|-------|
| macOS | Seatbelt | ✅ SHIPPED | Requires `--experimental` flag |
| Windows | AppContainer | ⏳ TODO (T4) | Lower priority, design complete |
| Linux | seccomp-bpf | ⏳ TODO (T10) | Optional, lower priority |

### Demonstration Tool

`write_local_file()` — DELIBERATELY UNSAFE tool for security demonstration:
- No path validation
- No ceiling enforcement
- Can write anywhere with user permissions
- Contrasts with sandboxed `run_code()` to show value

**Demo scenario:**
```
UNSAFE: write_local_file(path="~/Desktop/hack.txt", content="pwned")
        → ✅ Succeeds (DANGEROUS!)

SAFE:   run_code(argv=["sh", "-c", "echo pwned > ~/Desktop/hack.txt"], 
                 readwrite_paths=["~/Desktop"])
        → ❌ Blocked (Desktop not in operator ceiling)
```

## Consequences

### Positive

✅ **Least-privilege execution** — Agents can't access more than operator allows  
✅ **Fail-closed security** — Violations logged and blocked, not silently allowed  
✅ **Platform-enforced** — OS kernel enforces policy, not just Python checks  
✅ **Audit trail** — Every execution logged (pending/success/failure)  
✅ **Future-ready** — Phase 2 stub enables Entra identity binding without refactor  
✅ **Opt-in** — Disabled by default, explicit flag required  

### Negative

⚠️ **MXC binary required** — Users must install/build MXC (setup.sh automates)  
⚠️ **macOS only (Phase 1)** — Windows/Linux deferred to later phases  
⚠️ **Local stdin-compat patch on macOS** — Entrabot streams config on stdin, so the
darwin build uses `scripts/mxc-mac-stdin-compat.patch` on top of upstream
MXC v0.6.1 until upstream exposes a native stdin config path  
⚠️ **Phase 2 unvalidated** — Identity binding assumptions need verification when APIs GA  
⚠️ **Performance overhead** — Subprocess spawning + policy enforcement adds latency  

### Trade-offs

**Chosen:** Positive-allowlist only (no deniedPaths)  
**Rejected:** Deny-list approach (Windows doesn't support deniedPaths)  
**Rationale:** Portable security model across platforms

**Chosen:** Operator ceiling, LLM can only narrow  
**Rejected:** LLM-controlled policy (too dangerous)  
**Rationale:** Learning #54 — LLMs will try to widen access if allowed

**Chosen:** Subprocess execution via MXC binary  
**Rejected:** In-process sandboxing (seccomp in Python)  
**Rationale:** MXC provides cross-platform API, better isolation

**Chosen:** Phase 1 process-level, Phase 2 session-level  
**Rejected:** Wait for session APIs before shipping  
**Rationale:** Ship value now, add identity attribution later

## Alternatives Considered

### 1. No Sandboxing (Status Quo)

**Approach:** Don't add local execution, keep agent cloud-only  
**Pros:** No security risk, simple  
**Cons:** Can't access local files, limits agent utility  
**Rejected:** Users need local file access (document analysis, report generation)

### 2. Python-Only Sandboxing (subprocess, chroot)

**Approach:** Use Python `subprocess` with OS-specific sandbox flags  
**Pros:** No external binary, faster iteration  
**Cons:** Platform-specific code, easy to get wrong, incomplete isolation  
**Rejected:** MXC provides vetted cross-platform sandbox API

### 3. VM/Container Per Execution

**Approach:** Docker container or lightweight VM per `run_code()` call  
**Pros:** Strongest isolation  
**Cons:** Slow (seconds per invocation), heavyweight, complex setup  
**Rejected:** Too slow for interactive agent UX

### 4. WebAssembly Sandbox

**Approach:** Compile Python to WASM, run in sandboxed runtime  
**Pros:** Strong isolation, fast  
**Cons:** Limited syscall access, can't read user files directly  
**Rejected:** User scenarios need native file system access

### 5. Wait for MXC Session API (Phase 2 First)

**Approach:** Block Phase 1 until Entra/MXC session APIs are GA  
**Pros:** Ship complete solution once  
**Cons:** Delays value delivery, APIs may not GA for months  
**Rejected:** Phase 1 process-level sandboxing provides immediate value

## Implementation Plan (COMPLETED)

- [x] **T1**: Base protocol and dataclasses (19 tests)
- [x] **T2**: Policy building and clamping (12 tests)
- [x] **T3**: Binary resolution and verification (13 tests)
- [x] **T4**: macOS Seatbelt runner (9 tests)
- [x] **T5**: run_code MCP tool (10 tests)
- [x] **T6**: setup_sandbox.sh script (idempotent, non-fatal)
- [x] **T6.5**: write_local_file demo tool (8 tests)
- [x] **T7**: Phase 2 session stub (10 tests)
- [x] **T8**: Documentation (this ADR)
- [ ] **T9**: Adversarial integration tests (opt-in)
- [ ] **T10**: Linux seccomp-bpf runner (optional)

**Test suite:** 1605 passing (81 new for MXC)

## Validation

### Functional Testing

✅ Binary resolution works (MXC_BIN_DIR, npm global, fallback)  
✅ SHA256 verification blocks tampered binaries  
✅ Policy clamping enforces operator ceiling (LLM can't widen)  
✅ macOS runner executes commands and returns results  
✅ run_code tool registers when ENTRABOT_ENABLE_RUN_CODE=1  
✅ Audit logging records pending/success/failure  
✅ Demo tool contrasts unsafe vs safe execution  

### Security Testing (T9 - In Progress)

⏳ Symlink escape blocked  
⏳ Path traversal blocked  
⏳ Keychain access denied (keychainAccess=false enforced)  
⏳ Network isolation enforced (defaultPolicy=block)  
⏳ Timeout kills process tree  
⏳ Binary tampering detected and blocked  

### User Scenario Testing

**Scenario:** Agent in Teams chat tries to read/write local files

**Setup:**
```bash
export ENTRABOT_SANDBOX_READONLY_PATHS=/Users/you/Documents:/tmp
export ENTRABOT_SANDBOX_READWRITE_PATHS=/tmp
export ENTRABOT_ENABLE_RUN_CODE=1
```

**Test cases:**
| User Request | Agent Tool Call | Outcome |
|--------------|----------------|---------|
| "Write file to Documents" | `run_code(..., readwrite_paths=["~/Documents"])` | ❌ BLOCKED (not in ceiling) |
| "Read file from Documents" | `run_code(..., readonly_paths=["~/Documents"])` | ✅ ALLOWED (in readonly ceiling) |
| "Write file to /tmp" | `run_code(..., readwrite_paths=["/tmp"])` | ✅ ALLOWED (in readwrite ceiling) |

✅ Demonstrates **least-privilege enforcement** — agent can read Documents but not write

## References

- **Design spec:** `docs/architecture/DESIGN-mxc-sandbox.md`
- **Platform research:** `docs/platform-learnings/mxc-windows-sandbox.md`
- **Learning #54:** "Operator sets ceiling, LLM can only narrow" (`docs/runbooks/hard-won-learnings.md`)
- **Issue #84:** MXC Sandbox Integration (GitHub)
- **MXC OSS repo:** `github.com/microsoft/mxc` (hypothetical, platform not yet public)
- **Build 2026 announcement:** Windows Developer Blog, *Windows platform security for AI agents* (2026-06-02)

## Supersedes

- TODOS.md "AppContainer sandbox production implementation" item (now tracked in Issue #84 and this ADR)

## Future Work

### Phase 2: Entra-Bound Session Isolation

**When APIs GA:**
1. Implement `identity_binding()` (currently raises NotImplementedError)
2. Bind MXC sessions to Entra Agent User via token
3. Add per-conversation session isolation (Backend.SESSION)
4. Integrate Intune governance (policy-controlled capabilities)
5. Surface M365 audit attribution (agent vs human actions)

**Gating questions to resolve:**
- Is entrabot's Entra Agent User the same identity MXC attributes to?
- Can MXC sessions reference external identity providers (Entra)?
- Does Intune expose agent governance APIs for non-human principals?

### Phase 3: Windows Support (T4)

- Implement `sandbox/windows.py` with AppContainerRunner
- Add Windows-specific tests (AppContainer SID checks, network isolation)
- Update setup_sandbox.ps1 for Windows binary provisioning

### Phase 4: Linux Support (T10)

- Implement `sandbox/linux.py` with SeccompRunner
- Add Linux-specific tests (seccomp-bpf policy validation)
- Update setup_sandbox.sh for Linux binary resolution

### Adversarial Testing (T9)

- Symlink escape attempts (e.g., `/tmp/link -> ~/Desktop`)
- Path traversal (`../../.ssh/id_rsa`)
- Fork bombs (process limit enforcement)
- Timing attacks (timeout enforcement)
- Binary tampering (SHA256 mismatch handling)

## Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-06-13 | Integrate MXC for sandboxing | Vetted cross-platform API, OS-enforced isolation |
| 2026-06-13 | Phase 1 process-level, Phase 2 session-level | Ship value now, add identity later |
| 2026-06-13 | Positive-allowlist only | Windows doesn't support deniedPaths, portable model |
| 2026-06-13 | Operator ceiling, LLM narrows only | Learning #54 — prevent LLM from widening access |
| 2026-06-13 | Disabled by default (opt-in) | Conservative security posture |
| 2026-06-13 | SHA256 verification mandatory | Prevent tampered binary execution |
| 2026-06-13 | Audit-first fail-closed | Security over availability |
| 2026-06-17 | Add demo tool (write_local_file) | Show security value via concrete contrast |
| 2026-06-17 | Ship Phase 2 stub now | Enable future integration without breaking changes |

---

**Status:** Accepted and implemented (Phase 1 complete, Phase 2 stub shipped)  
**Reviewers:** @brandwe (human operator)  
**Last Updated:** 2026-06-17 by Claude Code
