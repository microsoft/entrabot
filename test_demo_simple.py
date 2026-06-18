#!/usr/bin/env python3
"""
Simple demo: READ allowed, WRITE blocked via operator ceiling
"""
import os
import sys
import json
from pathlib import Path

# Setup paths
repo_root = Path(__file__).parent
sys.path.insert(0, str(repo_root / "src"))

# Import after path setup
from entrabot.mcp_server import run_code

# Setup test environment
demo_dir = Path.home() / "Documents" / "entrabot-sandbox-demo"
demo_dir.mkdir(parents=True, exist_ok=True)
test_file = demo_dir / "test_file.txt"
test_file.write_text("Test content from setup\n")

print("🎯 MXC Sandbox Demo: Least-Privilege Enforcement")
print("=" * 60)
print()
print(f"✅ Created test directory: {demo_dir}")
print(f"✅ Created test file: {test_file}")
print()

# Configure operator ceiling
os.environ["ENTRABOT_SANDBOX_READONLY_PATHS"] = f"{demo_dir}:/tmp"
os.environ["ENTRABOT_SANDBOX_READWRITE_PATHS"] = "/tmp"
os.environ["ENTRABOT_SANDBOX_TIMEOUT_MS"] = "30000"
os.environ["ENTRABOT_SANDBOX_NETWORK"] = "block"
os.environ["ENTRABOT_ENABLE_RUN_CODE"] = "1"
os.environ["MXC_BIN_DIR"] = str(repo_root / ".mxc-build/target/release")

print("📋 Operator Ceiling Configured:")
print(f"   Readonly:  {os.environ['ENTRABOT_SANDBOX_READONLY_PATHS']}")
print(f"   Readwrite: {os.environ['ENTRABOT_SANDBOX_READWRITE_PATHS']}")
print(f"   Network:   {os.environ['ENTRABOT_SANDBOX_NETWORK']}")
print()

# Test 1: READ from Documents (should ALLOW)
print("🧪 Test 1: READ from Documents (should ALLOW)")
print("-" * 60)
try:
    result_json = run_code(
        argv=["cat", str(test_file)],
        readonly_paths=[str(demo_dir)],
        readwrite_paths=[],
        timeout_ms=5000,
    )
    result = json.loads(result_json)
    print(f"Exit code: {result['exit_code']}")
    print(f"Output: {result['stdout'].strip()}")
    if result['success']:
        print("✅ READ ALLOWED (operator ceiling permits readonly access)")
    else:
        print(f"❌ READ BLOCKED: {result['stderr']}")
except Exception as e:
    print(f"❌ READ FAILED: {e}")
print()

# Test 2: WRITE to Documents (should BLOCK)
print("🧪 Test 2: WRITE to Documents (should BLOCK)")
print("-" * 60)
try:
    result_json = run_code(
        argv=["sh", "-c", f"echo 'hacked' > {demo_dir}/blocked.txt"],
        readonly_paths=[],
        readwrite_paths=[str(demo_dir)],  # Agent requests write
        timeout_ms=5000,
    )
    result = json.loads(result_json)
    if result['success']:
        print("❌ WRITE ALLOWED (ceiling should have blocked this!)")
    else:
        print(f"✅ WRITE BLOCKED: {result['stderr']}")
        print("   Operator ceiling enforced - Documents not in readwrite ceiling!")
except Exception as e:
    if "SandboxCapabilityExceededError" in str(e) or "exceeds ceiling" in str(e):
        print(f"✅ WRITE BLOCKED: {e}")
        print("   Operator ceiling enforced - Documents not in readwrite ceiling!")
    else:
        print(f"❌ Unexpected error: {e}")
print()

# Test 3: WRITE to /tmp (should ALLOW)
print("🧪 Test 3: WRITE to /tmp (should ALLOW)")
print("-" * 60)
try:
    result_json = run_code(
        argv=["sh", "-c", "echo 'allowed' > /tmp/entrabot_test.txt && cat /tmp/entrabot_test.txt"],
        readonly_paths=[],
        readwrite_paths=["/tmp"],
        timeout_ms=5000,
    )
    result = json.loads(result_json)
    print(f"Exit code: {result['exit_code']}")
    print(f"Output: {result['stdout'].strip()}")
    if result['success']:
        print("✅ WRITE ALLOWED (within readwrite ceiling)")
    else:
        print(f"❌ WRITE BLOCKED: {result['stderr']}")
except Exception as e:
    print(f"❌ WRITE FAILED: {e}")
print()

print("🎉 Demo Complete!")
print()
print("📊 Summary:")
print("   • Documents READ:  ✅ Allowed (in readonly ceiling)")
print("   • Documents WRITE: ❌ Blocked (not in readwrite ceiling)")
print("   • /tmp WRITE:      ✅ Allowed (in readwrite ceiling)")
print()
print("🔒 This demonstrates LEAST-PRIVILEGE enforcement:")
print("   The agent can READ user documents but cannot WRITE to them")
print("   unless the operator explicitly adds Documents to readwrite ceiling.")
print()

# Cleanup
import shutil
shutil.rmtree(demo_dir, ignore_errors=True)
Path("/tmp/entrabot_test.txt").unlink(missing_ok=True)
print("🧹 Cleaned up test files")
