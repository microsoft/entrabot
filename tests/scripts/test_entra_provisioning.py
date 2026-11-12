"""Tests for provisioner app bootstrap edge cases."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "entra_provisioning.py"


@pytest.fixture
def provisioning_module(monkeypatch: pytest.MonkeyPatch):
    # The operator's statement that they checked an app, if their shell has one; and the
    # app ID setup writes to .env, which importing entrabot.config (as other tests do)
    # loads into the environment. A test that needs either sets it.
    monkeypatch.delenv("ENTRABOT_PROVISIONER_APP_ID_VERIFIED", raising=False)
    monkeypatch.delenv("ENTRABOT_PROVISIONER_APP_ID", raising=False)
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
            return 0, "[]", ""
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
            return 0, "[]", ""
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


# ─── which app registration becomes the Provisioner ──────────────────────────
#
# The Provisioner app gets tenant-wide Graph application permissions with admin consent.
# Anyone allowed to register applications can give an app its display name, so an app
# found by name is only taken over when this run would grant nobody anything: nobody else
# can use it, or an administrator has already approved it for everything setup needs.

NAME = "EntraBot Agent ID Provisioner"
VERIFIED = "ENTRABOT_PROVISIONER_APP_ID_VERIFIED"
# Printed on a line of its own when an existing app is not taken over; the Windows setup
# wizard looks for it (tests/winsetup/test_wizard.py pins the same text).
TAKEOVER_REFUSED = "ENTRABOT_RESULT=provisioner-takeover-refused"
GRAPH_APP = "00000003-0000-0000-c000-000000000000"
GRAPH_SP = "99999999-0000-0000-0000-000000000003"  # Graph's service principal in this tenant
LOOKALIKE_SP = "99999999-0000-0000-0000-0000000000ff"  # someone's app, named "Microsoft Graph"
GRAPH_ROLE = "eeeeeeee-0000-0000-0000-000000000001"
GRAPH_ROLE_2 = "eeeeeeee-0000-0000-0000-000000000002"
EXTRA_ROLE = "eeeeeeee-0000-0000-0000-0000000000ee"  # a Graph permission setup doesn't use
REQUIRED = ["Application.ReadWrite.All", "AppRoleAssignment.ReadWrite.All"]
ME = "aaaaaaaa-0000-0000-0000-000000000001"
MALLORY = "aaaaaaaa-0000-0000-0000-000000000002"
FIRST_ADMIN = "aaaaaaaa-0000-0000-0000-000000000003"
APP = "bbbbbbbb-0000-0000-0000-000000000001"
APP_OBJECT = "cccccccc-0000-0000-0000-000000000001"
OTHER_APP = "bbbbbbbb-0000-0000-0000-000000000002"
OTHER_APP_OBJECT = "cccccccc-0000-0000-0000-000000000002"
SP_OBJECT = "dddddddd-0000-0000-0000-000000000001"
OTHER_SP_OBJECT = "dddddddd-0000-0000-0000-000000000002"
NEW_APP = "bbbbbbbb-0000-0000-0000-00000000000f"
ADMIN = {"@odata.type": "#microsoft.graph.user", "id": ME, "userPrincipalName": "admin@contoso.com"}
MALLORY_USER = {
    "@odata.type": "#microsoft.graph.user",
    "id": MALLORY,
    "userPrincipalName": "mallory@contoso.com",
}
FIRST_ADMIN_USER = {
    "@odata.type": "#microsoft.graph.user",
    "id": FIRST_ADMIN,
    "userPrincipalName": "first-admin@contoso.com",
}


def _app(app_id: str = APP, object_id: str = APP_OBJECT, **overrides: Any) -> dict[str, Any]:
    """An app registration as ``az ad app list`` prints it (certificates without key material)."""
    app = {
        "id": object_id,
        "appId": app_id,
        "displayName": NAME,
        "createdDateTime": "2026-03-04T05:06:07Z",
        "passwordCredentials": [],
        "keyCredentials": [],
        # What an earlier setup run declares: setup's own Graph application permissions.
        "requiredResourceAccess": [
            {
                "resourceAppId": GRAPH_APP,
                "resourceAccess": [
                    {"id": GRAPH_ROLE, "type": "Role"},
                    {"id": GRAPH_ROLE_2, "type": "Role"},
                ],
            }
        ],
    }
    app.update(overrides)
    return app


def _granted(
    *role_ids: str, resource: str = GRAPH_SP, principal: str = SP_OBJECT
) -> list[dict[str, Any]]:
    """appRoleAssignments as Graph lists them: admin consent given for these app roles."""
    return [
        {
            "id": f"assignment-{n}",
            "appRoleId": role_id,
            "principalId": principal,
            "principalType": "ServicePrincipal",
            "resourceId": resource,
            "resourceDisplayName": "Microsoft Graph",  # what a look-alike would call itself too
        }
        for n, role_id in enumerate(role_ids)
    ]


class _Replies:
    """Successive answers to the same read; the last one repeats."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)

    def next(self) -> Any:
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def _key_credential(der: bytes, **overrides: Any) -> dict[str, Any]:
    """A keyCredentials entry as Graph returns it for a single app, key material included."""
    sha1 = hashlib.sha1(der).hexdigest().upper()  # noqa: S324 — an identifier
    entry = {
        "keyId": f"k-{sha1[:8].lower()}",
        "type": "AsymmetricX509Cert",
        "usage": "Verify",
        "key": base64.b64encode(der).decode(),
        "customKeyIdentifier": sha1,
    }
    entry.update(overrides)
    return entry


def _service_principal(**overrides: Any) -> dict[str, Any]:
    sp = {
        "id": SP_OBJECT,
        "appId": APP,
        "displayName": NAME,
        "passwordCredentials": [],
        "keyCredentials": [],
    }
    sp.update(overrides)
    return sp


def _machine_certificate() -> tuple[str, bytes]:
    """The cert+key bundle this machine's keystore holds, and its certificate's DER."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "entrabot-provisioner")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return cert_pem + key_pem, cert.public_bytes(serialization.Encoding.DER)


class _Directory:
    """Microsoft Entra as the signed-in administrator's ``az`` shows it.

    Read-only, apart from creating an app registration: any other command (adding a
    permission, admin consent, ...) fails the test. Unless a test says otherwise, an app is
    owned by the signed-in user and has no certificates, federated credentials or service
    principal, and a service principal has no admin consent.
    """

    def __init__(self, module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self.apps: list[dict[str, Any]] = []
        self.me: str | None = ME
        self.owners: dict[str, Any] = {}  # appId -> owners, or the az error reading them
        self.keys: dict[str, list[dict[str, Any]]] = {}  # app object ID -> keyCredentials
        self.federated: dict[str, list[dict[str, Any]]] = {}  # appId -> federated credentials
        self.principals: dict[str, dict[str, Any]] = {}  # appId -> its service principal
        self.sp_owners: dict[str, Any] = {}  # service principal object ID -> owners
        self.assignments: dict[str, Any] = {}  # service principal object ID -> consent given
        self.graph: Any = [GRAPH_SP, GRAPH_APP]  # what looking Graph up by its appId returns
        self.list_error: str | None = None
        self.calls: list[list[str]] = []
        monkeypatch.setattr(module, "run_az", self.run_az)

    @staticmethod
    def _reply(value: Any) -> tuple[int, str, str]:
        if isinstance(value, _Replies):
            value = value.next()
        if isinstance(value, str):
            return 1, "", value
        return 0, json.dumps(value), ""

    def run_az(self, args: list[str], capture: bool = True) -> tuple[int, str, str]:
        self.calls.append(list(args))
        assert args[-2:] == ["-o", "json"], f"az output must be read as JSON: {args}"
        if args[:3] == ["ad", "app", "list"]:
            if self.list_error:
                return 1, "", self.list_error
            flag, value = args[3], args[4]
            if flag == "--display-name":  # az matches the start of the name, any case
                found = [a for a in self.apps if a["displayName"].lower().startswith(value.lower())]
            else:
                assert flag == "--app-id", args
                found = [a for a in self.apps if a["appId"] == value]
            return 0, json.dumps(found), ""
        if args[:3] == ["ad", "signed-in-user", "show"]:
            if self.me is None:
                return 1, "", "ERROR: /me request is only valid with delegated authentication flow."
            return 0, json.dumps(self.me), ""
        if args[:4] == ["ad", "app", "owner", "list"]:
            return self._reply(self.owners.get(args[5], [ADMIN]))
        if args[:4] == ["ad", "app", "federated-credential", "list"]:
            return self._reply(self.federated.get(args[5], []))
        if args[0] == "rest":
            assert args[args.index("--method") + 1] == "GET", args
            url = args[args.index("--url") + 1]
            if "/servicePrincipals/" in url:
                principal_id = url.split("/servicePrincipals/", 1)[1].split("/", 1)[0]
                assert url.endswith(f"/{principal_id}/appRoleAssignments"), url
                granted = self.assignments.get(principal_id, [])
                if isinstance(granted, _Replies):
                    granted = granted.next()
                return self._reply(
                    granted if isinstance(granted, str | dict) else {"value": granted}
                )
            object_id = url.split("/applications/", 1)[1].split("?", 1)[0]
            return self._reply({"keyCredentials": self.keys.get(object_id, [])})
        if args[:3] == ["ad", "sp", "show"]:
            target = args[args.index("--id") + 1]
            if target == GRAPH_APP:  # Graph, by its well-known application ID
                assert args[args.index("--query") + 1] == "[id,appId]", args
                return self._reply(self.graph)
            principal = self.principals.get(target)
            if principal is None:
                return 3, "", f"ERROR: Service principal '{target}' doesn't exist"
            return 0, json.dumps(principal["id"]), ""
        if args[:3] == ["ad", "sp", "list"]:
            principal = self.principals.get(args[4])
            return self._reply([principal] if principal else [])
        if args[:4] == ["ad", "sp", "owner", "list"]:
            return self._reply(self.sp_owners.get(args[5], []))
        if args[:3] == ["ad", "app", "create"]:
            return 0, json.dumps(NEW_APP), ""
        raise AssertionError(f"unexpected az call: {args}")

    def looked_at_owners(self) -> bool:
        return any(call[2:4] == ["owner", "list"] for call in self.calls)


@pytest.fixture
def entra(provisioning_module, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """``ensure_app_registration`` on a machine with no Provisioner app recorded, against
    ``_Directory``. Consent, uploads and the password sweep are recorded, not run."""
    module = provisioning_module
    state: dict[str, str] = {"TENANT_ID": "tenant-id"}
    monkeypatch.delenv("ENTRABOT_TENANT_ID", raising=False)
    monkeypatch.setattr(module, "get_state", lambda key: state.get(key))
    monkeypatch.setattr(module, "set_state", state.__setitem__)
    monkeypatch.setattr(module, "clear_state", lambda key: state.pop(key, None))
    monkeypatch.setattr(
        module,
        "resolve_graph_permissions",
        lambda: {
            "Application.ReadWrite.All": GRAPH_ROLE,
            "AppRoleAssignment.ReadWrite.All": GRAPH_ROLE_2,
            "Directory.ReadWrite.All": EXTRA_ROLE,
        },
    )
    monkeypatch.setattr(module, "_application_status", lambda client_id: "exists")
    monkeypatch.setattr(
        module, "_generate_provisioner_cert", lambda: ("NEW-CERT", "NEW-KEY", "new-thumb")
    )
    world = SimpleNamespace(module=module, state=state, consents=[], uploads=[], sweeps=[])
    world.keystore = _Keystore(module, monkeypatch, None)
    world.directory = _Directory(module, monkeypatch)
    monkeypatch.setattr(
        module,
        "_ensure_permissions_and_consent",
        lambda client_id, values: world.consents.append(client_id) or False,
    )
    monkeypatch.setattr(
        module,
        "_remove_legacy_password_credentials",
        lambda client_id: world.sweeps.append(client_id) or 0,
    )
    monkeypatch.setattr(
        module, "_upload_cert_to_app", lambda client_id, pem: world.uploads.append(client_id)
    )
    return world


def _ensure(entra: SimpleNamespace) -> tuple[str, Any, str]:
    return entra.module.ensure_app_registration(REQUIRED, wait_for_propagation=False)


def _refused(
    entra: SimpleNamespace, capsys: pytest.CaptureFixture[str], *, recorded: str | None = None
) -> str:
    """Run setup, expect it to stop, check nothing was granted or changed; return its output."""
    with pytest.raises(entra.module.ProvisionerBootstrapError) as excinfo:
        _ensure(entra)
    out = capsys.readouterr().out + "\n" + str(excinfo.value)
    assert entra.consents == [], "no admin consent"
    assert entra.uploads == [], "no certificate uploaded"
    assert entra.sweeps == [], "no credential deleted"
    assert entra.keystore.events == []
    assert entra.state.get("PROVISIONER_CLIENT_ID") == recorded
    assert not [call for call in entra.directory.calls if call[:3] == ["ad", "app", "create"]]
    assert "Nothing was changed in Microsoft Entra" in out
    return out


def test_an_app_only_you_own_with_nothing_else_on_it_is_taken_over(
    entra: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    entra.directory.apps = [_app()]

    client_id, credential, _ = _ensure(entra)

    assert client_id == APP and entra.state["PROVISIONER_CLIENT_ID"] == APP
    assert entra.consents == [APP] and entra.uploads == [APP]
    assert credential == "NEW-CERTNEW-KEY"
    assert entra.directory.looked_at_owners(), "examined before anything was granted"
    out = capsys.readouterr().out
    assert f"Found existing provisioner app: {APP}" in out
    assert TAKEOVER_REFUSED not in out


@pytest.mark.parametrize("owners", [[MALLORY_USER], [ADMIN, MALLORY_USER]], ids=["other", "co"])
def test_an_app_someone_else_owns_is_not_taken_over(
    entra: SimpleNamespace, capsys: pytest.CaptureFixture[str], owners: list[dict[str, Any]]
) -> None:
    entra.directory.apps = [_app()]
    entra.directory.owners[APP] = owners

    out = _refused(entra, capsys)

    assert NAME in out and APP in out and "2026-03-04" in out
    assert "mallory@contoso.com" in out
    assert ("you (admin@contoso.com)" in out) is (ADMIN in owners)
    assert f"set {VERIFIED}" in out or f"{VERIFIED} to its application ID" in out
    assert "delete or rename" in out
    assert TAKEOVER_REFUSED in out.splitlines(), "on a line of its own, for the wizard"


def test_two_apps_with_the_name_are_not_taken_over(
    entra: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    # Both look fine on their own; setup still can't tell which one an earlier run made.
    entra.directory.apps = [
        _app(),
        _app(OTHER_APP, OTHER_APP_OBJECT, createdDateTime="2026-05-06T07:08:09Z"),
    ]

    out = _refused(entra, capsys)

    assert APP in out and OTHER_APP in out
    assert "2026-03-04" in out and "2026-05-06" in out
    assert VERIFIED in out
    assert TAKEOVER_REFUSED in out.splitlines()


def test_owners_that_cannot_be_read_mean_no_takeover(
    entra: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    entra.directory.apps = [_app()]
    entra.directory.owners[APP] = "ERROR: Insufficient privileges to complete the operation."

    out = _refused(entra, capsys)

    assert "Insufficient privileges" in out and APP in out


def test_an_unreadable_signed_in_account_means_no_takeover(
    entra: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    entra.directory.apps = [_app()]
    entra.directory.me = None

    out = _refused(entra, capsys)

    assert "/me request is only valid" in out


def test_an_ownerless_app_with_nothing_else_on_it_is_taken_over(entra: SimpleNamespace) -> None:
    # Only an administrator can change an app with no owners, and nothing on it lets anyone
    # else sign in as it: as safe as an app you own alone.
    entra.directory.apps = [_app()]
    entra.directory.owners[APP] = []

    client_id, _, _ = _ensure(entra)

    assert client_id == APP and entra.consents == [APP]


def test_an_ownerless_app_with_a_client_secret_is_not_taken_over(
    entra: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    entra.directory.apps = [_app(passwordCredentials=[{"keyId": "p-1", "hint": "Zq9"}])]
    entra.directory.owners[APP] = []

    out = _refused(entra, capsys)

    assert "1 client secret" in out
    assert "Zq9" not in out, "nothing about a secret beyond the count is printed"


@pytest.mark.parametrize(
    ("planted", "reported"),
    [
        ("client_secret", "1 client secret"),
        ("certificate", "1 certificate whose private key is not on this machine"),
        ("federated_credential", "1 federated credential"),
        ("service_principal_secret", "its service principal has 1 credential"),
        ("service_principal_owner", "mallory@contoso.com"),
        ("delegated_permission", "1 permission"),
    ],
)
def test_a_way_in_left_on_an_app_handed_over_to_you_blocks_the_takeover(
    entra: SimpleNamespace, capsys: pytest.CaptureFixture[str], planted: str, reported: str
) -> None:
    # Whoever registered the app can leave a way in, then make you its only owner.
    app = _app()
    if planted == "client_secret":
        app["passwordCredentials"] = [{"keyId": "p-1", "hint": "Zq9"}]
    elif planted == "certificate":
        entra.directory.keys[APP_OBJECT] = [_key_credential(b"mallory's certificate")]
    elif planted == "federated_credential":
        entra.directory.federated[APP] = [
            {"issuer": "https://token.actions.githubusercontent.com", "subject": "repo:m/x:ref"}
        ]
    elif planted == "service_principal_secret":
        entra.directory.principals[APP] = _service_principal(passwordCredentials=[{"keyId": "p"}])
    elif planted == "service_principal_owner":
        entra.directory.principals[APP] = _service_principal()
        entra.directory.sp_owners[SP_OBJECT] = [MALLORY_USER]
    else:
        # Admin consent grants everything an app declares, delegated scopes included.
        app["requiredResourceAccess"][0]["resourceAccess"].append(
            {"id": "ffffffff-0000-0000-0000-000000000001", "type": "Scope"}
        )
    entra.directory.apps = [app]

    out = _refused(entra, capsys)

    assert reported in out
    assert "Zq9" not in out and "repo:m/x" not in out


def test_this_machines_own_certificate_on_the_app_does_not_block_the_takeover(
    entra: SimpleNamespace,
) -> None:
    # The state file was lost (a fresh checkout); the app and the keystore entry were not.
    bundle, der = _machine_certificate()
    entra.keystore.pem = bundle
    entra.directory.apps = [_app()]
    entra.directory.keys[APP_OBJECT] = [_key_credential(der)]

    client_id, credential, _ = _ensure(entra)

    assert client_id == APP and credential == bundle
    assert entra.consents == [APP]


def test_a_certificate_that_only_claims_this_machines_thumbprint_blocks_the_takeover(
    entra: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    # customKeyIdentifier is whatever the uploader said; only the key material counts.
    bundle, der = _machine_certificate()
    entra.keystore.pem = bundle
    entra.directory.apps = [_app()]
    claimed = hashlib.sha1(der).hexdigest().upper()  # noqa: S324 — an identifier
    entra.directory.keys[APP_OBJECT] = [
        _key_credential(der),
        _key_credential(b"mallory's certificate", customKeyIdentifier=claimed),
    ]

    out = _refused(entra, capsys)

    assert "1 certificate whose private key is not on this machine" in out


def test_names_that_only_resemble_the_provisioners_are_not_candidates(
    entra: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    # az matches the start of a display name, in any case; only the exact name is a candidate.
    entra.directory.apps = [
        _app(displayName=f"{NAME} (old)"),
        _app(OTHER_APP, OTHER_APP_OBJECT, displayName=NAME.upper()),
    ]

    client_id, _, _ = _ensure(entra)

    assert client_id == NEW_APP and entra.consents == [NEW_APP]
    assert not entra.directory.looked_at_owners()
    assert "Created provisioner app" in capsys.readouterr().out


def test_a_search_that_fails_creates_nothing(
    entra: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    entra.directory.list_error = "ERROR: Please run 'az login' to setup account."

    out = _refused(entra, capsys)

    assert "az login" in out


def test_text_from_the_directory_is_printed_without_control_characters(
    entra: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    entra.directory.apps = [_app()]
    entra.directory.owners[APP] = [
        {
            "@odata.type": "#microsoft.graph.servicePrincipal",
            "id": MALLORY,
            "displayName": "evil\x1b[2J\x1b]0;owned\x07app",
        }
    ]

    out = _refused(entra, capsys)

    assert "\x1b" not in out and "\x07" not in out
    assert "evil" in out


# ─── an app an administrator has already approved ────────────────────────────
#
# Every computer uploads its own certificate to the one Provisioner app, often signed in as
# a different administrator than the app's owner. When the app already holds admin consent
# for every Graph permission setup needs, taking it over grants nobody anything.


def _second_machine(entra: SimpleNamespace, app: dict[str, Any] | None = None) -> None:
    """The Provisioner app as a second computer finds it: another administrator owns it,
    and it carries the first computer's certificate, whose key is on that computer."""
    entra.directory.apps = [app or _app()]
    entra.directory.owners[APP] = [FIRST_ADMIN_USER]
    entra.directory.keys[APP_OBJECT] = [_key_credential(b"the first computer's certificate")]
    entra.directory.principals[APP] = _service_principal()
    entra.directory.sp_owners[SP_OBJECT] = [FIRST_ADMIN_USER]


def _consented(entra: SimpleNamespace) -> list[list[str]]:
    """Every call that would add a permission or give admin consent."""
    return [
        call
        for call in entra.directory.calls
        if call[:3] == ["ad", "app", "permission"] or "admin-consent" in call
    ]


def test_an_app_an_administrator_already_approved_is_taken_over_without_consent(
    entra: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    _second_machine(entra)
    entra.directory.assignments[SP_OBJECT] = _granted(GRAPH_ROLE, GRAPH_ROLE_2)

    client_id, credential, _ = _ensure(entra)

    assert client_id == APP and entra.state["PROVISIONER_CLIENT_ID"] == APP
    assert entra.consents == [] and _consented(entra) == [], "no consent call made"
    assert entra.uploads == [APP] and credential == "NEW-CERTNEW-KEY", "this machine's cert"
    out = capsys.readouterr().out
    assert "already approved by an administrator for the permissions setup needs" in out
    assert TAKEOVER_REFUSED not in out


def test_an_approved_app_with_a_permission_an_administrator_also_approved_is_taken_over(
    entra: SimpleNamespace,
) -> None:
    # An earlier version of setup asked for a permission this one doesn't: already granted.
    app = _app()
    app["requiredResourceAccess"][0]["resourceAccess"].append({"id": EXTRA_ROLE, "type": "Role"})
    _second_machine(entra, app)
    entra.directory.assignments[SP_OBJECT] = _granted(GRAPH_ROLE, GRAPH_ROLE_2, EXTRA_ROLE)

    client_id, _, _ = _ensure(entra)

    assert client_id == APP and entra.consents == [] and _consented(entra) == []


@pytest.mark.parametrize(
    ("granted", "reported"),
    [
        # This run would have to consent the one that is missing.
        (_granted(GRAPH_ROLE), "not yet approved by an administrator for 1 of the permissions"),
        # Same role IDs, but granted by an app that only calls itself "Microsoft Graph".
        (
            _granted(GRAPH_ROLE, GRAPH_ROLE_2, resource=LOOKALIKE_SP),
            "not yet approved by an administrator for 2 of the permissions",
        ),
        # Granted to some other service principal, not this app's.
        (
            _granted(GRAPH_ROLE, GRAPH_ROLE_2, principal=OTHER_SP_OBJECT),
            "not yet approved by an administrator for 2 of the permissions",
        ),
    ],
    ids=["one_missing", "lookalike_graph", "other_principal"],
)
def test_an_app_not_approved_for_everything_setup_needs_falls_back_to_the_strict_rule(
    entra: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    granted: list[dict[str, Any]],
    reported: str,
) -> None:
    _second_machine(entra)
    entra.directory.assignments[SP_OBJECT] = granted

    out = _refused(entra, capsys)

    assert reported in out
    assert "someone other than you owns it: first-admin@contoso.com" in out
    assert "1 certificate whose private key is not on this machine" in out
    assert TAKEOVER_REFUSED in out.splitlines()


@pytest.mark.parametrize(
    ("problem", "reported"),
    [
        ("assignments", "ERROR: Forbidden"),
        ("more_than_one_page", "more of them than one read returns"),
        ("graph_lookup", "ERROR: Request_ResourceNotFound"),
        ("graph_lookup_wrong_app", "not Microsoft Graph's service principal"),
        ("service_principal", "ERROR: Insufficient privileges"),
    ],
)
def test_approval_that_cannot_be_read_falls_back_to_the_strict_rule(
    entra: SimpleNamespace, capsys: pytest.CaptureFixture[str], problem: str, reported: str
) -> None:
    _second_machine(entra)
    entra.directory.assignments[SP_OBJECT] = _granted(GRAPH_ROLE, GRAPH_ROLE_2)
    if problem == "assignments":
        entra.directory.assignments[SP_OBJECT] = "ERROR: Forbidden"
    elif problem == "more_than_one_page":
        entra.directory.assignments[SP_OBJECT] = {
            "value": _granted(GRAPH_ROLE, GRAPH_ROLE_2),
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/next-page",
        }
    elif problem == "graph_lookup":
        entra.directory.graph = "ERROR: Request_ResourceNotFound"
    elif problem == "graph_lookup_wrong_app":
        entra.directory.graph = [LOOKALIKE_SP, "ffffffff-0000-0000-0000-0000000000aa"]
    else:
        entra.directory.principals.pop(APP)
        real_run_az = entra.directory.run_az

        def sp_list_fails(args: list[str], capture: bool = True) -> tuple[int, str, str]:
            if args[:3] == ["ad", "sp", "list"]:
                entra.directory.calls.append(list(args))
                return 1, "", "ERROR: Insufficient privileges to complete the operation."
            return real_run_az(args, capture)

        entra.module.run_az = sp_list_fails

    out = _refused(entra, capsys)

    assert reported in out
    assert "someone other than you owns it" in out


def test_an_approved_app_that_asks_for_a_permission_nobody_approved_is_not_taken_over(
    entra: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    # Admin consent would grant the extra one too, so it is never given for this app.
    app = _app()
    app["requiredResourceAccess"][0]["resourceAccess"].append({"id": EXTRA_ROLE, "type": "Role"})
    _second_machine(entra, app)
    entra.directory.assignments[SP_OBJECT] = _granted(GRAPH_ROLE, GRAPH_ROLE_2)

    out = _refused(entra, capsys)

    assert "also asks for 1 permission no administrator has approved" in out
    assert _consented(entra) == []


def test_two_apps_with_the_name_are_refused_even_when_one_is_approved(
    entra: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    _second_machine(entra)
    entra.directory.assignments[SP_OBJECT] = _granted(GRAPH_ROLE, GRAPH_ROLE_2)
    entra.directory.apps.append(
        _app(OTHER_APP, OTHER_APP_OBJECT, createdDateTime="2026-05-06T07:08:09Z")
    )
    entra.directory.owners[OTHER_APP] = [MALLORY_USER]

    out = _refused(entra, capsys)

    assert APP in out and OTHER_APP in out
    assert "already approved by an administrator for the permissions setup needs" in out
    assert TAKEOVER_REFUSED in out.splitlines()


def test_an_approval_withdrawn_before_the_permission_step_takes_nothing_over(
    entra: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    # Approved when the app was examined, not any more when setup is about to go on: this
    # run would have to consent, so it stops, and the app is not recorded for next time.
    _second_machine(entra)
    entra.directory.assignments[SP_OBJECT] = _Replies(
        _granted(GRAPH_ROLE, GRAPH_ROLE_2), _granted(GRAPH_ROLE)
    )

    out = _refused(entra, capsys)

    assert _consented(entra) == []
    assert "1 of the permissions setup needs" in out


@pytest.mark.parametrize("unreadable", ["owners", "signed_in_user"])
def test_an_approved_app_does_not_depend_on_who_owns_it_or_who_is_signed_in(
    entra: SimpleNamespace, unreadable: str
) -> None:
    # Neither decides whether this run grants anything; only the existing approval does.
    _second_machine(entra)
    entra.directory.assignments[SP_OBJECT] = _granted(GRAPH_ROLE, GRAPH_ROLE_2)
    if unreadable == "owners":
        entra.directory.owners[APP] = "ERROR: Insufficient privileges to complete the operation."
    else:
        entra.directory.me = None

    client_id, _, _ = _ensure(entra)

    assert client_id == APP and entra.consents == [] and _consented(entra) == []


def test_consented_graph_roles_are_matched_to_graph_by_object_id_not_by_name(
    provisioning_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _Directory(provisioning_module, monkeypatch)
    directory.principals[APP] = _service_principal()
    directory.assignments[SP_OBJECT] = [
        *_granted(GRAPH_ROLE),
        *_granted(GRAPH_ROLE_2, resource=LOOKALIKE_SP),  # also says "Microsoft Graph"
    ]

    assert provisioning_module._get_consented_graph_role_ids(APP) == {GRAPH_ROLE}

    directory.graph = "ERROR: Request_ResourceNotFound"
    assert provisioning_module._get_consented_graph_role_ids(APP) == set(), "re-consent, as before"


# ─── ENTRABOT_PROVISIONER_APP_ID_VERIFIED: the operator names the app ────────


def test_the_app_the_operator_names_is_used_and_recorded(
    entra: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    entra.directory.apps = [_app()]
    entra.directory.owners[APP] = [MALLORY_USER]  # checked by the operator, not second-guessed
    monkeypatch.setenv(VERIFIED, f" {APP.upper()} ")

    client_id, _, _ = _ensure(entra)

    assert client_id == APP and entra.state["PROVISIONER_CLIENT_ID"] == APP
    assert entra.consents == [APP] and entra.uploads == [APP]
    assert entra.directory.calls == [["ad", "app", "list", "--app-id", APP, "-o", "json"]]
    assert VERIFIED in capsys.readouterr().out


def test_the_app_id_setup_writes_to_env_vouches_for_nothing(
    entra: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Setup writes ENTRABOT_PROVISIONER_APP_ID to .env itself, and a shell that loads .env
    # passes it on: it is not anyone's statement that they checked the app.
    entra.directory.apps = [_app()]
    entra.directory.owners[APP] = [MALLORY_USER]
    monkeypatch.setenv("ENTRABOT_PROVISIONER_APP_ID", APP)

    out = _refused(entra, capsys)

    assert "someone other than you owns it" in out
    assert not [call for call in entra.directory.calls if "--app-id" in call]


def test_a_named_app_that_does_not_exist_is_refused(
    entra: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    entra.directory.apps = [_app()]
    monkeypatch.setenv(VERIFIED, OTHER_APP)

    out = _refused(entra, capsys)

    assert OTHER_APP in out and VERIFIED in out
    assert not entra.directory.looked_at_owners(), "the app with the name is not used instead"
    assert TAKEOVER_REFUSED not in out


def test_a_named_app_that_cannot_be_looked_up_is_refused(
    entra: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    entra.directory.list_error = "ERROR: AADSTS50076: multi-factor authentication is required"
    monkeypatch.setenv(VERIFIED, APP)

    out = _refused(entra, capsys)

    assert "AADSTS50076" in out and APP in out


def test_a_value_that_is_not_an_application_id_is_refused_without_repeating_it(
    entra: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    entra.directory.apps = [_app()]
    monkeypatch.setenv(VERIFIED, "s3cr3t-pasted-by-mistake")

    out = _refused(entra, capsys)

    assert VERIFIED in out
    assert "s3cr3t" not in out
    assert entra.directory.calls == []


def test_a_recorded_app_is_used_as_before_without_looking_at_owners(
    entra: SimpleNamespace,
) -> None:
    entra.state.update(PROVISIONER_CLIENT_ID=APP, PROVISIONER_CERT_THUMBPRINT="thumb")
    entra.keystore.pem = PEM_BUNDLE
    # Two apps with the name: a lookup would refuse. A recorded app means no lookup.
    entra.directory.apps = [_app(), _app(OTHER_APP, OTHER_APP_OBJECT)]

    client_id, credential, _ = _ensure(entra)

    assert client_id == APP and credential == PEM_BUNDLE
    assert entra.directory.calls == [], "no search, no owner lookups"
    assert entra.consents == [APP] and entra.uploads == []
    assert entra.state["PROVISIONER_CERT_THUMBPRINT"] == "thumb"


def test_a_named_app_that_contradicts_the_recorded_one_is_refused(
    entra: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    entra.state.update(PROVISIONER_CLIENT_ID=APP, PROVISIONER_CERT_THUMBPRINT="thumb")
    entra.keystore.pem = PEM_BUNDLE
    entra.directory.apps = [_app(), _app(OTHER_APP, OTHER_APP_OBJECT)]
    monkeypatch.setenv(VERIFIED, OTHER_APP)

    out = _refused(entra, capsys, recorded=APP)

    assert APP in out and OTHER_APP in out and VERIFIED in out
    assert entra.state["PROVISIONER_CERT_THUMBPRINT"] == "thumb"
