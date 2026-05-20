"""Tests for scripts/health_check.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "health_check.py"
spec = importlib.util.spec_from_file_location("health_check", _SCRIPT)
hc_mod = importlib.util.module_from_spec(spec)
sys.modules["health_check"] = hc_mod

_status_stub = MagicMock()
_status_stub.main.return_value = 0


def _load_module():
    sys.modules["show_agent_status"] = _status_stub
    spec.loader.exec_module(hc_mod)


class TestHealthCheckWrapper:
    """health_check.py delegates to show_agent_status.py health-only mode."""

    def test_default_adds_health_only(self):
        _load_module()
        with patch.object(hc_mod.show_agent_status, "main", return_value=0) as main:
            rc = hc_mod.main([])
        assert rc == 0
        main.assert_called_once_with(["--health-only"])

    def test_forwards_extra_args_before_health_only(self):
        _load_module()
        with patch.object(hc_mod.show_agent_status, "main", return_value=1) as main:
            rc = hc_mod.main(["--json"])
        assert rc == 1
        main.assert_called_once_with(["--json", "--health-only"])

    def test_help_does_not_force_health_only(self):
        _load_module()
        with patch.object(hc_mod.show_agent_status, "main", return_value=0) as main:
            rc = hc_mod.main(["--help"])
        assert rc == 0
        main.assert_called_once_with(["--help"])
