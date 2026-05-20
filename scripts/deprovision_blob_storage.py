#!/usr/bin/env python3
"""
deprovision_blob_storage.py
===========================
Remove Azure Blob Storage resources provisioned by ``provision_blob_storage.py``.

Inverse of ``provision_blob_storage.py``.

Levels of destruction (safe defaults):
  - Default:            delete the container only
  - ``--delete-account``: also delete the storage account
  - ``--delete-resource-group``: also delete the resource group (implies account)

Requires ``az login`` and appropriate permissions.

Usage::

    python3 scripts/deprovision_blob_storage.py \\
        --storage-account entclaw... --container agent-...

    python3 scripts/deprovision_blob_storage.py \\
        --storage-account entclaw... --container agent-... \\
        --delete-account --delete-resource-group

    python3 scripts/deprovision_blob_storage.py \\
        --storage-account entclaw... --container agent-... \\
        --dry-run
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

DEFAULT_RESOURCE_GROUP = "entraclaw-rg"


def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def _run_az(args: list[str], *, capture: bool = True) -> subprocess.CompletedProcess:
    az_bin = shutil.which("az") or "az"
    cmd = [az_bin, *args]
    return subprocess.run(  # noqa: S603
        cmd,
        capture_output=capture,
        text=True,
        check=False,
    )


def delete_container(account: str, container: str) -> None:
    _eprint(f"  • Deleting container '{container}' on '{account}'...")
    res = _run_az([
        "storage", "container", "delete",
        "--account-name", account,
        "--name", container,
        "--auth-mode", "login",
        "--yes",
    ])
    if res.returncode != 0:
        raise RuntimeError(f"az storage container delete failed: {res.stderr.strip()}")
    _eprint("    ✓ deleted")


def delete_storage_account(account: str, resource_group: str) -> None:
    _eprint(f"  • Deleting storage account '{account}'...")
    res = _run_az([
        "storage", "account", "delete",
        "--name", account,
        "--resource-group", resource_group,
        "--yes",
    ])
    if res.returncode != 0:
        raise RuntimeError(f"az storage account delete failed: {res.stderr.strip()}")
    _eprint("    ✓ deleted")


def delete_resource_group(resource_group: str) -> None:
    _eprint(f"  • Deleting resource group '{resource_group}'...")
    res = _run_az([
        "group", "delete",
        "--name", resource_group,
        "--yes",
        "--no-wait",
    ])
    if res.returncode != 0:
        raise RuntimeError(f"az group delete failed: {res.stderr.strip()}")
    _eprint("    ✓ deletion initiated (async)")


def deprovision(
    *,
    storage_account: str,
    container: str,
    resource_group: str = DEFAULT_RESOURCE_GROUP,
    delete_account: bool = False,
    delete_resource_group: bool = False,
) -> None:
    """Delete blob storage resources in safe order: container → account → RG."""
    delete_container(storage_account, container)

    if delete_account:
        delete_storage_account(storage_account, resource_group)

    if delete_resource_group:
        _delete_rg = globals()["delete_resource_group"]
        _delete_rg(resource_group)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deprovision Azure Blob Storage for an EntraClaw Agent User."
    )
    parser.add_argument(
        "--storage-account",
        required=True,
        help="Storage account name.",
    )
    parser.add_argument(
        "--container",
        required=True,
        help="Container name.",
    )
    parser.add_argument(
        "--resource-group",
        default=DEFAULT_RESOURCE_GROUP,
        help=f"Resource group name (default: {DEFAULT_RESOURCE_GROUP}).",
    )
    parser.add_argument(
        "--delete-account",
        action="store_true",
        help="Also delete the storage account (not just the container).",
    )
    parser.add_argument(
        "--delete-resource-group",
        action="store_true",
        help="Also delete the resource group (implies --delete-account).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without actually deleting.",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip confirmation prompt.",
    )

    args = parser.parse_args(argv)

    if args.delete_resource_group:
        args.delete_account = True

    if args.dry_run:
        print("DRY RUN — the following would be deleted:")
        print(f"  Container: {args.container} on {args.storage_account}")
        if args.delete_account:
            print(f"  Storage account: {args.storage_account}")
        if args.delete_resource_group:
            print(f"  Resource group: {args.resource_group}")
        return 0

    if not args.yes:
        print("The following will be PERMANENTLY deleted:")
        print(f"  Container: {args.container} on {args.storage_account}")
        if args.delete_account:
            print(f"  Storage account: {args.storage_account}")
        if args.delete_resource_group:
            print(f"  Resource group: {args.resource_group}")
        answer = input("\nProceed? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 0

    try:
        deprovision(
            storage_account=args.storage_account,
            container=args.container,
            resource_group=args.resource_group,
            delete_account=args.delete_account,
            delete_resource_group=args.delete_resource_group,
        )
    except RuntimeError as exc:
        _eprint(f"ERROR: {exc}")
        return 1

    _eprint("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
