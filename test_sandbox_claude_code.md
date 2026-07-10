# Testing MXC Sandbox from Claude Code

## Setup Complete ✅

The MXC sandbox is installed and ready to test! Here's what's been set up:

- ✅ Test MXC binary created at `.mxc-build/target/release/mxc-exec-mac`
- ✅ SHA256 hash verified
- ✅ `.env` configured with `ENTRABOT_ENABLE_RUN_CODE=1`
- ✅ `run_code` tool registered in MCP server
- ✅ Python tests passing

## Test from Claude Code

### 1. Start the EntraBot MCP server

Make sure `entrabot` is running (it should auto-start when you open Claude Code in this project).

### 2. Try these commands

**Simple echo test:**
```
Can you use run_code to execute: echo "Hello from EntraBot sandbox!"
```

Expected: Should return stdout with "Hello from EntraBot sandbox!"

**List files:**
```
Use run_code to list files in /tmp
```

Expected: Should execute `ls /tmp` and return the listing

**Python simple:**
```
Use run_code to run Python: print("MXC sandbox is working!")
```

Note: Python commands with quotes may have shell escaping issues in the test mock. Real MXC will handle this properly.

**Check date:**
```
Use run_code to check the current date
```

Expected: Should execute `date` command

## What's Happening Behind the Scenes

When you call `run_code`:

1. ✅ **Binary resolution**: Finds `.mxc-build/target/release/mxc-exec-mac`
2. ✅ **SHA256 verification**: Checks hash matches `PINNED_HASHES`
3. ✅ **Policy building**: Creates MXC JSON with:
   - `process.commandLine`: Your command
   - `filesystem.readonlyPaths`: ["/tmp"]
   - `filesystem.readwritePaths`: ["/tmp"]
   - `network.defaultPolicy`: "block"
   - `timeout`: 30000ms
4. ✅ **Policy clamping**: LLM cannot widen operator ceiling (Learning #54)
5. ✅ **Audit logging**: "pending" before exec, "success"/"failure" after
6. ✅ **Execution**: Runs in test sandbox
7. ✅ **Result capture**: Returns stdout, stderr, exit_code, duration_ms

## Current Limitations (Test Mock)

The test mock binary mimics MXC behavior but:
- ⚠️ No actual sandboxing (just runs commands)
- ⚠️ Shell quoting issues with complex commands
- ⚠️ No network filtering
- ⚠️ No filesystem isolation

Real MXC will enforce all these constraints properly!

## Expected Log Output

You should see in the terminal where entrabot MCP is running:

```
audit: run_code sandbox → pending
audit: run_code sandbox → success
```

## Next Steps After Testing

Once you confirm it works from Claude Code:
1. Continue to T7-T10 (session stub, docs, comprehensive tests, Linux)
2. Or merge to main and document for real MXC integration when it's released

## Troubleshooting

**If run_code returns "unavailable":**
- Check `.env` has `ENTRABOT_ENABLE_RUN_CODE=1`
- Check `MXC_BIN_DIR` points to `.mxc-build/target/release`
- Restart EntraBot MCP server

**If hash mismatch:**
- Binary was modified, run `./scripts/setup_sandbox.sh --force-build`

**If no output:**
- Check MCP server logs for audit events
- Try simpler command first: `echo test`

