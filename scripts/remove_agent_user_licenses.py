#!/usr/bin/env python3
"""
remove_agent_user_licenses.py
=============================
Remove licenses assigned to the Agent User.

Inverse of the license-assignment steps in ``create_entra_agent_ids.py`` and
``deprovision_entra_agent_identity.py``.

Group-inherited licenses are reported but cannot be removed; only
directly-assigned licenses are touched.

Usage::

    # Remove all directly-assigned licenses
    python3 scripts/remove_agent_user_licenses.py --all

    # Remove a specific SKU
    python3 scripts/remove_agent_user_licenses.py --sku-id sku-aaa-bbb

    # Override the UPN from state
    python3 scripts/remove_agent_user_licenses.py --all --upn agent@example.com
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from entra_provisioning import (  # noqa: E402
    ProvisionerBootstrapError,
    get_existing_graph_token,
    get_state,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from entraclaw.graph_helpers import GRAPH_V1, graph_request  # noqa: E402


def _print_usage() -> None:
    print("usage: remove_agent_user_licenses.py (--all | --sku-id SKU_ID) [--upn UPN]")
    print("")
    print("Options:")
    print("  --all             Remove all directly-assigned licenses")
    print("  --sku-id SKU_ID   Remove one directly-assigned SKU")
    print("  --upn UPN         Override the Agent User UPN from local state")
    print("  --help, -h        Show this help")


def _lookup_user(token: str, upn: str) -> dict | None:
    """Lookup user by UPN, returning full license info."""
    resp = graph_request(
        "GET",
        f"/users?$filter=userPrincipalName eq '{upn}'"
        "&$select=id,userPrincipalName,assignedLicenses,licenseAssignmentStates",
        token,
        None,
        base_url=GRAPH_V1,
    )
    if resp.status_code != 200:
        return None
    users = resp.json().get("value", [])
    return users[0] if users else None


def _remove_licenses(token: str, user_id: str, sku_ids: list[str]) -> bool:
    """POST /users/{id}/assignLicense to remove the given SKUs."""
    resp = graph_request(
        "POST",
        f"/users/{user_id}/assignLicense",
        token,
        {
            "addLicenses": [],
            "removeLicenses": sku_ids,
        },
        base_url=GRAPH_V1,
    )
    if resp.status_code not in (200, 204):
        print(f"assignLicense failed: {resp.status_code} {resp.text}", file=sys.stderr)
        return False
    return True


def main(argv: list[str]) -> int:
    # ---- parse args ----
    if any(arg in ("--help", "-h") for arg in argv[1:]):
        _print_usage()
        return 0

    remove_all = False
    target_sku: str | None = None
    upn: str | None = None

    i = 1
    while i < len(argv):
        if argv[i] == "--all":
            remove_all = True
            i += 1
        elif argv[i] == "--sku-id" and i + 1 < len(argv):
            target_sku = argv[i + 1]
            i += 2
        elif argv[i] == "--upn" and i + 1 < len(argv):
            upn = argv[i + 1]
            i += 2
        else:
            i += 1

    if not remove_all and not target_sku:
        _print_usage()
        print("ERROR: provide --all or --sku-id <sku>", file=sys.stderr)
        return 2

    # ---- state ----
    if not upn:
        upn = get_state("AGENT_USER_UPN")
    if not upn:
        print(
            "ERROR: No Agent User UPN in state. Pass --upn or run provisioning first.",
            file=sys.stderr,
        )
        return 1

    try:
        token = get_existing_graph_token()
    except ProvisionerBootstrapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # ---- lookup ----
    user = _lookup_user(token, upn)
    if not user:
        print(f"ERROR: Agent User '{upn}' not found in directory.", file=sys.stderr)
        return 1

    user_id = user["id"]
    assigned = user.get("assignedLicenses", [])
    license_states = user.get("licenseAssignmentStates", [])

    if not assigned:
        print(f"No licenses assigned to {upn}.")
        return 0

    # Build set of group-inherited SKUs
    group_inherited = {
        ls["skuId"]
        for ls in license_states
        if ls.get("assignedByGroup")
    }

    # Determine which to remove
    if target_sku:
        to_remove = [target_sku]
    else:
        # --all: only directly-assigned
        to_remove = [
            lic["skuId"]
            for lic in assigned
            if lic["skuId"] not in group_inherited
        ]

    # Report group-inherited
    for sku in group_inherited:
        print(f"  ⚠ Skipping group-inherited license: {sku}")

    if not to_remove:
        print("No directly-assigned licenses to remove.")
        return 0

    print(f"Removing {len(to_remove)} license(s) from {upn}...")
    for sku in to_remove:
        print(f"  - {sku}")

    ok = _remove_licenses(token, user_id, to_remove)
    if not ok:
        return 1

    print("✓ Licenses removed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
