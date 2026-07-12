# Files and SharePoint Graph API

Entrabot works with OneDrive and SharePoint files two ways: a direct Microsoft
Graph integration (`src/entrabot/tools/files.py`) for general file operations,
and the Microsoft Agent 365 Work IQ provider (`src/entrabot/a365/`) for Word
document authoring and comments. This page is the platform reference for the
direct-Graph surface and where Work IQ takes over. For the practical guide to
choosing between them, see the
[Files and Work IQ guide](../guides/files-and-work-iq.md).

Microsoft Graph exposes Microsoft 365 documents through three layered resources:

1. **Drives and DriveItems** (`/drives`, `/me/drive`, `/sites/{site-id}/drive`) —
   the file-system shape: folders, files, metadata, sharing, comments. The same
   model covers OneDrive (personal), OneDrive for Business, and SharePoint
   document libraries.
2. **Workbook** (`/drives/{drive-id}/items/{item-id}/workbook/...`) — structured
   Excel access on top of an `.xlsx` DriveItem: worksheets, ranges, tables,
   formulas, named items, charts.
3. **Search and Sites** (`/search/query`, `/sites/{site-id}`) — discovery across
   the tenant.

Base URL: `https://graph.microsoft.com/v1.0` (stable) or `/beta` (required for
file comments). The Agent User obtains these scopes through the standard third
hop (`https://graph.microsoft.com/.default`); no additional auth hop is required,
only admin consent for the scopes.

## Permissions and scopes

| Scope | Covers | Required for |
|---|---|---|
| `Files.Read` | Read the user's own files | List/download from `/me/drive` |
| `Files.Read.All` | Read all files the user can access | `sharedWithMe`, sponsor-shared items, files in accessible sites |
| `Files.ReadWrite` | Write to the user's own OneDrive | Create/upload to the agent's OneDrive |
| `Files.ReadWrite.All` | Write to any accessible file | Edit shared-site files, add/reply to comments |
| `Sites.Read.All` | Read SharePoint sites | Site discovery, library traversal |
| `Sites.ReadWrite.All` | Write to SharePoint sites | Upload to SharePoint libraries |

Recommended baseline: `Files.ReadWrite.All` + `Sites.ReadWrite.All`. The pair
gives the agent the same file capabilities as a human user without tenant-wide
admin rights. Do not use `Files.ReadWrite.AppFolder`: it sandboxes the app to a
hidden `Apps/<app-name>/` folder that is not visible in the user's OneDrive UI —
the wrong shape for an Agent User that should look like a person.

## File discovery

### Files shared with the agent

```http
GET /v1.0/me/drive/sharedWithMe
```

Returns a `driveItem` collection of everything shared with the Agent User. The
`remoteItem` field carries the original `driveId` and `id`; both are needed to
address the source file (`/drives/{remoteItem.driveId}/items/{remoteItem.id}`).
This is a discovery list that changes as sharing changes — cache with a short TTL.
Permission: `Files.Read.All`.

### Search

```http
POST /v1.0/search/query
{
  "requests": [{
    "entityTypes": ["driveItem"],
    "query": { "queryString": "Q3 roadmap filetype:docx" },
    "from": 0, "size": 25
  }]
}
```

Supports KQL (`filetype:docx`, `path:"..."`, `author:"..."`,
`lastModifiedTime>2026-01-01`). Search is rate-limited at the tenant level; gate
it behind an explicit user request or a TTL cache. Permissions: `Files.Read.All`
and/or `Sites.Read.All`.

### Sites and libraries

```http
GET /v1.0/sites?search={keyword}
GET /v1.0/sites/{site-id}/drives
GET /v1.0/sites/{hostname}:/sites/{path}
```

Once you have a `driveId`, every DriveItem endpoint behaves the same for
SharePoint as for OneDrive.

## File content access

### Download bytes

```http
GET /v1.0/drives/{drive-id}/items/{item-id}/content
```

Returns the file's raw bytes (via a 302 redirect to a pre-signed URL — follow
redirects).

### Format conversion

```http
GET /v1.0/drives/{drive-id}/items/{item-id}/content?format=pdf
```

Server-side conversion to PDF from `doc`, `docx`, `eml`, `htm/html`, `md`,
`msg`, `odp`, `ods`, `odt`, `pps/ppsx`, `ppt/pptx`, `rtf`, `xls/xlsx`. There is
**no `?format=text` and no `?format=md`** — for plain-text extraction, download
the bytes and parse client-side (`python-docx`, `openpyxl`, `pypdf`). PDF is a
convenient common denominator: convert Office files to PDF and use one text
extractor for read-only ingestion.

SharePoint does not auto-convert an uploaded `.md` file to `.docx`. To produce a
Word document from Markdown, convert client-side and upload the resulting
`.docx`.

## Excel / Workbook API

The Workbook API is structured access on top of an `.xlsx` DriveItem.

```http
GET   .../workbook/worksheets/{sheet}/range(address='A1:D4')
PATCH .../workbook/worksheets/{sheet}/range(address='A1:B2')
GET   .../workbook/tables
GET   .../workbook/tables/{name}/rows
POST  .../workbook/tables/{name}/rows/add
```

Range reads return both `values` (rendered) and `formulas` (original `=...`
strings). Tables are first-class in Excel and auto-extend on append, so prefer
table operations over raw range writes when a table exists.

For atomic multi-write batches and performance, open a workbook session:

```http
POST .../workbook/createSession
{ "persistChanges": true }
```

Pass `Workbook-Session-Id: <session-id>` on subsequent requests and close the
session with `POST .../workbook/closeSession`, or let it expire after 5 minutes
idle. Workbook access uses the same `Files.ReadWrite` / `Files.ReadWrite.All` +
`Sites.ReadWrite.All` scopes; no separate scope is needed.

## Comments

There are two distinct comment surfaces. Route to the correct one:

### Word comments — use Work IQ (production path)

Word document UI comments and replies go through the Agent 365 Work IQ Word
adapter (`add_word_comment`, `reply_to_word_comment`, `read_word_document`),
**not** direct Graph. This is the supported path for reading and writing Word
document comments. See [Microsoft Agent 365 and Work IQ](microsoft-agent-365.md).

### Legacy direct-Graph comments

Direct Graph exposes a beta comments endpoint, retained for compatibility with
the Files surface:

```http
GET    /beta/drives/{drive-id}/items/{item-id}/comments
POST   /beta/drives/{drive-id}/items/{item-id}/comments
POST   /beta/drives/{drive-id}/items/{item-id}/comments/{comment-id}/replies
DELETE /beta/drives/{drive-id}/items/{item-id}/comments/{comment-id}
```

Constraints Entrabot enforces before calling this endpoint:

| Constraint | Behavior |
|---|---|
| **Beta only** — no v1.0 equivalent | Subject to breaking change; pinned to the `/beta` host. |
| **`.docx` and `.xlsx` only** | Other extensions (including `.pptx`, `.pdf`, `.md`) are rejected. |
| **SharePoint team sites only** | Personal OneDrive and OneDrive-for-Business (MySite) drives return `404 itemNotFound`; both are rejected up front. |
| **Files only** | Folder DriveItems are rejected. |
| **Document-level, not anchored** | Comments are general, not tied to a range or cell. |
| **No @-mentions via Graph** | The agent cannot tag the sponsor; the sponsor still sees the comment as a co-author. |

Permissions: `Files.ReadWrite` (own) or `Files.ReadWrite.All` (shared).

## Sharing

Two mechanisms with different trust shapes.

### `createLink`

```http
POST /v1.0/drives/{drive-id}/items/{item-id}/createLink
{ "type": "view", "scope": "organization", "expirationDateTime": "..." }
```

Returns a `sharingLink.webUrl`. Anyone with the link (within the chosen scope)
can open the file. Anonymous links are a leak hazard and org-scoped links bypass
a sponsor-only model, so this is rarely the right primitive for Entrabot.

### `invite`

```http
POST /v1.0/drives/{drive-id}/items/{item-id}/invite
{
  "recipients": [ { "email": "recipient@contoso.com" } ],
  "roles": ["read"],
  "requireSignIn": true,
  "sendInvitation": true
}
```

Permissions are scoped per recipient, survive link revocation, and create an
audit trail; the recipient sees the file in their `sharedWithMe`. This is the
right primitive for Entrabot.

**Authorization model — the requester is sponsor-gated, not the recipient.**
Entrabot's `share_file` enforces three checks before calling Graph `/invite`:

1. **The requester is a sponsor.** The human who directed the share
   (`requester_email`) must match a sponsor on the Agent Identity's Graph
   sponsors relationship (all email forms accepted: UPN, mail, otherMails,
   proxyAddresses, federated and decoded B2B addresses).
2. **The requester has a recent active-channel binding for the supplied
   `chat_id`.** The matched sponsor must have an active-channel binding — a
   record that the server has recently pushed an inbound message from that
   sponsor in that chat — and the bound chat must match the `chat_id`
   supplied to `share_file`. This defends against an agent being tricked into
   sharing a file under a different chat's authority than the one it's
   actually in, even when the sponsor is a genuine member of both chats.
3. **The requester is a member of the initiating chat, per Graph.** The
   sponsor's user ID must also appear in the `chat_id`'s member list, as a
   defense-in-depth check independent of the active-channel binding.

The **recipient** is not sponsor-gated — a sponsor may share with anyone they
choose, and the recipient is passed through to Graph `/invite`. Existing
permissions can be listed and revoked:

```http
GET    /v1.0/drives/{drive-id}/items/{item-id}/permissions
DELETE /v1.0/drives/{drive-id}/items/{item-id}/permissions/{perm-id}
```

## Upload

Small file (< 4 MB):

```http
PUT /v1.0/drives/{drive-id}/items/{parent-id}:/{filename}:/content
```

Use `@microsoft.graph.conflictBehavior` (`rename` / `replace` / `fail`) for
conflict handling. Larger files use a chunked upload session:

```http
POST /v1.0/drives/{drive-id}/items/{parent-id}:/{filename}:/createUploadSession
```

Then `PUT` chunks to the returned `uploadUrl`. Chunk size must be a multiple of
320 KiB (327,680 bytes); Microsoft recommends 5–10 MiB. The final chunk's
`Content-Range` must include the actual total size, which signals completion
(the server responds `201 Created` with the new DriveItem). A failed chunk can be
resumed by `GET`ing the `uploadUrl` to find the last accepted byte; the session
lasts about 7 days.

## Limitations

- **PowerPoint.** DriveItem operations (upload, download, share, `?format=pdf`)
  work, but there is no comment API for `.pptx`, no structured slide
  manipulation in Graph, and no "create blank presentation" endpoint. To author
  a deck, generate the `.pptx` client-side (for example with `python-pptx`) and
  upload it. There is no Graph path to comment on a PowerPoint.
- **Personal OneDrive and OneDrive-for-Business comments** are not supported by
  the beta comments endpoint (see the comments constraints above).
- **No range/cell-anchored comments and no @-mentions** through the comments
  API.

## Throttling

- Per app, per tenant: roughly 10,000 file-API requests / 10 minutes.
- Per user: lower — expect a few hundred requests/minute before `429`.
- Search is stricter; treat it as a turn-gated tool, not a per-call lookup.
- Reuse workbook sessions rather than re-creating them.

Always honor `Retry-After`, and back off on `429`/`503`.

## References

- [DriveItem resource](https://learn.microsoft.com/en-us/graph/api/resources/driveitem)
- [driveItem: sharedWithMe](https://learn.microsoft.com/en-us/graph/api/drive-sharedwithme)
- [driveItem: createUploadSession](https://learn.microsoft.com/en-us/graph/api/driveitem-createuploadsession)
- [Workbook resource](https://learn.microsoft.com/en-us/graph/api/resources/workbook)
- [Workbook: createSession](https://learn.microsoft.com/en-us/graph/api/workbook-createsession)
- [comment resource (beta)](https://learn.microsoft.com/en-us/graph/api/resources/comment?view=graph-rest-beta)
- [driveItem: createLink](https://learn.microsoft.com/en-us/graph/api/driveitem-createlink) / [driveItem: invite](https://learn.microsoft.com/en-us/graph/api/driveitem-invite)
- [Microsoft Search API](https://learn.microsoft.com/en-us/graph/search-concept-files)
- [Throttling](https://learn.microsoft.com/en-us/graph/throttling)
- [Microsoft Agent 365 and Work IQ](microsoft-agent-365.md)
