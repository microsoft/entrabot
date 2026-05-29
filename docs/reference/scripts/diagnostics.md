# Diagnostic scripts

Read-only probes for debugging the agent's identity, chat plumbing, sponsor allowlist, and MCP transport. None of these mutate state.

## `diagnose-chat.py`

Test Teams chat creation directly against Graph, bypassing the MCP server. Logs every detail.

### Usage

```bash
python scripts/diagnose-chat.py
```

### What it does

- Acquires an Agent User token.
- Calls `create_or_find_chat()` directly.
- Prints the resulting chat object, member list, and any Graph error details.

Use when `create_chat` is failing inside the MCP server and you need to see the raw Graph response.

## `diagnose_sponsor_emails.py`

Pinpoint why a sponsor's email fields come back null. Read-only. Probes 8 different Graph projections of the sponsor relationship.

### Usage

```bash
./.venv/bin/python scripts/diagnose_sponsor_emails.py
.\.venv\Scripts\python.exe scripts\diagnose_sponsor_emails.py
```

### What it probes

1. `/sponsors` raw nav-collection projection.
2. `/sponsors` with `$select`.
3. `/users/{sid}` via the Agent Identity FIC token.
4. `/users/{sid}` via the Agent User token with `$select`.
5. `/users/{sid}` via the Agent User token without `$select`.
6. `/users` search with `$filter=id eq '{sid}'` via the Agent User token.
7. Agent User `/me` — what does the token see itself as.
8. Decode the Agent User token to show its scopes (no signature verify).

Surface this when `SponsorGate` rejects a known sponsor and the symptom is null `mail`.

## `entrabot-mcp-debug.sh`

Debug wrapper for the `entrabot-mcp` MCP server. Tees the server's stderr to `/tmp/entrabot-debug.log` while passing it through to the parent (Claude Code) so normal error reporting still works.

### Usage

Edit `.mcp.json` so the `entrabot` server's `command` points at this script instead of `.venv/bin/entrabot-mcp` directly:

```json
"command": "scripts/entrabot-mcp-debug.sh"
```

Then tail the log:

```bash
tail -f /tmp/entrabot-debug.log
```

### What it does

- Writes a `===== wrapper start <ts> pid=<pid> =====` marker on every restart so you can tell restarts apart in the shared log.
- Execs the real `entrabot-mcp` with stderr both written to the log and passed through.

### Self-reference defence

The script contains an `entrabot-self-ref-target: ../.venv/bin/entrabot-mcp` marker. `efferent_copy._is_self_referential_peer` reads this marker so peer discovery skips the wrapper and avoids spawning a duplicate `entrabot-mcp`. Without it, swapping `.mcp.json` to point at this wrapper reintroduces the self-spawn cascade originally fixed by PR #36 (Learning #45).

## `list_agent_identities.py`

List Agent Identities under a Blueprint.

### Usage

```bash
# Use the Blueprint from state
python scripts/list_agent_identities.py

# Explicit Blueprint
python scripts/list_agent_identities.py --blueprint-app-id <APP_ID>
```

### What it does

- Mints a Provisioner Graph token.
- Queries the Graph beta API for all Agent Identity service principals.
- Filters to those belonging to the named Blueprint.
- Prints object ID, display name, and creation timestamp for each.

Use when you have multiple chains in the tenant and need to find a specific one.

## `list_sponsors.py`

List sponsors for the Agent Identity.

### Usage

```bash
python scripts/list_sponsors.py
python scripts/list_sponsors.py --agent-object-id <OID>
python scripts/list_sponsors.py --json
```

### What it does

- Reads `AGENT_OBJECT_ID` from `.entrabot-state.json` (or `--agent-object-id`).
- Queries `/servicePrincipals/{agent}/microsoft.graph.agentIdentity/sponsors`.
- Prints sponsor user ID, UPN, and mail for each.

Use as the read companion to `add_agent_sponsor.py` / `remove_agent_sponsor.py`.
