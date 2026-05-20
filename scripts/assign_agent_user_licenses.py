#!/usr/bin/env python3
"""Assign Teams and/or Copilot licenses to the Agent User.

Extracted from ``create_entra_agent_ids.py`` to allow standalone license
management.  The original ``assign_license_to_agent_user()`` function remains
in the monolith for ``setup.sh`` backward compatibility; this script is the
user-facing CLI equivalent.

Usage::

    # Auto-select best available Teams + Copilot SKUs
    python scripts/assign_agent_user_licenses.py

    # List available SKUs
    python scripts/assign_agent_user_licenses.py --list-available

    # Assign a specific SKU
    python scripts/assign_agent_user_licenses.py --sku ENTERPRISEPACK
"""

from __future__ import annotations

import argparse
import sys
import time

# fmt: off
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from entra_provisioning import (  # noqa: E402
    ProvisionerBootstrapError,
    get_existing_graph_token,
    get_state,
    set_state,
)

# fmt: on
from entraclaw.graph_helpers import GRAPH_V1, graph_request  # noqa: E402
from entraclaw.preflight import COPILOT_CAPABLE_SKUS, TEAMS_CAPABLE_SKUS  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers (extracted from create_entra_agent_ids.py)
# ---------------------------------------------------------------------------

def _get_available_skus(token: str) -> list[dict]:
    """Get all subscribed SKUs with available licenses."""
    resp = graph_request("GET", "/subscribedSkus", token, base_url=GRAPH_V1)
    if resp.status_code != 200:
        print(f"  WARNING: Could not list subscribed SKUs ({resp.status_code})")
        return []

    skus = resp.json().get("value", [])
    available = []
    for sku in skus:
        enabled = sku.get("prepaidUnits", {}).get("enabled", 0)
        consumed = sku.get("consumedUnits", 0)
        remaining = enabled - consumed
        if remaining > 0:
            available.append({
                "skuId": sku["skuId"],
                "skuPartNumber": sku.get("skuPartNumber", ""),
                "displayName": sku.get("skuPartNumber", sku["skuId"]),
                "remaining": remaining,
                "total": enabled,
            })
    return available


def _get_all_sku_part_numbers(token: str) -> dict[str, str] | None:
    """Map skuId → skuPartNumber for all subscribed SKUs."""
    resp = graph_request("GET", "/subscribedSkus", token, base_url=GRAPH_V1)
    if resp.status_code != 200:
        print(f"  WARNING: Could not resolve subscribed SKU names ({resp.status_code})")
        return None
    return {
        sku["skuId"]: sku.get("skuPartNumber", sku["skuId"])
        for sku in resp.json().get("value", [])
    }


def _check_existing_licenses(token: str, user_id: str) -> list[str]:
    """Check what licenses are already assigned to the user."""
    resp = graph_request(
        "GET", f"/users/{user_id}?$select=assignedLicenses", token, base_url=GRAPH_V1
    )
    if resp.status_code == 200:
        return [lic.get("skuId", "") for lic in resp.json().get("assignedLicenses", [])]
    return []


def _set_usage_location(token: str, user_id: str, location: str = "US") -> bool:
    """Set usageLocation on a user (required before license assignment)."""
    resp = graph_request(
        "PATCH", f"/users/{user_id}", token,
        json_body={"usageLocation": location},
        base_url=GRAPH_V1,
    )
    return resp.status_code in (200, 204)


def _ensure_usage_location(token: str, user_id: str) -> bool:
    """Retry setting usageLocation up to 5 times."""
    print("  Setting usageLocation on Agent User...")
    for attempt in range(5):
        if _set_usage_location(token, user_id):
            return True
        wait = 5 * (attempt + 1)
        print(f"  Agent User not ready yet, retrying in {wait}s...")
        time.sleep(wait)
    print("  WARNING: Could not set usageLocation after retries")
    return False


def _assign_license(token: str, user_id: str, sku: dict, label: str) -> bool:
    """POST /users/{id}/assignLicense with retry."""
    print(f"  Assigning {sku['displayName']} to Agent User...")
    resp = None
    for attempt in range(3):
        resp = graph_request(
            "POST", f"/users/{user_id}/assignLicense", token,
            json_body={
                "addLicenses": [{"skuId": sku["skuId"]}],
                "removeLicenses": [],
            },
            base_url=GRAPH_V1,
        )
        if resp.status_code in (200, 201):
            print(f"  [done] {label} license assigned: {sku['displayName']}")
            return True
        if attempt < 2:
            wait = 10 * (attempt + 1)
            print(f"  License assignment returned {resp.status_code}, retrying in {wait}s...")
            time.sleep(wait)

    print(f"  WARNING: {label} license assignment failed ({resp.status_code})")
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assign Teams and/or Copilot licenses to the Agent User.",
    )
    parser.add_argument(
        "--list-available", action="store_true",
        help="List available Teams/Copilot SKUs and exit.",
    )
    parser.add_argument(
        "--sku", metavar="PART_NUMBER",
        help="Assign a specific SKU by part number (e.g. ENTERPRISEPACK).",
    )
    args = parser.parse_args(argv)

    try:
        token = get_existing_graph_token()
    except ProvisionerBootstrapError as exc:
        print(f"ERROR: {exc}")
        return 1

    # --list-available: print and exit
    if args.list_available:
        all_skus = _get_available_skus(token)
        if not all_skus:
            print("No subscribed SKUs with available seats found.")
            return 0
        print(f"\n{'Part Number':<35} {'Remaining':>10} {'Total':>8}")
        print("-" * 55)
        for sku in all_skus:
            marker = ""
            if sku["skuPartNumber"] in TEAMS_CAPABLE_SKUS:
                marker = " [Teams]"
            elif sku["skuPartNumber"] in COPILOT_CAPABLE_SKUS:
                marker = " [Copilot]"
            print(
                f"  {sku['skuPartNumber']:<33} {sku['remaining']:>10}"
                f" {sku['total']:>8}{marker}"
            )
        return 0

    # Resolve Agent User ID
    agent_user_id = get_state("AGENT_USER_ID")
    if not agent_user_id:
        print("ERROR: AGENT_USER_ID not found in state.")
        print("  Run setup.sh first to provision the Agent User.")
        return 1

    print(f"\n--- License Assignment for Agent User {agent_user_id} ---\n")

    # Check existing licenses
    existing_sku_ids = _check_existing_licenses(token, agent_user_id)
    sku_id_to_name = _get_all_sku_part_numbers(token)
    if existing_sku_ids and sku_id_to_name is None:
        print("  WARNING: Could not resolve existing license SKU names.")
        print("  Skipping to avoid duplicate assignment.")
        return 1
    sku_id_to_name = sku_id_to_name or {}
    existing_names = [sku_id_to_name.get(sid, sid) for sid in existing_sku_ids]

    # --sku: assign a specific SKU
    if args.sku:
        all_skus = _get_available_skus(token)
        matched = [s for s in all_skus if s["skuPartNumber"] == args.sku]
        if not matched:
            print(f"  ERROR: SKU '{args.sku}' not found or has no available seats.")
            return 1
        if not _ensure_usage_location(token, agent_user_id):
            return 1
        if _assign_license(token, agent_user_id, matched[0], args.sku):
            return 0
        return 1

    # Auto-select: assign Teams + Copilot
    has_teams = any(name in TEAMS_CAPABLE_SKUS for name in existing_names)
    has_copilot = any(name in COPILOT_CAPABLE_SKUS for name in existing_names)

    if has_teams and has_copilot:
        print("  [skip] Agent User already has Teams and Copilot licenses.")
        return 0

    if has_teams:
        print("  [skip] Already has Teams-capable license.")
    if has_copilot:
        print("  [skip] Already has Copilot license.")

    all_skus = _get_available_skus(token)
    if not all_skus:
        print("  ERROR: No subscribed SKUs with available seats.")
        return 1

    if not _ensure_usage_location(token, agent_user_id):
        return 1

    if not has_teams:
        teams_skus = [s for s in all_skus if s["skuPartNumber"] in TEAMS_CAPABLE_SKUS]
        if teams_skus:
            chosen = teams_skus[0]
            if _assign_license(token, agent_user_id, chosen, "Teams"):
                set_state("AGENT_USER_LICENSE_SKU", chosen["skuPartNumber"])
        else:
            print("  No Teams-capable SKUs available.")

    if not has_copilot:
        copilot_skus = [s for s in all_skus if s["skuPartNumber"] in COPILOT_CAPABLE_SKUS]
        if copilot_skus:
            chosen = copilot_skus[0]
            if _assign_license(token, agent_user_id, chosen, "Copilot"):
                set_state("AGENT_USER_WORK_IQ_LICENSE_SKU", chosen["skuPartNumber"])
        else:
            print("  No Copilot SKUs available.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
