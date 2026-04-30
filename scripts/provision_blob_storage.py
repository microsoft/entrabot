"""Provision Azure Blob Storage for EntraClaw agent memory (ADR-005, Phase 5).

Idempotent — designed to be called from ``scripts/setup.sh`` on every run.
Already-provisioned resources are detected and reused; only missing pieces
get created.

What it does, in order:
  1. Ensure resource group ``entraclaw-rg`` exists (in the user's default
     subscription, in a sensible region).
  2. Ensure a Storage Account exists (one per tenant — name derived from
     the tenant ID so multiple devs in the same tenant converge on the
     same account without a global-unique-name race). Override with
     ``--with-storage-account NAME`` to attach to an existing/external
     account, or ``--create-new-storage`` to force a fresh
     randomly-suffixed account even when the deterministic-name one
     already exists.
  3. Ensure a container exists for *this* Agent User (named with the
     Agent User's object ID per ADR §"Resolved decisions" #1). Override
     with ``--with-container NAME``.
  4. Assign ``Storage Blob Data Contributor`` to the Agent User on the
     container — scoped to the container, not the account, so each Agent
     User only sees its own slice.

Prints two lines on stdout in ``KEY=value`` form so the calling shell
can grab them:

    BLOB_ENDPOINT=https://entclaw<hash>.blob.core.windows.net
    BLOB_CONTAINER=agent-<oid>

All progress / informational output goes to stderr.

Requires the user to be ``az login``'d already.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import secrets
import shutil
import subprocess
import sys

# Module-level constants
RESOURCE_GROUP = "entraclaw-rg"
DEFAULT_LOCATION = "eastus"
STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE = "Storage Blob Data Contributor"


def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def _run_az(args: list[str], *, capture: bool = True) -> subprocess.CompletedProcess:
    """Run an ``az`` subcommand. Returns the CompletedProcess.

    *args* should NOT include the leading ``"az"`` token. We capture both
    stdout and stderr so callers can inspect on failure.
    """
    az_bin = shutil.which("az") or "az"
    cmd = [az_bin, *args]
    return subprocess.run(  # noqa: S603 — args are constructed in this module only
        cmd,
        capture_output=capture,
        text=True,
        check=False,
    )


def storage_account_name_for_tenant(tenant_id: str) -> str:
    """Derive a globally-unique-but-deterministic Storage Account name.

    Azure rules: 3-24 chars, lowercase letters and numbers only. We use a
    short prefix + the first 16 hex chars of sha256(tenant_id) so the same
    tenant always converges on the same account.
    """
    digest = hashlib.sha256(tenant_id.encode()).hexdigest()[:16]
    return f"entclaw{digest}"


def random_storage_account_name_for_tenant(tenant_id: str) -> str:
    """Generate a tenant-scoped but unique Storage Account name.

    Used by ``--create-new-storage`` to force a fresh account even when
    the deterministic-name one already exists. Prefix + 8 hex of tenant
    hash + 8 random hex = 23 chars total, well under Azure's 24-char cap.
    """
    digest = hashlib.sha256(tenant_id.encode()).hexdigest()[:8]
    suffix = secrets.token_hex(4)
    return f"entclaw{digest}{suffix}"


def container_name_for_agent_user(agent_user_object_id: str) -> str:
    """Derive a container name for an Agent User.

    Azure rules: 3-63 chars, lowercase, alphanumeric + dashes. Object IDs
    are GUIDs (lowercase hex with dashes) — that's already valid input.
    """
    return f"agent-{agent_user_object_id.lower()}"


_STORAGE_ACCOUNT_RE = re.compile(r"^[a-z0-9]{3,24}$")
_CONTAINER_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{1,61}[a-z0-9])?$")


def validate_storage_account_name(name: str) -> None:
    """Azure: 3-24 chars, lowercase letters and digits only."""
    if not _STORAGE_ACCOUNT_RE.match(name):
        raise ValueError(
            f"Invalid storage account name {name!r}: must be 3-24 chars, "
            "lowercase letters and digits only."
        )


def validate_container_name(name: str) -> None:
    """Azure: 3-63 chars, lowercase, alphanumeric + dashes (no consecutive)."""
    if not _CONTAINER_RE.match(name) or "--" in name:
        raise ValueError(
            f"Invalid container name {name!r}: must be 3-63 chars, lowercase "
            "alphanumeric and dashes, no leading/trailing/consecutive dashes."
        )


def ensure_resource_group(name: str, location: str) -> None:
    _eprint(f"  • Ensuring resource group '{name}' in '{location}'...")
    res = _run_az(["group", "show", "--name", name])
    if res.returncode == 0:
        _eprint("    ✓ exists")
        return
    res = _run_az(["group", "create", "--name", name, "--location", location])
    if res.returncode != 0:
        raise RuntimeError(f"az group create failed: {res.stderr.strip()}")
    _eprint("    ✓ created")


def ensure_storage_account(name: str, resource_group: str, location: str) -> None:
    _eprint(f"  • Ensuring storage account '{name}'...")
    res = _run_az(
        ["storage", "account", "show", "--name", name, "--resource-group", resource_group]
    )
    if res.returncode == 0:
        _eprint("    ✓ exists")
        return
    res = _run_az(
        [
            "storage", "account", "create",
            "--name", name,
            "--resource-group", resource_group,
            "--location", location,
            "--sku", "Standard_LRS",
            "--kind", "StorageV2",
            "--allow-blob-public-access", "false",
            "--min-tls-version", "TLS1_2",
        ]
    )
    if res.returncode != 0:
        raise RuntimeError(f"az storage account create failed: {res.stderr.strip()}")
    _eprint("    ✓ created")


def ensure_container(account: str, container: str) -> None:
    _eprint(f"  • Ensuring container '{container}' on '{account}'...")
    # --auth-mode login uses the AAD identity instead of account keys
    res = _run_az(
        [
            "storage", "container", "show",
            "--account-name", account,
            "--name", container,
            "--auth-mode", "login",
        ]
    )
    if res.returncode == 0:
        _eprint("    ✓ exists")
        return
    res = _run_az(
        [
            "storage", "container", "create",
            "--account-name", account,
            "--name", container,
            "--auth-mode", "login",
        ]
    )
    if res.returncode != 0:
        raise RuntimeError(f"az storage container create failed: {res.stderr.strip()}")
    _eprint("    ✓ created")


def get_storage_account_id(account: str, resource_group: str) -> str:
    res = _run_az(
        ["storage", "account", "show",
         "--name", account, "--resource-group", resource_group,
         "--query", "id", "-o", "tsv"]
    )
    if res.returncode != 0:
        raise RuntimeError(f"az storage account show failed: {res.stderr.strip()}")
    return res.stdout.strip()


def assign_container_rbac(
    account: str, container: str, resource_group: str, principal_object_id: str
) -> None:
    """Assign Storage Blob Data Contributor on the container only."""
    account_id = get_storage_account_id(account, resource_group)
    scope = f"{account_id}/blobServices/default/containers/{container}"
    _eprint(
        f"  • Assigning '{STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE}' "
        f"to {principal_object_id} on container scope..."
    )
    # Idempotency: az role assignment create succeeds (returns the existing
    # assignment) when the assignment already exists, so no pre-check needed.
    res = _run_az(
        [
            "role", "assignment", "create",
            "--assignee-object-id", principal_object_id,
            "--assignee-principal-type", "User",
            "--role", STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE,
            "--scope", scope,
        ]
    )
    if res.returncode != 0:
        # 'PrincipalNotFound' / 'RoleAssignmentExists' are the common
        # benign cases — surface them as warnings, not failures.
        msg = (res.stderr + res.stdout).lower()
        if "already exists" in msg or "roleassignmentexists" in msg:
            _eprint("    ✓ already assigned")
            return
        raise RuntimeError(f"az role assignment create failed: {res.stderr.strip()}")
    _eprint("    ✓ assigned")


def blob_endpoint_for_account(account: str) -> str:
    return f"https://{account}.blob.core.windows.net"


def provision(
    *,
    tenant_id: str,
    agent_user_object_id: str,
    location: str = DEFAULT_LOCATION,
    storage_account: str | None = None,
    container: str | None = None,
    create_new_storage: bool = False,
) -> tuple[str, str]:
    """Run the full provisioning flow.

    The storage account name is chosen as follows, in order:
      1. ``storage_account`` argument, if provided (overrides everything).
      2. A randomly-suffixed name if ``create_new_storage`` is True.
      3. The deterministic per-tenant name (default behavior).

    The container name defaults to ``agent-<agent_user_object_id>`` and
    can be overridden with ``container``.

    Mutex: ``storage_account`` and ``create_new_storage`` cannot both be
    set; raises :class:`ValueError` if they are.

    Returns ``(blob_endpoint, container_name)`` on success.
    Raises :class:`RuntimeError` with the underlying ``az`` stderr on any
    step's failure.
    """
    if storage_account is not None and create_new_storage:
        raise ValueError(
            "storage_account and create_new_storage are mutually exclusive."
        )

    if storage_account is not None:
        validate_storage_account_name(storage_account)
        account = storage_account
    elif create_new_storage:
        account = random_storage_account_name_for_tenant(tenant_id)
    else:
        account = storage_account_name_for_tenant(tenant_id)

    if container is not None:
        validate_container_name(container)
        container_name = container
    else:
        container_name = container_name_for_agent_user(agent_user_object_id)

    ensure_resource_group(RESOURCE_GROUP, location)
    ensure_storage_account(account, RESOURCE_GROUP, location)
    ensure_container(account, container_name)
    assign_container_rbac(account, container_name, RESOURCE_GROUP, agent_user_object_id)

    return blob_endpoint_for_account(account), container_name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Provision Azure Blob Storage for an EntraClaw Agent User."
    )
    parser.add_argument("--tenant-id", required=True, help="Entra tenant ID (GUID).")
    parser.add_argument(
        "--agent-user-object-id",
        required=True,
        help="Agent User object ID (GUID).",
    )
    parser.add_argument(
        "--location", default=DEFAULT_LOCATION, help=f"Azure region (default {DEFAULT_LOCATION})."
    )
    parser.add_argument(
        "--with-storage-account",
        default=None,
        metavar="NAME",
        help=(
            "Use the named storage account instead of the deterministic "
            "per-tenant default. Will be created if it doesn't exist. "
            "Mutually exclusive with --create-new-storage."
        ),
    )
    parser.add_argument(
        "--with-container",
        default=None,
        metavar="NAME",
        help=(
            "Use the named container instead of agent-<oid>. Will be "
            "created if it doesn't exist."
        ),
    )
    parser.add_argument(
        "--create-new-storage",
        action="store_true",
        help=(
            "Force creation of a fresh randomly-suffixed storage account "
            "even when the deterministic-name one already exists. Mutually "
            "exclusive with --with-storage-account."
        ),
    )
    args = parser.parse_args(argv)

    if args.create_new_storage and args.with_storage_account is not None:
        parser.error(
            "--create-new-storage and --with-storage-account are mutually exclusive."
        )

    try:
        endpoint, container = provision(
            tenant_id=args.tenant_id,
            agent_user_object_id=args.agent_user_object_id,
            location=args.location,
            storage_account=args.with_storage_account,
            container=args.with_container,
            create_new_storage=args.create_new_storage,
        )
    except (RuntimeError, ValueError) as exc:
        _eprint(f"ERROR: {exc}")
        return 1

    # KEY=value lines for the calling shell to capture
    print(f"BLOB_ENDPOINT={endpoint}")
    print(f"BLOB_CONTAINER={container}")

    # Cost projection (printed once at setup completion per ADR)
    _eprint("")
    _eprint("  Estimated monthly cost (typical workload): ~$0.05–$0.50/mo per Agent User")
    _eprint("  (Standard_LRS, <1 GB blob storage, <10K transactions/day)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
