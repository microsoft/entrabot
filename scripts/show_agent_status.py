#!/usr/bin/env python3
"""Show consolidated Agent Identity status and health.

Reads local state/configuration and queries Microsoft Graph for live data about
the Blueprint, Agent Identity, Agent User, Sponsors, Permissions, Certificates,
Licenses, and Storage configuration.

Usage::

    python scripts/show_agent_status.py
    python scripts/show_agent_status.py --json
    python scripts/show_agent_status.py --health-only
    python scripts/show_agent_status.py --full-thumbprints

A certificate thumbprint is shown by its last four characters (``…B3C4``), the form the rest
of the tooling uses, in text and in JSON. ``--full-thumbprints`` shows it in full.
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
from entrabot.winsetup import oldkeys  # noqa: E402


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

# ANSI colour of each health label, used only where the terminal can draw it.
_STATUS_COLOR = {"pass": "32", "fail": "31", "warn": "33", "skip": "90"}

# The certificate thumbprints in _STATE_KEYS. Like every other thumbprint here, they are shown by
# their last four characters unless --full-thumbprints is given.
_THUMBPRINT_STATE_KEYS = frozenset({"PROVISIONER_CERT_THUMBPRINT", "BLUEPRINT_CERT_THUMBPRINT"})


def _shown_thumbprint(thumbprint: str | None, *, full: bool) -> str | None:
    """A certificate thumbprint as the report shows it.

    Its last four characters (``…B3C4``), the form the rest of the tooling uses: enough to
    tell two certificates apart. In full only when the operator asks for it
    (``--full-thumbprints``), to compare with the Microsoft Entra admin center. Nothing
    recorded stays as it is (``None``, shown as N/A).
    """
    if not thumbprint or full:
        return thumbprint
    return "…" + thumbprint[-4:].upper()


def _is_windows() -> bool:
    return sys.platform == "win32"


def _enable_windows_vt() -> bool:
    """Turn on ANSI colour in a classic Windows console. True when it worked.

    The setup wizard's own check, so both draw colour in exactly the same places.
    """
    from entrabot.winsetup.console import _enable_windows_vt as enable

    return enable()


def _use_color(stream: object = None) -> bool:
    """Should the health labels be coloured on *stream* (default: stdout)?

    Only on a terminal that can draw it. Written to a pipe, a file, a captured SSH
    session or a classic Windows console without VT processing, the escapes arrive as
    literal ``[32mPASS[0m``. ``NO_COLOR`` (https://no-color.org) always wins.
    """
    stream = sys.stdout if stream is None else stream
    if os.environ.get("NO_COLOR"):
        return False
    try:
        if not stream.isatty():  # type: ignore[attr-defined]
            return False
    except (AttributeError, ValueError):
        return False
    if not _is_windows():
        return True
    # Windows Terminal always draws colour; a classic console only once VT is switched on.
    return bool(os.environ.get("WT_SESSION")) or _enable_windows_vt()


def _status_label(status: str, *, color: bool) -> str:
    code = _STATUS_COLOR.get(status)
    if code is None:
        return status
    label = status.upper()
    return f"\033[{code}m{label}\033[0m" if color else label


def _configured(attribute: str, env_name: str) -> str | None:
    """A value from the runtime configuration (.env), which is where Windows setup
    records the Blueprint certificate; the state file never carries it there."""
    try:
        value = getattr(get_config(), attribute, None)
    except Exception:
        value = None
    return value or os.environ.get(env_name) or None


def _read_all_state() -> dict[str, str | None]:
    state = {key: get_state(key) for key in _STATE_KEYS}
    if not state.get("BLUEPRINT_CERT_THUMBPRINT"):
        state["BLUEPRINT_CERT_THUMBPRINT"] = _configured(
            "blueprint_cert_thumbprint", "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT"
        )
    return state


def _local_certificate_checks(state: dict[str, str | None]) -> list[Check]:
    """Windows: is the certificate named in .env in this user's store, keyed, in date?

    A registered certificate is no use if its key isn't on this machine or it has
    expired, and neither shows up in what Graph returns.

    Skipped, like the Blueprint's own certificate check, when no Blueprint is set up
    here at all. With a Blueprint but no certificate configured it fails: the agent
    exists and this machine can't sign for it.
    """
    if not _is_windows():
        return []
    from entrabot.platform import wincert

    sha1 = _configured("blueprint_cert_sha1", "ENTRABOT_BLUEPRINT_CERT_SHA1")
    has_blueprint = bool(state.get("BLUEPRINT_OBJECT_ID") or state.get("BLUEPRINT_APP_ID"))
    if not sha1 and not has_blueprint:
        return [Check("Local certificate", "skip", "No Blueprint is configured on this machine")]
    try:
        health = wincert.cert_health(sha1)
    except wincert.WinCertError as exc:
        return [Check("Local certificate", "warn", f"Could not check: {exc}")]
    status = {"ok": "pass", "expiring": "warn"}.get(health.status, "fail")
    return [Check("Local certificate", status, health.message)]


# What the older-setup-key check reads, the way setup and its health check read it
# (entrabot.winsetup.oldkeys). Not in _STATE_KEYS, which make up the report's state object: of an
# older key's thumbprint only the last four characters are ever shown, --full-thumbprints or not.
_OLD_SETUP_KEY_STATE = (
    oldkeys.REVOKE_PENDING,
    oldkeys.REVOKE_PENDING_EXPIRES,
    "PROVISIONER_CLIENT_ID",
    "PROVISIONER_CERT_THUMBPRINT",
)


def _old_setup_key_checks() -> list[Check]:
    """Older setup keys that setup could not switch off in Microsoft Entra.

    An older install kept the Provisioner's private key in a plain file. Setup records the
    certificate it could not take off the Provisioner app, which can manage applications in
    the tenant, and retries on every run. Until then this warns: never a failure, because the
    agent signs in either way. Skipped, like the other checks, when the state names no
    Provisioner app and records nothing. Not tied to the platform: the keys are written on
    Windows, but the state file is read wherever this runs.
    """
    name = oldkeys.CHECK_NAME
    recorded = {key: get_state(key) for key in _OLD_SETUP_KEY_STATE}
    finding = oldkeys.old_setup_key_check(oldkeys.old_setup_keys(recorded))
    if finding is not None:
        return [Check(name, "warn", f"{finding.detail} {finding.fix}")]
    if not recorded["PROVISIONER_CLIENT_ID"]:
        return [Check(name, "skip", "No setup (Provisioner) app is recorded on this machine")]
    return [Check(name, "pass", "No older setup key is waiting to be switched off")]


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
    *,
    full_thumbprints: bool = False,
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
                shown = _shown_thumbprint(local_thumb, full=full_thumbprints)
                checks.append(
                    Check(
                        "Certificate",
                        "warn",
                        f"Local thumbprint {shown} not found in Entra "
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
    key_credentials: list[dict] | None, local_thumb: str | None, *, full_thumbprints: bool = False
) -> list[dict[str, object]]:
    """One row per Blueprint certificate. Its thumbprints (the computed SHA-256 one, and
    ``customKeyIdentifier``, which Graph sets to the SHA-1 one) are shown by their last four
    characters unless *full_thumbprints*; the match is made on the full values."""
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
                "customKeyIdentifier": _shown_thumbprint(
                    credential.get("customKeyIdentifier"), full=full_thumbprints
                ),
                "computedThumbprint": _shown_thumbprint(thumbprint, full=full_thumbprints) or None,
                "matchesLocalBlueprintCert": bool(local_thumb and thumbprint == local_thumb),
            }
        )
    return rows


def _snapshot(token: str, *, full_thumbprints: bool = False) -> dict[str, object]:
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
        full_thumbprints=full_thumbprints,
    )
    checks.extend(_local_certificate_checks(state))
    checks.extend(_old_setup_key_checks())
    failed = sum(1 for check in checks if check.status == "fail")
    warned = sum(1 for check in checks if check.status == "warn")
    passed = sum(1 for check in checks if check.status == "pass")

    # What is shown. The checks above were made on the full values.
    shown_state = {
        key: _shown_thumbprint(value, full=full_thumbprints)
        if key in _THUMBPRINT_STATE_KEYS
        else value
        for key, value in state.items()
    }
    return {
        "tenant": {"id": state.get("TENANT_ID")},
        "provisioner": {
            "client_id": state.get("PROVISIONER_CLIENT_ID"),
            "cert_thumbprint": shown_state.get("PROVISIONER_CERT_THUMBPRINT"),
        },
        "blueprint": {
            "app_object_id": state.get("BLUEPRINT_APP_ID"),
            "object_id": state.get("BLUEPRINT_OBJECT_ID"),
            "cert_thumbprint": shown_state.get("BLUEPRINT_CERT_THUMBPRINT"),
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
        "state": shown_state,
        "platform": _platform_info(),
        "storage": _storage_info(),
        "sponsors": sponsors,
        "sponsors_error": sponsors_error,
        "licenses": _license_rows(licenses, sku_names),
        "licenses_error": licenses_error,
        "permissions": _permission_rows(permissions, resource_names),
        "permissions_error": permissions_error,
        "key_credentials": _key_rows(
            key_credentials,
            state.get("BLUEPRINT_CERT_THUMBPRINT"),
            full_thumbprints=full_thumbprints,
        ),
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
    color = _use_color()
    for check in health["checks"]:
        icon = _status_label(check["status"], color=color)
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
    parser.add_argument(
        "--full-thumbprints",
        action="store_true",
        help="Show certificate thumbprints in full, to compare them with the Microsoft Entra "
        "admin center. By default only their last four characters are shown.",
    )
    args = parser.parse_args(argv)

    try:
        token = get_existing_graph_token()
    except ProvisionerBootstrapError as exc:
        print(f"ERROR: Cannot acquire token: {exc}")
        return 1

    snapshot = _snapshot(token, full_thumbprints=args.full_thumbprints)

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
