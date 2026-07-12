# Files and Microsoft Agent 365 Work IQ

EntraBot has two shipped paths for working with files in OneDrive and
SharePoint: a direct Microsoft Graph integration, and a Microsoft Agent 365
Work IQ provider. Both are implemented and available — this guide covers what
each one does and when to reach for which.

## Two paths, not a proposal

**Direct Graph (`src/entrabot/tools/files.py`)** covers general-purpose file
operations:

- Resolving a OneDrive/SharePoint share URL to a drive item.
- Listing recent and shared files.
- Reading text, PDF, and DOCX content.
- Writing text files.
- Uploading binary files, including chunked upload sessions for larger
  files.
- Sharing a file, provided the requester is an Agent Identity sponsor and a
  member of the initiating chat; the recipient can be anyone the sponsor
  chooses.
- Adding legacy Graph comments on SharePoint `.docx`/`.xlsx` files (no
  read/list operation — see Work IQ Word for reading comments).

**Work IQ (`src/entrabot/a365/`)** is a separate provider boundary — catalog,
manifest, token acquisition, and MCP client — with two typed adapters on top:

- **Word adapter** — get document content (with comments), create a new
  document, create a comment, and reply to a comment. This is the production
  path for Word document read/create/comment/reply workflows.
- **ODSP adapter** — look up OneDrive/SharePoint file or folder metadata by
  URL, and read small text or binary files.

Work IQ is not Word-only: the ODSP adapter handles generic OneDrive/SharePoint
metadata and small-file reads independently of the Word-specific tools.

## When to use which

- Use **direct Graph** for generic drive/item resolution, arbitrary file
  reads, uploads, and sharing — anything that isn't specifically a Word
  document workflow.
- Use **Work IQ Word** for reading, creating, commenting on, and replying to
  comments on Word documents — this is the current production path for that
  workflow, not the legacy Graph comment API.
- Use **Work IQ ODSP** for OneDrive/SharePoint metadata lookups and small
  text/binary reads when you're already working through the Work IQ
  provider boundary.

## Security and bounds

File content read through either path — direct Graph or Work IQ — is
wrapped as external, model-facing content through the XPIA boundary before
it reaches the model, the same treatment given to Teams and email content.

Direct Graph additionally enforces a site denylist and size limits on reads,
and every write (comment, upload, share) is recorded through the audit
layer.

## Consent and setup

If a direct Graph call fails because the Agent User is missing Files or
Sites scopes, run:

```bash
python3 scripts/grant_files_consent.py
```

This PATCHes the existing consent grant to add the missing scopes without
re-running full provisioning. See the
[`grant_files_consent.py` reference](../reference/scripts/auth-and-certs/grant-files-consent-py.md).

Work IQ requires its own setup step, run once per environment:

```bash
./scripts/setup.sh --configure-a365-work-iq
```

```powershell
.\scripts\setup-windows.ps1 -ConfigureA365WorkIq
```

See [`setup.sh` reference](../reference/scripts/setup/setup-sh.md) and
[`setup-windows.ps1` reference](../reference/scripts/setup/setup-windows-ps1.md).

## See also

- [Microsoft Agent 365](../platform-docs/microsoft-agent-365.md)
- [Files Graph API](../platform-docs/files-graph-api.md)
- [MCP Tools Reference](../reference/mcp-tools.md)
