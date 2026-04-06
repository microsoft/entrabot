#!/usr/bin/env python3
"""
Shared helpers for Entra Graph provisioning.

Centralizes the dedicated provisioner app registration used for Agent Identity
Blueprint and Agent Identity creation via the Graph beta API.

Pattern borrowed from agent-foundry-poc — the provisioner is a dedicated app with
client_credentials flow, because Azure CLI tokens include Directory.AccessAsUser.All
which the Agent Identity APIs explicitly reject (hard 403).

Usage:
    # As a library (from create_entra_agent_ids.py):
    from entra_provisioning import get_graph_token, run_az, get_signed_in_user_id

    # As a standalone bootstrap:
    python3 scripts/entra_provisioning.py
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

MS_GRAPH_API_ID = "00000003-0000-0000-c000-000000000000"
PROVISIONER_APP_DISPLAY_NAME = "Openclaw Agent ID Provisioner"

# Application.ReadWrite.All — required for Blueprint CRUD
APP_READWRITE_ALL_ID = "1bfefb4e-e0b5-418b-a88f-73c46d2cc8e9"

BASE_PERMISSION_VALUES = [
    "Application.ReadWrite.All",
    "LicenseAssignment.ReadWrite.All",
    "Organization.Read.All",
]

AGENT_ID_PERMISSION_MATCHERS = [
    "AgentIdentity",
    "AgentIdUser",
]


class ProvisionerBootstrapError(RuntimeError):
    """Raised when the provisioner app cannot be created or consented."""


# ---------------------------------------------------------------------------
# State persistence (replaces azd env for openclaw)
# ---------------------------------------------------------------------------

_STATE_FILE = Path(__file__).resolve().parent.parent / ".openclaw-state.json"


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
    """Read a value from the local provision state file."""
    return _load_state().get(key)


def set_state(key: str, value: str) -> None:
    """Write a value to the local provision state file."""
    state = _load_state()
    state[key] = value
    _save_state(state)


def clear_state(key: str) -> None:
    """Remove a key from the local provision state file."""
    state = _load_state()
    state.pop(key, None)
    _save_state(state)


# ---------------------------------------------------------------------------
# az CLI helpers
# ---------------------------------------------------------------------------


def run_az(args: list[str], capture: bool = True) -> tuple[int, str, str]:
    """Run an az CLI command, return (returncode, stdout, stderr)."""
    result = subprocess.run(["az"] + args, capture_output=capture, text=True)
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
        return [f"https://graph.microsoft.com/beta/users/{user_id}"]
    return []


# ---------------------------------------------------------------------------
# Permission discovery
# ---------------------------------------------------------------------------


def _load_graph_app_roles() -> list[dict]:
    """Load all app roles from the Microsoft Graph service principal."""
    rc, out, err = run_az([
        "ad", "sp", "show", "--id", MS_GRAPH_API_ID,
        "--query", "appRoles[].{id:id,value:value}",
        "-o", "json",
    ])
    if rc != 0 or not out:
        raise ProvisionerBootstrapError(err or "could not query Microsoft Graph app roles")
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise ProvisionerBootstrapError(f"failed to parse Graph role list: {exc}") from exc


def resolve_graph_permissions() -> dict[str, str]:
    """Return a map of permission value -> role ID for all Graph app roles."""
    roles = _load_graph_app_roles()
    return {
        role["value"]: role["id"]
        for role in roles
        if role.get("value") and role.get("id")
    }


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
    rc, out, err = run_az([
        "ad", "app", "show",
        "--id", client_id,
        "--query",
        "requiredResourceAccess[?resourceAppId=='00000003-0000-0000-c000-000000000000']"
        ".resourceAccess[].id",
        "-o", "json",
    ])
    if rc != 0 or not out:
        if err:
            raise ProvisionerBootstrapError(err)
        return set()
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ProvisionerBootstrapError(f"failed to parse existing app permissions: {exc}") from exc
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


def _ensure_permissions_and_consent(client_id: str, required_values: list[str]) -> None:
    """Add Graph permissions and grant admin consent for the provisioner app."""
    _ensure_service_principal(client_id)

    permission_specs = _resolve_permission_specs(required_values)
    existing_role_ids = _get_existing_permission_role_ids(client_id)
    missing_specs = [
        spec for _, spec in permission_specs
        if spec.split("=", 1)[0] not in existing_role_ids
    ]
    print(f"  Ensuring {len(permission_specs)} Graph application permissions on provisioner app...")

    if missing_specs:
        cmd = [
            "ad", "app", "permission", "add",
            "--id", client_id,
            "--api", MS_GRAPH_API_ID,
            "--api-permissions",
        ] + missing_specs
        rc, _, err = run_az(cmd)
        if rc != 0 and "already exists" not in err.lower():
            lowered = err.lower()
            if any(term in lowered for term in ["insufficient", "authorization", "privilege"]):
                _print_admin_required(
                    "add the required Microsoft Graph application permissions", err
                )
            raise ProvisionerBootstrapError(err or "permission add failed")
    else:
        print("  Provisioner app already has the required Graph permissions")

    # Grant admin consent with retry
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
            return
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
    """Ensure the dedicated provisioner app exists with correct permissions.

    Returns (client_id, client_secret, tenant_id).
    Uses state file for persistence across runs.
    """
    # Resolve tenant ID
    tenant_id = os.environ.get("OPENCLAW_TENANT_ID") or get_state("TENANT_ID")
    if not tenant_id:
        rc, out, err = run_az(["account", "show", "--query", "tenantId", "-o", "tsv"])
        if rc != 0 or not out:
            raise ProvisionerBootstrapError(
                err or "cannot determine tenant ID; run 'az login' first"
            )
        tenant_id = out
        set_state("TENANT_ID", tenant_id)

    # Check for cached provisioner credentials
    client_id = get_state("PROVISIONER_CLIENT_ID")
    client_secret = get_state("PROVISIONER_CLIENT_SECRET")

    # Validate cached app still exists
    if client_id and not _application_exists(client_id):
        print(f"  Cached provisioner app is stale: {client_id}")
        client_id = None
        client_secret = None
        clear_state("PROVISIONER_CLIENT_ID")
        clear_state("PROVISIONER_CLIENT_SECRET")

    # Find or create provisioner app
    if not client_id:
        rc, out, _ = run_az([
            "ad", "app", "list",
            "--display-name", PROVISIONER_APP_DISPLAY_NAME,
            "--query", "[0].appId",
            "-o", "tsv",
        ])
        if rc == 0 and out:
            client_id = out
            print(f"  Found existing provisioner app: {client_id}")
            set_state("PROVISIONER_CLIENT_ID", client_id)
        else:
            print("  Creating dedicated Entra provisioner app registration...")
            rc, out, err = run_az([
                "ad", "app", "create",
                "--display-name", PROVISIONER_APP_DISPLAY_NAME,
                "--sign-in-audience", "AzureADMyOrg",
                "--query", "appId",
                "-o", "tsv",
            ])
            if rc != 0 or not out:
                lowered = (err or "").lower()
                if any(
                    term in lowered
                    for term in ["insufficient", "authorization", "privilege", "permission"]
                ):
                    _print_admin_required(
                        "create the dedicated provisioner app registration", err
                    )
                raise ProvisionerBootstrapError(err or "app registration creation failed")
            client_id = out
            print(f"  Created provisioner app: {client_id}")
            set_state("PROVISIONER_CLIENT_ID", client_id)

    # Ensure permissions and admin consent
    _ensure_permissions_and_consent(client_id, required_values)

    # Create secret only if not cached
    if not client_secret:
        print("  Creating provisioner app client secret...")
        rc, out, err = run_az([
            "ad", "app", "credential", "reset",
            "--id", client_id,
            "--append",
            "--years", "1",
            "-o", "json",
        ])
        if rc != 0 or not out:
            lowered = (err or "").lower()
            if any(
                term in lowered
                for term in ["insufficient", "authorization", "privilege", "permission"]
            ):
                _print_admin_required("create a client secret on the provisioner app", err)
            raise ProvisionerBootstrapError(err or "client secret creation failed")
        try:
            secret_data = json.loads(out)
            client_secret = secret_data["password"]
        except (json.JSONDecodeError, KeyError) as exc:
            raise ProvisionerBootstrapError(
                f"failed to extract secret from credential reset output: {exc}"
            ) from exc
        set_state("PROVISIONER_CLIENT_SECRET", client_secret)
        print("  Stored provisioner app secret in state file")
    else:
        print("  Using cached provisioner app secret")

    if wait_for_propagation:
        print("  Waiting 30s for Graph permission propagation...")
        time.sleep(30)

    return client_id, client_secret, tenant_id


def get_graph_token(
    required_values: list[str] | None = None,
    wait_for_propagation: bool = True,
) -> str:
    """Get a Graph API access token via the dedicated provisioner app.

    Auto-creates the provisioner app registration if needed.
    """
    if required_values is None:
        required_values = build_required_permission_values()

    client_id, client_secret, tenant_id = ensure_app_registration(
        required_values,
        wait_for_propagation=wait_for_propagation,
    )

    try:
        from azure.identity import ClientSecretCredential
    except ImportError as exc:
        raise ProvisionerBootstrapError(
            "azure-identity is required. Install with: pip install azure-identity"
        ) from exc

    credential = ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
    )
    return credential.get_token("https://graph.microsoft.com/.default").token


# ---------------------------------------------------------------------------
# CLI entrypoint — standalone provisioner bootstrap
# ---------------------------------------------------------------------------


def bootstrap_cli() -> int:
    """Bootstrap the provisioner app and print summary."""
    try:
        required_values = build_required_permission_values()
        client_id, _, tenant_id = ensure_app_registration(required_values)
        print("")
        print("Provisioner bootstrap complete")
        print(f"  Tenant:      {tenant_id}")
        print(f"  Client ID:   {client_id}")
        print(f"  Permissions: {', '.join(required_values)}")
        return 0
    except ProvisionerBootstrapError as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(bootstrap_cli())
