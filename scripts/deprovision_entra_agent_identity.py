#!/usr/bin/env python3
"""Targeted Agent User teardown for EntraClaw identity chains.

Deletes a single Agent User chain in the safe order:

1. Remove assigned licenses from the Agent User.
2. Delete the Agent User.
3. Delete the parent Agent Identity service principal.
4. Delete the parent Blueprint application.

Azure Blob Storage is intentionally out of scope. Containers and storage
accounts must be deleted manually or by a future explicit storage teardown.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from entra_provisioning import ProvisionerBootstrapError, get_existing_graph_token  # noqa: E402

# The repo root is one directory up; src/ contains the entraclaw package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from entraclaw.graph_helpers import (  # noqa: E402
    graph_collection_values,
    graph_request,
    odata_escape,
    require_ok,
)


@dataclass(frozen=True)
class AgentUserChain:
    user_id: str
    user_principal_name: str
    assigned_license_ids: list[str]
    group_assigned_license_ids: list[str]
    agent_identity_object_id: str
    agent_identity_app_id: str
    agent_identity_display_name: str
    blueprint_app_id: str
    blueprint_object_id: str
    blueprint_display_name: str


def resolve_agent_user_chain(token: str, upn: str) -> AgentUserChain | None:
    user_resp = graph_request(
        "GET",
        f"/users?$filter=userPrincipalName eq '{odata_escape(upn)}'"
        "&$select=id,userPrincipalName,identityParentId,assignedLicenses,"
        "licenseAssignmentStates",
        token,
    )
    require_ok(user_resp, f"Lookup Agent User {upn}")
    users = user_resp.json().get("value", [])
    if not users:
        return None

    user = users[0]
    user_id = user.get("id", "")
    agent_identity_object_id = user.get("identityParentId", "")
    if not user_id or not agent_identity_object_id:
        raise RuntimeError(f"Agent User {upn} is missing id or identityParentId")

    sp_resp = graph_request(
        "GET",
        f"/servicePrincipals/{agent_identity_object_id}",
        token,
    )
    require_ok(sp_resp, f"Lookup Agent Identity {agent_identity_object_id}")
    sp = sp_resp.json()
    blueprint_app_id = sp.get("agentIdentityBlueprintId", "")
    if not blueprint_app_id:
        raise RuntimeError(
            f"Agent Identity {agent_identity_object_id} has no agentIdentityBlueprintId"
        )

    blueprint_resp = graph_request(
        "GET",
        f"/applications?$filter=appId eq '{odata_escape(blueprint_app_id)}'"
        "&$select=id,appId,displayName",
        token,
    )
    require_ok(blueprint_resp, f"Lookup Blueprint {blueprint_app_id}")
    blueprints = blueprint_resp.json().get("value", [])
    if not blueprints:
        raise RuntimeError(f"Blueprint app not found for appId {blueprint_app_id}")
    blueprint = blueprints[0]

    license_states = user.get("licenseAssignmentStates") or []
    if license_states:
        assigned_license_ids = [
            state.get("skuId", "")
            for state in license_states
            if state.get("skuId") and not state.get("assignedByGroup")
        ]
        group_assigned_license_ids = [
            state.get("skuId", "")
            for state in license_states
            if state.get("skuId") and state.get("assignedByGroup")
        ]
    else:
        assigned_license_ids = [
            license_item.get("skuId", "")
            for license_item in user.get("assignedLicenses", [])
            if license_item.get("skuId")
        ]
        group_assigned_license_ids = []

    return AgentUserChain(
        user_id=user_id,
        user_principal_name=user.get("userPrincipalName", upn),
        assigned_license_ids=assigned_license_ids,
        group_assigned_license_ids=group_assigned_license_ids,
        agent_identity_object_id=agent_identity_object_id,
        agent_identity_app_id=sp.get("appId", ""),
        agent_identity_display_name=sp.get("displayName", ""),
        blueprint_app_id=blueprint_app_id,
        blueprint_object_id=blueprint.get("id", ""),
        blueprint_display_name=blueprint.get("displayName", ""),
    )


def ensure_blueprint_has_no_other_agent_identities(
    token: str, chain: AgentUserChain
) -> None:
    identities = [
        item
        for item in graph_collection_values(
            "/servicePrincipals/microsoft.graph.agentIdentity"
            "?$select=id,appId,displayName,agentIdentityBlueprintId&$top=999",
            token,
            f"List Agent Identities for Blueprint {chain.blueprint_app_id}",
        )
        if item.get("agentIdentityBlueprintId") == chain.blueprint_app_id
    ]
    other_identities = [
        item for item in identities if item.get("id") != chain.agent_identity_object_id
    ]
    if other_identities:
        raise RuntimeError(
            f"Blueprint has {len(other_identities)} other Agent Identity object(s); "
            "refusing to delete shared Blueprint"
        )


def remove_agent_user_licenses(token: str, chain: AgentUserChain) -> None:
    if chain.group_assigned_license_ids:
        print(
            f"  {len(chain.group_assigned_license_ids)} license(s) are group-inherited; "
            "they will be released when the Agent User is deleted."
        )
    if not chain.assigned_license_ids:
        print(f"  [skip] No directly assigned licenses on {chain.user_principal_name}")
        return
    print(
        f"  Removing {len(chain.assigned_license_ids)} license(s) from "
        f"{chain.user_principal_name}..."
    )
    resp = graph_request(
        "POST",
        f"/users/{chain.user_id}/assignLicense",
        token,
        json_body={"addLicenses": [], "removeLicenses": chain.assigned_license_ids},
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Failed to remove licenses ({resp.status_code}): {resp.text[:500]}")
    print("  [done] Licenses removed")


def delete_resource(token: str, path: str, label: str) -> None:
    resp = graph_request("DELETE", path, token)
    if resp.status_code in (200, 204):
        print(f"  [done] Deleted {label}")
        return
    if resp.status_code == 404:
        print(f"  [skip] {label} already deleted")
        return
    raise RuntimeError(f"Failed to delete {label} ({resp.status_code}): {resp.text[:500]}")


def deprovision_agent_user(token: str, upn: str, *, dry_run: bool = False) -> str:
    print(f"\n--- Deprovisioning {upn} ---\n")
    chain = resolve_agent_user_chain(token, upn)
    if chain is None:
        print(f"  [skip] Agent User not found: {upn}")
        return "missing"

    print(f"  Agent User:     {chain.user_principal_name} ({chain.user_id})")
    print(
        "  Agent Identity: "
        f"{chain.agent_identity_display_name} ({chain.agent_identity_object_id})"
    )
    print(f"  Blueprint:      {chain.blueprint_display_name} ({chain.blueprint_app_id})")
    print("  Cloud storage is not deleted by this script.")

    ensure_blueprint_has_no_other_agent_identities(token, chain)

    if dry_run:
        print("  [dry-run] Would remove licenses, Agent User, Agent Identity, and Blueprint")
        return "dry-run"

    remove_agent_user_licenses(token, chain)
    delete_resource(token, f"/users/{chain.user_id}", "Agent User")
    delete_resource(
        token,
        f"/servicePrincipals/{chain.agent_identity_object_id}",
        "Agent Identity",
    )
    delete_resource(
        token,
        f"/applications/{chain.blueprint_object_id}",
        "Blueprint application",
    )
    return "deleted"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deprovision targeted EntraClaw Agent User identity chains."
    )
    parser.add_argument(
        "--agent-user-upn",
        action="append",
        required=True,
        help="Agent User UPN to deprovision. May be repeated.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and print the chain without deleting anything.",
    )
    args = parser.parse_args(argv)

    try:
        token = get_existing_graph_token()
    except ProvisionerBootstrapError as exc:
        print(f"ERROR: {exc}")
        return 1

    for upn in args.agent_user_upn:
        try:
            deprovision_agent_user(token, upn, dry_run=args.dry_run)
        except RuntimeError as exc:
            print(f"ERROR: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
