"""Tests for keyring sanity check (Phase 2)."""

from __future__ import annotations

from unittest.mock import MagicMock

from entrabot.platform import keyring_sanity


class TestKeyringSanity:
    def test_passes_when_roundtrip_ok(self) -> None:
        store = MagicMock()
        # store + retrieve roundtrip OK
        captured = {}

        def fake_store(service, key, value):
            captured[(service, key)] = value

        def fake_retrieve(service, key):
            return captured.get((service, key))

        store.store.side_effect = fake_store
        store.retrieve.side_effect = fake_retrieve
        store.delete = MagicMock()

        result = keyring_sanity.check(store)
        assert result.ok is True
        assert result.stored_bytes >= 1700  # roughly a 2048-bit PEM
        store.delete.assert_called()  # cleanup happened

    def test_fails_when_retrieve_returns_truncated(self) -> None:
        store = MagicMock()
        store.store = MagicMock()
        store.retrieve.return_value = "short-truncated-value"
        store.delete = MagicMock()

        result = keyring_sanity.check(store)
        assert result.ok is False
        assert "truncated" in result.diagnostic.lower() or "mismatch" in result.diagnostic.lower()

    def test_fails_when_retrieve_returns_none(self) -> None:
        store = MagicMock()
        store.store = MagicMock()
        store.retrieve.return_value = None
        store.delete = MagicMock()

        result = keyring_sanity.check(store)
        assert result.ok is False
        assert "none" in result.diagnostic.lower() or "missing" in result.diagnostic.lower()

    def test_fails_when_store_raises(self) -> None:
        store = MagicMock()
        store.store.side_effect = RuntimeError("backend boom")
        store.retrieve = MagicMock()
        store.delete = MagicMock()

        result = keyring_sanity.check(store)
        assert result.ok is False
        assert "backend boom" in result.diagnostic

    def test_cleanup_runs_even_on_failure(self) -> None:
        store = MagicMock()
        store.store = MagicMock()
        store.retrieve.return_value = "wrong"
        store.delete = MagicMock()

        keyring_sanity.check(store)
        store.delete.assert_called_once()
