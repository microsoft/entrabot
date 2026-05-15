#!/usr/bin/env python3
"""Ensure Microsoft Agent 365 Work IQ MCP tenant resources are usable.

The A365 CLI can fail to materialize the first-party resource service
principals it needs, then still exit successfully after printing
``OAuth2 grants failed``. This script uses Entraclaw's provisioner app token to
create the resource service principals and Blueprint-wide OAuth grants before
the CLI runs its own permission step.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from entra_provisioning import (  # noqa: E402
    ProvisionerBootstrapError,
    get_graph_token,
    get_state,
)

GRAPH_V1 = "https://graph.microsoft.com/v1.0"

A365_AGENT_TOOLS_APP_ID = "ea9ffc3e-8a23-4a7d-836d-234d7c7565c1"
A365_WORD_MCP_APP_ID = "c2d0c2b6-8013-4346-9f8b-b81d3b754a29"


class ResponseLike(Protocol):
    status_code: int
    text: str

    def json(self) -> dict[str, Any]: ...


RequestFn = Callable[..., ResponseLike]
SleepFn = Callable[[float], None]


class A365PermissionError(RuntimeError):
    """Raised when A365 Work IQ permissions cannot be made ready."""


@dataclass(frozen=True)
class RequiredResource:
    app_id: str
    display_name: str
    scope: str


REQUIRED_RESOURCES = (
    RequiredResource(
        app_id=A365_AGENT_TOOLS_APP_ID,
        display_name="Agent 365 OneDrive/SharePoint tools",
        scope="McpServers.OneDriveSharepoint.All",
    ),
    RequiredResource(
        app_id=A365_AGENT_TOOLS_APP_ID,
        display_name="Agent 365 Tools metadata",
        scope="McpServersMetadata.Read.All",
    ),
    RequiredResource(
        app_id=A365_WORD_MCP_APP_ID,
        display_name="Work IQ Word MCP",
        scope="Tools.ListInvoke.All",
    ),
)


def _odata_escape(value: str) -> str:
    return value.replace("'", "''")


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _json_or_empty(response: ResponseLike) -> dict[str, Any]:
    try:
        return response.json()
    except ValueError:
        return {}


def _request(
    request: RequestFn,
    method: str,
    url: str,
    token: str,
    *,
    json_body: dict[str, Any] | None = None,
    expected: tuple[int, ...] = (200,),
    action: str,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"headers": _headers(token)}
    if json_body is not None:
        kwargs["json"] = json_body
    response = request(method, url, **kwargs)
    if response.status_code not in expected:
        raise A365PermissionError(
            f"{action} failed ({response.status_code}): {response.text[:400]}"
        )
    return _json_or_empty(response)


def _find_service_principal(
    app_id: str,
    token: str,
    *,
    request: RequestFn,
) -> dict[str, Any] | None:
    data = _request(
        request,
        "GET",
        f"{GRAPH_V1}/servicePrincipals"
        f"?$filter=appId eq '{_odata_escape(app_id)}'"
        "&$select=id,appId,displayName",
        token,
        action=f"query service principal {app_id}",
    )
    values = data.get("value", [])
    if not isinstance(values, list) or not values:
        return None
    first = values[0]
    if not isinstance(first, dict):
        raise A365PermissionError(f"Malformed service principal response for {app_id}")
    return first


def _ensure_resource_service_principal(
    resource: RequiredResource,
    token: str,
    *,
    request: RequestFn,
    sleep: SleepFn,
) -> str:
    existing = _find_service_principal(resource.app_id, token, request=request)
    if existing:
        object_id = str(existing.get("id") or "")
        if not object_id:
            raise A365PermissionError(
                f"Service principal for {resource.display_name} has no object id"
            )
        print(f"  [skip] {resource.display_name} SP already exists: {object_id}")
        return object_id

    print(f"  Creating {resource.display_name} service principal...")
    data = _request(
        request,
        "POST",
        f"{GRAPH_V1}/servicePrincipals",
        token,
        json_body={"appId": resource.app_id},
        expected=(200, 201),
        action=f"create {resource.display_name} service principal",
    )
    object_id = str(data.get("id") or "")
    if object_id:
        print(f"  [new] {resource.display_name} SP created: {object_id}")
        return object_id

    for attempt in range(4):
        wait = 5 * (attempt + 1)
        sleep(wait)
        existing = _find_service_principal(resource.app_id, token, request=request)
        if existing and existing.get("id"):
            object_id = str(existing["id"])
            print(f"  [new] {resource.display_name} SP created: {object_id}")
            return object_id

    raise A365PermissionError(
        f"Created {resource.display_name} service principal but could not resolve object id"
    )


def _resolve_blueprint_sp_object_id(
    blueprint_app_id: str,
    token: str,
    *,
    request: RequestFn,
) -> str:
    existing = _find_service_principal(blueprint_app_id, token, request=request)
    if not existing or not existing.get("id"):
        raise A365PermissionError(
            "Blueprint service principal was not found. Run Entraclaw provisioning before "
            "configuring A365 Work IQ."
        )
    return str(existing["id"])


def _find_oauth_grant(
    blueprint_sp_object_id: str,
    resource_sp_object_id: str,
    token: str,
    *,
    request: RequestFn,
) -> dict[str, Any] | None:
    data = _request(
        request,
        "GET",
        f"{GRAPH_V1}/oauth2PermissionGrants"
        f"?$filter=clientId eq '{_odata_escape(blueprint_sp_object_id)}'"
        f" and resourceId eq '{_odata_escape(resource_sp_object_id)}'"
        " and consentType eq 'AllPrincipals'",
        token,
        action=f"query OAuth grant for resource {resource_sp_object_id}",
    )
    values = data.get("value", [])
    if not isinstance(values, list) or not values:
        return None
    first = values[0]
    if not isinstance(first, dict):
        raise A365PermissionError(
            f"Malformed OAuth grant response for resource {resource_sp_object_id}"
        )
    return first


def _ensure_oauth_grant(
    resource: RequiredResource,
    blueprint_sp_object_id: str,
    resource_sp_object_id: str,
    token: str,
    *,
    request: RequestFn,
    sleep: SleepFn = time.sleep,
) -> None:
    existing = _find_oauth_grant(
        blueprint_sp_object_id,
        resource_sp_object_id,
        token,
        request=request,
    )
    if existing:
        existing_scopes = set(str(existing.get("scope") or "").split())
        if resource.scope in existing_scopes:
            print(f"  [skip] {resource.display_name} grant already has {resource.scope}")
            return
        grant_id = str(existing.get("id") or "")
        if not grant_id:
            raise A365PermissionError(
                f"Existing {resource.display_name} grant has no grant id"
            )
        merged = " ".join(sorted(existing_scopes | {resource.scope}))
        _request(
            request,
            "PATCH",
            f"{GRAPH_V1}/oauth2PermissionGrants/{grant_id}",
            token,
            json_body={"scope": merged},
            expected=(200, 204),
            action=f"patch {resource.display_name} OAuth grant",
        )
        print(f"  [updated] Added {resource.scope} to {resource.display_name} grant")
        return

    response = request(
        "POST",
        f"{GRAPH_V1}/oauth2PermissionGrants",
        headers=_headers(token),
        json={
            "clientId": blueprint_sp_object_id,
            "consentType": "AllPrincipals",
            "resourceId": resource_sp_object_id,
            "scope": resource.scope,
        },
    )
    if response.status_code == 409 and "Permission entry already exists" in response.text:
        for attempt in range(4):
            wait = 5 * (attempt + 1)
            sleep(wait)
            existing = _find_oauth_grant(
                blueprint_sp_object_id,
                resource_sp_object_id,
                token,
                request=request,
            )
            if existing:
                existing_scopes = set(str(existing.get("scope") or "").split())
                if resource.scope in existing_scopes:
                    print(
                        f"  [skip] {resource.display_name} grant already has "
                        f"{resource.scope}"
                    )
                    return
                grant_id = str(existing.get("id") or "")
                if not grant_id:
                    raise A365PermissionError(
                        f"Existing {resource.display_name} grant has no grant id"
                    )
                merged = " ".join(sorted(existing_scopes | {resource.scope}))
                _request(
                    request,
                    "PATCH",
                    f"{GRAPH_V1}/oauth2PermissionGrants/{grant_id}",
                    token,
                    json_body={"scope": merged},
                    expected=(200, 204),
                    action=f"patch {resource.display_name} OAuth grant",
                )
                print(
                    f"  [updated] Added {resource.scope} to "
                    f"{resource.display_name} grant after create conflict"
                )
                return
        raise A365PermissionError(
            f"create {resource.display_name} OAuth grant conflicted, but the "
            "existing grant could not be read back"
        )
    if response.status_code not in (200, 201):
        raise A365PermissionError(
            f"create {resource.display_name} OAuth grant failed "
            f"({response.status_code}): {response.text[:400]}"
        )
    print(f"  [new] {resource.display_name} grant created: {resource.scope}")


def ensure_a365_work_iq_permissions(
    *,
    token: str,
    blueprint_app_id: str,
    request: RequestFn = requests.request,
    sleep: SleepFn = time.sleep,
) -> None:
    """Ensure the A365 Work IQ resource SPs and Blueprint grants exist."""
    if not blueprint_app_id.strip():
        raise A365PermissionError("Blueprint App ID is required")

    print("\n--- Ensuring Microsoft Agent 365 Work IQ permissions ---\n")
    blueprint_sp_object_id = _resolve_blueprint_sp_object_id(
        blueprint_app_id.strip(),
        token,
        request=request,
    )
    print(f"  Blueprint SP: {blueprint_sp_object_id}")

    for resource in REQUIRED_RESOURCES:
        resource_sp_object_id = _ensure_resource_service_principal(
            resource,
            token,
            request=request,
            sleep=sleep,
        )
        _ensure_oauth_grant(
            resource,
            blueprint_sp_object_id,
            resource_sp_object_id,
            token,
            request=request,
            sleep=sleep,
        )

    print("\n  ✅ A365 Work IQ resource service principals and grants are ready")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ensure A365 Work IQ Word service principals and OAuth grants."
    )
    parser.add_argument(
        "--blueprint-app-id",
        default="",
        help="Agent Identity Blueprint application ID. Defaults to .entraclaw-state.json.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    blueprint_app_id = args.blueprint_app_id.strip() or (get_state("BLUEPRINT_APP_ID") or "")
    if not blueprint_app_id:
        print("ERROR: Blueprint App ID not found. Run provisioning first.", file=sys.stderr)
        return 2

    try:
        token = get_graph_token(wait_for_propagation=False)
        ensure_a365_work_iq_permissions(token=token, blueprint_app_id=blueprint_app_id)
    except (A365PermissionError, ProvisionerBootstrapError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
