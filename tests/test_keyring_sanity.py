"""Tests for keyring sanity check (Phase 2)."""

from __future__ import annotations

from unittest.mock import MagicMock

import keyring
import pytest

from entrabot.platform import keyring_backend, keyring_sanity


def _native_backend(path: str) -> object:
    module_name, class_name = path.rsplit(".", 1)
    cls = getattr(__import__(module_name, fromlist=[class_name]), class_name)
    return object.__new__(cls)


def _fake_backend(module_name: str, class_name: str) -> object:
    cls = type(class_name, (), {"__module__": module_name})
    return cls()


@pytest.fixture
def native_keyring_backend(monkeypatch: pytest.MonkeyPatch) -> str:
    backend_path = "keyring.backends.macOS.Keyring"
    monkeypatch.setattr(keyring_backend.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(keyring, "get_keyring", lambda: _native_backend(backend_path))
    return backend_path


class TestKeyringSanity:
    def test_passes_when_roundtrip_ok(self, native_keyring_backend: str) -> None:
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
        assert result.backend == native_keyring_backend
        store.delete.assert_called()  # cleanup happened

    def test_fails_when_retrieve_returns_truncated(self, native_keyring_backend: str) -> None:
        store = MagicMock()
        store.store = MagicMock()
        store.retrieve.return_value = "short-truncated-value"
        store.delete = MagicMock()

        result = keyring_sanity.check(store)
        assert result.ok is False
        assert result.backend == native_keyring_backend
        assert "truncated" in result.diagnostic.lower() or "mismatch" in result.diagnostic.lower()

    def test_fails_when_retrieve_returns_none(self, native_keyring_backend: str) -> None:
        store = MagicMock()
        store.store = MagicMock()
        store.retrieve.return_value = None
        store.delete = MagicMock()

        result = keyring_sanity.check(store)
        assert result.ok is False
        assert result.backend == native_keyring_backend
        assert "none" in result.diagnostic.lower() or "missing" in result.diagnostic.lower()

    def test_fails_when_store_raises(self, native_keyring_backend: str) -> None:
        store = MagicMock()
        store.store.side_effect = RuntimeError("backend boom")
        store.retrieve = MagicMock()
        store.delete = MagicMock()

        result = keyring_sanity.check(store)
        assert result.ok is False
        assert result.backend == native_keyring_backend
        assert "backend boom" in result.diagnostic

    def test_cleanup_runs_even_on_failure(self, native_keyring_backend: str) -> None:
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
        backend = _fake_backend("keyrings.alt.file", "PlaintextKeyring")
        monkeypatch.setattr(keyring_backend.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
        store = MagicMock()

        result = keyring_sanity.check(store)

        assert result.ok is False
        assert result.backend == "keyrings.alt.file.PlaintextKeyring"
        assert "PlaintextKeyring" in result.diagnostic
        assert "insecure backend selected" in result.diagnostic
        store.store.assert_not_called()
        store.retrieve.assert_not_called()
        store.delete.assert_not_called()

    def test_reports_none_backend_when_keyring_is_uninspectable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fail_get_keyring() -> object:
            raise RuntimeError("keyring config unavailable")

        monkeypatch.setattr(keyring_backend.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(keyring, "get_keyring", fail_get_keyring)
        store = MagicMock()

        result = keyring_sanity.check(store)

        assert result.ok is False
        assert result.backend is None
        assert "uninspectable" in result.diagnostic
        store.store.assert_not_called()
        store.retrieve.assert_not_called()
        store.delete.assert_not_called()

    def test_uses_unique_probe_key_per_call(self, native_keyring_backend: str) -> None:
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
        assert first.backend == native_keyring_backend
        assert second.backend == native_keyring_backend
        assert all(key.startswith("roundtrip-probe-") for key in stored_keys)

    def test_probe_key_includes_token_hex_suffix(
        self,
        monkeypatch: pytest.MonkeyPatch,
        native_keyring_backend: str,
    ) -> None:
        store = MagicMock()
        captured: dict[tuple[str, str], str] = {}

        def fake_token_hex(byte_count: int) -> str:
            if byte_count == 8:
                return "0123456789abcdef"
            return "a" * (byte_count * 2)

        def fake_store(service: str, key: str, value: str) -> None:
            captured[(service, key)] = value

        def fake_retrieve(service: str, key: str) -> str | None:
            return captured.get((service, key))

        monkeypatch.setattr(keyring_sanity.secrets, "token_hex", fake_token_hex)
        store.store.side_effect = fake_store
        store.retrieve.side_effect = fake_retrieve

        result = keyring_sanity.check(store)

        assert result.ok is True
        assert result.backend == native_keyring_backend
        store.store.assert_called_once()
        assert store.store.call_args.args[1] == "roundtrip-probe-0123456789abcdef"
