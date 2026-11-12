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
    # These tests exercise the keystore-PEM flow (macOS / Linux). Windows keeps the key
    # in CNG instead; that flow is covered in test_entra_provisioning_windows.py.
    module._uses_cng = lambda: False
    yield module
    sys.modules.pop("entra_provisioning", None)


def test_build_sponsors_bind_uses_v1_user_reference(
    provisioning_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provisioning_module, "get_signed_in_user_id", lambda: "sponsor-id")

    assert provisioning_module.build_sponsors_bind() == [
        "https://graph.microsoft.com/v1.0/users/sponsor-id"
    ]


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
    monkeypatch.setattr(provisioning_module, "_application_status", lambda client_id: "not_found")
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
    monkeypatch.setattr(provisioning_module, "_application_status", lambda client_id: "exists")
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
    monkeypatch.setattr(provisioning_module, "_application_status", lambda client_id: "exists")
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
    monkeypatch.setattr(provisioning_module, "_application_status", lambda client_id: "exists")
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


# ─── stale-app detection: only a confirmed "not found" may discard anything ──


PEM_BUNDLE = (
    "-----BEGIN CERTIFICATE-----\ncert\n-----END CERTIFICATE-----\n"
    "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----\n"
)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ((0, '{"appId": "client-id"}', ""), "exists"),
        (
            (
                3,
                "",
                "ERROR: (Request_ResourceNotFound) Resource 'client-id' does not exist or one "
                "of its queried reference-property objects are not present.",
            ),
            "not_found",
        ),
        ((1, "", "ERROR: No application found matching 'client-id'"), "not_found"),
        ((1, "", "ERROR: Please run 'az login' to setup account."), "unknown"),
        (
            (1, "", "HTTPSConnectionPool(host='graph.microsoft.com'): Max retries exceeded"),
            "unknown",
        ),
        ((1, "", "AADSTS50076: multi-factor authentication is required"), "unknown"),
        ((1, "", ""), "unknown"),
    ],
)
def test_application_status_tells_not_found_from_could_not_check(
    provisioning_module, monkeypatch: pytest.MonkeyPatch, result, expected
) -> None:
    monkeypatch.setattr(provisioning_module, "run_az", lambda args, capture=True: result)
    assert provisioning_module._application_status("client-id") == expected
    assert provisioning_module._application_exists("client-id") is (expected == "exists")


class _Keystore:
    """The OS keystore as the keychain helpers see it."""

    def __init__(self, module, monkeypatch: pytest.MonkeyPatch, pem: str | None) -> None:
        self.pem = pem
        self.events: list[str] = []
        monkeypatch.setattr(module, "_keychain_get_cert", lambda tenant: self.pem)
        monkeypatch.setattr(module, "_keychain_store_cert", self._store)

    def _store(self, tenant: str, pem: str) -> None:
        self.events.append("store")
        self.pem = pem


def _stale_setup(provisioning_module, monkeypatch: pytest.MonkeyPatch, status: str):
    state: dict[str, str] = {
        "TENANT_ID": "tenant-id",
        "PROVISIONER_CLIENT_ID": "old-client-id",
        "PROVISIONER_CERT_THUMBPRINT": "old-thumb",
    }
    monkeypatch.delenv("ENTRABOT_TENANT_ID", raising=False)
    monkeypatch.setattr(provisioning_module, "get_state", lambda key: state.get(key))
    monkeypatch.setattr(provisioning_module, "set_state", state.__setitem__)
    monkeypatch.setattr(provisioning_module, "clear_state", lambda key: state.pop(key, None))
    monkeypatch.setattr(provisioning_module, "_application_status", lambda client_id: status)
    monkeypatch.setattr(provisioning_module, "_ensure_permissions_and_consent", lambda *a: False)
    monkeypatch.setattr(provisioning_module, "_remove_legacy_password_credentials", lambda *a: 0)
    return state


def test_a_failed_check_is_not_a_stale_app_and_discards_nothing(
    provisioning_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _stale_setup(provisioning_module, monkeypatch, "unknown")
    keystore = _Keystore(provisioning_module, monkeypatch, PEM_BUNDLE)
    monkeypatch.setattr(
        provisioning_module,
        "run_az",
        lambda args, capture=True: (_ for _ in ()).throw(AssertionError(f"az called: {args}")),
    )

    with pytest.raises(provisioning_module.ProvisionerBootstrapError, match="[Cc]ould not check"):
        provisioning_module.ensure_app_registration(["Application.ReadWrite.All"])

    assert state["PROVISIONER_CLIENT_ID"] == "old-client-id"
    assert state["PROVISIONER_CERT_THUMBPRINT"] == "old-thumb"
    assert keystore.pem == PEM_BUNDLE and keystore.events == []


def test_confirmed_stale_app_keeps_the_old_key_until_the_replacement_is_uploaded(
    provisioning_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _stale_setup(provisioning_module, monkeypatch, "not_found")
    keystore = _Keystore(provisioning_module, monkeypatch, PEM_BUNDLE)

    def fake_run_az(args, capture=True):
        if args[:4] == ["ad", "app", "list", "--display-name"]:
            return 0, "", ""
        if args[:3] == ["ad", "app", "create"]:
            return 0, '"new-client-id"', ""
        raise AssertionError(f"unexpected az call: {args}")

    monkeypatch.setattr(provisioning_module, "run_az", fake_run_az)
    monkeypatch.setattr(
        provisioning_module,
        "_generate_provisioner_cert",
        lambda: ("NEW-CERT", "NEW-KEY", "new-thumb"),
    )

    def failing_upload(client_id, cert_pem):
        raise provisioning_module.ProvisionerBootstrapError("upload refused")

    monkeypatch.setattr(provisioning_module, "_upload_cert_to_app", failing_upload)
    with pytest.raises(provisioning_module.ProvisionerBootstrapError, match="upload refused"):
        provisioning_module.ensure_app_registration(["Application.ReadWrite.All"])
    assert keystore.pem == PEM_BUNDLE, "nothing is discarded before a replacement exists"

    uploads: list[tuple[str, str]] = []
    monkeypatch.setattr(
        provisioning_module, "_upload_cert_to_app", lambda c, pem: uploads.append((c, pem))
    )
    client_id, credential, _tenant = provisioning_module.ensure_app_registration(
        ["Application.ReadWrite.All"], wait_for_propagation=False
    )

    assert client_id == "new-client-id"
    assert uploads == [("new-client-id", "NEW-CERT")]
    assert credential == "NEW-CERTNEW-KEY" and keystore.pem == "NEW-CERTNEW-KEY"
    assert state["PROVISIONER_CERT_THUMBPRINT"] == "new-thumb"


def test_load_existing_says_could_not_check_when_az_fails(
    provisioning_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"TENANT_ID": "tenant-id", "PROVISIONER_CLIENT_ID": "client-id"}
    monkeypatch.delenv("ENTRABOT_TENANT_ID", raising=False)
    monkeypatch.setattr(provisioning_module, "get_state", lambda key: state.get(key))
    monkeypatch.setattr(provisioning_module, "_application_status", lambda client_id: "unknown")

    with pytest.raises(provisioning_module.ProvisionerBootstrapError) as excinfo:
        provisioning_module.load_existing_app_registration()

    assert "ould not check" in str(excinfo.value)
    assert "was not found" not in str(excinfo.value)
