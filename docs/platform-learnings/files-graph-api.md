# Files & SharePoint Graph API

> **Last updated:** 2026-04-30
> **Context:** Entrabot identity research — research into Microsoft Graph capabilities for documents (Word, Excel, PowerPoint), SharePoint sites, file comments, and sharing, with the goal of giving the Agent User the same document-level powers as a human user.
> **Companion:** [`teams-graph-api.md`](./teams-graph-api.md) covers the Teams/Chat surface; this doc covers everything else a human user does in Microsoft 365 with files.

## Overview

The Microsoft Graph API exposes Microsoft 365 documents through three layered resources:

1. **Drives & DriveItems** (`/drives`, `/me/drive`, `/sites/{site-id}/drive`) — the file-system shape: folders, files, metadata, sharing, comments. The same resource model covers OneDrive (personal), OneDrive for Business, and SharePoint document libraries.
2. **Workbook** (`/drives/{drive-id}/items/{item-id}/workbook/...`) — structured Excel access on top of an `.xlsx` driveItem: worksheets, ranges, tables, formulas, named items, charts.
3. **Search & Sites** (`/search/query`, `/sites/{site-id}`) — discovery across the tenant.

Base URL: `https://graph.microsoft.com/v1.0` (stable) or `/beta` (preview, **required for file comments**).

### Why this matters for Entrabot

Today the Agent User can talk in Teams. It cannot read a spec document, leave a comment on a draft, author a Word/Excel file, or upload it to a SharePoint site. Those are the four core "knowledge worker" verbs a human user does in M365 every day. Graph exposes all of them under the same `Files.ReadWrite` / `Sites.ReadWrite.All` scopes the Agent User can already consent to via the existing third hop (`https://graph.microsoft.com/.default`).

No new auth hop is required — only additional admin consent during `setup.sh`.

---

## Permissions & Scopes

The Agent User gets these via the standard Hop 3 (`acquire_agent_user_token`, `resource_scope="https://graph.microsoft.com/.default"`). Provisioning (`scripts/create_entra_agent_ids.py` + `setup.sh`) must grant admin consent for each scope before the third hop sees them.

| Scope | Covers | Required for |
|-------|--------|--------------|
| `Files.Read` | Read user's own files (delegated) | List, download from `/me/drive` |
| `Files.Read.All` | Read **all files the user has access to** | `sharedWithMe`, items shared by sponsor, files in any site the agent has been added to |
| `Files.ReadWrite` | Write to user's own OneDrive | Create + upload spec to agent's own OneDrive |
| `Files.ReadWrite.All` | Write to any file the user has access to | Edit spec stored in a shared site, add/reply to comments on shared files |
| `Sites.Read.All` | Read SharePoint sites | Site discovery, document-library traversal |
| `Sites.ReadWrite.All` | Write to SharePoint sites | Upload to SharePoint document libraries (vs. only the agent's OneDrive) |

Recommended baseline for Entrabot: `Files.ReadWrite.All` + `Sites.ReadWrite.All`. The pair gives the agent the same file capabilities as a human user without making it a tenant-wide admin.

`Files.ReadWrite` (delegated, user-scoped) is sufficient for "agent only writes to its own OneDrive" but the moment the agent needs to touch a sponsor-shared file, you need `.All`.

> **Anti-pattern:** Don't use `Files.ReadWrite.AppFolder`. That scope sandboxes the app to a hidden `Apps/<app-name>/` folder and isn't visible in the user's OneDrive UI. Wrong shape for an Agent User that should look like a person.

---

## File Discovery

### 1. List files shared with the agent user

```
GET /v1.0/me/drive/sharedWithMe
```

Returns a `driveItem` collection covering everything any other user (including the sponsor) has explicitly shared with the agent. The `remoteItem` field carries the original `driveId` and `id` — you need both to address the source file (`/drives/{remoteItem.driveId}/items/{remoteItem.id}`).

**Permission:** `Files.Read.All` (delegated).

**Idempotency note:** This is a discovery list — it changes whenever the sponsor shares or unshares something. Cache with a short TTL (≤ 5 minutes) and a delta query if traffic warrants it.

### 2. Search across files

```
POST /v1.0/search/query
Content-Type: application/json

{
  "requests": [{
    "entityTypes": ["driveItem"],
    "query": { "queryString": "Q3 roadmap filetype:docx" },
    "from": 0, "size": 25,
    "fields": ["name", "webUrl", "createdDateTime", "lastModifiedDateTime",
               "parentReference", "driveItem"]
  }]
}
```

Supports KQL syntax: `filetype:docx`, `path:"https://contoso.sharepoint.com/sites/Engineering"`, `author:"the user"`, `lastModifiedTime>2026-01-01`. Hits arrive as `driveItem` resources you can pipe into the same download/comment endpoints.

**Permission:** `Files.Read.All` and/or `Sites.Read.All`.

**Throttling:** Search is rate-limited at the tenant level. Don't call it on every turn — gate behind an explicit user request ("find me…") or a TTL cache.

### 3. List sites & document libraries

```
GET /v1.0/sites?search={keyword}
GET /v1.0/sites/{site-id}/drives
GET /v1.0/sites/{hostname}:/sites/{path}     # site by URL path
```

Once you have a `driveId`, every other DriveItem endpoint works the same way for SharePoint as for OneDrive.

---

## File Content Access

### Download raw bytes

```
GET /v1.0/drives/{drive-id}/items/{item-id}/content
```

Returns the file's raw bytes (302-redirect to a pre-signed URL on the back end; `httpx.Client(follow_redirects=True)`).

### Format conversion (Office files only)

```
GET /v1.0/drives/{drive-id}/items/{item-id}/content?format=pdf
```

Server-side conversion to PDF. Accepted source formats: `doc`, `docx`, `eml`, `htm/html`, `md`, `msg`, `odp`, `ods`, `odt`, `pps/ppsx`, `ppt/pptx`, `rtf`, `xls/xlsx`. **No `?format=text` and no `?format=md`** — for plain-text extraction the agent must download the bytes and parse client-side (`python-docx` for `.docx`, `openpyxl` for `.xlsx`, `pypdf` for `.pdf`).

**Useful trick:** PDF is the universal common denominator. Convert `.docx` / `.pptx` / `.xlsx` → PDF on the server, then use a single PDF-text extractor on the agent side. Fewer parser dependencies, identical text quality for read-only "ingest a spec" use cases.

### Markdown upload + auto-conversion

When uploading a file with `name="spec.md"` to OneDrive, SharePoint **does not auto-convert** it to `.docx`. To produce a Word document from agent-authored Markdown:

1. Convert Markdown → DOCX client-side (`pandoc`, `python-docx`, or `mistune` + custom OOXML).
2. Upload the resulting `.docx` via the upload endpoint below.

There is no Graph endpoint that ingests Markdown and emits Word.

---

## Excel / Workbook API

The Workbook API is structured access on top of an `.xlsx` driveItem — significantly richer than reading raw bytes.

### Read a range

```
GET /v1.0/drives/{drive-id}/items/{item-id}/workbook/worksheets/{sheet-name-or-id}/range(address='A1:D4')
```

Response includes both `values` (rendered cell values, type-coerced to JSON) and `formulas` (the original `=...` strings). Exactly the shape an LLM wants.

### Write a range

```
PATCH /v1.0/drives/{drive-id}/items/{item-id}/workbook/worksheets/{sheet}/range(address='A1:B2')
Content-Type: application/json

{ "values": [["Name", "Score"], ["Brandon", 42]] }
```

### Tables (the high-leverage shape)

```
GET  .../workbook/tables                         # list tables in workbook
GET  .../workbook/tables/{name}/rows             # all rows
POST .../workbook/tables/{name}/rows/add         # append rows
PATCH .../workbook/tables/{name}/columns/{col}  # rename / reorder
```

Tables are first-class in Excel. Every "I'm tracking X in a spreadsheet" use case is a table — the agent should always prefer table operations over raw range writes when a table exists, because tables auto-extend and survive sort/filter.

### Workbook sessions (atomic batches)

```
POST /v1.0/drives/{drive-id}/items/{item-id}/workbook/createSession
{ "persistChanges": true }
→ returns { "id": "<session-id>" }
```

Pass `Workbook-Session-Id: <session-id>` on every subsequent request, then either `POST .../workbook/closeSession` (commits) or let it expire after 5 minutes idle (rolls back if `persistChanges=false`).

Sessions exist for two reasons: (1) atomic multi-write batches, and (2) **performance** — without a session, every write reloads the workbook server-side. With one, Excel keeps it in memory.

**Entrabot discipline:** Open a session for any sequence ≥ 3 writes. Always close it. Treat session-id leaks as a bug.

### Permissions

Same as DriveItem: `Files.ReadWrite` (own OneDrive) or `Files.ReadWrite.All` + `Sites.ReadWrite.All` (anywhere). Workbook does **not** need a separate scope.

---

## File Comments (BETA only)

```
# List
GET    /beta/drives/{drive-id}/items/{item-id}/comments

# Add a top-level comment
POST   /beta/drives/{drive-id}/items/{item-id}/comments
{ "content": "Question about section 3 — should this also cover the macOS path?" }

# Reply to an existing comment
POST   /beta/drives/{drive-id}/items/{item-id}/comments/{comment-id}/replies
{ "content": "Yes — see PR #58 which already does that." }

# List replies on a comment
GET    /beta/drives/{drive-id}/items/{item-id}/comments/{comment-id}/replies

# Delete (own comments only)
DELETE /beta/drives/{drive-id}/items/{item-id}/comments/{comment-id}
```

**Hard limitations to encode in the tool layer:**

| Constraint | Implication for Entrabot |
|------------|---------------------------|
| **Beta only** — no `v1.0` equivalent | Tool docstrings must say "uses Graph beta; subject to breaking change." Pin `httpx` baseURL on the `/beta` host explicitly for these calls. |
| **Word + Excel only** — PowerPoint comments unsupported | The `add_file_comment` tool must reject `.pptx` files with a clear error. Don't silently 404. |
| **OneDrive for Business + SharePoint only** — personal OneDrive unsupported | Reject if `drive.driveType == "personal"`. |
| **Files only — no folder comments** | Reject driveItems where `folder` is set. |
| **No anchored comments** — comments are document-level, not range/cell-level | Set expectations in the tool: this is a "general comment," not "comment on cell B7." |
| **No @-mentions via Graph** — even though the Office UI supports them | The agent can't tag the sponsor. The sponsor still sees the comment in their notification feed because they own/co-author the file. |

**Permissions:** `Files.ReadWrite` (own files) or `Files.ReadWrite.All` (shared files) — same as the DriveItem comment endpoints.

**Throttling:** Comments inherit the general DriveItem rate budget (≈10,000 requests / 10 min / app / tenant). Comments are unlikely to be the bottleneck unless the agent is in a tight comment-reply loop with the sponsor — back off on `429` with `Retry-After`.

---

## Sharing

Two distinct mechanisms with different trust shapes:

### `createLink` — generate a sharing URL

```
POST /v1.0/drives/{drive-id}/items/{item-id}/createLink
{
  "type": "view",          # or "edit", or "embed"
  "scope": "organization", # or "anonymous", or "users"
  "expirationDateTime": "2026-05-15T23:59:00Z",
  "password": "optional"
}
```

Returns a `sharingLink.webUrl` you can paste in chat or email. **Anyone with the link** (within the chosen scope) can open the file.

Use case fit for Entrabot: rare. Anonymous links are a secret-leak hazard; org-scoped links bypass the sponsor-only model.

### `invite` — explicit user invitation

```
POST /v1.0/drives/{drive-id}/items/{item-id}/invite
{
  "recipients": [ { "email": "sponsor@contoso.com" } ],
  "roles":          ["read"],         # or ["write"]
  "requireSignIn":  true,
  "sendInvitation": true,
  "message":        "I drafted the v2 onboarding spec — ready for your review."
}
```

Permissions are scoped per-recipient and survive link revocation. **This is the right primitive for Entrabot**: matches the sponsor-only trust model (the agent shares only with people on its sponsor list), creates an audit trail, and the recipient sees the file in their `sharedWithMe`.

**Discipline for the `share_file` tool:** validate that every `recipients[*].email` matches a sponsor on the Agent Identity's Graph sponsors relationship before calling Graph. If a non-sponsor email appears, reject in the tool, not at Graph — Graph will accept any tenant user, but Entrabot's policy is sponsor-only.

### Listing & revoking permissions

```
GET    /v1.0/drives/{drive-id}/items/{item-id}/permissions
DELETE /v1.0/drives/{drive-id}/items/{item-id}/permissions/{perm-id}
```

Useful for the `share_file` tool to first check whether the sponsor is already invited and skip the redundant call (idempotency).

---

## Upload

### Small file (< 4 MB)

```
PUT /v1.0/drives/{drive-id}/items/{parent-id}:/{filename}:/content
Content-Type: <mime-type>
<binary body>
```

One request, returns the new `driveItem`. Use `@microsoft.graph.conflictBehavior` (`rename` / `replace` / `fail`) on the URL or via JSON wrapper for finer control.

### Large file (chunked, ≥ 4 MB)

```
# 1. Create the upload session
POST /v1.0/drives/{drive-id}/items/{parent-id}:/{filename}:/createUploadSession
{ "item": { "@microsoft.graph.conflictBehavior": "rename", "name": "spec.docx" } }
→ returns { "uploadUrl": "https://...", "expirationDateTime": "..." }

# 2. PUT chunks of 320 KiB × N (Microsoft recommends 5–10 MiB) to uploadUrl
PUT <uploadUrl>
Content-Length: <chunk-size>
Content-Range: bytes 0-1048575/16777216
<chunk bytes>
```

**Chunk size rule:** must be a multiple of 320 KiB (327,680 bytes). Microsoft recommends 5–10 MiB chunks for best throughput. Last chunk's `Content-Range` must include the actual total file size — that's how the server knows the upload is complete (responds `201 Created` with the new `driveItem`).

**Resilience:** if a chunk fails, you can `GET <uploadUrl>` to ask "where am I?" and resume from the last accepted byte. The session is good for ~7 days.

For Entrabot, treat 4 MB as the small/large boundary: Word specs are virtually always small; PowerPoints with media are virtually always large. Pick one upload primitive based on `os.path.getsize()`.

---

## PowerPoint — the gap

Graph's PowerPoint surface is materially weaker than Word/Excel:

- ✅ Upload, download, share, format-convert (`?format=pdf`) — all work via DriveItem.
- ❌ **Comment APIs do not work on `.pptx`** (`Files.ReadWrite.All` returns 4xx).
- ❌ **No structured slide manipulation** in Graph. The `presentation` resource exists in `/beta` but exposes only metadata (slide titles, count) — no shape/text/image creation, no layout control.
- ❌ **No "create blank presentation"** endpoint.

If Entrabot needs to author a PowerPoint, the right architecture is:

1. Generate the `.pptx` client-side using [`python-pptx`](https://python-pptx.readthedocs.io/) (full slide / shape / image / chart control, MIT license, no Microsoft dependency).
2. Upload the result via the chunked upload endpoint above.
3. Share via `invite`.

For commenting on a PowerPoint, there is no Graph path. The sponsor can still see the file in `sharedWithMe`, but the agent cannot leave a programmatic comment. Document this in the `add_file_comment` tool error message so the model can route the user to a different channel (Teams reply quoting the slide).

---

## Throttling

General DriveItem and Workbook traffic is bounded by:

- **Per-app, per-tenant:** ~10,000 requests / 10 minutes for file APIs.
- **Per-user:** lower — exact limit not published; expect a few hundred req/min before `429`.
- **Workbook sessions:** session creation itself is rate-limited; reuse a session across calls.
- **Search:** stricter; treat as a turn-gated tool, not a per-tool-call lookup.

Always honor `Retry-After`. The standard Entrabot retry harness (`_with_token_retry`) does not currently handle `429` — wrap Graph calls in a separate `_with_throttle_retry` helper that backs off on 429/503.

---

## Concrete mapping to scenarios

| Scenario | Endpoints used | Tools needed |
|---|---|---|
| Sponsor shares a Word spec with the agent → agent reads it → agent leaves a clarifying comment | `GET /me/drive/sharedWithMe` → `GET /drives/{d}/items/{i}/content?format=pdf` (or raw `.docx`) → `POST /beta/drives/{d}/items/{i}/comments` | `list_shared_files`, `read_file`, `add_file_comment` |
| Agent drafts a spec → uploads to SharePoint → shares with sponsor | (client-side md→docx) → `POST /sites/{s}/drives/{d}/items/{p}:/{n}:/createUploadSession` → chunked `PUT` → `POST /drives/{d}/items/{i}/invite` | `create_word_doc` (or `create_excel_workbook`), `upload_to_sharepoint`, `share_file` |
| Agent runs a quarterly report and pastes results into Excel | Workbook session → `PATCH range` or `POST tables/.../rows/add` | `excel_read_range`, `excel_write_range`, `excel_append_table_rows` |
| Sponsor replies to the agent's comment → agent follows up | `GET /beta/drives/{d}/items/{i}/comments/{c}/replies` (poll or webhook) → `POST .../replies` | `list_file_comments`, `reply_to_file_comment` |
| Agent searches "find the spec we wrote about Windows port" | `POST /search/query` with `entityTypes=["driveItem"]` | `search_files` |

---

## References

- [DriveItem resource](https://learn.microsoft.com/en-us/graph/api/resources/driveitem) — file/folder model
- [sharedWithMe](https://learn.microsoft.com/en-us/graph/api/drive-sharedwithme)
- [createUploadSession](https://learn.microsoft.com/en-us/graph/api/driveitem-createuploadsession)
- [Workbook resource](https://learn.microsoft.com/en-us/graph/api/resources/workbook)
- [Workbook session](https://learn.microsoft.com/en-us/graph/api/workbook-createsession)
- [Comment resource (beta)](https://learn.microsoft.com/en-us/graph/api/resources/comment?view=graph-rest-beta)
- [createLink](https://learn.microsoft.com/en-us/graph/api/driveitem-createlink) / [invite](https://learn.microsoft.com/en-us/graph/api/driveitem-invite)
- [Microsoft Search API](https://learn.microsoft.com/en-us/graph/search-concept-files)
- [Throttling](https://learn.microsoft.com/en-us/graph/throttling)
- [python-pptx](https://python-pptx.readthedocs.io/) — third-party PowerPoint authoring
