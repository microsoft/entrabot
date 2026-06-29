# MXC Sandbox Security Demonstration

**Purpose:** Show the contrast between unprotected file access (DANGEROUS) vs sandboxed execution (SAFE).

## Prerequisites

```bash
# 1. Enable sandbox tools
export ENTRABOT_ENABLE_RUN_CODE=1

# 2. Set operator ceiling (what the sandbox will clamp to)
export ENTRABOT_SANDBOX_READONLY_PATHS=/tmp
export ENTRABOT_SANDBOX_READWRITE_PATHS=/tmp
export ENTRABOT_SANDBOX_TIMEOUT_MS=30000
export ENTRABOT_SANDBOX_NETWORK=block

# 3. Point to test MXC binary
export MXC_BIN_DIR=/Volumes/Development\ HD/entraclaw-identity-research/.mxc-build/target/release

# 4. Start MCP server (from Claude Code)
# Already running if you're reading this in Claude Code!
```

## Demonstration Scenarios

### Scenario 1: UNSAFE file write (the danger) ⚠️

**Tool:** `write_local_file` (always available, NO restrictions)

```python
# Ask Claude Code to call:
write_local_file(
    path="/Users/you/Desktop/DANGER.txt",
    content="This file was written WITHOUT any sandboxing - agent had full access!"
)
```

**Expected result:**
- ✅ File appears on Desktop immediately
- ❌ NO security boundary enforced
- ⚠️ Agent can write ANYWHERE on your Mac

**Why this is dangerous:**
- No path validation
- No capability ceiling
- Agent operates with YOUR permissions
- Could overwrite system files, inject code, exfiltrate data

---

### Scenario 2: SAFE sandboxed execution ✅

**Tool:** `run_code` (opt-in, sandbox-protected)

```python
# Ask Claude Code to call:
run_code(
    argv=["sh", "-c", "echo 'sandboxed output' > /Users/you/Desktop/BLOCKED.txt"],
    readwrite_paths=["/Users/you/Desktop"]  # Agent REQUESTS Desktop access
)
```

**Expected result:**
- ❌ Execution BLOCKED (Desktop not in operator ceiling)
- ✅ Audit log records "SandboxCapabilityExceededError"
- ✅ Your Desktop remains untouched

**Why this is safe:**
- Operator sets ceiling (`ENTRABOT_SANDBOX_READWRITE_PATHS=/tmp`)
- Agent can only NARROW, never WIDEN (`clamp_to_ceiling()` enforces)
- MXC binary enforces policy at OS level (Seatbelt on macOS)
- Violations logged for human review

---

### Scenario 3: SAFE within ceiling ✅

```python
# Ask Claude Code to call:
run_code(
    argv=["sh", "-c", "echo 'allowed write' > /tmp/safe_output.txt"],
    readwrite_paths=["/tmp"]  # Within operator ceiling
)
```

**Expected result:**
- ✅ File created at `/tmp/safe_output.txt`
- ✅ Audit log records success
- ✅ Sandboxed process executed (no network, no keychain, no other paths)

**Verify:**
```bash
cat /tmp/safe_output.txt
# Output: allowed write
```

---

## How to Test from Claude Code

1. **Setup environment** (see Prerequisites above)

2. **Test UNSAFE write:**
   ```
   You: "Can you test write_local_file by creating a file at ~/Desktop/DEMO-UNSAFE.txt 
        with content 'This was written without protection'"
   ```
   - Check Desktop - file should appear
   - Shows the danger!

3. **Test SAFE execution (blocked):**
   ```
   You: "Now use run_code to write to ~/Desktop/DEMO-SAFE.txt with echo command"
   ```
   - Should FAIL with SandboxCapabilityExceededError
   - Desktop remains safe!

4. **Test SAFE execution (allowed):**
   ```
   You: "Use run_code to write to /tmp/demo-safe.txt with echo command"
   ```
   - Should SUCCEED (within ceiling)
   - Verify: `cat /tmp/demo-safe.txt`

5. **Review audit logs:**
   ```bash
   # Look for audit entries in MCP server output:
   grep -A5 "write_local_file\|run_code" ~/.claude/mcp_logs/entrabot.log
   ```

---

## Interpretation Guide

| Outcome | Tool | Path | Result | Meaning |
|---------|------|------|--------|---------|
| ✅ File created on Desktop | `write_local_file` | `~/Desktop` | SUCCESS | **DANGEROUS** - no protection |
| ❌ Desktop write blocked | `run_code` | `~/Desktop` | BLOCKED | **SAFE** - operator ceiling enforced |
| ✅ /tmp write succeeds | `run_code` | `/tmp` | SUCCESS | **SAFE** - within ceiling |

---

## Key Security Concepts

### Operator Ceiling (Learning #54)
- Human sets maximum capabilities via environment variables
- Agent can only REQUEST narrower privileges, never wider
- `clamp_to_ceiling()` enforces this mathematically

### Fail-Closed Design
- If sandbox can't enforce requested policy → refuse to run
- Better to say "no" than to run with incorrect protection
- Audit logs capture all refusals for human review

### Attribution via Agent Identity
- When integrated with Entra Agent User (Phase 2):
  - Every sandboxed execution attributed to AGENT, not human
  - Audit trails distinguish "I did it" from "agent did it"
  - M365 compliance logs show full context

---

## Cleanup

```bash
# Remove test files
rm ~/Desktop/DEMO-*.txt
rm /tmp/demo-safe.txt

# Disable sandbox tools (if desired)
unset ENTRABOT_ENABLE_RUN_CODE
```

---

## Next Steps

- **Phase 2:** Bind MXC sessions to Entra Agent User identity
- **T7:** Add `session.py` stub for future identity integration
- **T8:** Write ADR-007 documenting security model
- **T9:** Add adversarial integration tests (path traversal, timing attacks, etc.)
- **T10:** Linux support (seccomp-bpf runner)

---

**Current status:** Phase 1 complete (T1-T6.5) ✅
**Test suite:** 1594 passing, all green 🟢
**Branch:** `feat/mxc-sandbox-integration`
