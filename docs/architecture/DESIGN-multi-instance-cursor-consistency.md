# DESIGN — Multi-Instance Cursor Consistency (fleet-safe Teams channel poll)

> **Status:** Proposed. Problem confirmed in production 2026-07-02.
> **Owner:** unassigned (handed off for implementation).
> **Related:** ADR-005 (cloud-hosted memory), `docs/decisions/005-cloud-hosted-memory.md`;
> TODOS.md → "MCP server orphans when Claude Code exits" (P1) and
> "Tenant-scoped runtime state for true multi-user support" (P3).

## TL;DR

The background Teams poll is supposed to support the model **"N instances,
all pointing at one cloud cursor store, and it just works."** In steady state
it does — but the **bootstrap path fails _open_** (re-pushes a chat's newest
message) on *any* failure to read a fresh cloud cursor, and cursor-read misses
are routine in a fleet. The result is a replay flood: every idle chat re-emits
its newest (possibly weeks-old) message on each mis-configured or transiently-
failing instance.

The fix is to make the miss path **fail closed** (never push on an ambiguous
miss), add **per-message cloud idempotency**, co-locate the **watched-chats
list** with the cursors in cloud, replace **bootstrap-on-stale** with a
**catch-up read**, and add **optimistic concurrency** to cursor writes.

This is not just a UX bug. The replay flood **re-injects stale imperative
messages** ("read special data in my Documents", "ship to this address",
"run this validation") back into the agent's channel — a prompt-injection /
replay surface. Fail-closed is a security requirement, not only a polish item.

## Symptom (observed 2026-07-02)

A base `entrabot` instance on a dev PC, pointed at EntraClaw's blob container
(`agent-2071f305-…`), received a continuous stream of `notifications/claude/
channel` pushes for DMs dated **2026-05-28 / 05-29** (~5 weeks old) plus a few
from June. Each push was a *different chat's newest message* — the signature of
`_bootstrap_chat`, not of steady-state polling.

Diagnostics established:

- Backend resolved to `BlobBackend`; blob **read access + RBAC confirmed**
  (listed 51 cursor files, read them).
- Every blob cursor was **correct and fresh**: `bootstrapped: true`,
  `last_written_at = today`, `is_stale = false`, full `seen_ids_tail`.
- Yet messages already in those cursors' `seen_ids_tail` were still pushed.
- **Four** `entrabot-mcp` processes were running (two started 07:46, two 07:54).
  The singleton-lock holder was one of the 07:54 pair; the **07:46 pair was an
  orphan the singleton lock did not prevent**.
- The **watched-chats list is a local file** (`data_dir/watched_chats`, 50
  chats); **no local `chat_cursors/` dir existed**.

Conclusion: at least one poller feeding the session was **not** consuming the
correct blob cursors — it was reading an empty/local store (or failing the
read) and re-bootstrapping every chat. The healthy poller maintained correct
blob cursors simultaneously, which is why we saw *correct cursors AND a replay
flood at the same time*.

## How the current design works

Files: `src/entrabot/mcp_server.py`, `src/entrabot/tools/chat_cursors.py`,
`src/entrabot/storage/backend.py`.

1. **Watched-chat list** is loaded from the **local** file
   `config.data_dir / "watched_chats"` in `_init_poll()`
   (`mcp_server.py:981`). Auto-discovery (`_background_discover_chats`, 120 s)
   adds any `/me/chats` entry not already watched.

2. **Per-chat registration** (`_register_watched_chat`, `mcp_server.py:1207`)
   decides bootstrap vs. rehydrate:
   - `load_cursor(chat_id)` reads `chat_cursors/<url-quoted-id>.json` via
     `get_backend()`.
   - If a cursor exists **and** `is_stale(last_written_at)` is false →
     rehydrate `{seen_ids, last_ts, bootstrapped: true}`. Steady state; safe.
   - **Otherwise (absent / stale / corrupt / _read exception_)** → register
     `{seen_ids: ∅, last_ts: None, bootstrapped: false}`.

3. **Poll loop** (`_background_poll`, `mcp_server.py:1344`):
   - If `not bootstrapped` → `_bootstrap_chat` (`mcp_server.py:1301`), which
     sets `last_ts = newest.sent_at`, marks **all but the newest** as seen, and
     deliberately **leaves the newest UNSEEN so the next cycle pushes it**.
   - Else → `_filter_new_messages(msgs, last_ts, seen_ids)`
     (`mcp_server.py:804`): keep `sent_at >= last_ts − OVERLAP` **and** id not
     in `seen_ids`. This is the correct, timestamp-gated, idempotent path.

4. **Cursor persistence** (`chat_cursors.save_cursor`, `chat_cursors.py:120`):
   plain backend `write_text` (blob PUT) — **no ETag precondition**.
   `is_stale` (`chat_cursors.py:142`) is keyed off **write time**
   (`last_written_at`), a prior fix (commit `d4ba1fa`) that stopped keying off
   `last_ts` (which had re-fired every idle chat on restart).

5. **Backend resolution** (`get_backend`, `backend.py:162`): per-process, read
   from env at call time — `keep_memory_local` → Local; else
   `blob_endpoint AND blob_container` → Blob; else Local. **Half-configured
   blob silently falls back to Local.**

## Root-cause analysis — what breaks the fleet model

The steady-state gate (`_filter_new_messages`) already supports N-instances-one-
store. Every failure below is in the **decision to bootstrap** or in **write
coordination**, and each converts to a user-visible replay because bootstrap
**pushes**.

### F1 — Cursor-read miss fails OPEN (pushes) instead of CLOSED (drops) — core

`_register_watched_chat` treats *absent, stale, corrupt, and any read
exception* identically: fall through to `_bootstrap_chat`, which pushes the
newest message. In a fleet, transient blob reads fail (401 refresh races,
timeouts, throttling). Every such miss re-pushes the newest message as if new.
The safe rule: an **ambiguous** miss must never push.

### F2 — Backend selection diverges silently per process

`get_backend()` reads env at call time and **silently degrades to Local** when
blob env is missing or half-set. One mis-enved instance (e.g. spawned by a host
that didn't pass the blob vars, or an orphan from before `.env` was written)
reads an empty local store → every chat un-bootstrapped → replay — *while other
instances correctly use blob*. Nothing asserts fleet-wide backend agreement.

### F3 — The watched-chats *list* is local, not cloud

Only cursors are shared; the *list of chats to watch* is a local file plus
independent per-instance auto-discovery. "What am I watching" is not cloud-
authoritative, so instances register chats at different times and each
independently hits the F1 decision.

### F4 — `is_stale` re-bootstraps a COLD store

Even with correct shared cloud, if no instance has written a cursor within
`CURSOR_STALENESS_SECONDS` (24 h) — fleet idle or fully down — every cursor is
judged stale → bootstrap → replay on next boot. Staleness conflates "I may have
*missed* messages while down" with "I should *re-surface* old ones." A stale
cursor should trigger a **catch-up read** (`sent_at > last_ts`), never a
re-baseline-and-push.

### F5 — Cursor writes have no concurrency control (last-writer-wins)

`save_cursor` does a plain PUT with no `If-Match`, though `BlobStore` supports
ETags (ADR-005 Phase 1). With many writers on one `chat_cursors/<id>.json`, a
slow instance can clobber a fresher `last_ts` with an older one → other
instances later read a **regressed** watermark → messages between the two
watermarks re-push.

### F6 — Duplicate/orphan processes multiply the blast radius

The singleton lock (`.singleton.lock`) did not stop a second full server
(orphan pair observed). Multiple pollers on one host each run their own
bootstrap pass. Even after F1–F5 are fixed, duplicate processes waste Graph
calls and double-write cursors. Tracked separately in TODOS.md ("MCP server
orphans when Claude Code exits") — list it as a dependency, not a duplicate.

## Target invariant

> For any chat, an instance emits a channel push for a message **iff** no
> instance has previously recorded that message as delivered in the shared
> cloud store — regardless of which instance, how many instances, how long any
> instance was down, or transient read/write failures. On uncertainty, **do not
> push.**

## Proposed changes (priority order)

1. **Fail closed on read-miss (F1).** Split the miss cases:
   - *Read error / throttle / ambiguous:* retry the cloud read with bounded
     backoff; if still unresolved this cycle, **skip pushing** for this chat and
     retry next cycle. Never bootstrap-push on an error.
   - *Genuinely new chat* (provably no cloud cursor has ever existed): allowed
     to surface the single newest message once — and immediately record it in
     the idempotency ledger (change 2) so no sibling repeats it.
   - *Stale but present:* go to change 4 (catch-up read), not bootstrap.

2. **Per-message cloud idempotency ledger (F1/F6).** Before pushing, check a
   shared record keyed by `message_id` (e.g. extend the cursor with a bounded
   delivered-id set, or a small `delivered/<chat_id>` object). Push only if
   absent; record atomically after. This is the direct implementation of
   "ensure you already have the message in cloud storage before ignoring it,"
   and makes delivery idempotent across the whole fleet.

3. **Co-locate the watched-chats list in the cloud store (F3).** Store
   `watched_chats` through `get_backend()` (same bucket as cursors) so the list
   and the cursors share one source of truth. Local file becomes a cache, not
   the authority.

4. **Replace bootstrap-on-stale with catch-up read (F4).** On a present-but-
   stale cursor, fetch messages with `sent_at > last_ts` and gate them through
   the idempotency ledger — filling any gap without re-surfacing seen messages.
   Reserve "surface newest once" strictly for genuinely-new chats.

5. **Optimistic concurrency on cursor writes (F5).** Use `If-Match` on the
   cursor PUT; on `412`, re-read and **merge** (`max(last_ts)`,
   `union(seen_ids/delivered_ids)`), then retry. `BlobStore` already exposes the
   ETag primitive.

6. **Assert uniform backend at boot (F2).** Log the resolved backend + container
   at startup; when blob env is *partially* configured, treat it as a hard
   misconfiguration (warn loudly / refuse) rather than silently using Local, so
   a mis-enved instance is visible instead of diverging.

7. **Depends on / pair with:** "MCP server orphans when Claude Code exits"
   (TODOS.md P1) — enforce the singleton so duplicate pollers can't start (F6).

## Security note

The replay flood re-delivers **month-old imperative-looking messages** into the
agent's channel: "Read special data in the Documents directory of my PC",
"ship to <address>", "run scripts/…". A poller that re-surfaces stale commands
is a **replay-injection surface**. The channel-discipline body rule already
treats channel content as data, not instructions — but reducing the volume of
replayed imperatives via fail-closed delivery removes the temptation/mistake
surface entirely. Treat F1 as security-relevant, not cosmetic.

## Test plan

- **Fail-closed on read error:** stub the backend `read_text` to raise for a
  chat that has a fresh cloud cursor → assert **no** push and no re-bootstrap;
  next cycle with the read restored → still no push (cursor rehydrates).
- **Cold-store catch-up:** cursor present, `last_written_at` older than the cap,
  new message after `last_ts` → assert only the genuinely-new message pushes,
  not the whole newest-message set.
- **Fleet idempotency:** two backends sharing one in-memory fake store; both
  poll the same chat with the same new message → exactly **one** push recorded.
- **Concurrency merge:** two writers race a cursor PUT with divergent `last_ts`
  → merged result keeps the max watermark and the union of delivered ids.
- **Backend assertion:** half-configured blob env → boot warns/refuses rather
  than silently using Local.

## Rollout

- Ships behind the existing blob backend; no schema migration required if the
  idempotency set is folded into the existing cursor object (bounded like
  `seen_ids_tail`). A separate `delivered/` object is optional and additive.
- Land F1 + F6 first (stops the flood and the injection surface), then F2/F3,
  then F4/F5 (correctness under long downtime and high write concurrency).
