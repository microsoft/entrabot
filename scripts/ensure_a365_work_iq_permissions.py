#!/usr/bin/env python3
"""Ensure Microsoft Agent 365 Work IQ MCP tenant resources are usable.

The A365 CLI can fail to materialize the first-party resource service
principals it needs, then still exit successfully after printing
``OAuth2 grants failed``. This script uses Entrabot's provisioner app token to
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
    get_existing_graph_token,
    get_state,
)

# The repo root is one directory up; src/ contains the entrabot package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from entrabot.graph_helpers import odata_escape as _odata_escape  # noqa: E402

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
        display_name="Agent 365 Excel tools",
        scope="McpServers.Excel.All",
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


RESOURCE_NAMES = {
    A365_AGENT_TOOLS_APP_ID: "Agent 365 tools",
    A365_WORD_MCP_APP_ID: "Work IQ Word MCP",
}
# How long to wait between tries when Entra cannot yet read something written moments ago.
RETRY_WAITS: tuple[float, ...] = (5, 10, 15, 20)


def _scopes_by_resource() -> list[tuple[str, str, tuple[str, ...]]]:
    """``(app ID, name, scopes)`` for each resource app, in the order they are listed.

    Entra keeps one grant per client and resource, so every scope of a resource belongs in
    the same write. Writing them one at a time meant patching a grant created moments
    earlier, which Entra could not always read yet (404 on a new agent).
    """
    grouped: dict[str, list[str]] = {}
    for resource in REQUIRED_RESOURCES:
        scopes = grouped.setdefault(resource.app_id, [])
        if resource.scope not in scopes:
            scopes.append(resource.scope)
    return [
        (app_id, RESOURCE_NAMES.get(app_id, app_id), tuple(scopes))
        for app_id, scopes in grouped.items()
    ]


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
    app_id: str,
    display_name: str,
    token: str,
    *,
    request: RequestFn,
    sleep: SleepFn,
) -> str:
    existing = _find_service_principal(app_id, token, request=request)
    if existing:
        object_id = str(existing.get("id") or "")
        if not object_id:
            raise A365PermissionError(f"Service principal for {display_name} has no object id")
        print(f"  [skip] {display_name} SP already exists: {object_id}")
        return object_id

    print(f"  Creating {display_name} service principal...")
    data = _request(
        request,
        "POST",
        f"{GRAPH_V1}/servicePrincipals",
        token,
        json_body={"appId": app_id},
        expected=(200, 201),
        action=f"create {display_name} service principal",
    )
    object_id = str(data.get("id") or "")
    if object_id:
        print(f"  [new] {display_name} SP created: {object_id}")
        return object_id

    for wait in RETRY_WAITS:
        sleep(wait)
        existing = _find_service_principal(app_id, token, request=request)
        if existing and existing.get("id"):
            object_id = str(existing["id"])
            print(f"  [new] {display_name} SP created: {object_id}")
            return object_id

    raise A365PermissionError(
        f"Created {display_name} service principal but could not resolve object id"
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
            "Blueprint service principal was not found. Run Entrabot provisioning before "
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


def _add_scopes_to_grant(
    display_name: str,
    scopes: tuple[str, ...],
    existing: dict[str, Any],
    token: str,
    *,
    request: RequestFn,
    sleep: SleepFn,
) -> None:
    """Make an existing grant carry ``scopes`` too, keeping whatever it already has."""
    existing_scopes = set(str(existing.get("scope") or "").split())
    missing = [scope for scope in scopes if scope not in existing_scopes]
    if not missing:
        print(f"  [skip] {display_name} grant already has {' '.join(scopes)}")
        return
    grant_id = str(existing.get("id") or "")
    if not grant_id:
        raise A365PermissionError(f"Existing {display_name} grant has no grant id")
    merged = " ".join(sorted(existing_scopes | set(scopes)))
    # A grant that was created moments ago can be listed before it can be updated: Entra
    # answers 404 until it has caught up. Wait and try again rather than give up.
    waits = iter(RETRY_WAITS)
    while True:
        response = request(
            "PATCH",
            f"{GRAPH_V1}/oauth2PermissionGrants/{grant_id}",
            headers=_headers(token),
            json={"scope": merged},
        )
        if response.status_code in (200, 204):
            break
        wait = next(waits, None) if response.status_code == 404 else None
        if wait is None:
            raise A365PermissionError(
                f"patch {display_name} OAuth grant failed "
                f"({response.status_code}): {response.text[:400]}"
            )
        sleep(wait)
    print(f"  [updated] Added {' '.join(missing)} to {display_name} grant")


def _ensure_oauth_grant(
    display_name: str,
    scopes: tuple[str, ...],
    blueprint_sp_object_id: str,
    resource_sp_object_id: str,
    token: str,
    *,
    request: RequestFn,
    sleep: SleepFn = time.sleep,
) -> None:
    """Give the Blueprint ``scopes`` on one resource, in one write."""
    existing = _find_oauth_grant(
        blueprint_sp_object_id,
        resource_sp_object_id,
        token,
        request=request,
    )
    if existing:
        _add_scopes_to_grant(display_name, scopes, existing, token, request=request, sleep=sleep)
        return

    response = request(
        "POST",
        f"{GRAPH_V1}/oauth2PermissionGrants",
        headers=_headers(token),
        json={
            "clientId": blueprint_sp_object_id,
            "consentType": "AllPrincipals",
            "resourceId": resource_sp_object_id,
            "scope": " ".join(sorted(scopes)),
        },
    )
    if response.status_code == 409 and "Permission entry already exists" in response.text:
        for wait in RETRY_WAITS:
            sleep(wait)
            existing = _find_oauth_grant(
                blueprint_sp_object_id,
                resource_sp_object_id,
                token,
                request=request,
            )
            if existing:
                _add_scopes_to_grant(
                    display_name, scopes, existing, token, request=request, sleep=sleep
                )
                return
        raise A365PermissionError(
            f"create {display_name} OAuth grant conflicted, but the "
            "existing grant could not be read back"
        )
    if response.status_code not in (200, 201):
        raise A365PermissionError(
            f"create {display_name} OAuth grant failed "
            f"({response.status_code}): {response.text[:400]}"
        )
    print(f"  [new] {display_name} grant created: {' '.join(sorted(scopes))}")


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

    for app_id, display_name, scopes in _scopes_by_resource():
        resource_sp_object_id = _ensure_resource_service_principal(
            app_id,
            display_name,
            token,
            request=request,
            sleep=sleep,
        )
        _ensure_oauth_grant(
            display_name,
            scopes,
            blueprint_sp_object_id,
            resource_sp_object_id,
            token,
            request=request,
            sleep=sleep,
        )

    print("\n  ✅ A365 Work IQ resource service principals and grants are ready")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ensure A365 Work IQ service principals and OAuth grants."
    )
    parser.add_argument(
        "--blueprint-app-id",
        default="",
        help="Agent Identity Blueprint application ID. Defaults to .entrabot-state.json.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    blueprint_app_id = args.blueprint_app_id.strip() or (get_state("BLUEPRINT_APP_ID") or "")
    if not blueprint_app_id:
        print("ERROR: Blueprint App ID not found. Run provisioning first.", file=sys.stderr)
        return 2

    try:
        token = get_existing_graph_token()
        ensure_a365_work_iq_permissions(token=token, blueprint_app_id=blueprint_app_id)
    except (A365PermissionError, ProvisionerBootstrapError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
