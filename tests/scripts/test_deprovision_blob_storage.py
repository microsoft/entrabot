"""Tests for scripts/deprovision_blob_storage.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Load the script as a module
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deprovision_blob_storage.py"
spec = importlib.util.spec_from_file_location("deprovision_blob_storage", _SCRIPT)
deprov_mod = importlib.util.module_from_spec(spec)
sys.modules["deprovision_blob_storage"] = deprov_mod
assert spec.loader is not None
spec.loader.exec_module(deprov_mod)


def _ok(stdout: str = "") -> MagicMock:
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def _err(stderr: str = "boom", returncode: int = 1) -> MagicMock:
    return MagicMock(returncode=returncode, stdout="", stderr=stderr)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDeleteContainer:
    @patch.object(deprov_mod, "_run_az")
    def test_delete_success(self, mock_az):
        mock_az.return_value = _ok()
        deprov_mod.delete_container("myaccount", "mycontainer")
        args = mock_az.call_args[0][0]
        assert "storage" in args
        assert "container" in args
        assert "delete" in args
        assert "myaccount" in args
        assert "mycontainer" in args

    @patch.object(deprov_mod, "_run_az")
    def test_delete_failure_raises(self, mock_az):
        mock_az.return_value = _err("not found")
        with pytest.raises(RuntimeError, match="not found"):
            deprov_mod.delete_container("myaccount", "mycontainer")


class TestDeleteStorageAccount:
    @patch.object(deprov_mod, "_run_az")
    def test_delete_success(self, mock_az):
        mock_az.return_value = _ok()
        deprov_mod.delete_storage_account("myaccount", "entrabot-rg")
        args = mock_az.call_args[0][0]
        assert "storage" in args
        assert "account" in args
        assert "delete" in args

    @patch.object(deprov_mod, "_run_az")
    def test_delete_failure_raises(self, mock_az):
        mock_az.return_value = _err()
        with pytest.raises(RuntimeError):
            deprov_mod.delete_storage_account("myaccount", "entrabot-rg")


class TestDeleteResourceGroup:
    @patch.object(deprov_mod, "_run_az")
    def test_delete_success(self, mock_az):
        mock_az.return_value = _ok()
        deprov_mod.delete_resource_group("entrabot-rg")
        args = mock_az.call_args[0][0]
        assert "group" in args
        assert "delete" in args

    @patch.object(deprov_mod, "_run_az")
    def test_delete_failure_raises(self, mock_az):
        mock_az.return_value = _err()
        with pytest.raises(RuntimeError):
            deprov_mod.delete_resource_group("entrabot-rg")


class TestDeprovision:
    """Integration test for the deprovision() orchestrator."""

    @patch.object(deprov_mod, "_run_az")
    def test_container_only_default(self, mock_az):
        """Default: delete container only."""
        mock_az.return_value = _ok()
        deprov_mod.deprovision(
            storage_account="acct1",
            container="cont1",
            resource_group="rg1",
            delete_account=False,
            delete_resource_group=False,
        )
        # Should have called _run_az once (container delete)
        assert mock_az.call_count == 1
        args = mock_az.call_args[0][0]
        assert "container" in args
        assert "delete" in args

    @patch.object(deprov_mod, "_run_az")
    def test_delete_account_also(self, mock_az):
        """--delete-account deletes container + account."""
        mock_az.return_value = _ok()
        deprov_mod.deprovision(
            storage_account="acct1",
            container="cont1",
            resource_group="rg1",
            delete_account=True,
            delete_resource_group=False,
        )
        assert mock_az.call_count == 2

    @patch.object(deprov_mod, "_run_az")
    def test_delete_everything(self, mock_az):
        """--delete-resource-group implies account + container too."""
        mock_az.return_value = _ok()
        deprov_mod.deprovision(
            storage_account="acct1",
            container="cont1",
            resource_group="rg1",
            delete_account=True,
            delete_resource_group=True,
        )
        assert mock_az.call_count == 3


class TestMainCLI:
    @patch.object(deprov_mod, "deprovision")
    def test_requires_storage_account(self, mock_deprov):
        """Must provide --storage-account."""
        with pytest.raises(SystemExit) as exc_info:
            deprov_mod.main(["--container", "c"])
        assert exc_info.value.code == 2

    @patch.object(deprov_mod, "deprovision")
    def test_requires_container(self, mock_deprov):
        """Must provide --container."""
        with pytest.raises(SystemExit) as exc_info:
            deprov_mod.main(["--storage-account", "a"])
        assert exc_info.value.code == 2

    @patch.object(deprov_mod, "deprovision")
    @patch("builtins.input", return_value="yes")
    def test_happy_path(self, mock_input, mock_deprov):
        rc = deprov_mod.main([
            "--storage-account", "acct1",
            "--container", "cont1",
            "--yes",
        ])
        assert rc == 0
        mock_deprov.assert_called_once()

    @patch.object(deprov_mod, "deprovision")
    def test_dry_run(self, mock_deprov, capsys):
        rc = deprov_mod.main([
            "--storage-account", "acct1",
            "--container", "cont1",
            "--dry-run",
        ])
        assert rc == 0
        mock_deprov.assert_not_called()
        out = capsys.readouterr().out
        assert "dry run" in out.lower() or "DRY RUN" in out
