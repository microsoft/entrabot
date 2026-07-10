#!/usr/bin/env python3
"""One-shot cursor migration for the 2026-07-09 UPN-identity fix.

The pre-fix background Teams poll identified self-authored messages by
display name. When the agent was renamed ("EntraBot Agent" →
"EntraClaw Agent") the filter no-op'd, and every cursor whose ``last_ts``
sat just before a self-authored message became eligible to replay that
message (and every later self-authored one) as fresh inbound.

This script bumps every cursor's ``last_ts`` to ``now`` (or later) and
merges recent self-authored message IDs into ``seen_ids_tail`` so the
fleet-safe per-message cloud-idempotency layer will drop any residual
replays.

Contract:

* **Idempotent, stably.** A migration-run flag blob at
  ``chat_cursors/_migrated_upn_fix.json`` is written after the first
  successful run; every subsequent invocation reads the flag first and
  returns without touching any cursor. A per-cursor timestamp predicate
  (``last_ts >= now``) is unstable — it drifts as time passes and a
  legitimate ``save_cursor`` strips any per-cursor marker — so we use a
  separate namespace instead.
* ``--dry-run`` prints planned changes and writes nothing (including the flag).
* ``--verify`` reports whether the flag is present. Read-only.
* Only touches the operational cursor prefix (``chat_cursors/``) — persona-sati
  memory and interaction logs are never read or written.

Live invocation (Brandon runs this after reviewing the diff):

    python scripts/migrate_cursors_to_upn.py --dry-run    # inspect plan
    python scripts/migrate_cursors_to_upn.py              # execute
    python scripts/migrate_cursors_to_upn.py --verify     # confirm
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

# Support running as ``python scripts/migrate_cursors_to_upn.py`` from repo root
# even when the ``entrabot`` package hasn't been installed into the current
# interpreter (``pip install -e .`` isn't guaranteed on every operator's box).
_REPO_SRC = Path(__file__).resolve().parent.parent / "src"
if _REPO_SRC.is_dir() and str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from entrabot.config import get_config  # noqa: E402
from entrabot.storage.backend import get_backend  # noqa: E402
from entrabot.tools.chat_cursors import (  # noqa: E402
    MAX_SEEN_IDS_TAIL,
    bound_seen_ids,
)

logger = logging.getLogger("entrabot.migrate_cursors_to_upn")

# Prefix owned by ``chat_cursors.py``. Kept as a local constant so a future
# refactor there doesn't silently re-scope this migration.
_CURSOR_PREFIX = "chat_cursors/"

# Stable migration marker. Lives under the cursor prefix but in a distinct
# ``_``-prefixed namespace so ``_list_cursor_keys`` can filter it out. Its
# presence is the ONLY signal for "migration already ran" — a per-cursor
# ``last_ts >= now`` predicate would drift as time passes, and any per-cursor
# field would be stripped by the next ``save_cursor`` call from the poll.
_MIGRATION_FLAG_KEY = "chat_cursors/_migrated_upn_fix.json"

# The migration bumps ``last_ts`` this many seconds past ``now`` so a
# concurrently-running poll doesn't race back before us on a machine with
# skewed clocks. 2s > the poll's overlap window.
_BUMP_SECONDS = 2

# We fetch this many recent messages per chat when looking for self-authored
# IDs to salt into ``seen_ids_tail``. Same order of magnitude as the poll's
# per-cycle read.
_RECENT_LOOKBACK_COUNT = 20


def _now_iso() -> str:
    """UTC ISO-8601 with Z suffix, milliseconds precision (matches cursor schema)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def _bumped_ts() -> str:
    dt = datetime.now(UTC) + timedelta(seconds=_BUMP_SECONDS)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def _read_migration_flag() -> dict | None:
    """Return the migration flag payload if present, else ``None``.

    The flag blob is the *stable* skip predicate — its presence means the
    migration has already run at some point on this backend, regardless of
    how far individual cursors have advanced since. Any read error is treated
    as "flag absent" so a temporary I/O hiccup can't lock the migration out.
    """
    backend = get_backend()
    try:
        raw = backend.read_text(_MIGRATION_FLAG_KEY)
    except Exception:  # noqa: BLE001
        return None
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_migration_flag(*, cursors_migrated: int) -> None:
    """Write the migration flag after a successful non-dry-run pass."""
    backend = get_backend()
    payload = {
        "migration": "upn-fix-2026-07-09",
        "written_at": _now_iso(),
        "cursors_migrated": cursors_migrated,
    }
    backend.write_text(_MIGRATION_FLAG_KEY, json.dumps(payload))


def _list_cursor_keys() -> list[str]:
    """Return every real ``chat_cursors/*.json`` key currently in the backend.

    Filters out the migration flag blob (``chat_cursors/_migrated_upn_fix.json``)
    and any other ``_``-prefixed namespace keys so a future migration can add
    its own flag without this one accidentally processing it as a cursor.
    """
    backend = get_backend()
    return [
        k
        for k in backend.list(_CURSOR_PREFIX)
        if k.endswith(".json") and not k[len(_CURSOR_PREFIX) :].startswith("_")
    ]


def _chat_id_from_key(key: str) -> str:
    """Recover the original ``chat_id`` from a cursor key.

    Inverse of :func:`entrabot.tools.chat_cursors.cursor_key`. Falls back to
    the raw key when parsing fails — the caller only needs *something* stable
    for logging.
    """
    if not key.startswith(_CURSOR_PREFIX) or not key.endswith(".json"):
        return key
    quoted = key[len(_CURSOR_PREFIX) : -len(".json")]
    try:
        return unquote(quoted)
    except Exception:  # noqa: BLE001 — never fail migration for a log format issue.
        return quoted


def recent_self_authored_ids(
    chat_id: str,
    agent_upn: str | None,
    agent_object_id: str | None,
) -> list[str]:
    """Fetch the last *_RECENT_LOOKBACK_COUNT* self-authored message IDs.

    Split into its own function so the migration test can monkeypatch it —
    the tests never touch Graph. Safe to call live: on any error we return
    ``[]`` so migration proceeds with just the ``last_ts`` bump.
    """
    try:
        # Deferred import: Graph modules pull httpx + auth. Failing at import
        # time when Graph is offline would block the ``last_ts`` bump too.
        import asyncio

        from entrabot.tools.teams import acquire_agent_user_token, read
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Skipping seen_ids_tail lookup for %s (import failed): %s: %s",
            chat_id,
            type(exc).__name__,
            exc,
        )
        return []

    try:
        cfg = get_config()
        token = acquire_agent_user_token(cfg)
        msgs = asyncio.run(
            read(chat_id=chat_id, count=_RECENT_LOOKBACK_COUNT, token=token)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not fetch recent messages for %s (self-id lookup skipped): "
            "%s: %s",
            chat_id,
            type(exc).__name__,
            exc,
        )
        return []

    upn_lower = (agent_upn or "").strip().lower() or None
    oid = (agent_object_id or "").strip() or None
    ids: list[str] = []
    for m in msgs:
        msg_upn = str(m.get("sender_upn") or "").strip().lower()
        msg_oid = str(m.get("sender_id") or "").strip()
        upn_match = bool(upn_lower and msg_upn and msg_upn == upn_lower)
        oid_match = bool(oid and msg_oid and msg_oid == oid)
        if upn_match or oid_match:
            ids.append(m["message_id"])
    return ids


def run(
    *,
    dry_run: bool = False,
    agent_upn: str | None = None,
    agent_object_id: str | None = None,
) -> dict:
    """Migrate every ``chat_cursors/*.json`` blob.

    Returns a summary dict::

        {
          "inspected": <N>,
          "changed": <M>,   # cursors actually written (0 on dry-run)
          "would_change": <M>,   # cursors that WOULD be written on non-dry-run
          "skipped_already_migrated": <K>,
          "chats": [{"chat_id": ..., "action": "bump"|"skip"|"error"}, ...]
        }

    Fail-safe: an error on one cursor does NOT abort the migration; that
    cursor is recorded as ``error`` in the per-chat details and processing
    continues.
    """
    cfg = get_config()
    resolved_upn = agent_upn if agent_upn is not None else cfg.agent_user_upn
    resolved_oid = (
        agent_object_id
        if agent_object_id is not None
        else (cfg.agent_user_id or cfg.agent_object_id)
    )

    backend = get_backend()

    # Stable skip: if the migration flag exists, we've already run to
    # completion on this backend. Return early — even a --dry-run just reports
    # "nothing to do" rather than re-simulating work that was already done.
    flag = _read_migration_flag()
    if flag is not None:
        return {
            "inspected": 0,
            "changed": 0,
            "would_change": 0,
            "skipped_already_migrated": 0,
            "chats": [],
            "flag_present": True,
            "flag_written_at": flag.get("written_at"),
            "flag_cursors_migrated": flag.get("cursors_migrated"),
        }

    keys = _list_cursor_keys()
    summary = {
        "inspected": len(keys),
        "changed": 0,
        "would_change": 0,
        "skipped_already_migrated": 0,
        "chats": [],
        "flag_present": False,
    }

    for key in keys:
        chat_id = _chat_id_from_key(key)
        try:
            raw = backend.read_text(key)
            if raw is None:
                summary["chats"].append({"chat_id": chat_id, "action": "skip-missing"})
                continue
            cursor = json.loads(raw)
            if not isinstance(cursor, dict):
                summary["chats"].append(
                    {"chat_id": chat_id, "action": "skip-bad-shape"}
                )
                continue

            self_ids = recent_self_authored_ids(chat_id, resolved_upn, resolved_oid)
            merged_tail = bound_seen_ids(
                list(cursor.get("seen_ids_tail") or []) + self_ids
            )
            new_cursor = {
                "last_ts": _bumped_ts(),
                "seen_ids_tail": merged_tail[-MAX_SEEN_IDS_TAIL:],
                "bootstrapped": True,
                "last_written_at": _now_iso(),
            }

            summary["would_change"] += 1
            if dry_run:
                summary["chats"].append(
                    {
                        "chat_id": chat_id,
                        "action": "would-bump",
                        "old_last_ts": cursor.get("last_ts"),
                        "new_last_ts": new_cursor["last_ts"],
                        "self_ids_added": len(self_ids),
                    }
                )
                continue

            backend.write_text(key, json.dumps(new_cursor))
            summary["changed"] += 1
            summary["chats"].append(
                {
                    "chat_id": chat_id,
                    "action": "bumped",
                    "old_last_ts": cursor.get("last_ts"),
                    "new_last_ts": new_cursor["last_ts"],
                    "self_ids_added": len(self_ids),
                }
            )
        except Exception as exc:  # noqa: BLE001 — one bad cursor mustn't stop the run.
            logger.warning(
                "Migration error for %s (skipped): %s: %s",
                chat_id,
                type(exc).__name__,
                exc,
            )
            summary["chats"].append(
                {
                    "chat_id": chat_id,
                    "action": "error",
                    "error_type": type(exc).__name__,
                }
            )

    # Write the stable migration flag on a successful non-dry-run pass. The
    # flag's presence — not per-cursor state — is the skip predicate for
    # future invocations.
    if not dry_run:
        _write_migration_flag(cursors_migrated=summary["changed"])

    return summary


def verify() -> dict:
    """Report whether the migration flag is present.

    Read-only. The flag is a stable predicate: once written, it stays written,
    so verify's answer does not drift over time (unlike a per-cursor
    ``last_ts >= now`` check, which would flip cursors from "migrated" back to
    "pending" a few seconds after the initial bump).
    """
    flag = _read_migration_flag()
    keys = _list_cursor_keys()
    return {
        "flag_present": flag is not None,
        "flag_written_at": (flag or {}).get("written_at"),
        "flag_cursors_migrated": (flag or {}).get("cursors_migrated"),
        "cursors_present": len(keys),
    }


def _print_summary(summary: dict, *, dry_run: bool) -> None:
    prefix = "[DRY-RUN] " if dry_run else ""
    if summary.get("flag_present") and summary["inspected"] == 0:
        print(
            f"{prefix}Migration flag already present "
            f"(written_at={summary.get('flag_written_at')}, "
            f"cursors_migrated={summary.get('flag_cursors_migrated')}). "
            "Nothing to do."
        )
        return
    print(f"{prefix}Inspected: {summary['inspected']}")
    print(f"{prefix}Would change: {summary['would_change']}")
    print(f"{prefix}Changed (written): {summary['changed']}")
    errors = sum(1 for c in summary["chats"] if c.get("action") == "error")
    if errors:
        print(f"{prefix}Errors: {errors} (see log)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate chat_cursors to the UPN-identity fix (2026-07-09 replay bug). "
            "Bumps last_ts past now and seeds seen_ids_tail with recent "
            "self-authored message IDs."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned changes without writing.",
    )
    mode.add_argument(
        "--verify",
        action="store_true",
        help="Read-only report: how many cursors are already migrated.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.verify:
        report = verify()
        print(json.dumps(report, indent=2))
        return 0

    summary = run(dry_run=args.dry_run)
    _print_summary(summary, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
