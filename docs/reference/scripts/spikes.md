# Spike scripts

Throwaway research scripts used to validate Graph API shapes or third-party tooling before writing production code. Kept in-tree so future contributors can see what was probed and why.

These are not part of the supported surface. They may break on Graph schema changes. Treat as documentation.

## `spike_a365_work_iq.py`

Inspect local Agent 365 Work IQ setup. Prints the configured Work IQ server metadata from `ToolingManifest.json` and validates that Word is present before implementation begins.

### Usage

```bash
python scripts/spike_a365_work_iq.py
```

Set `ENTRABOT_A365_TOOLING_MANIFEST` to override the manifest path. Defaults: `ToolingManifest.json`, `.a365/ToolingManifest.json`.

### What it does

- Loads the manifest.
- Lists registered Work IQ MCP servers.
- Confirms the Word tool surface is registered.

Used to derisk the A365 Work IQ MCP integration before any wiring in production code. See `docs/platform-docs/microsoft-agent-365.md` for the platform context.

## `spike_file_comments.py`

Capture verbatim Graph beta JSON shape for file comments.

### Usage

```bash
python scripts/spike_file_comments.py <share-url>
```

### What it does

- Mints a fresh Agent User token (picks up just-PATCHed Files / Sites scopes).
- Resolves the SharePoint URL via `GET /shares/{share-id}/driveItem`.
- Dumps raw responses from:
  - `GET /beta/drives/{drive}/items/{item}/comments`
  - `GET /beta/drives/{drive}/items/{item}/comments/{comment_id}/replies` (only if at least one comment exists)

Used to pin down the exact JSON shape before implementing the file-comment MCP tools. The findings are captured in `docs/architecture/PLAN-files-mcp-tools.md`.
