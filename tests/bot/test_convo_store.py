"""Tests for conversation reference persistence.

Uses tmp_path and monkeypatch to isolate file operations from real home dir.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from entrabot.bot import convo_store


@pytest.fixture(autouse=True)
def _isolate_refs_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CONVO_REFS_PATH at a temp directory for every test."""
    refs_path = tmp_path / "bot" / "conversation_refs.json"
    monkeypatch.setattr(convo_store, "CONVO_REFS_PATH", refs_path)


def _sample_ref(label: str = "a") -> dict[str, Any]:
    """Return a minimal conversation reference dict for testing."""
    return {
        "conversation": {"id": f"conv-{label}"},
        "bot": {"id": "bot-1", "name": "EntraBot"},
        "serviceUrl": f"https://smba.trafficmanager.net/{label}",
    }


# --- save_reference ---


class TestSaveReference:
    def test_save_creates_file(self, tmp_path: Path) -> None:
        """Save to non-existent file creates it with correct content."""
        convo_store.save_reference("conv-a", _sample_ref("a"))

        refs_path = tmp_path / "bot" / "conversation_refs.json"
        assert refs_path.exists()
        data = json.loads(refs_path.read_text())
        assert "conv-a" in data
        assert data["conv-a"]["serviceUrl"] == "https://smba.trafficmanager.net/a"

    def test_save_updates_existing(self, tmp_path: Path) -> None:
        """Save two different conversation IDs, both present."""
        convo_store.save_reference("conv-a", _sample_ref("a"))
        convo_store.save_reference("conv-b", _sample_ref("b"))

        refs_path = tmp_path / "bot" / "conversation_refs.json"
        data = json.loads(refs_path.read_text())
        assert "conv-a" in data
        assert "conv-b" in data

    def test_save_overwrites_same_id(self, tmp_path: Path) -> None:
        """Save same ID twice with different data, latest wins."""
        convo_store.save_reference("conv-a", _sample_ref("a"))
        updated = _sample_ref("a")
        updated["serviceUrl"] = "https://updated.example.com"
        convo_store.save_reference("conv-a", updated)

        refs_path = tmp_path / "bot" / "conversation_refs.json"
        data = json.loads(refs_path.read_text())
        assert data["conv-a"]["serviceUrl"] == "https://updated.example.com"


# --- load_reference ---


class TestLoadReference:
    def test_load_reference_found(self) -> None:
        """Save then load by ID returns the saved reference."""
        ref = _sample_ref("x")
        convo_store.save_reference("conv-x", ref)

        loaded = convo_store.load_reference("conv-x")
        assert loaded is not None
        assert loaded["serviceUrl"] == ref["serviceUrl"]

    def test_load_reference_not_found(self) -> None:
        """Load non-existent ID returns None."""
        assert convo_store.load_reference("no-such-id") is None


# --- load_all_references ---


class TestLoadAllReferences:
    def test_load_all_references(self) -> None:
        """Save 3 refs, load all, verify dict has all 3."""
        for label in ("a", "b", "c"):
            convo_store.save_reference(f"conv-{label}", _sample_ref(label))

        all_refs = convo_store.load_all_references()
        assert len(all_refs) == 3
        assert set(all_refs.keys()) == {"conv-a", "conv-b", "conv-c"}

    def test_load_all_missing_file(self) -> None:
        """Load from non-existent file returns empty dict."""
        assert convo_store.load_all_references() == {}

    def test_load_all_corrupted_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Corrupted file returns empty dict and logs a warning."""
        refs_path = tmp_path / "bot" / "conversation_refs.json"
        refs_path.parent.mkdir(parents=True, exist_ok=True)
        refs_path.write_text("NOT VALID JSON {{{")

        with caplog.at_level(logging.WARNING, logger="entrabot.bot.convo_store"):
            result = convo_store.load_all_references()

        assert result == {}
        assert any("corrupt" in r.message.lower() for r in caplog.records)
        # File must NOT be deleted — user can inspect
        assert refs_path.exists()


# --- delete_reference ---


class TestDeleteReference:
    def test_delete_reference(self) -> None:
        """Save then delete, verify it's gone but others remain."""
        convo_store.save_reference("conv-a", _sample_ref("a"))
        convo_store.save_reference("conv-b", _sample_ref("b"))

        convo_store.delete_reference("conv-a")

        assert convo_store.load_reference("conv-a") is None
        assert convo_store.load_reference("conv-b") is not None

    def test_delete_nonexistent_is_noop(self) -> None:
        """Delete ID that doesn't exist raises no error."""
        convo_store.delete_reference("ghost-id")  # should not raise
