"""Tests for provisioner app bootstrap edge cases."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "entra_provisioning.py"


@pytest.fixture
def provisioning_module():
    spec = importlib.util.spec_from_file_location("entra_provisioning", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["entra_provisioning"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("entra_provisioning", None)


def test_existing_local_cert_is_uploaded_when_provisioner_app_is_recreated(
    provisioning_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    state: dict[str, str] = {"TENANT_ID": "tenant-id"}
    pem_bundle = (
        "-----BEGIN CERTIFICATE-----\ncert\n-----END CERTIFICATE-----\n"
        "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----\n"
    )
    uploads: list[tuple[str, str]] = []

    monkeypatch.delenv("ENTRABOT_TENANT_ID", raising=False)

    def fake_run_az(args, capture=True):
        del capture
        if args[:4] == ["ad", "app", "list", "--display-name"]:
            return 0, "", ""
        if args[:3] == ["ad", "app", "create"]:
            return 0, "new-client-id", ""
        raise AssertionError(f"unexpected az call: {args}")

    monkeypatch.setattr(provisioning_module, "run_az", fake_run_az)
    monkeypatch.setattr(provisioning_module, "get_state", lambda key: state.get(key))
    monkeypatch.setattr(provisioning_module, "set_state", state.__setitem__)
    monkeypatch.setattr(provisioning_module, "clear_state", lambda key: state.pop(key, None))
    monkeypatch.setattr(provisioning_module, "_keychain_get_cert", lambda tenant: pem_bundle)
    monkeypatch.setattr(provisioning_module, "_keychain_delete_cert", lambda tenant: None)
    monkeypatch.setattr(provisioning_module, "_application_exists", lambda client_id: False)
    monkeypatch.setattr(provisioning_module, "_ensure_permissions_and_consent", lambda *a: None)
    monkeypatch.setattr(provisioning_module, "_remove_legacy_password_credentials", lambda *a: 0)
    monkeypatch.setattr(provisioning_module, "_thumbprint_from_cert_pem", lambda cert: "thumb")
    monkeypatch.setattr(
        provisioning_module,
        "_upload_cert_to_app",
        lambda client_id, cert_pem: uploads.append((client_id, cert_pem)),
    )

    client_id, returned_pem, tenant_id = provisioning_module.ensure_app_registration(
        ["Application.ReadWrite.All"], wait_for_propagation=False
    )

    assert client_id == "new-client-id"
    assert returned_pem == pem_bundle
    assert tenant_id == "tenant-id"
    assert uploads == [
        (
            "new-client-id",
            "-----BEGIN CERTIFICATE-----\ncert\n-----END CERTIFICATE-----\n",
        )
    ]
    assert state["PROVISIONER_CERT_THUMBPRINT"] == "thumb"


def test_wait_for_propagation_skips_sleep_when_permissions_unchanged(
    provisioning_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    state: dict[str, str] = {
        "TENANT_ID": "tenant-id",
        "PROVISIONER_CLIENT_ID": "client-id",
        "PROVISIONER_CERT_THUMBPRINT": "thumb",
    }
    pem_bundle = (
        "-----BEGIN CERTIFICATE-----\ncert\n-----END CERTIFICATE-----\n"
        "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----\n"
    )
    sleeps: list[int] = []

    monkeypatch.delenv("ENTRABOT_TENANT_ID", raising=False)
    monkeypatch.setattr(provisioning_module, "get_state", lambda key: state.get(key))
    monkeypatch.setattr(provisioning_module, "set_state", state.__setitem__)
    monkeypatch.setattr(provisioning_module, "clear_state", lambda key: state.pop(key, None))
    monkeypatch.setattr(provisioning_module, "_keychain_get_cert", lambda tenant: pem_bundle)
    monkeypatch.setattr(provisioning_module, "_application_exists", lambda client_id: True)
    monkeypatch.setattr(provisioning_module, "_ensure_permissions_and_consent", lambda *a: False)
    monkeypatch.setattr(provisioning_module, "_remove_legacy_password_credentials", lambda *a: 0)
    monkeypatch.setattr(provisioning_module.time, "sleep", sleeps.append)

    provisioning_module.ensure_app_registration(
        ["Application.ReadWrite.All"],
        wait_for_propagation=True,
    )

    assert sleeps == []


def test_wait_for_propagation_sleeps_when_permissions_changed(
    provisioning_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    state: dict[str, str] = {
        "TENANT_ID": "tenant-id",
        "PROVISIONER_CLIENT_ID": "client-id",
        "PROVISIONER_CERT_THUMBPRINT": "thumb",
    }
    pem_bundle = (
        "-----BEGIN CERTIFICATE-----\ncert\n-----END CERTIFICATE-----\n"
        "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----\n"
    )
    sleeps: list[int] = []

    monkeypatch.delenv("ENTRABOT_TENANT_ID", raising=False)
    monkeypatch.setattr(provisioning_module, "get_state", lambda key: state.get(key))
    monkeypatch.setattr(provisioning_module, "set_state", state.__setitem__)
    monkeypatch.setattr(provisioning_module, "clear_state", lambda key: state.pop(key, None))
    monkeypatch.setattr(provisioning_module, "_keychain_get_cert", lambda tenant: pem_bundle)
    monkeypatch.setattr(provisioning_module, "_application_exists", lambda client_id: True)
    monkeypatch.setattr(provisioning_module, "_ensure_permissions_and_consent", lambda *a: True)
    monkeypatch.setattr(provisioning_module, "_remove_legacy_password_credentials", lambda *a: 0)
    monkeypatch.setattr(provisioning_module.time, "sleep", sleeps.append)

    provisioning_module.ensure_app_registration(
        ["Application.ReadWrite.All"],
        wait_for_propagation=True,
    )

    assert sleeps == [30]


def test_permissions_and_consent_skips_admin_consent_when_permissions_present(
    provisioning_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(provisioning_module, "_ensure_service_principal", lambda client_id: None)
    monkeypatch.setattr(
        provisioning_module,
        "_resolve_permission_specs",
        lambda required_values: [("Application.ReadWrite.All", "role-id=Role")],
    )
    monkeypatch.setattr(
        provisioning_module,
        "_get_existing_permission_role_ids",
        lambda client_id: {"role-id"},
    )
    # Declared AND effectively consented -> no drift -> skip admin consent.
    monkeypatch.setattr(
        provisioning_module,
        "_get_consented_graph_role_ids",
        lambda client_id: {"role-id"},
    )

    def fake_run_az(args, capture=True):
        del capture
        calls.append(args)
        return 0, "", ""

    monkeypatch.setattr(provisioning_module, "run_az", fake_run_az)

    changed = provisioning_module._ensure_permissions_and_consent(
        "client-id",
        ["Application.ReadWrite.All"],
    )

    assert changed is False
    assert calls == []


def test_permissions_and_consent_reconsents_on_consent_drift(
    provisioning_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permission declared in requiredResourceAccess but NOT effectively
    admin-consented on the SP (consent drift) must trigger a fresh admin
    consent — the old check only compared declared permissions and would
    silently skip, leaving the permission unconsented forever.
    """
    calls: list[list[str]] = []

    monkeypatch.setattr(provisioning_module, "_ensure_service_principal", lambda client_id: None)
    monkeypatch.setattr(
        provisioning_module,
        "_resolve_permission_specs",
        lambda required_values: [
            ("Application.ReadWrite.All", "role-rw=Role"),
            ("AppRoleAssignment.ReadWrite.All", "role-apra=Role"),
        ],
    )
    # Both permissions are DECLARED on the app...
    monkeypatch.setattr(
        provisioning_module,
        "_get_existing_permission_role_ids",
        lambda client_id: {"role-rw", "role-apra"},
    )
    # ...but only one is effectively CONSENTED on the SP -> drift on role-apra.
    monkeypatch.setattr(
        provisioning_module,
        "_get_consented_graph_role_ids",
        lambda client_id: {"role-rw"},
    )

    def fake_run_az(args, capture=True):
        del capture
        calls.append(args)
        return 0, "", ""

    monkeypatch.setattr(provisioning_module, "run_az", fake_run_az)

    changed = provisioning_module._ensure_permissions_and_consent(
        "client-id",
        ["Application.ReadWrite.All", "AppRoleAssignment.ReadWrite.All"],
    )

    assert changed is True, "consent drift must warrant a propagation wait"
    # admin-consent MUST be run; no `permission add` is needed (already declared).
    consent_calls = [c for c in calls if c[:4] == ["ad", "app", "permission", "admin-consent"]]
    add_calls = [c for c in calls if c[:4] == ["ad", "app", "permission", "add"]]
    assert consent_calls, "admin-consent must run when a declared permission is unconsented"
    assert not add_calls, "no permission add needed when everything is already declared"


def test_get_consented_graph_role_ids_parses_app_role_assignments(
    provisioning_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_get_consented_graph_role_ids reads the SP's appRoleAssignments
    (effective consent), not requiredResourceAccess (declared)."""
    captured: dict[str, list[str]] = {}

    def fake_run_az(args, capture=True):
        del capture
        captured["args"] = args
        return 0, '["role-rw", "role-apra"]', ""

    monkeypatch.setattr(provisioning_module, "run_az", fake_run_az)

    role_ids = provisioning_module._get_consented_graph_role_ids("client-id")

    assert role_ids == {"role-rw", "role-apra"}
    # Must query appRoleAssignments (effective consent), filtered to Graph.
    joined = " ".join(captured["args"])
    assert "appRoleAssignments" in joined
    assert "client-id" in joined


def test_get_consented_graph_role_ids_returns_empty_on_read_failure(
    provisioning_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If effective consent can't be read, return an empty set so the caller
    re-runs admin-consent (idempotent) rather than silently assuming consent."""
    monkeypatch.setattr(provisioning_module, "run_az", lambda args, capture=True: (1, "", "boom"))
    assert provisioning_module._get_consented_graph_role_ids("client-id") == set()


def test_required_permissions_include_app_role_assignment_write(
    provisioning_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        provisioning_module,
        "resolve_graph_permissions",
        lambda: {
            "Application.ReadWrite.All": "app-rw-role-id",
            "AppRoleAssignment.ReadWrite.All": "app-role-assignment-role-id",
            "AgentIdentity.Create": "agent-identity-create-role-id",
        },
    )

    values = provisioning_module.build_required_permission_values()

    assert "AppRoleAssignment.ReadWrite.All" in values
    assert values.index("AppRoleAssignment.ReadWrite.All") < values.index("AgentIdentity.Create")


def test_load_existing_app_registration_requires_bootstrap_state(
    provisioning_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENTRABOT_TENANT_ID", raising=False)
    monkeypatch.setattr(provisioning_module, "get_state", lambda key: None)

    with pytest.raises(provisioning_module.ProvisionerBootstrapError) as exc:
        provisioning_module.load_existing_app_registration()

    assert "scripts/entra_provisioning.py" in str(exc.value)


def test_load_existing_app_registration_does_not_repair_permissions(
    provisioning_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    state: dict[str, str] = {
        "TENANT_ID": "tenant-id",
        "PROVISIONER_CLIENT_ID": "client-id",
    }
    pem_bundle = (
        "-----BEGIN CERTIFICATE-----\ncert\n-----END CERTIFICATE-----\n"
        "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----\n"
    )

    monkeypatch.delenv("ENTRABOT_TENANT_ID", raising=False)
    monkeypatch.setattr(provisioning_module, "get_state", lambda key: state.get(key))
    monkeypatch.setattr(provisioning_module, "_application_exists", lambda client_id: True)
    monkeypatch.setattr(provisioning_module, "_keychain_get_cert", lambda tenant: pem_bundle)
    monkeypatch.setattr(
        provisioning_module,
        "_ensure_permissions_and_consent",
        lambda *a: (_ for _ in ()).throw(AssertionError("should not repair permissions")),
    )

    client_id, returned_pem, tenant_id = provisioning_module.load_existing_app_registration()

    assert client_id == "client-id"
    assert returned_pem == pem_bundle
    assert tenant_id == "tenant-id"
