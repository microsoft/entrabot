"""Tests for scripts/provision_blob_storage.py (ADR-005, Phase 5).

The script orchestrates ``az`` CLI calls. We mock ``subprocess.run`` and
verify (a) the deterministic name helpers, (b) the right ``az`` commands
get issued in the right order, (c) idempotency — i.e. ``show`` succeeds
short-circuits ``create``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Load the script as a module without packaging it
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "provision_blob_storage.py"
spec = importlib.util.spec_from_file_location("provision_blob_storage", _SCRIPT)
provision_blob_storage = importlib.util.module_from_spec(spec)
sys.modules["provision_blob_storage"] = provision_blob_storage
assert spec.loader is not None
spec.loader.exec_module(provision_blob_storage)


def _ok(stdout: str = "") -> MagicMock:
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def _err(stderr: str = "boom", returncode: int = 1) -> MagicMock:
    return MagicMock(returncode=returncode, stdout="", stderr=stderr)


class TestNameHelpers:
    def test_storage_account_name_is_deterministic(self) -> None:
        a = provision_blob_storage.storage_account_name_for_tenant("tid-123")
        b = provision_blob_storage.storage_account_name_for_tenant("tid-123")
        assert a == b

    def test_storage_account_name_is_lowercase_alnum_within_24(self) -> None:
        name = provision_blob_storage.storage_account_name_for_tenant("tid-123")
        assert 3 <= len(name) <= 24
        assert name == name.lower()
        assert name.isalnum()

    def test_storage_account_name_differs_per_tenant(self) -> None:
        a = provision_blob_storage.storage_account_name_for_tenant("tid-1")
        b = provision_blob_storage.storage_account_name_for_tenant("tid-2")
        assert a != b

    def test_container_name_for_agent_user(self) -> None:
        oid = "ABCD-1234-5678-90AB"
        name = provision_blob_storage.container_name_for_agent_user(oid)
        assert name.startswith("agent-")
        assert name == name.lower()

    def test_random_storage_account_name_is_unique_per_call(self) -> None:
        a = provision_blob_storage.random_storage_account_name_for_tenant("tid-x")
        b = provision_blob_storage.random_storage_account_name_for_tenant("tid-x")
        assert a != b

    def test_random_storage_account_name_is_valid(self) -> None:
        name = provision_blob_storage.random_storage_account_name_for_tenant("tid-x")
        assert 3 <= len(name) <= 24
        assert name == name.lower()
        assert name.isalnum()
        # Same tenant should keep the same prefix segment.
        a = provision_blob_storage.random_storage_account_name_for_tenant("tid-x")
        assert a[:15] == name[:15]  # entclaw + 8 hex of tenant


class TestNameValidators:
    def test_validate_storage_account_accepts_valid(self) -> None:
        provision_blob_storage.validate_storage_account_name("entclaw1234abcd")

    @pytest.mark.parametrize(
        "bad",
        [
            "ab",                       # too short
            "a" * 25,                   # too long
            "Entclaw",                  # uppercase
            "ent-claw",                 # dash not allowed
            "entclaw_x",                # underscore
        ],
    )
    def test_validate_storage_account_rejects_invalid(self, bad: str) -> None:
        with pytest.raises(ValueError):
            provision_blob_storage.validate_storage_account_name(bad)

    def test_validate_container_accepts_valid(self) -> None:
        provision_blob_storage.validate_container_name("agent-abc-123")
        provision_blob_storage.validate_container_name("abc")

    @pytest.mark.parametrize(
        "bad",
        [
            "ab",                       # too short
            "Agent",                    # uppercase
            "-leading",                 # leading dash
            "trailing-",                # trailing dash
            "double--dash",             # consecutive dashes
            "with_underscore",          # underscore
        ],
    )
    def test_validate_container_rejects_invalid(self, bad: str) -> None:
        with pytest.raises(ValueError):
            provision_blob_storage.validate_container_name(bad)


class TestEndToEndProvisionAllNew:
    """Resource group, storage account, container all need creating."""

    def test_creates_in_order_and_assigns_rbac(self) -> None:
        results = [
            _err("not found"),  # group show
            _ok(),              # group create
            _err("not found"),  # account show
            _ok(),              # account create
            _err("not found"),  # container show
            _ok(),              # container create
            _ok("/subscriptions/sub/resourceGroups/entraclaw-rg/providers/Microsoft.Storage/storageAccounts/acct"),  # noqa: E501
            _ok(),              # role assignment create
        ]
        with patch.object(provision_blob_storage, "_run_az", side_effect=results) as m:
            endpoint, container = provision_blob_storage.provision(
                tenant_id="tid-123",
                agent_user_object_id="oid-abc",
            )

        assert endpoint.startswith("https://")
        assert endpoint.endswith(".blob.core.windows.net")
        assert container.startswith("agent-")

        all_calls = [c.args[0] for c in m.call_args_list]
        assert any(args[:3] == ["group", "create", "--name"] for args in all_calls)
        assert any(args[:3] == ["storage", "account", "create"] for args in all_calls)
        assert any(args[:3] == ["storage", "container", "create"] for args in all_calls)
        assert any(args[:3] == ["role", "assignment", "create"] for args in all_calls)

    def test_idempotent_when_everything_exists(self) -> None:
        results = [
            _ok(),  # group show
            _ok(),  # account show
            _ok(),  # container show
            _ok("/subscriptions/sub/resourceGroups/entraclaw-rg/providers/Microsoft.Storage/storageAccounts/acct"),  # noqa: E501
            _ok(),  # role assignment create (idempotent itself)
        ]
        with patch.object(provision_blob_storage, "_run_az", side_effect=results) as m:
            provision_blob_storage.provision(
                tenant_id="tid-123",
                agent_user_object_id="oid-abc",
            )
        all_calls = [c.args[0] for c in m.call_args_list]
        assert not any(args[:3] == ["group", "create", "--name"] for args in all_calls)
        assert not any(args[:3] == ["storage", "account", "create"] for args in all_calls)
        assert not any(args[:3] == ["storage", "container", "create"] for args in all_calls)


class TestRoleAssignmentBenignErrors:
    def test_already_exists_treated_as_success(self) -> None:
        results = [
            _ok(),  # group show
            _ok(),  # account show
            _ok(),  # container show
            _ok("/subscriptions/.../accounts/x"),  # show -o tsv
            _err("RoleAssignmentExists: ..."),  # role assignment create
        ]
        with patch.object(provision_blob_storage, "_run_az", side_effect=results):
            # Should NOT raise
            provision_blob_storage.provision(
                tenant_id="tid-123", agent_user_object_id="oid-abc"
            )


class TestFailures:
    def test_group_create_failure_raises_runtime_error(self) -> None:
        results = [_err("not found"), _err("auth blocked")]
        with (
            patch.object(provision_blob_storage, "_run_az", side_effect=results),
            pytest.raises(RuntimeError, match="az group create failed"),
        ):
            provision_blob_storage.provision(
                tenant_id="tid-123", agent_user_object_id="oid-abc"
            )


class TestMain:
    def test_main_prints_kv_lines_on_success(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch.object(
            provision_blob_storage,
            "provision",
            return_value=("https://acct.blob.core.windows.net", "agent-oid"),
        ):
            rc = provision_blob_storage.main(
                ["--tenant-id", "tid", "--agent-user-object-id", "oid"]
            )
        assert rc == 0
        out = capsys.readouterr().out.splitlines()
        assert "BLOB_ENDPOINT=https://acct.blob.core.windows.net" in out
        assert "BLOB_CONTAINER=agent-oid" in out

    def test_main_returns_nonzero_on_failure(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch.object(
            provision_blob_storage,
            "provision",
            side_effect=RuntimeError("nope"),
        ):
            rc = provision_blob_storage.main(
                ["--tenant-id", "tid", "--agent-user-object-id", "oid"]
            )
        assert rc == 1


class TestOverrides:
    """Tests for --with-storage-account, --with-container, --create-new-storage."""

    def test_with_storage_account_uses_provided_name(self) -> None:
        results = [
            _ok(),  # group show
            _ok(),  # account show (the override name)
            _ok(),  # container show
            _ok("/subscriptions/sub/resourceGroups/entraclaw-rg/providers/Microsoft.Storage/storageAccounts/myaccount"),  # noqa: E501
            _ok(),  # role assignment create
        ]
        with patch.object(provision_blob_storage, "_run_az", side_effect=results) as m:
            endpoint, _ = provision_blob_storage.provision(
                tenant_id="tid-123",
                agent_user_object_id="oid-abc",
                storage_account="myaccount",
            )
        assert endpoint == "https://myaccount.blob.core.windows.net"
        # Verify the override name was the one passed to az
        account_show_args = m.call_args_list[1].args[0]
        assert "myaccount" in account_show_args

    def test_with_container_uses_provided_name(self) -> None:
        results = [
            _ok(),  # group show
            _ok(),  # account show
            _ok(),  # container show — should be using override name
            _ok("/subscriptions/.../accounts/x"),
            _ok(),  # role assignment
        ]
        with patch.object(provision_blob_storage, "_run_az", side_effect=results) as m:
            _, container = provision_blob_storage.provision(
                tenant_id="tid-123",
                agent_user_object_id="oid-abc",
                container="my-shared-container",
            )
        assert container == "my-shared-container"
        container_show_args = m.call_args_list[2].args[0]
        assert "my-shared-container" in container_show_args

    def test_create_new_storage_uses_random_name(self) -> None:
        results = [
            _err("not found"),  # group show
            _ok(),              # group create
            _err("not found"),  # account show — random name doesn't exist yet
            _ok(),              # account create
            _err("not found"),  # container show
            _ok(),              # container create
            _ok("/subscriptions/.../accounts/x"),
            _ok(),              # role assignment
        ]
        with patch.object(provision_blob_storage, "_run_az", side_effect=results):
            endpoint, _ = provision_blob_storage.provision(
                tenant_id="tid-123",
                agent_user_object_id="oid-abc",
                create_new_storage=True,
            )
        # Random name should differ from the deterministic one
        deterministic = provision_blob_storage.storage_account_name_for_tenant("tid-123")
        assert f"https://{deterministic}.blob" not in endpoint

    def test_storage_account_and_create_new_storage_mutex_at_function(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            provision_blob_storage.provision(
                tenant_id="tid",
                agent_user_object_id="oid",
                storage_account="myaccount",
                create_new_storage=True,
            )

    def test_invalid_storage_account_name_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid storage account name"):
            provision_blob_storage.provision(
                tenant_id="tid",
                agent_user_object_id="oid",
                storage_account="UPPERCASE",
            )

    def test_invalid_container_name_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid container name"):
            provision_blob_storage.provision(
                tenant_id="tid",
                agent_user_object_id="oid",
                container="bad_underscore",
            )


class TestMainMutex:
    def test_cli_mutex_exits_nonzero(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            provision_blob_storage.main(
                [
                    "--tenant-id", "tid",
                    "--agent-user-object-id", "oid",
                    "--with-storage-account", "acct",
                    "--create-new-storage",
                ]
            )
        assert exc_info.value.code != 0
        err = capsys.readouterr().err
        assert "mutually exclusive" in err

    def test_cli_with_storage_account_threads_through(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch.object(
            provision_blob_storage,
            "provision",
            return_value=("https://x.blob.core.windows.net", "agent-x"),
        ) as m:
            rc = provision_blob_storage.main(
                [
                    "--tenant-id", "tid",
                    "--agent-user-object-id", "oid",
                    "--with-storage-account", "myaccount",
                    "--with-container", "mycontainer",
                ]
            )
        assert rc == 0
        kwargs = m.call_args.kwargs
        assert kwargs["storage_account"] == "myaccount"
        assert kwargs["container"] == "mycontainer"
        assert kwargs["create_new_storage"] is False

    def test_cli_create_new_storage_threads_through(self) -> None:
        with patch.object(
            provision_blob_storage,
            "provision",
            return_value=("https://x.blob.core.windows.net", "agent-x"),
        ) as m:
            rc = provision_blob_storage.main(
                [
                    "--tenant-id", "tid",
                    "--agent-user-object-id", "oid",
                    "--create-new-storage",
                ]
            )
        assert rc == 0
        assert m.call_args.kwargs["create_new_storage"] is True
        assert m.call_args.kwargs["storage_account"] is None
