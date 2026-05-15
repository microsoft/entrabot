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

    monkeypatch.delenv("ENTRACLAW_TENANT_ID", raising=False)

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
