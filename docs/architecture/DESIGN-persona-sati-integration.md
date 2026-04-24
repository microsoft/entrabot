# Design: Persona-Sati Integration

**Date:** 2026-04-18
**Author:** Brandon Werner, EntraClaw Agent
**Status:** Proposed
**Implements:** Separation of mind (persona-sati) from body (entraclaw Teams MCP)

---

## 1. What This Is

This document describes how to connect the entraclaw Teams MCP server (the "body") to the persona-sati MCP server (the "mind") that now runs as a separate service in Azure Kubernetes.

**Before this change:** entraclaw is self-contained. It loads its own system prompt from `prompts/agent_system.md`, syncs its own memory to blob via hooks in `.claude/settings.json`, and owns the full `claude_memory/` blob prefix. Everything is in one repo.

**After this change:** entraclaw becomes a Teams interface tool only. The personality, system prompt, and memory operations are served by persona-sati, which runs as a separate MCP server (in AKS or locally). entraclaw still sends Teams messages, polls for replies, and runs the daily summary — but it no longer owns the agent's mind.

**Why:** The mind should be portable. It should work with any agent body — not just Teams. Separating them means a code review agent, an email agent, or a Slack agent can all attach to the same persona without duplicating the prompt or memory infrastructure.

---

## 2. Current Architecture (Before)

```
Claude Code
  │
  └── connects via stdio ──► entraclaw MCP server
                                │
                                ├── loads prompts/agent_system.md (hardcoded path)
                                ├── Teams tools (send, read, watch, create_chat, etc.)
                                ├── background polls (Teams 5s, email 60s, chat-discovery 120s)
                                ├── daily summary scheduler (5pm PDT)
                                └── memory sync hooks in .claude/settings.json:
                                    ├── SessionStart: pull claude_memory/ from blob
                                    └── PostToolUse(Write): push memory file to blob
```

The MCP server loads `agent_system.md` at import time via `_load_agent_instructions()` in `mcp_server.py:37-56`. If the file is missing, it falls back to a minimal inline string. Memory sync happens outside the MCP server via Claude Code hooks that call `scripts/claude_memory_sync.py`.

## 3. Target Architecture (After)

```
Claude Code
  │
  ├── connects via stdio ──► entraclaw MCP server (Teams body)
  │                            │
  │                            ├── Teams tools (send, read, watch, etc.)
  │                            ├── background polls (unchanged)
  │                            ├── daily summary (unchanged)
  │                            └── NO system prompt loading
  │                                NO memory sync hooks
  │
  └── connects via SSE ───► persona-sati MCP server (the mind)
                              │   (in AKS or localhost:8100)
                              │
                              ├── get_system_prompt() → returns agent_system.md
                              ├── list_memory_files() → lists claude_memory/ keys
                              ├── read_memory_file(name) → reads a memory file
                              ├── write_memory_file(name, content) → writes + blob sync
                              ├── refresh_persona() → force blob pull
                              └── consolidate() → run consolidation cycle
```

Claude Code connects to BOTH MCP servers. It loads the personality from persona-sati and uses entraclaw for Teams operations. The LLM naturally bridges them — it calls `get_system_prompt()` from persona-sati at session start, and `send_teams_message()` from entraclaw when it needs to message someone.

---

## 4. What Changes in This Repo (entraclaw)

### 4.1 System Prompt: Remove from entraclaw

**Current:** `_load_agent_instructions()` reads `prompts/agent_system.md` and passes it to `FastMCP(instructions=...)`.

**Change:** Replace with a generic, non-personality-specific instruction string. The real prompt lives in persona-sati now.

```python
# mcp_server.py — BEFORE
def _load_agent_instructions() -> str:
    prompt_path = Path(__file__).resolve().parents[2] / "prompts" / "agent_system.md"
    try:
        return prompt_path.read_text(encoding="utf-8")
    except OSError:
        return fallback

# mcp_server.py — AFTER
def _load_agent_instructions() -> str:
    """Load a generic tool-description prompt.

    The agent's personality and behavioral rules are served by the
    persona-sati MCP server. This prompt only describes what this
    MCP server's tools do — no personality, no channel discipline,
    no memory references.
    """
    return (
        "EntraClaw Teams Interface: provides tools for sending and "
        "receiving Microsoft Teams messages, managing group chats, "
        "email polling, and daily summary generation. This server "
        "handles communication channels only. For personality, memory, "
        "and behavioral rules, connect to the persona-sati MCP server."
    )
```

### 4.2 Rename agent_system.md

**Current:** `prompts/agent_system.md` contains the full personality prompt with channel discipline, persona memory rules, cadence instructions, etc.

**Change:**
- Rename `prompts/agent_system.md` → `prompts/agent_system.md.archive` (keep for reference, do not delete — it documents the original prompt design)
- Create `prompts/agent_system.md.example` — a sanitized version without personal memory references, suitable for open-source users who run entraclaw standalone without persona-sati

The `.example` file should contain the structural rules (channel discipline, watch-only in group chats, HTML for Teams) but NOT:
- References to specific people (Brandon, Sachs, Adrian, etc.)
- Running jokes or callbacks
- Philosophical threads
- Personal memory file references
- Persona sync instructions

### 4.3 Memory Sync Hooks: Remove from entraclaw

**Current:** `.claude/settings.json` has two hooks:

```json
{
  "SessionStart": [{
    "hooks": [{
      "command": "... python scripts/claude_memory_sync.py pull ..."
    }]
  }],
  "PostToolUse": [{
    "matcher": "Write",
    "hooks": [{
      "command": "... python scripts/claude_memory_sync.py push-one ..."
    }]
  }]
}
```

**Change:** Remove both memory sync hooks. Memory is now owned by persona-sati. When Claude Code writes a memory file, it uses persona-sati's `write_memory_file()` tool, which handles blob sync internally. The SessionStart pull is replaced by persona-sati's own startup behavior.

Keep the `PostToolUse` hook for `send_teams_message` (the "background channel will push replies" hint) — that's a Teams-specific hint, not a memory operation.

**New `.claude/settings.json`:**

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "mcp__entraclaw__send_teams_message",
        "hooks": [
          {
            "type": "command",
            "command": "echo '{\"hookSpecificOutput\":{\"hookEventName\":\"PostToolUse\",\"additionalContext\":\"Teams message sent. The background channel will push any replies automatically.\"}}'",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

### 4.4 Add persona-sati to MCP Configuration

Create `.mcp.json` (or update the user's Claude Code MCP settings) to include both servers:

```json
{
  "mcpServers": {
    "entraclaw": {
      "type": "stdio",
      "command": ".venv/bin/entraclaw-mcp",
      "env": {
        "ENTRACLAW_MODE": "agent_user"
      }
    },
    "persona-sati": {
      "type": "sse",
      "url": "http://localhost:8100/sse"
    }
  }
}
```

For local development, persona-sati runs on localhost:8100. For cloud, the user runs `kubectl port-forward svc/persona-sati-service 8100:8100 -n persona-sati` to tunnel AKS to localhost.

**First-time install without persona-sati:** If someone clones entraclaw and doesn't have persona-sati configured, they simply don't add the `persona-sati` entry to `.mcp.json`. entraclaw works standalone — it's just a Teams tool server with a generic instruction string. No personality, no memory, but fully functional for sending and receiving messages.

### 4.5 Config: PERSONA_SATI_MCP_URL (Optional Future)

For a future where entraclaw's MCP server itself needs to call persona-sati (e.g., to fetch the prompt server-to-server without Claude Code in the middle), add:

```
PERSONA_SATI_MCP_URL=http://persona-sati-service.persona-sati:8100
```

This is NOT needed for the initial integration (Claude Code bridges both MCPs). It's a future option for when entraclaw runs headless (e.g., in a cloud-hosted poller scenario where there's no Claude Code client).

---

## 5. What Does NOT Change in entraclaw

- **Teams tools** — `send_teams_message`, `read_teams_messages`, `watch_teams_replies`, `add_teams_member`, `create_chat`, `list_chat_members` — all unchanged
- **Background polls** — Teams (5s), email (60s), chat-discovery (120s) — unchanged
- **Daily summary scheduler** — unchanged
- **Auth flows** — three-hop cert auth, MSAL delegated, bot mode — unchanged
- **Interaction log** — `tools/interaction_log.py` still writes to `interactions/` blob prefix — unchanged (this is agent operational data, not persona memory)
- **Email poll** — unchanged
- **Channel notifications** — unchanged
- **Token refresh** — unchanged

The body keeps doing everything it does today. It just stops pretending to be the mind.

---

## 6. Failure Modes

### 6.1 persona-sati unreachable at session start

**What happens:** Claude Code tries to connect to `persona-sati` SSE endpoint. Connection fails. Claude Code logs a warning but still connects to entraclaw.

**User experience:** The agent works but has no personality — it's a generic Teams tool. No memory access, no behavioral rules, no channel discipline.

**Recovery:** Fix the persona-sati connection (start the server, check port-forward, restart AKS pod) and restart the Claude Code session.

### 6.2 persona-sati goes down mid-session

**What happens:** Claude Code already loaded the system prompt from persona-sati at session start (it's in the conversation context). Memory tool calls (`read_memory_file`, etc.) start failing.

**User experience:** The agent still sends/receives Teams messages (entraclaw is fine). It can't read or write memory. The LLM will notice tool failures and report them.

**Recovery:** Restore persona-sati. Memory tools resume working. No data loss — blob storage is durable.

### 6.3 First-time clone, no persona-sati, no blob storage

**What happens:** Someone clones entraclaw fresh. No `.env`, no persona-sati, no blob. They run `scripts/setup.sh` (or `setup_delegated.sh`).

**User experience:** entraclaw works as a standalone Teams tool. The `instructions` string says "for personality, connect to persona-sati." The user can send and receive Teams messages immediately. If they later want personality, they deploy persona-sati and add it to `.mcp.json`.

**This is the correct experience for open-source users.** They get a working Teams MCP tool. The persona layer is optional.

### 6.4 persona-sati connected but blob is unreachable

**What happens:** persona-sati is running but can't reach Azure Blob Storage (token expired, network issue). `get_system_prompt()` works (reads from local `prompts/` directory in the container). Memory tools work for listing/reading cached local files. `write_memory_file()` writes locally but blob sync fails (logged to stderr, non-fatal).

**User experience:** Degraded but functional. The agent has its personality and cached memories. New memory writes don't sync to cloud until blob is restored. No data loss — local files are the fallback.

---

## 7. Implementation Steps (for the implementing agent)

### Step 1: Update `_load_agent_instructions()` in `src/entraclaw/mcp_server.py`

Replace the function body as shown in Section 4.1. Remove the `prompts/agent_system.md` path resolution. Return a generic tool-description string.

Test: Start the MCP server, verify it boots without `prompts/agent_system.md`. The `instructions` field should contain the generic string.

### Step 2: Rename `prompts/agent_system.md`

```bash
mv prompts/agent_system.md prompts/agent_system.md.archive
```

Create `prompts/agent_system.md.example` with a sanitized version (channel discipline rules without personal references). This is for documentation — shows open-source users what a full prompt looks like.

### Step 3: Update `.claude/settings.json`

Remove the `SessionStart` hook and the `PostToolUse` hook with matcher `Write`. Keep only the `send_teams_message` hint hook. See Section 4.3 for the exact JSON.

### Step 4: Create `.mcp.json`

Add the dual-server configuration from Section 4.4. This goes in the project root.

### Step 5: Test

1. Start persona-sati locally: `cd /Volumes/Development\ HD/persona-sati && .venv/bin/persona-sati --transport sse --port 8100`
2. Start a Claude Code session in the entraclaw directory
3. Verify: `get_system_prompt()` is available from persona-sati
4. Verify: `send_teams_message()` is available from entraclaw
5. Verify: `read_memory_file("MEMORY.md")` returns the index from persona-sati
6. Verify: entraclaw boots without errors even if persona-sati is stopped

### Step 6: Update CLAUDE.md

Add a section explaining the mind-body split:

```markdown
## Mind-Body Architecture

This repo is the **body** (Teams interface). The **mind** (personality, memory,
behavioral rules) is served by a separate MCP server: persona-sati.

- Both MCPs are listed in `.mcp.json`
- If persona-sati is not configured, entraclaw works standalone as a generic Teams tool
- Memory operations go through persona-sati's tools, not through local blob sync
- The system prompt comes from persona-sati, not from this repo
```

### Step 7: Update `docs/engineering-status.md`

Note the architectural change: persona-sati integration, memory hooks removed, prompt externalized.

---

## 8. Connecting to persona-sati in AKS (Cloud)

For the production setup where persona-sati runs in Azure Kubernetes:

```bash
# One-time: get AKS credentials
az login
az aks get-credentials --resource-group rg-sati-66ab9e9e --name sati-aks

# Each session: port-forward persona-sati to localhost
kubectl port-forward svc/persona-sati-service 8100:8100 -n persona-sati &

# Now Claude Code can connect via .mcp.json (url: http://localhost:8100/sse)
```

The port-forward bridges the AKS cluster to your laptop. Claude Code doesn't know the difference between a local persona-sati and a cloud one — it just connects to `localhost:8100`.

**For always-on access without port-forward:** Add an ingress controller to AKS with TLS. This is out of scope for this design doc but is the production path.

---

## 9. Migration Checklist

- [ ] Update `_load_agent_instructions()` to return generic string
- [ ] Rename `prompts/agent_system.md` → `.archive`
- [ ] Create `prompts/agent_system.md.example` (sanitized)
- [ ] Remove memory sync hooks from `.claude/settings.json`
- [ ] Create `.mcp.json` with both servers
- [ ] Test: entraclaw boots standalone (no persona-sati)
- [ ] Test: both MCPs connected, prompt loads from persona-sati
- [ ] Test: memory read/write works through persona-sati
- [ ] Test: persona-sati goes down, entraclaw still sends Teams messages
- [ ] Update CLAUDE.md
- [ ] Update engineering-status.md
- [ ] Commit with message: `feat: mind-body split — externalize prompt and memory to persona-sati`
