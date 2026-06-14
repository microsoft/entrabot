"""Tests for keyring sanity check (Phase 2)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from entrabot import errors
from entrabot.platform import keyring_sanity


@pytest.fixture(autouse=True)
def _allow_safe_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        keyring_sanity,
        "assert_allowed_keyring_backend",
        lambda: "tests.SafeKeyring",
        raising=False,
    )


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
        assert result.backend == "tests.SafeKeyring"
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

    def test_rejects_insecure_backend_without_writing_probe(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        error_cls = getattr(errors, "InsecureKeyringBackendError", RuntimeError)

        def reject_backend() -> str:
            raise error_cls("keyrings.alt.file.PlaintextKeyring")

        monkeypatch.setattr(
            keyring_sanity,
            "assert_allowed_keyring_backend",
            reject_backend,
            raising=False,
        )
        store = MagicMock()

        result = keyring_sanity.check(store)

        assert result.ok is False
        assert result.backend == "keyrings.alt.file.PlaintextKeyring"
        assert "insecure backend selected" in result.diagnostic
        store.store.assert_not_called()
        store.retrieve.assert_not_called()
        store.delete.assert_not_called()

    def test_uses_unique_probe_key_per_call(self) -> None:
        values: dict[tuple[str, str], str] = {}
        stored_keys: list[str] = []
        store = MagicMock()

        def fake_store(service: str, key: str, value: str) -> None:
            stored_keys.append(key)
            values[(service, key)] = value

        def fake_retrieve(service: str, key: str) -> str | None:
            return values.get((service, key))

        def fake_delete(service: str, key: str) -> None:
            values.pop((service, key), None)

        store.store.side_effect = fake_store
        store.retrieve.side_effect = fake_retrieve
        store.delete.side_effect = fake_delete

        first = keyring_sanity.check(store)
        second = keyring_sanity.check(store)

        assert first.ok is True
        assert second.ok is True
        assert len(stored_keys) == 2
        assert len(set(stored_keys)) == 2
        assert all(key.startswith("roundtrip-probe-") for key in stored_keys)
