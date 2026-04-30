"""Tests for Dev Tunnel subprocess management."""

from unittest.mock import MagicMock, patch

import pytest

from entraclaw.bot.tunnel import TunnelError, TunnelManager


class TestTunnelManager:
    def test_start_returns_url(self) -> None:
        mock_proc = MagicMock()
        mock_proc.stdout.readline.return_value = (
            b"Connect via browser: https://abc123.devtunnels.ms\n"
        )
        mock_proc.poll.return_value = None
        with patch("subprocess.Popen", return_value=mock_proc):
            mgr = TunnelManager(port=3978)
            url = mgr.start()
        assert url == "https://abc123.devtunnels.ms"
        assert mgr.is_running

    def test_start_raises_if_devtunnel_not_found(self) -> None:
        with patch("subprocess.Popen", side_effect=FileNotFoundError("devtunnel not found")):
            mgr = TunnelManager(port=3978)
            with pytest.raises(TunnelError, match="devtunnel CLI not found"):
                mgr.start()

    def test_start_raises_if_process_exits_immediately(self) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1
        mock_proc.stderr.read.return_value = b"auth failed"
        with patch("subprocess.Popen", return_value=mock_proc):
            mgr = TunnelManager(port=3978)
            with pytest.raises(TunnelError, match="auth failed"):
                mgr.start()

    def test_stop_terminates_process(self) -> None:
        mock_proc = MagicMock()
        mock_proc.stdout.readline.return_value = b"Connect via browser: https://x.devtunnels.ms\n"
        mock_proc.poll.return_value = None
        with patch("subprocess.Popen", return_value=mock_proc):
            mgr = TunnelManager(port=3978)
            mgr.start()
            mgr.stop()
        mock_proc.terminate.assert_called_once()
        assert not mgr.is_running

    def test_stop_noop_when_not_running(self) -> None:
        mgr = TunnelManager(port=3978)
        mgr.stop()  # should not raise

    def test_url_none_before_start(self) -> None:
        mgr = TunnelManager(port=3978)
        assert mgr.url is None

    def test_default_port(self) -> None:
        mgr = TunnelManager()
        assert mgr.port == 3978

    def test_custom_port(self) -> None:
        mgr = TunnelManager(port=4000)
        assert mgr.port == 4000
