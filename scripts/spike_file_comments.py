#!/usr/bin/env python3
"""Task 0 spike — capture verbatim Graph beta JSON shape for file comments.

Mints a fresh Agent User token (picks up the just-PATCHed Files / Sites
scopes), resolves the SharePoint URL passed on the command line, and
dumps the raw response from:

    GET /shares/{share-id}/driveItem        (resolution)
    GET /beta/drives/{drive}/items/{item}/comments
    GET /beta/drives/{drive}/items/{item}/comments/{comment_id}/replies
        (only if at least one comment exists)

Throwaway after the spike findings are recorded in the implementation
plan (docs/superpowers/plans/2026-05-04-file-comment-reply-tools.md).

Usage:
    python scripts/spike_file_comments.py <share-url>
"""

from __future__ import annotations

import base64
import json
import sys
from urllib.parse import urlparse

import httpx

from entrabot.config import EntraBotConfig
from entrabot.tools.teams import acquire_agent_user_token

GRAPH_V1 = "https://graph.microsoft.com/v1.0"
GRAPH_BETA = "https://graph.microsoft.com/beta"


def _share_id_from_url(url: str) -> str:
    """Encode a SharePoint sharing URL as Graph's share-id (b64url)."""
    encoded = base64.urlsafe_b64encode(url.strip().encode("utf-8")).decode("ascii")
    return "u!" + encoded.rstrip("=")


def _print_section(label: str) -> None:
    print("\n" + "=" * 60)
    print(label)
    print("=" * 60)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <share-url>", file=sys.stderr)
        return 1

    share_url = sys.argv[1]
    parsed = urlparse(share_url)
    if not parsed.scheme or not parsed.netloc:
        print(f"ERROR: not a valid URL: {share_url}", file=sys.stderr)
        return 1

    # Load .env into os.environ before reading config
    env_path = __import__("pathlib").Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        import os
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    config = EntraBotConfig.from_env()
    print("Minting fresh Agent User token (picks up newly-PATCHed scopes)...")
    token = acquire_agent_user_token(config)
    headers = {"Authorization": f"Bearer {token}"}

    # First try direct share-URL resolution. If that 403s (DF tenant
    # share-link recipient-locked), fall back to /me/drive/sharedWithMe
    # which lists docs the Agent User has been ADDED to as a recipient.
    share_id = _share_id_from_url(share_url)
    resolve_url = f"{GRAPH_V1}/shares/{share_id}/driveItem"

    _print_section("Step 1a — resolve share URL")
    print(f"GET {resolve_url}")
    with httpx.Client(timeout=30.0) as c:
        r = c.get(resolve_url, headers=headers)
    print(f"status: {r.status_code}")

    drive_id = ""
    item_id = ""
    name = ""

    if r.status_code == 200:
        drive_item = r.json()
        drive_id = (drive_item.get("parentReference") or {}).get("driveId") or ""
        item_id = drive_item.get("id") or ""
        name = drive_item.get("name", "")
    else:
        print(r.text[:300])
        _print_section("Step 1b — fallback: /me/drive/sharedWithMe")
        shared_url = f"{GRAPH_V1}/me/drive/sharedWithMe"
        print(f"GET {shared_url}")
        with httpx.Client(timeout=30.0) as c:
            r2 = c.get(shared_url, headers=headers)
        print(f"status: {r2.status_code}")
        if r2.status_code != 200:
            print(r2.text[:500])
            return 2
        items = r2.json().get("value", [])
        print(f"found {len(items)} shared items")
        # Match by name fragment (case-insensitive)
        target = "Entra Agent User Identity"
        match = next(
            (it for it in items if target.lower() in (it.get("name") or "").lower()),
            None,
        )
        if not match:
            print(
                f"\n(no shared item matching {target!r} — "
                "falling back to first shared .docx for shape capture)"
            )
            for it in items[:20]:
                print(f"  - {it.get('name', '(no name)')}")
            match = next(
                (it for it in items if (it.get("name") or "").lower().endswith(".docx")),
                None,
            )
            if not match:
                print("\nERROR: no .docx in sharedWithMe at all")
                return 3
        # sharedWithMe returns shortcut items — the real driveId/itemId
        # live under remoteItem
        remote = match.get("remoteItem") or {}
        drive_id = (remote.get("parentReference") or {}).get("driveId") or ""
        item_id = remote.get("id") or ""
        name = remote.get("name") or match.get("name", "")
        print(f"\nmatched: {name}")

    print(f"drive_id: {drive_id}")
    print(f"item_id:  {item_id}")
    print(f"name:     {name}")
    if not drive_id or not item_id:
        print("ERROR: drive_id or item_id missing")
        return 3

    comments_url = f"{GRAPH_BETA}/drives/{drive_id}/items/{item_id}/comments"
    _print_section("Step 2 — list comments")
    print(f"GET {comments_url}")
    with httpx.Client(timeout=30.0) as c:
        r = c.get(comments_url, headers=headers)
    print(f"status: {r.status_code}")
    if r.status_code != 200:
        print(r.text[:1000])
        return 4
    comments_body = r.json()
    print(json.dumps(comments_body, indent=2)[:6000])

    items = comments_body.get("value", []) if isinstance(comments_body, dict) else []
    if not items:
        print("\n(no comments on this document — replies endpoint will be skipped)")
        return 0

    first = items[0]
    comment_id = str(first.get("id", ""))
    if not comment_id:
        print("\nWARNING: first comment has no 'id' field — cannot test replies endpoint")
        return 0

    replies_url = f"{GRAPH_BETA}/drives/{drive_id}/items/{item_id}/comments/{comment_id}/replies"
    _print_section(f"Step 3 — list replies for comment {comment_id}")
    print(f"GET {replies_url}")
    with httpx.Client(timeout=30.0) as c:
        r = c.get(replies_url, headers=headers)
    print(f"status: {r.status_code}")
    if r.status_code != 200:
        print(r.text[:1000])
        return 5
    print(json.dumps(r.json(), indent=2)[:4000])

    return 0


if __name__ == "__main__":
    sys.exit(main())
