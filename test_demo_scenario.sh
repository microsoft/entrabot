#!/bin/bash
# Demo scenario: Agent in Teams tries read (allowed) vs write (blocked) to Documents

set -e

echo "🎯 MXC Sandbox Demo: Least-Privilege Enforcement"
echo "=================================================="
echo ""

# Setup test environment
DEMO_DIR="$HOME/Documents/entrabot-sandbox-demo"
mkdir -p "$DEMO_DIR"
echo "Test content from setup" > "$DEMO_DIR/test_file.txt"

echo "✅ Created test directory: $DEMO_DIR"
echo "✅ Created test file: $DEMO_DIR/test_file.txt"
echo ""

# Configure operator ceiling
export ENTRABOT_SANDBOX_READONLY_PATHS="$DEMO_DIR:/tmp"
export ENTRABOT_SANDBOX_READWRITE_PATHS="/tmp"
export ENTRABOT_SANDBOX_TIMEOUT_MS=30000
export ENTRABOT_SANDBOX_NETWORK=block
export ENTRABOT_ENABLE_RUN_CODE=1
export MXC_BIN_DIR="/Volumes/Development HD/entraclaw-identity-research/.mxc-build/target/release"

echo "📋 Operator Ceiling Configured:"
echo "   Readonly:  $ENTRABOT_SANDBOX_READONLY_PATHS"
echo "   Readwrite: $ENTRABOT_SANDBOX_READWRITE_PATHS"
echo "   Network:   $ENTRABOT_SANDBOX_NETWORK"
echo ""

cd "/Volumes/Development HD/entraclaw-identity-research"
source .venv/bin/activate

echo "🧪 Test 1: READ from Documents (should ALLOW)"
echo "---------------------------------------------"
python << PYTHON
import sys
sys.path.insert(0, "src")
from entrabot.sandbox import get_sandbox_runner

runner = get_sandbox_runner()
result = runner.run_command(
    command_line="cat $DEMO_DIR/test_file.txt",
    readonly_paths=["$DEMO_DIR"],
    readwrite_paths=[],
    timeout_ms=5000,
)
print(f"Exit code: {result.exit_code}")
print(f"Output: {result.stdout.strip()}")
if result.exit_code == 0:
    print("✅ READ ALLOWED (operator ceiling permits readonly access)")
else:
    print(f"❌ READ BLOCKED: {result.stderr}")
PYTHON
echo ""

echo "🧪 Test 2: WRITE to Documents (should BLOCK)"
echo "---------------------------------------------"
python << PYTHON
import sys
sys.path.insert(0, "src")
from entrabot.sandbox.policy import build_policy, clamp_to_ceiling
from entrabot.sandbox.base import Backend
import os

# Agent requests write to Documents
agent_policy = build_policy(
    backend=Backend.PROCESS,
    command_line="echo 'hacked' > $DEMO_DIR/blocked.txt",
    readonly_paths=[],
    readwrite_paths=["$DEMO_DIR"],
    timeout_ms=5000,
)

# Operator ceiling (from env)
ceiling_readonly = os.getenv("ENTRABOT_SANDBOX_READONLY_PATHS", "").split(":")
ceiling_readwrite = os.getenv("ENTRABOT_SANDBOX_READWRITE_PATHS", "").split(":")

ceiling_policy = build_policy(
    backend=Backend.PROCESS,
    command_line="",
    readonly_paths=ceiling_readonly,
    readwrite_paths=ceiling_readwrite,
    timeout_ms=int(os.getenv("ENTRABOT_SANDBOX_TIMEOUT_MS", "30000")),
)

try:
    clamped = clamp_to_ceiling(agent_policy, ceiling_policy)
    print("❌ Policy clamping ALLOWED write (should have been blocked!)")
    print(f"Clamped policy: {clamped}")
except Exception as e:
    print(f"✅ WRITE BLOCKED: {e}")
    print("   Operator ceiling enforced - Documents not in readwrite ceiling!")
PYTHON
echo ""

echo "🧪 Test 3: WRITE to /tmp (should ALLOW)"
echo "----------------------------------------"
python << PYTHON
import sys
sys.path.insert(0, "src")
from entrabot.sandbox import get_sandbox_runner

runner = get_sandbox_runner()
result = runner.run_command(
    command_line="echo 'allowed' > /tmp/entrabot_test_write.txt && cat /tmp/entrabot_test_write.txt",
    readonly_paths=[],
    readwrite_paths=["/tmp"],
    timeout_ms=5000,
)
print(f"Exit code: {result.exit_code}")
print(f"Output: {result.stdout.strip()}")
if result.exit_code == 0:
    print("✅ WRITE ALLOWED (within readwrite ceiling)")
else:
    print(f"❌ WRITE BLOCKED: {result.stderr}")
PYTHON
echo ""

echo "🎉 Demo Complete!"
echo ""
echo "📊 Summary:"
echo "   • Documents READ:  ✅ Allowed (in readonly ceiling)"
echo "   • Documents WRITE: ❌ Blocked (not in readwrite ceiling)"
echo "   • /tmp WRITE:      ✅ Allowed (in readwrite ceiling)"
echo ""
echo "🔒 This demonstrates LEAST-PRIVILEGE enforcement:"
echo "   The agent can READ user documents but cannot WRITE to them"
echo "   unless the operator explicitly adds Documents to readwrite ceiling."
echo ""

# Cleanup
rm -rf "$DEMO_DIR"
rm -f /tmp/entrabot_test_write.txt
echo "🧹 Cleaned up test files"
