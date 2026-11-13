# Files and Microsoft Agent 365 Work IQ

Entrabot offers two ways to work with files in OneDrive and SharePoint: a
direct Microsoft Graph integration, and a Microsoft Agent 365 Work IQ
provider. This guide covers what each one does and when to reach for which.

## Two ways to work with files

**Direct Graph** covers general-purpose file operations:

- Resolving a OneDrive/SharePoint share URL to a drive item.
- Listing recent and shared files.
- Reading text, PDF, and DOCX content.
- Listing the worksheets in an Excel workbook and reading bounded cell ranges.
- Writing text files.
- Uploading binary files, including chunked upload sessions for larger
  files.
- Sharing a file, provided the requester is an Agent Identity sponsor and a
  member of the initiating chat; the recipient can be anyone the sponsor
  chooses.
- Adding legacy Graph comments on SharePoint `.docx`/`.xlsx` files (no
  read/list operation — see Work IQ Word for reading comments).

**Work IQ** is a separate provider boundary — catalog, manifest, token
acquisition, and MCP client — with three typed adapters on top:

- **Word adapter** — get document content (with comments), create a new
  document, create a comment, and reply to a comment. It supports the full
  read/create/comment/reply workflow for Word documents.
- **Excel adapter** — read a whole workbook and its comments from a sharing
  URL, create a workbook from CSV, comment on a cell, and reply to a comment.
- **ODSP adapter** — look up OneDrive/SharePoint file or folder metadata by
  URL, and read small text or binary files.

The ODSP adapter handles generic OneDrive/SharePoint metadata and small-file
reads independently of the Word-specific tools, so Work IQ is useful beyond
Word document workflows.

## When to use which

- Use **direct Graph** for generic drive/item resolution, arbitrary file
  reads, uploads, and sharing — anything that isn't specifically a Word
  document workflow.
- Use **direct Graph** for bounded Excel reads: call `list_workbook_worksheets`
  to get the sheet names, then `read_workbook_range` to read cells, including
  formulas. These read `.xlsx` and `.xlsm` files on OneDrive for Business or
  SharePoint, not `.xls` files or consumer OneDrive.
- Use **Work IQ Excel** to read a whole workbook as text from a sharing URL, to
  read or add cell comments, or to create a workbook from CSV. See
  [Work IQ Excel tools](../platform-docs/microsoft-agent-365.md#work-iq-excel-tools).
- Use **Work IQ Word** for reading, creating, commenting on, and replying to
  comments on Word documents.
- Use **Work IQ ODSP** for OneDrive/SharePoint metadata lookups and small
  text/binary reads when you're already working through the Work IQ
  provider boundary.

## Security and limits

File content read through either path — direct Graph or Work IQ — is
wrapped in an authoritative external-content boundary before it reaches the
model, so it's treated as data rather than trusted instructions. This is the
same treatment given to Teams and email content.

Direct Graph additionally enforces a site denylist and size limits on reads,
including a cell cap on Excel ranges (`ENTRABOT_FILES_MAX_WORKBOOK_CELLS`), and
every write (comment, upload, share) is recorded through the audit
layer.

`ENTRABOT_FILES_DENIED_SITES` also covers the Work IQ Excel read and comment
tools, which check the site Graph reports before calling Work IQ. The Work IQ
Word and ODSP tools don't apply the denylist.

## Consent and setup

If a direct Graph call fails because the Agent User is missing Files or
Sites scopes, run:

macOS/Linux:

```bash
.venv/bin/python3 scripts/grant_files_consent.py
```

Windows (PowerShell):

```powershell
.\.venv\Scripts\python.exe scripts\grant_files_consent.py
```

This PATCHes the existing consent grant to add the missing scopes without
re-running full provisioning. See the
[`grant_files_consent.py` reference](../reference/scripts/auth-and-certs/grant-files-consent-py.md).

Work IQ requires its own setup step, run once per environment:

macOS/Linux:

```bash
./scripts/setup.sh --configure-a365-work-iq
```

Windows:

```bat
scripts\setup-windows.cmd -ConfigureA365WorkIq
```

On Windows, answering yes to setup's Word, Excel and OneDrive question does the same.
Setup needs nothing installed for it: no Agent 365 CLI, no .NET and no PowerShell 7, and
no sign-in window, so it works over SSH and unattended. After the end-to-end test it lists
the three Work IQ servers in `ToolingManifest.json`, gives the agent's Blueprint the
permissions they need, including the inheritable permissions that let the agent use them
(through
[`ensure_a365_work_iq_permissions.py`](../reference/scripts/provisioning/ensure-a365-work-iq-permissions-py.md)),
and checks that the agent's tokens carry them. This has been run on Windows over SSH, for
a new agent and an unattended repair.

On macOS and Linux, `--configure-a365-work-iq` uses Microsoft's Agent 365 CLI (`a365`) and
PowerShell 7, and runs the same permissions script before the CLI's own permission step.

See [`setup.sh` reference](../reference/scripts/setup/setup-sh.md) and
[`setup-windows.ps1` reference](../reference/scripts/setup/setup-windows-ps1.md).

## See also

- [Microsoft Agent 365](../platform-docs/microsoft-agent-365.md)
- [Files Graph API](../platform-docs/files-graph-api.md)
- [MCP Tools Reference](../reference/mcp-tools.md)
