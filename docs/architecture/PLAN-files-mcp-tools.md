# PLAN: SharePoint / Files / Excel MCP Tools (V1)

> **Status:** PR1 IMPLEMENTED 2026-04-30 on `feat/files-graph-api`. 4 tools (resolve_file_url, list_recent_files, read_file, add_file_comment) + eval scaffold + 50 unit tests + 5 eval traces all passing on `pytest -m eval`. Tenant consent grant extended to include `Files.Read.All Sites.Read.All Sites.ReadWrite.All` (idempotent PATCH path covers existing tenants on next setup.sh run). PR2 (author/upload/share) and PR3 (Excel reads) deferred to follow-up sessions.
> **Branch:** `feat/files-graph-api`
> **Author:** office-hours session 2026-04-30, locked by CEO review 2026-04-30, eng review 2026-04-30, PR1 shipped 2026-04-30
> **Companion (research):** [`docs/platform-learnings/files-graph-api.md`](../platform-learnings/files-graph-api.md)
> **Companion (deferred Office authoring):** [`docs/architecture/PLAN-files-llm-authoring-v2.md`](./PLAN-files-llm-authoring-v2.md)
> **Approach:** narrow MCP tools, two scenarios, markdown-only V1 authoring, sponsor-only sharing, configurable site denylist

## Why

The Agent User can talk in Teams. It cannot read a spec, comment on it, or author + share a new one. Closing this gap is the single biggest unlock for "agent participates in M365 work."

Two scenarios drive V1:

1. **Read + comment.** A user in Teams says "read the spec at this SharePoint URL — what do you think?" The agent fetches the document, ingests it, asks a clarifying question, and surfaces that question both as a Teams chat reply (always) and as a real document comment (Word / Excel only — see D1).

2. **Author + share.** A user says "draft a spec for X, upload it, and share with me." The agent generates the document as **Markdown** (`.md`), uploads to SharePoint or its own OneDrive, and `invite`s the sponsor — only the sponsor — with edit rights.

V2 (`PLAN-files-llm-authoring-v2.md`) adds Office-format (.docx / .xlsx / .pptx) authoring via a template + IR + renderer pipeline — explicitly deferred from V1 per CEO-review D2.

## Locked Decisions (CEO review)

| # | Decision | Locked value |
|---|---|---|
| D1 | Scenario 1 primitive | **Both:** Teams chat reply (primary) + file-comment (opportunistic, Word/Excel only) |
| D2 | Authoring format in V1 | **Markdown only.** Office authoring deferred to V2 plan stub |
| D3 | Tool API shape | **Narrow tools** (one tool per verb — matches existing `tools/teams.py` convention) |
| D4 | Scope | **Both scenarios in V1** + V2 plan stub written as separate deliverable |
| D5 | Mode | **HOLD SCOPE** — review locked the plan, no scope expansion |
| D6 | Graph 5xx policy | **Read tools retry-with-jitter on 5xx; mutation tools fail-fast** |
| D7 | Upload `conflictResolution` default | **`rename`** (no overwrite) |
| D8 | LLM eval gating | **Include LLM eval; gate the PR1 merge** |
| D10 | URL → ID resolution | **`resolve_file_url` is a first-class PR1 tool** |
| D11 | Site scope governance | **Configurable site denylist** via `ENTRACLAW_FILES_DENIED_SITES` (default empty) |

D9 was not asked (premise 4 was committed without question).

## Premises

1. **The Agent User is a real Entra user.** Existing third hop (`acquire_agent_user_token`, scope `https://graph.microsoft.com/.default`) returns a Graph token covering whatever scopes the Agent User was provisioned for. **No new auth hop.**
2. **Sharing is sponsor-only by Entraclaw policy.** `share_file` enforces it via `entraclaw.identity.sponsors.list_sponsors()`; Graph itself does not.
3. **Every mutation enforces the sponsor allowlist** — not just `share_file`. `upload_file` and `write_text_file` audit the recipient/site context against the allowlist before the Graph call. (Premise 4 from CEO review, committed.)
4. **PowerPoint slide commenting is not possible via Graph.** `comment_on_file` rejects `.pptx`/`.ppt`/`.odp` and falls back to chat reply with a deep link.
5. **File comments require beta.** `/beta` host is isolated to one helper. Comment endpoints are the **only** beta calls.
6. **No anonymous links.** `createLink` with `scope="anonymous"` is never exposed. `invite` only.
7. **Site denylist gates every read AND write.** `_check_site_allowed(site_id)` runs before every Graph call that touches a site/library. Denylist defaults empty (no restriction); operators set `ENTRACLAW_FILES_DENIED_SITES` to opt in.

## Scope

### In scope (V1)

- File discovery: shared-with-me (`list_recent_files`), URL → ID resolution (`resolve_file_url`)
- File read: download + text extraction for prose formats only (`.md`, `.txt`, `.docx`, `.pdf`)
- File comments: list, add, reply (Word + Excel only) **plus** chat-reply fallback for everything else
- File authoring: **Markdown only** (`.md`)
- File upload: small-PUT + chunked, `conflictResolution=rename` default (D7)
- File sharing: `invite` to sponsor with role enforcement
- Workbook range reads (Excel only — write deferred to V1.1)
- Site denylist enforcement on every read/write (D11)
- Reuse of existing `RetryOn429Transport` from day 1; extended with `allow_5xx_retry` per D6

### NOT in scope (deferred — see "Deferred / TODOs" section below)

- Office-format authoring (`.docx`, `.xlsx`, `.pptx`) → **V2 plan stub** (`PLAN-files-llm-authoring-v2.md`)
- `search_files` (KQL site search) → P2 TODO; needs `Sites.Read.All` consent — cut from V1 to keep PR1 permission scope coherent
- `list_sites` (site enumeration) → P2 TODO; same `Sites.Read.All` reason
- PowerPoint slide authoring (`python-pptx`) → V2
- Anchored / cell-level comments — Graph doesn't support them
- Co-authoring real-time collaboration sessions
- Webhook subscriptions for comment notifications — V1.1 will use `_background_poll_comments()` (same shape as `_background_poll`)
- Site/library creation
- Excel range writes / table appends → V1.1 (only `read_workbook_range` and `list_workbook_tables` ship in PR3)

## Auth changes

### Code — none

`acquire_agent_user_token` already returns a Graph-scoped token. The `.default` scope means "all consented scopes for this user." Adding scopes is a provisioning task, not a code task.

### Provisioning — `scripts/create_entra_agent_ids.py` + `setup.sh`

Add admin consent for **one** delegated scope on the Agent Identity's app registration in PR1:

| Scope | Why | Sensitivity |
|---|---|---|
| `Files.ReadWrite.All` | Read sponsor-shared files; add comments; write to shared docs; resolve URLs to IDs | Tenant-scoped, signed-in user only |

PR2 adds:

| `Sites.ReadWrite.All` | Upload to SharePoint document libraries (vs. just OneDrive) | Same |

`Sites.Read.All` is **not** consented in V1 (because `search_files` and `list_sites` are deferred). If a P2 PR adds them, it adds the consent at the same time.

A new helper in `create_entra_agent_ids.py` (mirror of `grant_agent_user_storage_consent`) names this `grant_agent_user_files_consent`. Setup adds a `--with-files` flag; running without it leaves files capability disabled. Default off until a tenant admin opts in — matches `--use-cloud-memory`.

### Sponsor-only enforcement (Premise 3)

`entraclaw.identity.sponsors.list_sponsors()` is the **single** authority. Reused by every mutation tool: `share_file` (recipient must be in list), `upload_file` (target site must NOT be in deny list), `write_text_file` (same).

### Site denylist (D11)

```python
# src/entraclaw/tools/files.py
def _check_site_allowed(site_id: str) -> None:
    denied = os.getenv("ENTRACLAW_FILES_DENIED_SITES", "").split(",")
    denied = [s.strip() for s in denied if s.strip()]
    if site_id in denied:
        raise SiteNotAllowedError(
            f"Site {site_id} is in ENTRACLAW_FILES_DENIED_SITES; "
            f"the agent is not permitted to read from or write to this site."
        )
```

Called by `read_file`, `resolve_file_url`, `list_recent_files`, `comment_on_file`, `upload_file`, `write_text_file`, `share_file`, `read_workbook_range`, `list_workbook_tables`. **Single audit point** — easy to test, easy to verify.

## Tool surface (9 tools)

Each tool is `@mcp.tool()`-decorated in `mcp_server.py` (wrapped with `_with_token_retry`), implemented in `src/entraclaw/tools/files.py`, and audit-logged via the shared `_audit_graph_call` async context manager (one helper, used 9 times — see Cross-cutting concerns).

**All tool functions are `async`** with the contract `*, token: str, transport: httpx.AsyncBaseTransport | None = None` (mirrors `tools/email.py` and `tools/teams.py`). The MCP wrapper supplies `token` from `acquire_agent_user_token` and leaves `transport` defaulted; tests inject a `respx`-driven transport.

### PR 1 — Read & Comment (Scenario 1) — **4 tools**

```python
async def resolve_file_url(url: str, *, token: str, transport=None) -> FileRef: ...
async def list_recent_files(limit: int = 25, *, token: str, transport=None) -> RecentFilesPage: ...
async def read_file(file_ref: FileRef, *, as_format: Literal["raw", "auto"] = "auto",
                    token: str, transport=None) -> FileContent: ...
async def add_file_comment(file_ref: FileRef, content: str,
                           *, token: str, transport=None) -> CommentResult: ...
```

**A1 (eng review): module boundary preserved.** `tools/files.py` does NOT import `tools/teams.py`. The chat-reply leg from D1 is the model's job — it calls `add_file_comment` for the document side and `send_teams_message` for the Teams side as two separate tool calls. This keeps comment-side errors isolated from Teams-side errors and lets each surface its own audit event.

**A2 (eng review): `FileRef` carries `site_id`.** The resolver does the site lookup once; downstream tools never re-resolve. `_check_site_allowed(site_id)` is a pure local function that reads `ENTRACLAW_FILES_DENIED_SITES`.

```python
@dataclass(frozen=True)
class FileRef:
    drive_id: str
    item_id: str
    name: str
    mime_type: str
    kind: Literal["onedrive_personal", "onedrive_business", "sharepoint"]
    site_id: str | None  # populated for sharepoint kind, None for OneDrive
```

**A3 (eng review): resolver uses `GET /shares/{base64url(url)}/driveItem`.** This single endpoint covers SharePoint URLs (`https://contoso.sharepoint.com/sites/X/...`), OneDrive personal URLs, OneDrive business URLs, and shared-link URLs in one call — the response includes `parentReference.siteId` and `parentReference.driveId` for free. Errors: `FileNotFoundError` (404), `SiteNotAllowedError` (resolved site in denylist), `UrlNotResolvableError` (malformed/unrecognized URL).

`RecentFilesPage = {files: list[FileSummary], denied_count: int}` from `/me/drive/sharedWithMe`. `denied_count` is the number of files filtered out by the site denylist — surfaced so the model can tell the user "I see N more files but I can't access those sites."

`FileSummary = {drive_id, item_id, name, web_url, mime_type, size_bytes, modified_at, shared_by}`.

`read_file(file_ref, as_format="auto")` policy:
- `.md` / `.txt` / `.html` → fetch raw, decode, return text
- `.docx` → `GET /content?format=pdf`, extract via `pypdf`, return text
- `.pdf` → fetch raw, extract via `pypdf` (after size check)
- `.xlsx` / `.xls` → **reject** with `UnsupportedReadFormatError(message="Use read_workbook_range for Excel data")` (PR3)
- `.pptx` / `.ppt` → **reject** with `UnsupportedReadFormatError(message="PowerPoint reading is not supported in V1; user can paste slide content into chat")`
- everything else → `UnsupportedReadFormatError`

**P1 (eng review): size cap before download.** Before fetching `.pdf` content, `read_file` issues a `GET /items/{id}` and rejects with `FileTooLargeError` if `size > ENTRACLAW_FILES_MAX_PDF_BYTES` (default `52_428_800` = 50 MiB). For `.docx`-via-PDF the same check applies after the format=pdf conversion's `Content-Length`. Avoids a 200 MB download to extract one paragraph of text.

`FileContent = {drive_id, item_id, name, mime_type, text: str, page_count: int | None, truncated: bool}`. `truncated=True` if extracted text exceeds `ENTRACLAW_FILES_MAX_TEXT_BYTES` (default `200_000` — a 50-page spec).

**A1+A4+A5 (eng review): `add_file_comment` is Files-only and tightly scoped.**

- Endpoint: `POST /beta/drives/{drive-id}/items/{item-id}/comments` (the **only** beta endpoint in this PR — see "Beta surface isolation"). The `/workbook/comments` and `/document/comments` shapes from earlier drafts are wrong; Microsoft's beta surface uses one path for both Word and Excel.
- **A5 reject conditions** (raise `UnsupportedCommentFormatError`):
  - File extension not in `{.docx, .xlsx}` (rejects `.pptx`, `.pdf`, `.md`, etc.)
  - `file_ref.kind == "onedrive_personal"` (Microsoft does not GA personal-OneDrive comments)
  - `file_ref.mime_type` indicates a folder (`folder` facet on the driveItem)
- **No chat-reply leg.** Returns `CommentResult = {comment_id: str, content: str, web_url: str}`. The model orchestrates the Teams reply with `send_teams_message` if it wants to.

### PR 2 — Author, Upload, Share (Scenario 2) — **3 tools**

```python
async def write_text_file(content: str, filename: str,
                          target: UploadTarget = OneDriveTarget("/"),
                          *, conflict: Literal["rename", "replace", "fail"] = "rename",
                          token: str, transport=None) -> FileSummary: ...
async def upload_file(content: bytes, filename: str,
                      target: UploadTarget = OneDriveTarget("/"),
                      *, conflict: Literal["rename", "replace", "fail"] = "rename",
                      token: str, transport=None) -> FileSummary: ...
async def share_file(file_ref: FileRef, recipient_email: str,
                     role: Literal["read", "write"] = "write",
                     message: str | None = None,
                     *, token: str, transport=None) -> SharePermission: ...
```

**C1 (eng review): `write_text_file` accepts `conflict` symmetric with `upload_file`.** Default `"rename"` per D7. Markdown / plaintext only — Office formats are V2 (`PLAN-files-llm-authoring-v2.md`).

`UploadTarget` is a tagged union: `OneDriveTarget(folder_path: str = "/")` (agent's own OneDrive — no SharePoint write needed), or `SharePointTarget(site_id: str, library_id: str | None, folder_path: str = "/")` (named site library). Both target types are dataclasses; `_check_site_allowed(target.site_id)` runs whenever `target` is a `SharePointTarget`.

`upload_file` picks small-PUT vs. chunked-upload by `len(content)`:
- `< 4 MiB` → single `PUT` to `/items/{parent}:/{filename}:/content`
- `≥ 4 MiB` → `createUploadSession` + 5 MiB chunks

**T2 (eng review): chunked-upload uses protocol-native `nextExpectedRanges` resume.** On a 5xx mid-stream, re-issue `GET {uploadUrl}` to read `nextExpectedRanges`, then resume from the first byte the server still wants. Up to 3 retries per chunk; cap on total wall-time. This is the only mutation that retries on 5xx, and only for an in-progress upload session — D6 fail-fast still applies to fresh mutation calls.

`share_file` takes a `FileRef` (A2 — no re-resolve). Pre-call check: `recipient_email` must be in `entraclaw.identity.sponsors.list_sponsors()`. Otherwise raise `NotASponsorError` with the sponsor list so the model can correct itself. `requireSignIn=true` always. `sendInvitation=true` always. Audit records the Graph permission ID for clean future revocation (V1.1 `unshare_file`).

### PR 3 — Workbook depth (read-only) — **2 tools**

```python
async def read_workbook_range(file_ref: FileRef, sheet: str, address: str,
                              *, token: str, transport=None) -> ExcelRange: ...
async def list_workbook_tables(file_ref: FileRef,
                               *, token: str, transport=None) -> list[ExcelTable]: ...
```

`ExcelRange = {address, values: list[list], formulas: list[list], num_rows, num_cols}`. Opens and closes a workbook session per call. Excel **writes** (`excel_write_range`, `excel_append_table_rows`) deferred to V1.1.

## Phasing

Three PRs, each independently shippable. **Each PR reuses `RetryOn429Transport` from day 1** — no new throttle helper.

### PR 1 — Read & Comment (Scenario 1)

- 4 tools above (`resolve_file_url`, `list_recent_files`, `read_file`, `add_file_comment`) + `_check_site_allowed`, `_audit_graph_call` helper, `RetryOn429Transport` extended with `allow_5xx_retry=True` for read calls (D6)
- Provisioning: `Files.ReadWrite.All` consent grant, `--with-files` flag, `ENTRACLAW_FILES_DENIED_SITES` env doc
- **Eval scaffolding (T1, eng review):** `tests/evals/__init__.py`, `tests/evals/conftest.py`, `tests/evals/rubric.py`, `tests/evals/test_files_scenario_1.py` (5 traces) — full directory ships in PR1, not a follow-up
- **LLM eval suite (gates merge per D8):** five Scenario-1 traces — agent reads spec, asks clarifying question, posts `add_file_comment` + `send_teams_message` (model orchestrates both legs). Eval asserts: comment lands on doc, chat reply lands in chat, deep link works, denied site rejects with correct error, truncated doc surfaces truncation honestly.
- Tests: ~50 new unit tests (T4, eng review), mirroring `tests/tools/test_email.py` + `tests/tools/test_teams.py` (respx mocks, real Graph response shapes, both 5xx-retry paths exercised, T3 regression-safe default for existing email/teams)

### PR 2 — Author, Upload, Share (Scenario 2)

- 3 tools above + sponsor-allowlist enforcement on every share mutation
- Provisioning: `Sites.ReadWrite.All` consent grant
- Tests: ~30 new tests (T4, eng review), including chunked-upload happy-path + mid-stream 503 → `nextExpectedRanges` resume (T2), denylist rejection, non-sponsor share rejection, `write_text_file` `conflict` parameter symmetry (C1)
- LLM eval extension: three Scenario-2 traces — agent drafts markdown, uploads, shares with sponsor; one with denied target site; one with non-sponsor recipient

### PR 3 — Workbook reads

- 2 tools above + workbook session lifecycle helper
- Tests: ~15 new tests
- No new permissions

## Cross-cutting concerns

### Throttling & retry (extends existing `RetryOn429Transport`)

`src/entraclaw/tools/rate_limit.py` already implements `RetryOn429Transport` (async httpx transport with Retry-After backoff). Files PR1 extends it with one new kwarg:

```python
class RetryOn429Transport(httpx.AsyncBaseTransport):
    def __init__(self, *, max_attempts: int = 3, allow_5xx_retry: bool = False) -> None: ...
```

Per D6: `read_file`, `resolve_file_url`, `list_recent_files`, `read_workbook_range`, `list_workbook_tables` use `allow_5xx_retry=True` with exponential jitter (200ms / 800ms / 2400ms). `comment_on_file`, `write_text_file`, `upload_file`, `share_file` use `allow_5xx_retry=False` — fail fast on 5xx so the model can decide whether to re-issue. **No new throttle helper invented.**

### Audit logging — single helper (C3, eng review)

Every file-touching tool emits two audit events: pre-call (`outcome="pending"`) and post-call (`outcome="success"`/`"failure"`). Resource format: `{drive_id}:{item_id}` for file-scoped events, `{site_id}` for site-scoped events. Premise 3 means the sponsor allowlist check itself emits an audit event when it rejects.

**DRY: one helper, used 9 times.**

```python
@asynccontextmanager
async def _audit_graph_call(verb: str, resource: str,
                            *, allow_5xx_retry: bool = False) -> AsyncIterator[None]:
    """Pre-call pending audit, run body, post-call success/failure audit.

    `allow_5xx_retry` is a passthrough flag the caller can use when constructing
    its `httpx.AsyncClient(transport=RetryOn429Transport(allow_5xx_retry=True))`.
    Kept on the helper signature so reads vs. mutations are visibly different at
    every call site.
    """
```

This replaces nine ad-hoc `audit_log(...)` / `try / except / audit_log(failure)` blocks with a single concern. Tests assert exactly one pre-event and one post-event per tool call.

### Beta surface isolation

Comments are the **only** beta calls. Define `GRAPH_BETA_HOST = "https://graph.microsoft.com/beta"` once in `tools/files.py` and gate all comment calls through a single helper. If Microsoft GAs comments to v1.0, that's a one-line change.

### Channel notifications for comment replies

Out of scope for V1 (V1.1 follow-up): when a sponsor replies to an agent's file comment, the agent should see it the same way it sees a Teams DM (`notifications/claude/channel`). Implementation: `_background_poll_comments()` task at 60s interval (files change less often than Teams chats).

## Testing strategy

Mirror `tests/tools/test_email.py` and `tests/tools/test_teams.py`. Target counts after eng-review T4: PR1 ~50 unit tests, PR2 ~30, PR3 ~15.

- **respx** for Graph HTTP mocking — never hit real Graph in unit tests
- **Real Graph response shapes** — copy from Graph Explorer for fidelity, never invent
- **Each tool gets:** happy path, missing-permission (403), file-not-found (404), throttled (429 with `Retry-After`), token-expired-during-call (401), denylist-rejected, **for read tools:** 503 retry-success and 503 retry-exhausted
- **Chunked upload (T2):** multi-chunk happy path; mid-stream 503 → `nextExpectedRanges` resume; per-chunk retry cap exhausted; protocol-aborted upload-session 410 surfaces cleanly
- **`add_file_comment` reject tests (A5):** `.pptx`, `.pdf`, `.md`, personal-OneDrive `.docx`, folder driveItem, denylisted site
- **`share_file` reject test** for non-sponsor recipient
- **`read_file` reject tests** for `.xlsx`, `.pptx`, denylisted site, **PDF over `ENTRACLAW_FILES_MAX_PDF_BYTES` (P1)**
- **`resolve_file_url` tests** for SharePoint URL, OneDrive personal URL, OneDrive business URL, malformed URL, denylisted site, 404, 429 backoff
- **T3 regression test** for `RetryOn429Transport` default behavior — `allow_5xx_retry=False` (default) MUST NOT retry on 5xx (proves PR1 didn't change existing email/teams retry semantics)
- **T5 fixture factories** in `tests/tools/conftest.py`: `make_file_ref()`, `make_file_summary()`, `make_file_content()`, `make_excel_range()` — keep test bodies focused on the path under test, not Pydantic boilerplate

Target: ≥ 80% line coverage per `pytest --cov-fail-under=80`.

### LLM eval suite (D8 — gates PR1 merge per T1, eng review)

Lives in `tests/evals/`. **Scaffolding ships in PR1, not a follow-up:**

- `tests/evals/__init__.py`
- `tests/evals/conftest.py` — replay harness: respx-driven Graph fixture loader, model-call snapshot/replay, scoring rubric injector
- `tests/evals/rubric.py` — `EvalRubric` dataclass: `must_call_tools`, `must_call_in_order`, `must_not_call_tools`, `assertions` (callable list over the post-trace state)
- `tests/evals/test_files_scenario_1.py` — 5 traces:
  1. **Happy path** — agent reads a `.docx` spec, asks clarifying question, posts `add_file_comment` AND a `send_teams_message` chat reply (model orchestrates both legs)
  2. **`.pptx` reject** — agent gracefully reports it can't read PowerPoint, suggests user paste content
  3. **Denied site** — agent hits `SiteNotAllowedError`, surfaces the error and tells user the operator denied that site
  4. **Truncated long doc** — agent reads a 60-page PDF that gets truncated, mentions truncation in its summary, doesn't pretend it read everything
  5. **5xx retry success** — Graph returns 503 once, then 200; the model never sees the failure (transparent retry)

Marker: `eval` (registered in `pyproject.toml`). Run via `pytest tests/evals -m eval`. The unit test suite excludes the `eval` marker by default to keep `pytest -v` fast. **PR1 cannot merge until `pytest tests/evals -m eval` passes.**

P2 (eng review): live/snapshot/replay modes for the eval LLM are deferred — V1.1 follow-up. PR1 ships with snapshot mode only (model responses are pre-recorded JSON in `tests/evals/snapshots/`).

## Failure-mode registry (per D6)

| Failure | Tool | Retry policy | User experience |
|---|---|---|---|
| 429 throttle | all | `RetryOn429Transport` (Retry-After, max 3) | transparent retry; logged |
| 503 transient | reads | retry with jitter (3 attempts, 200/800/2400ms) | transparent retry; logged |
| 503 transient | mutations (non-chunked) | **fail fast**, surface to model | model can decide to re-issue |
| 503 transient | chunked upload (in-flight) | `nextExpectedRanges` resume (T2; max 3/chunk) | transparent within upload |
| 401 token expired | all | `_with_token_retry` (existing) | refresh + retry once |
| 404 file not found | all | no retry | `FileNotFoundError` → model surfaces to user |
| 403 missing permission | all | no retry | `MissingPermissionError` → operator must re-consent |
| Denied site | all | no retry | `SiteNotAllowedError` → operator updates `ENTRACLAW_FILES_DENIED_SITES` |
| Non-sponsor share | `share_file` | no retry | `NotASponsorError` with sponsor list → model corrects |
| Unsupported read format | `read_file` | no retry | `UnsupportedReadFormatError` with hint to `read_workbook_range` |
| Unsupported comment target (A5) | `add_file_comment` | no retry | `UnsupportedCommentFormatError` (rejects `.pptx`, personal OneDrive, folder, non-Office formats) |
| **File too large (P1)** | `read_file` | no retry | `FileTooLargeError` when `size > ENTRACLAW_FILES_MAX_PDF_BYTES` (default 50 MiB) |
| Upload conflict | `upload_file` / `write_text_file` | no retry (default `rename` resolves it) | renamed file returned in `FileSummary` |

## Deferred / TODOs (P2 work)

- `search_files` + `list_sites` (need `Sites.Read.All`; cut from PR1 for permission scope coherence)
- Excel writes (`excel_write_range`, `excel_append_table_rows`) — V1.1
- Workbook session context manager for batched writes — V1.1
- Webhook subscriptions for comment replies (currently V1.1 polls)
- Site/library creation
- `unshare_file` for clean revocation of `share_file` permissions
- Office-format authoring (`.docx`, `.xlsx`, `.pptx`) — see `PLAN-files-llm-authoring-v2.md`
- PowerPoint slide authoring + reading

## What I noticed about how you think

- You named both scenarios concretely. That's the kind of specificity that makes a plan implementable instead of aspirational.
- You picked C (full surface) when offered the smaller wedges in office-hours, then accepted CEO-review's recommendation to **trim to the locked surface above** — that's the right kind of evolution from brainstorm to ship plan.
- The sponsor-only sharing constraint came in early and unprompted. You're already thinking like a security architect about this surface, and the CEO review extended that into Premise 3 + D11.

## The Assignment (now de-risked, not deferred)

Before PR1 lands: open this plan in a Teams chat with your sponsor and ask one question — **"Are there any sites or document libraries the Agent User must NOT touch?"** Capture the answer in `ENTRACLAW_FILES_DENIED_SITES` in `.env.example` and document it in PR1's setup notes. The denylist is configurable, so the answer doesn't need to be perfect — but capturing it now beats finding out after a tool accidentally reads from a Legal-restricted SharePoint site.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | CLEAR (HOLD_SCOPE) | 7 issues surfaced, 1 critical gap (5xx policy), 0 unresolved; 10 decisions locked (D1-D11, D9 skipped) |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 (Claude fallback — Codex hung 11+ min) | ISSUES_FOUND | 7 outside-voice findings: 5 committed, 2 became D10 + D11 |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 (this run, post-rewrite) | CLEAR | 12 issues across 4 sections; 6 decisions taken via AskUserQuestion (A1/A2/A3/C1/T1/T2); 6 mechanical commits (A4/A5/C2/C3/C4/T3-T6/P1/P2); 0 unresolved; 0 critical failure-mode gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — (no UI scope) | n/a |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | n/a |

**Eng review decisions (this run):**
- **A1:** `comment_on_file` split into `add_file_comment` (Files-only); model orchestrates the chat-reply leg via existing `send_teams_message`. `tools/files.py` does not import from `tools/teams.py`.
- **A2:** `_check_site_allowed` API mismatch resolved — `resolve_file_url` returns `FileRef.site_id`; downstream tools (`read_file`, `add_file_comment`, etc.) accept `FileRef` directly. `list_recent_files` post-filters denied items + surfaces `denied_count`.
- **A3:** `resolve_file_url` uses `GET /shares/{base64url(url)}/driveItem` — the canonical Graph sharing-URL resolver; returns `parentReference.siteId` for free.
- **A4:** Endpoint corrected — `POST /beta/drives/{drive-id}/items/{item-id}/comments` (NOT `/workbook|document/comments`); same for Word and Excel.
- **A5:** `add_file_comment` rejects folder driveItems (per platform-learnings line 206) in addition to .pptx and personal OneDrive.
- **C1:** `write_text_file` accepts `conflict: Literal["rename","replace","fail"] = "rename"`, mirroring `upload_file`.
- **C2:** All `tools/files.py` functions are `async def`, take `*, token: str, transport: httpx.AsyncBaseTransport | None = None`. `mcp_server.py` wraps each with `_with_token_retry`.
- **C3:** Single `_audit_graph_call(verb, resource, *, allow_5xx_retry=False)` async context manager (or `@graph_call` decorator) — used 9 times; not a framework, just DRY.
- **C4:** §"Tool surface" needs in-doc rewrite to reflect post-decision shapes (FileRef param, async signatures, dropped chat-reply plumbing on `add_file_comment`).
- **T1:** PR1 includes eval scaffolding — `tests/evals/conftest.py` (replay harness), `pyproject.toml` `eval` marker registration, `tests/evals/rubric.py` (dataclass schema), `tests/evals/test_files_scenario_1.py` (5 traces). PR1 cannot merge until `pytest tests/evals -m eval` passes.
- **T2:** Chunked upload uses protocol-native `nextExpectedRanges` resume per chunk (max 3 attempts/chunk); session-create failures fail-fast per D6.
- **T3-T6:** Test plan revisions — regression test for `RetryOn429Transport.allow_5xx_retry=False` default; PR1 ~50 tests / PR2 ~30 / PR3 ~15 (plan undercounted ~30%); fixture factories in `tests/tools/conftest.py`; denylist tests assert `respx_mock.calls.call_count == 0`.
- **P1:** `ENTRACLAW_FILES_MAX_PDF_BYTES` env (default 50 MB) + `FileTooLargeError`; rejection happens BEFORE PDF download.
- **P2:** Plan needs to specify whether eval-mode LLM is live, snapshotted, or replayed (affects CI cost + gate reliability).

**Outside voice (this run):** SKIPPED — plan was outside-voice'd 6h ago in CEO review; calling again on same content is noise. CEO-stage outside voice surfaced 7 findings (5 committed, 2 became D10/D11). This eng review caught the implementation-detail layer (architectural module boundaries, API mismatches, eval scaffolding, chunked-upload semantics) that the strategy-level outside voice could not.

**Cross-model:** No tension in this run (outside voice skipped).

**TODOS.md updates:** 6 entries added under P2 — `search_files`+`list_sites`, Excel writes + workbook session manager, comment-reply webhook subs, `unshare_file`, V2 Office authoring (back-ref to V2 plan), site/library creation. All carry back-references to this plan's §"Deferred / TODOs".

**Test plan artifact:** `~/.gstack/projects/entraclaw-identity-research/user-main-eng-review-test-plan-20260430-110538.md`.

**Worktree parallelization:** Sequential — PR1 → PR2 → PR3 all touch `src/entraclaw/tools/files.py`. Within PR1, the 4 tools share the file + the `_audit_graph_call` helper, so splitting tools across worktrees would create merge conflicts. Single PR per phase is correct.

**Failure-mode critical gaps:** 0. Every codepath in the test diagram has either a planned test, an error handler, or both. After P1's `FileTooLargeError` addition, the failure registry is complete.

**UNRESOLVED:** 0
**VERDICT:** CEO + ENG CLEARED — ready for `/ship` once the in-doc rewrites land (A1/A2/A3/C1/C4/T1/T2 + P1 in failure registry). Eng-review-required gate satisfied.
