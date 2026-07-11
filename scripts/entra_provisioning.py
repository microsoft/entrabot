#!/usr/bin/env python3
"""
Shared helpers for Entra Graph provisioning.

Centralizes the dedicated "EntraBot Agent ID Provisioner" app registration
used for Agent Identity Blueprint and Agent Identity creation via the
Graph beta API.

Pattern borrowed from a related internal project — the provisioner is a dedicated app
with client_credentials flow, because Azure CLI tokens include
Directory.AccessAsUser.All which the Agent Identity APIs explicitly reject
(hard 403).

The Provisioner authenticates via an X.509 cert whose private key lives
in macOS Keychain (via `keyring`). The public cert is registered on the
app registration in Entra. No client_secret anywhere on disk. Matches the
Blueprint-cert pattern already used for agent-body auth (ADR-003).

Usage:
    # As a library:
    from entra_provisioning import get_existing_graph_token, get_bootstrap_graph_token

    # As a standalone bootstrap:
    python3 scripts/entra_provisioning.py
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

MS_GRAPH_API_ID = "00000003-0000-0000-c000-000000000000"
PROVISIONER_APP_DISPLAY_NAME = "EntraBot Agent ID Provisioner"

# Application.ReadWrite.All — required for Blueprint CRUD
APP_READWRITE_ALL_ID = "1bfefb4e-e0b5-418b-a88f-73c46d2cc8e9"

BASE_PERMISSION_VALUES = [
    "Application.ReadWrite.All",
    "AppRoleAssignment.ReadWrite.All",
    "DelegatedPermissionGrant.ReadWrite.All",
    "LicenseAssignment.ReadWrite.All",
    "Organization.Read.All",
    "User.ReadWrite.All",
]

AGENT_ID_PERMISSION_MATCHERS = [
    "AgentIdentity",
    "AgentIdUser",
]


class ProvisionerBootstrapError(RuntimeError):
    """Raised when the provisioner app cannot be created or consented."""


# ---------------------------------------------------------------------------
# Cert-auth secret storage — KEYCHAIN ONLY, never on disk
# ---------------------------------------------------------------------------
# CLAUDE.md non-negotiable: "Never write secrets to logs or memory files".
# The Provisioner app is authenticated via an X.509 cert whose private
# key lives in macOS Keychain (via `keyring`). The public cert is
# registered on the app registration in Entra. No client_secret
# anywhere. Matches the Blueprint-cert pattern already used for agent-
# body auth.
_KEYCHAIN_SERVICE_CERT = "entrabot-provisioner-cert"


def _keyring_module():
    """Import keyring lazily so missing dep produces a clear error."""
    try:
        import keyring as _kr

        return _kr
    except ImportError as exc:
        raise ProvisionerBootstrapError(
            "keyring is required for cert-auth. Install with: pip install -e '.[provisioning]'"
        ) from exc


def _assert_secure_keyring_backend() -> None:
    """Fail closed if the active ``keyring`` backend is not OS-native.

    Without this guard, the Provisioner cert + private key would be written
    in cleartext on Linux hosts that have ``keyrings.alt`` installed and no
    D-Bus session (containers, headless CI, sudo'd shells). The runtime
    CredentialStore already enforces this for the agent body; mirroring the
    check here closes the provisioning-time gap.
    """
    if sys.platform == "win32":
        # Windows uses _windows_file_store_cert with explicit ACLs, not the
        # keyring backend, so the assertion does not apply here.
        return
    from entrabot.platform.keyring_backend import assert_allowed_keyring_backend

    assert_allowed_keyring_backend()


def _keychain_get_cert(account: str) -> str | None:
    """Return the PEM (cert+key) bundle from Keychain/file, or None if absent."""
    if sys.platform == "win32":
        return _windows_file_get_cert(account)
    _assert_secure_keyring_backend()
    kr = _keyring_module()
    return kr.get_password(_KEYCHAIN_SERVICE_CERT, account)


def _keychain_store_cert(account: str, pem_bundle: str) -> None:
    """Store the PEM (cert+key) bundle in Keychain/local file — overwrites if present.

    On Windows, Credential Manager has a 2560-byte blob limit which PEM bundles
    exceed. Fall back to a file in %LOCALAPPDATA%\\entrabot\\ with strict ACLs.
    """
    if sys.platform == "win32":
        _windows_file_store_cert(account, pem_bundle)
        return
    _assert_secure_keyring_backend()
    kr = _keyring_module()
    kr.set_password(_KEYCHAIN_SERVICE_CERT, account, pem_bundle)


def _windows_file_store_cert(account: str, pem_bundle: str) -> None:
    """Store PEM in %LOCALAPPDATA%\\entrabot\\provisioner-cert-<account>.pem."""
    cert_dir = Path(os.environ.get("LOCALAPPDATA", ""), "entrabot")
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / f"provisioner-cert-{account}.pem"
    cert_path.write_text(pem_bundle, encoding="utf-8")
    # Lock down ACLs: only current user gets modify
    user = f"{os.environ.get('USERDOMAIN', '.')}\\{os.environ['USERNAME']}"
    subprocess.run(
        ["icacls", str(cert_path), "/inheritance:r", "/grant:r", f"{user}:M"],
        capture_output=True,
        check=False,
    )


def _windows_file_get_cert(account: str) -> str | None:
    """Read PEM from %LOCALAPPDATA%\\entrabot\\provisioner-cert-<account>.pem."""
    cert_path = Path(
        os.environ.get("LOCALAPPDATA", ""), "entrabot", f"provisioner-cert-{account}.pem"
    )
    if cert_path.is_file():
        return cert_path.read_text(encoding="utf-8")
    return None


def _keychain_delete_cert(account: str) -> None:
    """Remove the Keychain/file entry. No-op if absent."""
    if sys.platform == "win32":
        cert_path = Path(
            os.environ.get("LOCALAPPDATA", ""), "entrabot", f"provisioner-cert-{account}.pem"
        )
        cert_path.unlink(missing_ok=True)
        return
    _assert_secure_keyring_backend()
    kr = _keyring_module()
    with contextlib.suppress(kr.errors.PasswordDeleteError):
        kr.delete_password(_KEYCHAIN_SERVICE_CERT, account)


def _generate_provisioner_cert() -> tuple[str, str, str]:
    """Generate a fresh self-signed cert for Provisioner auth.

    Returns (cert_pem, key_pem, thumbprint_hex). The PEMs are strings
    (UTF-8); thumbprint is lowercase hex (matches Entra's
    ``customKeyIdentifier`` / ``keyId`` representation).

    Private material is returned as strings in memory — caller must
    store it in Keychain, never to disk.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:
        raise ProvisionerBootstrapError(
            "cryptography is required for cert-auth. Install with: pip install -e '.[provisioning]'"
        ) from exc

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "entrabot-provisioner"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "EntraBot"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC))
        .not_valid_after(datetime.now(UTC) + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(encoding=serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    thumbprint = cert.fingerprint(hashes.SHA1()).hex()
    return cert_pem, key_pem, thumbprint


def _cert_pem_from_bundle(pem_bundle: str) -> str:
    """Extract the public certificate PEM from a cert+key PEM bundle."""
    marker = "-----END CERTIFICATE-----"
    if marker not in pem_bundle:
        raise ProvisionerBootstrapError("stored Provisioner cert bundle has no certificate")
    return pem_bundle.split(marker, 1)[0] + marker + "\n"


def _thumbprint_from_cert_pem(cert_pem: str) -> str:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
    except ImportError as exc:
        raise ProvisionerBootstrapError(
            "cryptography is required for cert-auth. Install with: pip install -e '.[provisioning]'"
        ) from exc

    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    return cert.fingerprint(hashes.SHA1()).hex()


def _upload_cert_to_app(app_id: str, cert_pem: str) -> None:
    """Register a public cert on the Provisioner app via az CLI.

    Uses ``--append`` for idempotent multi-cert support (matches the
    Blueprint pattern). The public cert is written to a mktemp'd file
    only for the duration of the az CLI call; private key is NEVER
    touched.
    """
    with tempfile.NamedTemporaryFile(suffix=".crt", mode="w", delete=False) as tf:
        tf.write(cert_pem)
        cert_path = tf.name
    try:
        rc, out, err = run_az(
            [
                "ad",
                "app",
                "credential",
                "reset",
                "--id",
                app_id,
                "--cert",
                f"@{cert_path}",
                "--append",
                "-o",
                "json",
            ]
        )
        if rc != 0:
            raise ProvisionerBootstrapError(err or "failed to upload cert to Provisioner app")
        # az prints a JSON blob on success; we just care that rc == 0
        _ = out
    finally:
        with contextlib.suppress(OSError):
            os.unlink(cert_path)


def _remove_legacy_password_credentials(app_id: str) -> int:
    """Delete any password credentials from the Provisioner app.

    Returns the count of credentials deleted. Used during migration
    from the secret-auth era — once cert-auth is in place, a lingering
    password credential is a usable backdoor and MUST be removed.

    ``az ad app credential delete`` without ``--cert`` targets password
    credentials; with ``--cert`` it targets key credentials. We only
    delete passwords here.
    """
    rc, out, err = run_az(
        [
            "ad",
            "app",
            "show",
            "--id",
            app_id,
            "--query",
            "passwordCredentials[].keyId",
            "-o",
            "json",
        ]
    )
    if rc != 0:
        raise ProvisionerBootstrapError(err or "could not list Provisioner password credentials")
    try:
        key_ids = json.loads(out) if out else []
    except json.JSONDecodeError as exc:
        raise ProvisionerBootstrapError(f"failed to parse passwordCredentials list: {exc}") from exc

    removed = 0
    for key_id in key_ids:
        rc, _, err = run_az(
            [
                "ad",
                "app",
                "credential",
                "delete",
                "--id",
                app_id,
                "--key-id",
                key_id,
            ]
        )
        if rc != 0:
            raise ProvisionerBootstrapError(err or f"failed to delete password credential {key_id}")
        removed += 1
    return removed


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

_STATE_FILE = Path(__file__).resolve().parent.parent / ".entrabot-state.json"


def _load_state() -> dict:
    if _STATE_FILE.is_file():
        try:
            return json.loads(_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_state(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def get_state(key: str) -> str | None:
    """Read a value from .entrabot-state.json."""
    return _load_state().get(key)


def set_state(key: str, value: str) -> None:
    """Write a value to .entrabot-state.json."""
    state = _load_state()
    state[key] = value
    _save_state(state)


def clear_state(key: str) -> None:
    """Remove a key from .entrabot-state.json."""
    state = _load_state()
    state.pop(key, None)
    _save_state(state)


# ---------------------------------------------------------------------------
# az CLI helpers
# ---------------------------------------------------------------------------


def run_az(args: list[str], capture: bool = True) -> tuple[int, str, str]:
    """Run an az CLI command, return (returncode, stdout, stderr)."""
    az_bin = shutil.which("az") or "az"
    result = subprocess.run([az_bin, *args], capture_output=capture, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def get_signed_in_user_id() -> str | None:
    """Get the object ID of the currently signed-in Azure CLI user."""
    rc, out, _ = run_az(["ad", "signed-in-user", "show", "--query", "id", "-o", "tsv"])
    if rc == 0 and out:
        return out
    return None


def build_sponsors_bind() -> list[str]:
    """Build the sponsors@odata.bind array for Blueprint/Agent Identity creation."""
    user_id = get_signed_in_user_id()
    if user_id:
        return [f"https://graph.microsoft.com/v1.0/users/{user_id}"]
    return []


# ---------------------------------------------------------------------------
# Permission discovery
# ---------------------------------------------------------------------------


def _load_graph_app_roles() -> list[dict]:
    """Load all app roles from the Microsoft Graph service principal."""
    rc, out, err = run_az(
        [
            "ad",
            "sp",
            "show",
            "--id",
            MS_GRAPH_API_ID,
            "--query",
            "appRoles[].{id:id,value:value}",
            "-o",
            "json",
        ]
    )
    if rc != 0 or not out:
        raise ProvisionerBootstrapError(err or "could not query Microsoft Graph app roles")
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise ProvisionerBootstrapError(f"failed to parse Graph role list: {exc}") from exc


def resolve_graph_permissions() -> dict[str, str]:
    """Return a map of permission value -> role ID for all Graph app roles."""
    roles = _load_graph_app_roles()
    return {role["value"]: role["id"] for role in roles if role.get("value") and role.get("id")}


def build_required_permission_values() -> list[str]:
    """Build the list of permission values needed for Agent Identity provisioning.

    Dynamically discovers all AgentIdentity-related permissions from Graph,
    adds Application.ReadWrite.All for Blueprint CRUD.
    """
    required = list(BASE_PERMISSION_VALUES)
    graph_permissions = resolve_graph_permissions()
    for value in sorted(graph_permissions):
        if any(matcher in value for matcher in AGENT_ID_PERMISSION_MATCHERS):
            required.append(value)
    # Deduplicate while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for v in required:
        if v not in seen:
            seen.add(v)
            deduped.append(v)
    return deduped


# ---------------------------------------------------------------------------
# Permission management
# ---------------------------------------------------------------------------


def _resolve_permission_specs(required_values: list[str]) -> list[tuple[str, str]]:
    """Resolve permission value names to (value, "roleId=Role") specs."""
    permission_map = resolve_graph_permissions()
    specs = []
    missing = []
    for value in required_values:
        role_id = permission_map.get(value)
        if role_id:
            specs.append((value, f"{role_id}=Role"))
        else:
            missing.append(value)
    if missing:
        raise ProvisionerBootstrapError(
            "missing Microsoft Graph application permissions in tenant: "
            + ", ".join(sorted(missing))
        )
    return specs


def _get_existing_permission_role_ids(client_id: str) -> set[str]:
    """Get the set of role IDs already assigned to an app."""
    rc, out, err = run_az(
        [
            "ad",
            "app",
            "show",
            "--id",
            client_id,
            "--query",
            "requiredResourceAccess[?resourceAppId=='00000003-0000-0000-c000-000000000000']"
            ".resourceAccess[].id",
            "-o",
            "json",
        ]
    )
    if rc != 0 or not out:
        if err:
            raise ProvisionerBootstrapError(err)
        return set()
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ProvisionerBootstrapError(f"failed to parse existing app permissions: {exc}") from exc
    return {item for item in data if item}


def _get_consented_graph_role_ids(client_id: str) -> set[str]:
    """Return Microsoft Graph app-role IDs effectively admin-consented on the
    provisioner's service principal.

    Unlike :func:`_get_existing_permission_role_ids` (which reads
    ``requiredResourceAccess`` — the *declared* permissions on the app),
    this inspects the SP's ``appRoleAssignments`` — the *effective* grants
    produced by admin consent. Consent drift (a permission requested but
    never consented) is invisible to ``requiredResourceAccess``, so the only
    way to detect it is to read the actual grants.

    On a read failure we return an empty set, which makes the caller treat
    everything as unconsented and re-run admin consent. ``admin-consent`` is
    idempotent, so erring toward re-consent is safe.
    """
    rc, out, err = run_az(
        [
            "rest",
            "--method",
            "GET",
            "--url",
            f"https://graph.microsoft.com/v1.0/servicePrincipals(appId='{client_id}')"
            "/appRoleAssignments",
            "--query",
            "value[?resourceDisplayName=='Microsoft Graph'].appRoleId",
            "-o",
            "json",
        ]
    )
    if rc != 0 or not out:
        return set()
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return set()
    return {item for item in data if item}


def _print_admin_required(action: str, err: str = "") -> None:
    print(f"  ERROR: The signed-in operator could not {action}.")
    print("  This step requires an Entra administrator with permission to")
    print("  create/update app registrations and grant Microsoft Graph admin consent.")
    print("  Ask an administrator to complete the setup, then rerun.")
    if err:
        print(f"  Details: {err}")


def _application_exists(client_id: str) -> bool:
    rc, _, _ = run_az(["ad", "app", "show", "--id", client_id], capture=True)
    return rc == 0


def _ensure_service_principal(client_id: str) -> None:
    """Ensure a service principal exists for the given app."""
    rc, _, _ = run_az(["ad", "sp", "show", "--id", client_id], capture=True)
    if rc == 0:
        return
    rc, _, err = run_az(["ad", "sp", "create", "--id", client_id])
    if rc != 0 and "already exists" not in err.lower():
        raise ProvisionerBootstrapError(err or "service principal creation failed")


def _ensure_permissions_and_consent(client_id: str, required_values: list[str]) -> bool:
    """Add Graph permissions and grant admin consent if permissions changed.

    "Changed" means either (a) a required permission was missing from the
    app's ``requiredResourceAccess`` and had to be added, OR (b) a required
    permission is declared but not effectively admin-consented on the SP
    (consent drift). Either case warrants re-running admin consent; the
    caller uses the ``True`` return to decide whether a propagation wait is
    warranted.

    Checking *consented* permissions (not just *declared* ones) is what lets
    this self-heal consent drift: a permission added to the app's required
    list after the last admin-consent stays unconsented forever if we only
    compare the declared set.
    """
    _ensure_service_principal(client_id)

    permission_specs = _resolve_permission_specs(required_values)
    required_role_ids = {spec.split("=", 1)[0] for _, spec in permission_specs}
    declared_role_ids = _get_existing_permission_role_ids(client_id)
    consented_role_ids = _get_consented_graph_role_ids(client_id)

    missing_specs = [
        spec for _, spec in permission_specs if spec.split("=", 1)[0] not in declared_role_ids
    ]
    unconsented_role_ids = sorted(required_role_ids - consented_role_ids)
    print(f"  Ensuring {len(permission_specs)} Graph application permissions on provisioner app...")

    if not missing_specs and not unconsented_role_ids:
        print("  Provisioner app already has the required Graph permissions (declared + consented)")
        return False

    if missing_specs:
        cmd = [
            "ad",
            "app",
            "permission",
            "add",
            "--id",
            client_id,
            "--api",
            MS_GRAPH_API_ID,
            "--api-permissions",
            *missing_specs,
        ]
        rc, _, err = run_az(cmd)
        if rc != 0 and "already exists" not in err.lower():
            lowered = err.lower()
            if any(term in lowered for term in ["insufficient", "authorization", "privilege"]):
                _print_admin_required(
                    "add the required Microsoft Graph application permissions", err
                )
            raise ProvisionerBootstrapError(err or "permission add failed")
    elif unconsented_role_ids:
        # Declared but not effectively consented — classic consent drift,
        # usually because a permission was added to the required list after
        # the last admin consent. Re-run consent so it becomes effective.
        print(
            f"  Consent drift: {len(unconsented_role_ids)} required permission(s) declared "
            "but not admin-consented; re-running admin consent"
        )

    # Grant admin consent. Runs when permissions were newly added in this
    # invocation OR when declared permissions remain unconsented (drift). The
    # read/action path uses load_existing_app_registration (no permission
    # repair), so this does not re-consent on every token acquisition.
    print("  Granting admin consent for provisioner app...")
    consent_error = ""
    for attempt in range(4):
        if attempt:
            wait = 10 * (attempt + 1)
            print(f"  Retrying admin consent in {wait}s (attempt {attempt + 1}/4)...")
            time.sleep(wait)
        rc, _, err = run_az(["ad", "app", "permission", "admin-consent", "--id", client_id])
        if rc == 0:
            print("  Admin consent granted")
            return True
        consent_error = err

    lowered = consent_error.lower()
    if any(term in lowered for term in ["insufficient", "authorization", "privilege", "admin"]):
        _print_admin_required("grant admin consent for the provisioner app", consent_error)
    raise ProvisionerBootstrapError(consent_error or "admin consent failed")


# ---------------------------------------------------------------------------
# Provisioner app lifecycle
# ---------------------------------------------------------------------------


def ensure_app_registration(
    required_values: list[str],
    wait_for_propagation: bool = True,
) -> tuple[str, str, str]:
    """Ensure the dedicated provisioner app exists with cert-auth.

    Returns (client_id, pem_bundle, tenant_id) where ``pem_bundle`` is
    the PEM-encoded cert+private-key (cert-auth material).

    The private key ONLY exists in macOS Keychain; ``pem_bundle`` in
    memory is transient — callers must not write it to disk. State
    file tracks only non-secret identifiers (app id, thumbprint).
    """
    tenant_id = os.environ.get("ENTRABOT_TENANT_ID") or get_state("TENANT_ID")
    if not tenant_id:
        rc, out, err = run_az(["account", "show", "--query", "tenantId", "-o", "tsv"])
        if rc != 0 or not out:
            raise ProvisionerBootstrapError(
                err or "cannot determine tenant ID; run 'az login' first"
            )
        tenant_id = out
        set_state("TENANT_ID", tenant_id)

    # SECURITY: legacy migration path — if a prior (secret-auth) run
    # left PROVISIONER_CLIENT_SECRET in the state file, we purge it
    # BEFORE the cert-auth flow starts. This guarantees the state
    # file never carries a secret forward.
    if get_state("PROVISIONER_CLIENT_SECRET"):
        print(
            "  WARNING: legacy PROVISIONER_CLIENT_SECRET found in state file. "
            "Cert-auth supersedes secret-auth; purging the secret from disk."
        )
        clear_state("PROVISIONER_CLIENT_SECRET")

    client_id = get_state("PROVISIONER_CLIENT_ID")
    pem_bundle = _keychain_get_cert(tenant_id)

    # Stale-app detection
    if client_id and not _application_exists(client_id):
        print(f"  Cached provisioner app is stale: {client_id}")
        client_id = None
        pem_bundle = None
        clear_state("PROVISIONER_CLIENT_ID")
        clear_state("PROVISIONER_CERT_THUMBPRINT")
        _keychain_delete_cert(tenant_id)

    # Find-or-create the Provisioner app registration
    if not client_id:
        rc, out, _ = run_az(
            [
                "ad",
                "app",
                "list",
                "--display-name",
                PROVISIONER_APP_DISPLAY_NAME,
                "--query",
                "[0].appId",
                "-o",
                "tsv",
            ]
        )
        if rc == 0 and out:
            client_id = out
            print(f"  Found existing provisioner app: {client_id}")
            set_state("PROVISIONER_CLIENT_ID", client_id)
        else:
            print("  Creating dedicated EntraBot provisioner app...")
            rc, out, err = run_az(
                [
                    "ad",
                    "app",
                    "create",
                    "--display-name",
                    PROVISIONER_APP_DISPLAY_NAME,
                    "--sign-in-audience",
                    "AzureADMyOrg",
                    "--query",
                    "appId",
                    "-o",
                    "tsv",
                ]
            )
            if rc != 0 or not out:
                lowered = (err or "").lower()
                if any(
                    term in lowered
                    for term in ["insufficient", "authorization", "privilege", "permission"]
                ):
                    _print_admin_required("create the dedicated provisioner app registration", err)
                raise ProvisionerBootstrapError(err or "app registration creation failed")
            client_id = out
            print(f"  Created provisioner app: {client_id}")
            set_state("PROVISIONER_CLIENT_ID", client_id)

    permissions_changed = _ensure_permissions_and_consent(client_id, required_values)

    # SECURITY: any leftover password credentials on the app are a
    # backdoor — remove them unconditionally. This closes the window
    # opened by the secret-auth era.
    try:
        if _remove_legacy_password_credentials(client_id):
            print("  Removed legacy app credentials from Provisioner app.")
    except ProvisionerBootstrapError as exc:
        print(f"  WARN: could not enumerate/delete legacy app credentials: {type(exc).__name__}")

    # Cert-auth path: generate + upload + Keychain-store if absent
    if not pem_bundle:
        print("  Generating cert for Provisioner (RSA 2048, 365 days)...")
        cert_pem, key_pem, thumbprint = _generate_provisioner_cert()
        print(f"  Uploading public cert to app (SHA-1 thumb: {thumbprint})...")
        _upload_cert_to_app(client_id, cert_pem)
        pem_bundle = cert_pem + key_pem
        _keychain_store_cert(tenant_id, pem_bundle)
        set_state("PROVISIONER_CERT_THUMBPRINT", thumbprint)
        print(
            f"  Cert private key stored in macOS Keychain "
            f"(service='{_KEYCHAIN_SERVICE_CERT}', account='{tenant_id}')."
        )
    else:
        thumbprint = get_state("PROVISIONER_CERT_THUMBPRINT")
        if not thumbprint:
            cert_pem = _cert_pem_from_bundle(pem_bundle)
            thumbprint = _thumbprint_from_cert_pem(cert_pem)
            print(
                "  Local Provisioner cert exists but is not recorded on this "
                f"app; uploading public cert (SHA-1 thumb: {thumbprint})..."
            )
            _upload_cert_to_app(client_id, cert_pem)
            set_state("PROVISIONER_CERT_THUMBPRINT", thumbprint)
        print(f"  Using existing Provisioner cert (thumb: {thumbprint})")

    if wait_for_propagation and permissions_changed:
        print("  Waiting 30s for Graph permission propagation...")
        time.sleep(30)

    return client_id, pem_bundle, tenant_id


def load_existing_app_registration() -> tuple[str, str, str]:
    """Load an already-bootstrapped provisioner app without mutating Entra.

    Utility scripts use this path so read/status/action commands don't create
    app registrations, add permissions, grant consent, or wait for propagation.
    """
    tenant_id = os.environ.get("ENTRABOT_TENANT_ID") or get_state("TENANT_ID")
    client_id = get_state("PROVISIONER_CLIENT_ID")
    if not tenant_id or not client_id:
        raise ProvisionerBootstrapError(
            "Provisioner app is not bootstrapped. Run: python3 scripts/entra_provisioning.py"
        )

    if not _application_exists(client_id):
        raise ProvisionerBootstrapError(
            "Provisioner app from state was not found in Entra. "
            "Run: python3 scripts/entra_provisioning.py"
        )

    pem_bundle = _keychain_get_cert(tenant_id)
    if not pem_bundle:
        raise ProvisionerBootstrapError(
            "Provisioner certificate private key is missing locally. "
            "Run: python3 scripts/entra_provisioning.py"
        )
    return client_id, pem_bundle, tenant_id


def get_graph_token(
    required_values: list[str] | None = None,
    wait_for_propagation: bool = True,
    auto_provision: bool = True,
) -> str:
    """Get a Graph API access token via the Provisioner app's cert.

    Uses ``CertificateCredential`` — private key comes from Keychain,
    never from disk. Auto-provisions the app + cert on first run unless
    ``auto_provision`` is false.
    """
    if auto_provision:
        if required_values is None:
            required_values = build_required_permission_values()
        client_id, pem_bundle, tenant_id = ensure_app_registration(
            required_values,
            wait_for_propagation=wait_for_propagation,
        )
    else:
        client_id, pem_bundle, tenant_id = load_existing_app_registration()

    try:
        from azure.identity import CertificateCredential
    except ImportError as exc:
        raise ProvisionerBootstrapError(
            "azure-identity is required. Install with: pip install -e '.[provisioning]'"
        ) from exc

    credential = CertificateCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        certificate_data=pem_bundle.encode(),
    )
    return credential.get_token("https://graph.microsoft.com/.default").token


def get_bootstrap_graph_token(
    required_values: list[str] | None = None,
    wait_for_propagation: bool = True,
) -> str:
    """Get a Graph token, creating or repairing the provisioner app if needed."""
    return get_graph_token(
        required_values=required_values,
        wait_for_propagation=wait_for_propagation,
        auto_provision=True,
    )


def get_existing_graph_token() -> str:
    """Get a Graph token from an already-bootstrapped provisioner app."""
    return get_graph_token(wait_for_propagation=False, auto_provision=False)


# ---------------------------------------------------------------------------
# CLI entrypoint — standalone provisioner bootstrap
# ---------------------------------------------------------------------------


def bootstrap_cli() -> int:
    """Bootstrap the provisioner app and print summary."""
    try:
        required_values = build_required_permission_values()
        ensure_app_registration(required_values)
        print("")
        print("Provisioner bootstrap complete")
        print(f"  Permissions: {', '.join(required_values)}")
        return 0
    except ProvisionerBootstrapError as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(bootstrap_cli())
