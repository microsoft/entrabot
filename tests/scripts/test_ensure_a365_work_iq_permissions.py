from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ensure_a365_work_iq_permissions.py"


def load_module():
    spec = importlib.util.spec_from_file_location("ensure_a365_work_iq_permissions", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}
        self.text = str(self._body)

    def json(self) -> dict[str, Any]:
        return self._body


def test_creates_missing_resource_sps_and_blueprint_grants() -> None:
    module = load_module()
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    service_principal_gets: dict[str, int] = {}

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        body = kwargs.get("json")
        calls.append((method, url, body))

        if method == "GET" and "servicePrincipals?$filter=appId eq 'blueprint-app'" in url:
            return FakeResponse(200, {"value": [{"id": "blueprint-sp"}]})
        if method == "GET" and "servicePrincipals?$filter=appId eq 'ea9ffc3e" in url:
            service_principal_gets["agent-tools"] = (
                service_principal_gets.get("agent-tools", 0) + 1
            )
            if service_principal_gets["agent-tools"] == 1:
                return FakeResponse(200, {"value": []})
            return FakeResponse(
                200,
                {"value": [{"id": f"sp-{module.A365_AGENT_TOOLS_APP_ID[:4]}"}]},
            )
        if method == "GET" and "servicePrincipals?$filter=appId eq 'c2d0c2b6" in url:
            return FakeResponse(200, {"value": []})
        if method == "POST" and url.endswith("/servicePrincipals"):
            assert body is not None
            return FakeResponse(201, {"id": f"sp-{body['appId'][:4]}"})
        if method == "GET" and "oauth2PermissionGrants" in url:
            return FakeResponse(200, {"value": []})
        if method == "POST" and url.endswith("/oauth2PermissionGrants"):
            return FakeResponse(201, {"id": "grant-id"})
        raise AssertionError(f"unexpected request: {method} {url} {body}")

    module.ensure_a365_work_iq_permissions(
        token="token",
        blueprint_app_id="blueprint-app",
        request=request,
        sleep=lambda _seconds: None,
    )

    sp_posts = [
        body
        for method, url, body in calls
        if method == "POST" and url.endswith("/servicePrincipals")
    ]
    assert sp_posts == [
        {"appId": module.A365_AGENT_TOOLS_APP_ID},
        {"appId": module.A365_WORD_MCP_APP_ID},
    ]

    grant_posts = [
        body
        for method, url, body in calls
        if method == "POST" and url.endswith("/oauth2PermissionGrants")
    ]
    assert grant_posts == [
        {
            "clientId": "blueprint-sp",
            "consentType": "AllPrincipals",
            "resourceId": f"sp-{module.A365_AGENT_TOOLS_APP_ID[:4]}",
            "scope": "McpServers.OneDriveSharepoint.All",
        },
        {
            "clientId": "blueprint-sp",
            "consentType": "AllPrincipals",
            "resourceId": f"sp-{module.A365_AGENT_TOOLS_APP_ID[:4]}",
            "scope": "McpServersMetadata.Read.All",
        },
        {
            "clientId": "blueprint-sp",
            "consentType": "AllPrincipals",
            "resourceId": f"sp-{module.A365_WORD_MCP_APP_ID[:4]}",
            "scope": "Tools.ListInvoke.All",
        },
    ]


def test_patches_existing_grant_when_scope_is_missing() -> None:
    module = load_module()
    patch_bodies: list[dict[str, str]] = []

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        body = kwargs.get("json")
        if method == "GET" and "servicePrincipals?$filter=appId eq 'blueprint-app'" in url:
            return FakeResponse(200, {"value": [{"id": "blueprint-sp"}]})
        if method == "GET" and "servicePrincipals?$filter=appId eq 'ea9ffc3e" in url:
            return FakeResponse(200, {"value": [{"id": "agent-tools-sp"}]})
        if method == "GET" and "servicePrincipals?$filter=appId eq 'c2d0c2b6" in url:
            return FakeResponse(200, {"value": [{"id": "word-sp"}]})
        if method == "GET" and "resourceId eq 'agent-tools-sp'" in url:
            return FakeResponse(200, {"value": [{"id": "metadata-grant", "scope": "Old.Scope"}]})
        if method == "GET" and "resourceId eq 'word-sp'" in url:
            return FakeResponse(
                200,
                {"value": [{"id": "word-grant", "scope": "Tools.ListInvoke.All"}]},
            )
        if method == "PATCH" and url.endswith("/oauth2PermissionGrants/metadata-grant"):
            assert body is not None
            patch_bodies.append(body)
            return FakeResponse(204)
        raise AssertionError(f"unexpected request: {method} {url} {body}")

    module.ensure_a365_work_iq_permissions(
        token="token",
        blueprint_app_id="blueprint-app",
        request=request,
        sleep=lambda _seconds: None,
    )

    assert patch_bodies == [
        {"scope": "McpServers.OneDriveSharepoint.All Old.Scope"},
        {"scope": "McpServersMetadata.Read.All Old.Scope"},
    ]


def test_post_conflict_requeries_and_patches_existing_grant() -> None:
    module = load_module()
    grant_queries = 0
    patch_bodies: list[dict[str, str]] = []
    sleeps: list[float] = []
    resource = module.RequiredResource(
        app_id=module.A365_AGENT_TOOLS_APP_ID,
        display_name="Agent 365 Tools metadata",
        scope="McpServersMetadata.Read.All",
    )

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        nonlocal grant_queries
        body = kwargs.get("json")
        if method == "GET" and "oauth2PermissionGrants" in url:
            grant_queries += 1
            if grant_queries == 1:
                return FakeResponse(200, {"value": []})
            return FakeResponse(
                200,
                {
                    "value": [
                        {
                            "id": "agent-tools-grant",
                            "scope": "McpServers.OneDriveSharepoint.All",
                        }
                    ]
                },
            )
        if method == "POST" and url.endswith("/oauth2PermissionGrants"):
            return FakeResponse(
                409,
                {
                    "error": {
                        "code": "Request_MultipleObjectsWithSameKeyValue",
                        "message": "Permission entry already exists.",
                    }
                },
            )
        if method == "PATCH" and url.endswith("/oauth2PermissionGrants/agent-tools-grant"):
            assert body is not None
            patch_bodies.append(body)
            return FakeResponse(204)
        raise AssertionError(f"unexpected request: {method} {url} {body}")

    module._ensure_oauth_grant(
        resource,
        blueprint_sp_object_id="blueprint-sp",
        resource_sp_object_id="agent-tools-sp",
        token="token",
        request=request,
        sleep=sleeps.append,
    )

    assert sleeps == [5]
    assert patch_bodies == [
        {"scope": "McpServers.OneDriveSharepoint.All McpServersMetadata.Read.All"}
    ]


def test_raises_when_blueprint_service_principal_is_missing() -> None:
    module = load_module()

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        if method == "GET" and "servicePrincipals?$filter=appId eq 'missing-blueprint'" in url:
            return FakeResponse(200, {"value": []})
        raise AssertionError(f"unexpected request: {method} {url}")

    with pytest.raises(module.A365PermissionError, match="Blueprint service principal"):
        module.ensure_a365_work_iq_permissions(
            token="token",
            blueprint_app_id="missing-blueprint",
            request=request,
            sleep=lambda _seconds: None,
        )
