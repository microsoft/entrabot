#!/usr/bin/env python3
"""Show consolidated Agent Identity status and health.

Reads local state/configuration and queries Microsoft Graph for live data about
the Blueprint, Agent Identity, Agent User, Sponsors, Permissions, Certificates,
Licenses, and Storage configuration.

Usage::

    python scripts/show_agent_status.py
    python scripts/show_agent_status.py --json
    python scripts/show_agent_status.py --health-only
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import sys
from dataclasses import dataclass
from urllib.parse import urlparse

# fmt: off
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from entra_provisioning import (  # noqa: E402
    ProvisionerBootstrapError,
    get_existing_graph_token,
    get_state,
)

# fmt: on
from entrabot.config import get_config  # noqa: E402
from entrabot.graph_helpers import GRAPH_BETA, GRAPH_V1, graph_request  # noqa: E402


@dataclass
class Check:
    name: str
    status: str = "skip"  # pass | fail | warn | skip
    detail: str = ""


_STATE_KEYS = [
    "TENANT_ID",
    "PROVISIONER_CLIENT_ID",
    "PROVISIONER_CERT_THUMBPRINT",
    "BLUEPRINT_APP_ID",
    "BLUEPRINT_OBJECT_ID",
    "BLUEPRINT_CERT_THUMBPRINT",
    "AGENT_ID",
    "AGENT_OBJECT_ID",
    "AGENT_USER_ID",
    "AGENT_USER_UPN",
    "AGENT_USER_WORK_IQ_LICENSE_SKU",
]

_REQUIRED_STATE_KEYS = [
    "TENANT_ID",
    "BLUEPRINT_APP_ID",
    "BLUEPRINT_OBJECT_ID",
    "AGENT_ID",
    "AGENT_OBJECT_ID",
    "AGENT_USER_ID",
    "AGENT_USER_UPN",
    "PROVISIONER_CLIENT_ID",
    "PROVISIONER_CERT_THUMBPRINT",
]

_STATUS_ICON = {
    "pass": "\033[32mPASS\033[0m",
    "fail": "\033[31mFAIL\033[0m",
    "warn": "\033[33mWARN\033[0m",
    "skip": "\033[90mSKIP\033[0m",
}


def _read_all_state() -> dict[str, str | None]:
    return {key: get_state(key) for key in _STATE_KEYS}


def _graph_list(
    method: str, path: str, token: str, *, base_url: str
) -> tuple[list[dict] | None, str | None]:
    resp = graph_request(method, path, token, base_url=base_url)
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"
    return resp.json().get("value", []), None


def _fetch_sponsors(token: str, agent_oid: str) -> tuple[list[dict] | None, str | None]:
    path = (
        f"/servicePrincipals/{agent_oid}"
        "/microsoft.graph.agentIdentity/sponsors"
        "?$select=id,displayName,userPrincipalName,mail"
    )
    return _graph_list("GET", path, token, base_url=GRAPH_BETA)


def _fetch_sku_names(token: str) -> dict[str, str]:
    path = "/subscribedSkus?$select=skuId,skuPartNumber"
    resp = graph_request("GET", path, token, base_url=GRAPH_V1)
    if resp.status_code == 200:
        return {sku["skuId"]: sku["skuPartNumber"] for sku in resp.json().get("value", [])}
    return {}


def _fetch_licenses(token: str, agent_user_id: str) -> tuple[list[dict] | None, str | None]:
    path = f"/users/{agent_user_id}?$select=assignedLicenses"
    resp = graph_request("GET", path, token, base_url=GRAPH_V1)
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"
    return resp.json().get("assignedLicenses", []), None


def _fetch_permissions(
    token: str, agent_oid: str, agent_user_id: str
) -> tuple[list[dict] | None, str | None]:
    path = (
        "/oauth2PermissionGrants"
        f"?$filter=clientId eq '{agent_oid}'"
        f" and principalId eq '{agent_user_id}'"
    )
    return _graph_list("GET", path, token, base_url=GRAPH_V1)


def _fetch_resource_names(token: str, grants: list[dict] | None) -> dict[str, str]:
    names: dict[str, str] = {}
    for grant in grants or []:
        resource_id = grant.get("resourceId")
        if not resource_id or resource_id in names:
            continue
        path = f"/servicePrincipals/{resource_id}?$select=displayName"
        resp = graph_request("GET", path, token, base_url=GRAPH_V1)
        if resp.status_code == 200:
            names[resource_id] = resp.json().get("displayName") or resource_id
        else:
            names[resource_id] = resource_id
    return names


def _fetch_key_credentials(
    token: str, blueprint_object_id: str
) -> tuple[list[dict] | None, str | None]:
    path = f"/applications/{blueprint_object_id}?$select=keyCredentials"
    resp = graph_request("GET", path, token, base_url=GRAPH_V1)
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"
    return resp.json().get("keyCredentials", []), None


def _compute_cert_thumbprint(der_key_b64: str) -> str:
    try:
        der = base64.b64decode(der_key_b64)
        return base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode()
    except Exception:
        return ""


def _storage_account_from_endpoint(endpoint: str | None) -> str | None:
    if not endpoint:
        return None
    hostname = urlparse(endpoint).hostname or ""
    if hostname.endswith(".blob.core.windows.net"):
        return hostname.split(".", 1)[0]
    return None


def _storage_info() -> dict[str, object]:
    config = get_config()
    blob_endpoint = config.blob_endpoint or os.environ.get("ENTRABOT_BLOB_ENDPOINT")
    blob_container = config.blob_container or os.environ.get("ENTRABOT_BLOB_CONTAINER")
    keep_local = config.keep_memory_local or os.environ.get(
        "ENTRABOT_KEEP_MEMORY_LOCAL", ""
    ).lower() in {"1", "true", "yes"}
    mode = "local" if keep_local or not (blob_endpoint and blob_container) else "azure_blob"
    return {
        "mode": mode,
        "resource_group": os.environ.get("ENTRABOT_RESOURCE_GROUP", "entrabot-rg"),
        "blob_endpoint": blob_endpoint,
        "blob_container": blob_container,
        "storage_account": _storage_account_from_endpoint(blob_endpoint),
        "keep_memory_local": keep_local,
        "data_dir": str(config.data_dir),
        "log_dir": str(config.log_dir),
        "audit_dir": str(config.audit_dir),
    }


def _platform_info() -> dict[str, str]:
    return {
        "os": platform.system() or sys.platform,
        "platform": sys.platform,
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def _build_checks(
    state: dict[str, str | None],
    sponsors: list[dict] | None,
    sponsors_error: str | None,
    licenses: list[dict] | None,
    licenses_error: str | None,
    permissions: list[dict] | None,
    permissions_error: str | None,
    key_credentials: list[dict] | None,
    key_credentials_error: str | None,
) -> list[Check]:
    missing = [key for key in _REQUIRED_STATE_KEYS if not state.get(key)]
    checks = [
        Check(
            "State completeness",
            "fail" if missing else "pass",
            f"Missing: {', '.join(missing)}" if missing else "All required keys present",
        )
    ]

    if not state.get("AGENT_OBJECT_ID") and not state.get("AGENT_ID"):
        checks.append(Check("Sponsors", "skip", "No AGENT_OBJECT_ID or AGENT_ID"))
    elif sponsors_error:
        checks.append(Check("Sponsors", "fail", sponsors_error))
    elif not sponsors:
        checks.append(Check("Sponsors", "fail", "No sponsors assigned"))
    else:
        names = ", ".join(sponsor.get("displayName", "?") for sponsor in sponsors)
        checks.append(Check("Sponsors", "pass", f"{len(sponsors)} sponsor(s): {names}"))

    if not state.get("AGENT_USER_ID"):
        checks.append(Check("Licenses", "skip", "No AGENT_USER_ID"))
    elif licenses_error:
        checks.append(Check("Licenses", "fail", licenses_error))
    elif not licenses:
        checks.append(Check("Licenses", "fail", "No licenses assigned"))
    else:
        checks.append(Check("Licenses", "pass", f"{len(licenses)} license(s)"))

    if not (state.get("AGENT_OBJECT_ID") or state.get("AGENT_ID")) or not state.get(
        "AGENT_USER_ID"
    ):
        checks.append(Check("Permissions", "skip", "Missing Agent Identity or Agent User ID"))
    elif permissions_error:
        checks.append(Check("Permissions", "fail", permissions_error))
    elif not permissions:
        checks.append(Check("Permissions", "fail", "No permission grants"))
    else:
        total_scopes = sum(len(grant.get("scope", "").split()) for grant in permissions)
        checks.append(
            Check("Permissions", "pass", f"{len(permissions)} grant(s), {total_scopes} scope(s)")
        )

    if not state.get("BLUEPRINT_OBJECT_ID") and not state.get("BLUEPRINT_APP_ID"):
        checks.append(Check("Certificate", "skip", "No BLUEPRINT_OBJECT_ID or BLUEPRINT_APP_ID"))
    elif key_credentials_error:
        checks.append(Check("Certificate", "fail", key_credentials_error))
    elif not key_credentials:
        checks.append(Check("Certificate", "fail", "No key credentials found"))
    else:
        local_thumb = state.get("BLUEPRINT_CERT_THUMBPRINT")
        if local_thumb:
            matches = any(
                _compute_cert_thumbprint(cred.get("key", "")) == local_thumb
                for cred in key_credentials
            )
            if not matches:
                checks.append(
                    Check(
                        "Certificate",
                        "warn",
                        f"Local thumbprint {local_thumb[:16]}... not found in Entra "
                        f"({len(key_credentials)} cert(s) on Blueprint)",
                    )
                )
            else:
                checks.append(
                    Check(
                        "Certificate",
                        "pass",
                        f"{len(key_credentials)} key credential(s) on Blueprint",
                    )
                )
        else:
            checks.append(
                Check("Certificate", "warn", "BLUEPRINT_CERT_THUMBPRINT is not set locally")
            )

    return checks


def _license_rows(licenses: list[dict] | None, sku_names: dict[str, str]) -> list[dict[str, str]]:
    return [
        {
            "skuId": license_row.get("skuId", "N/A"),
            "skuPartNumber": sku_names.get(license_row.get("skuId", ""), "N/A"),
        }
        for license_row in licenses or []
    ]


def _permission_rows(
    permissions: list[dict] | None, resource_names: dict[str, str]
) -> list[dict[str, object]]:
    rows = []
    for grant in permissions or []:
        resource_id = grant.get("resourceId", "N/A")
        rows.append(
            {
                "id": grant.get("id"),
                "resourceId": resource_id,
                "resourceName": resource_names.get(resource_id, resource_id),
                "consentType": grant.get("consentType"),
                "scopes": grant.get("scope", "").split(),
            }
        )
    return rows


def _key_rows(
    key_credentials: list[dict] | None, local_thumb: str | None
) -> list[dict[str, object]]:
    rows = []
    for credential in key_credentials or []:
        thumbprint = _compute_cert_thumbprint(credential.get("key", ""))
        rows.append(
            {
                "keyId": credential.get("keyId"),
                "displayName": credential.get("displayName"),
                "type": credential.get("type"),
                "usage": credential.get("usage"),
                "endDateTime": credential.get("endDateTime"),
                "customKeyIdentifier": credential.get("customKeyIdentifier"),
                "computedThumbprint": thumbprint or None,
                "matchesLocalBlueprintCert": bool(local_thumb and thumbprint == local_thumb),
            }
        )
    return rows


def _snapshot(token: str) -> dict[str, object]:
    state = _read_all_state()
    agent_oid = state.get("AGENT_OBJECT_ID") or state.get("AGENT_ID")
    blueprint_oid = state.get("BLUEPRINT_OBJECT_ID") or state.get("BLUEPRINT_APP_ID")
    agent_user_id = state.get("AGENT_USER_ID")

    sponsors, sponsors_error = (None, None)
    licenses, licenses_error = (None, None)
    permissions, permissions_error = (None, None)
    key_credentials, key_credentials_error = (None, None)
    sku_names: dict[str, str] = {}
    resource_names: dict[str, str] = {}

    if agent_oid:
        sponsors, sponsors_error = _fetch_sponsors(token, agent_oid)
    if agent_user_id:
        licenses, licenses_error = _fetch_licenses(token, agent_user_id)
        sku_names = _fetch_sku_names(token)
    if agent_oid and agent_user_id:
        permissions, permissions_error = _fetch_permissions(token, agent_oid, agent_user_id)
        resource_names = _fetch_resource_names(token, permissions)
    if blueprint_oid:
        key_credentials, key_credentials_error = _fetch_key_credentials(token, blueprint_oid)

    checks = _build_checks(
        state,
        sponsors,
        sponsors_error,
        licenses,
        licenses_error,
        permissions,
        permissions_error,
        key_credentials,
        key_credentials_error,
    )
    failed = sum(1 for check in checks if check.status == "fail")
    warned = sum(1 for check in checks if check.status == "warn")
    passed = sum(1 for check in checks if check.status == "pass")

    return {
        "tenant": {"id": state.get("TENANT_ID")},
        "provisioner": {
            "client_id": state.get("PROVISIONER_CLIENT_ID"),
            "cert_thumbprint": state.get("PROVISIONER_CERT_THUMBPRINT"),
        },
        "blueprint": {
            "app_object_id": state.get("BLUEPRINT_APP_ID"),
            "object_id": state.get("BLUEPRINT_OBJECT_ID"),
            "cert_thumbprint": state.get("BLUEPRINT_CERT_THUMBPRINT"),
        },
        "agent_identity": {
            "app_object_id": state.get("AGENT_ID"),
            "object_id": state.get("AGENT_OBJECT_ID"),
        },
        "agent_user": {
            "object_id": state.get("AGENT_USER_ID"),
            "upn": state.get("AGENT_USER_UPN"),
            "work_iq_license_sku": state.get("AGENT_USER_WORK_IQ_LICENSE_SKU"),
        },
        "state": state,
        "platform": _platform_info(),
        "storage": _storage_info(),
        "sponsors": sponsors,
        "sponsors_error": sponsors_error,
        "licenses": _license_rows(licenses, sku_names),
        "licenses_error": licenses_error,
        "permissions": _permission_rows(permissions, resource_names),
        "permissions_error": permissions_error,
        "key_credentials": _key_rows(key_credentials, state.get("BLUEPRINT_CERT_THUMBPRINT")),
        "key_credentials_error": key_credentials_error,
        "health": {
            "checks": [check.__dict__ for check in checks],
            "passed": passed,
            "failed": failed,
            "warnings": warned,
            "total": len(checks),
        },
    }


def _v(state: dict[str, str | None], key: str) -> str:
    return state.get(key) or "N/A"


def _print_health(snapshot: dict[str, object], *, title: bool = True) -> None:
    health = snapshot["health"]
    if title:
        print("\n=== EntraBot Health Check ===\n")
    for check in health["checks"]:
        icon = _STATUS_ICON.get(check["status"], check["status"])
        print(f"  {icon}  {check['name']}")
        if check["detail"]:
            print(f"        {check['detail']}")
    print()
    print(
        f"  {health['passed']} passed, {health['failed']} failed, "
        f"{health['warnings']} warnings, {health['total']} total"
    )
    print()


def _print_text(snapshot: dict[str, object]) -> None:
    state = snapshot["state"]
    storage = snapshot["storage"]
    platform_info = snapshot["platform"]

    print("\n=== EntraBot Agent Identity Status ===\n")
    _print_health(snapshot, title=False)

    print("  Local Platform")
    print(f"    OS:                   {platform_info['os']} ({platform_info['platform']})")
    print(f"    Release:              {platform_info['release']}")
    print(f"    Machine:              {platform_info['machine']}")
    print(f"    Python:               {platform_info['python']}")
    print()

    print("  Tenant")
    print(f"    Tenant ID:            {_v(state, 'TENANT_ID')}")
    print()

    print("  Provisioner")
    print(f"    Client ID:            {_v(state, 'PROVISIONER_CLIENT_ID')}")
    print(f"    Cert Thumbprint:      {_v(state, 'PROVISIONER_CERT_THUMBPRINT')}")
    print()

    print("  Blueprint")
    print(f"    App ID:               {_v(state, 'BLUEPRINT_APP_ID')}")
    print(f"    Object ID:            {_v(state, 'BLUEPRINT_OBJECT_ID')}")
    print(f"    Cert Thumbprint:      {_v(state, 'BLUEPRINT_CERT_THUMBPRINT')}")
    print()

    print("  Agent Identity")
    print(f"    App ID:               {_v(state, 'AGENT_ID')}")
    print(f"    Object ID:            {_v(state, 'AGENT_OBJECT_ID')}")
    print()

    print("  Agent User")
    print(f"    Object ID:            {_v(state, 'AGENT_USER_ID')}")
    print(f"    UPN:                  {_v(state, 'AGENT_USER_UPN')}")
    print(f"    Work IQ License SKU:  {_v(state, 'AGENT_USER_WORK_IQ_LICENSE_SKU')}")
    print()

    print("  Storage")
    print(f"    Mode:                 {storage['mode']}")
    print(f"    Resource Group:       {storage['resource_group']}")
    print(f"    Storage Account:      {storage['storage_account'] or 'N/A'}")
    print(f"    Blob Endpoint:        {storage['blob_endpoint'] or 'N/A'}")
    print(f"    Blob Container:       {storage['blob_container'] or 'N/A'}")
    print(f"    Keep Memory Local:    {storage['keep_memory_local']}")
    print(f"    Data Dir:             {storage['data_dir']}")
    print(f"    Log Dir:              {storage['log_dir']}")
    print(f"    Audit Dir:            {storage['audit_dir']}")
    print()

    sponsors = snapshot["sponsors"]
    print(f"  Sponsors ({len(sponsors or [])})")
    if snapshot["sponsors_error"]:
        print(f"    Unavailable: {snapshot['sponsors_error']}")
    elif sponsors:
        for sponsor in sponsors:
            upn = sponsor.get("userPrincipalName") or "N/A"
            mail = sponsor.get("mail") or "N/A"
            print(
                f"    - {sponsor.get('displayName', 'N/A')} ({upn},"
                f" mail={mail}, id={sponsor.get('id', 'N/A')})"
            )
    else:
        print("    (none)")
    print()

    licenses = snapshot["licenses"]
    print(f"  Licenses ({len(licenses)})")
    if snapshot["licenses_error"]:
        print(f"    Unavailable: {snapshot['licenses_error']}")
    elif licenses:
        for license_row in licenses:
            print(f"    - {license_row['skuId']} ({license_row['skuPartNumber']})")
    else:
        print("    (none)")
    print()

    permissions = snapshot["permissions"]
    print(f"  Permission Grants ({len(permissions)})")
    if snapshot["permissions_error"]:
        print(f"    Unavailable: {snapshot['permissions_error']}")
    elif permissions:
        for grant in permissions:
            print(f"    - Resource: {grant['resourceName']} ({grant['resourceId']})")
            print(f"      Consent:  {grant.get('consentType') or 'N/A'}")
            print(f"      Scopes:   {' '.join(grant['scopes'])}")
    else:
        print("    (none)")
    print()

    keys = snapshot["key_credentials"]
    print(f"  Blueprint Key Credentials ({len(keys)})")
    if snapshot["key_credentials_error"]:
        print(f"    Unavailable: {snapshot['key_credentials_error']}")
    elif keys:
        for key in keys:
            match = "yes" if key["matchesLocalBlueprintCert"] else "no"
            print(f"    - Key ID:               {key.get('keyId') or 'N/A'}")
            print(f"      Display Name:         {key.get('displayName') or 'N/A'}")
            print(
                f"      Type/Usage:           {key.get('type') or 'N/A'}"
                f" / {key.get('usage') or 'N/A'}"
            )
            print(f"      Expires:              {key.get('endDateTime') or 'N/A'}")
            print(f"      Custom Key ID:        {key.get('customKeyIdentifier') or 'N/A'}")
            print(f"      SHA-256 Thumbprint:   {key.get('computedThumbprint') or 'N/A'}")
            print(f"      Matches Local Cert:   {match}")
    else:
        print("    (none)")
    print()


def _print_json(snapshot: dict[str, object]) -> None:
    print(json.dumps(snapshot, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show consolidated Agent Identity status and health."
    )
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON.")
    parser.add_argument("--health-only", action="store_true", help="Only print health checks.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return non-zero when health checks fail. Implied by --health-only.",
    )
    args = parser.parse_args(argv)

    try:
        token = get_existing_graph_token()
    except ProvisionerBootstrapError as exc:
        print(f"ERROR: Cannot acquire token: {exc}")
        return 1

    snapshot = _snapshot(token)

    if args.json:
        _print_json(snapshot)
    elif args.health_only:
        _print_health(snapshot)
    else:
        _print_text(snapshot)

    has_failures = snapshot["health"]["failed"] > 0
    return 1 if (args.strict or args.health_only) and has_failures else 0


if __name__ == "__main__":
    sys.exit(main())
