# Agent prompt: wire entraclaw to consume `PERSONA_SATI_MCP_URL`

**Status: DONE (2026-04-18, v1).** Implemented in `src/entraclaw/mcp_server.py:_load_agent_instructions()`. This prompt is kept as a reference example of how to delegate a scoped, self-contained task to a fresh Claude Code session — the spec below is what shipped. For the historical TODO doc see `docs/TODO-persona-sati-integration.md`.

---

Paste this into a fresh Claude Code session running in the `entraclaw-identity-research` directory.

---

```
You are completing TODO 4 from the persona-sati project's two-command
plan. The full spec is already written in this repo at:

  docs/TODO-persona-sati-integration.md

Read that file end-to-end before writing any code. It contains:
- Why this is being done (body/mind separation; entraclaw should pull
  its system prompt from the cloud persona-sati, falling back to a
  local string when persona-sati is unavailable).
- The exact function replacement for _load_agent_instructions() in
  src/entraclaw/mcp_server.py.
- Four test cases to add.
- A verification checklist.

Your task in order:

1. Read docs/TODO-persona-sati-integration.md end-to-end.

2. Replace the existing `_load_agent_instructions()` in
   src/entraclaw/mcp_server.py with the reference implementation from
   the TODO doc. Do not change anything else in that file; only that
   one function.

3. Add the four unit tests described in the TODO doc. Put them in
   tests/test_mcp_server_integration.py if that file exists, or create
   tests/test_load_agent_instructions.py. Use pytest's monkeypatch for
   env vars and unittest.mock.patch for subprocess + asyncio.run +
   the MCP client path.

4. Run the existing test suite plus the new tests:
     pytest -v --tb=short
     ruff check .
   Both must pass before you commit.

5. Commit on branch `feat/persona-sati-client-integration` with a
   message that summarizes: "_load_agent_instructions() now pulls
   the prompt from persona-sati when PERSONA_SATI_MCP_URL is set;
   falls back to the local string on any failure."

6. Push the branch and open a PR against main. Body should include:
     - Cross-reference to docs/TODO-persona-sati-integration.md
     - Confirmation that all four test cases pass
     - Confirmation that the fallback path is tested (boot must succeed
       even when persona-sati is offline)
     - Note that this completes TODO 4 of
       persona-sati/docs/plans/remaining-work-to-two-command-goal.md

DO NOT:
- Edit any file other than src/entraclaw/mcp_server.py and a test
  file.
- Add new dependencies. The mcp package is already a dep (this repo
  IS an MCP server).
- Remove or change the local fallback prompt text. Downstream tests
  rely on its prefix "EntraClaw Teams Interface".
- Write anything to stdout from _load_agent_instructions() or the
  code paths it calls. All diagnostic logging goes to sys.stderr.
- Let any exception escape _load_agent_instructions(). The MCP server
  boot must succeed even if persona-sati is completely unreachable.
- Commit .entraclaw-state.json, any backup files, or local secrets.

When the PR is open, reply with:
  (a) PR URL
  (b) pytest + ruff output (final lines — passed counts)
  (c) confirmation that each of the 4 test cases passes
  (d) the diff stat (files/lines changed)
```

---

## Before you paste the prompt

Make sure a fresh Claude Code session in this repo can see the TODO doc:

```bash
ls docs/TODO-persona-sati-integration.md
```

And optionally back up the current mcp_server.py in case you want to compare:

```bash
cp src/entraclaw/mcp_server.py /tmp/mcp_server.py.before
```
