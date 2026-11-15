"""What the running code reports about its version (entrabot.version)."""

from __future__ import annotations

import subprocess

import pytest

from entrabot import version as version_mod


def _git(answers: dict[str, str | None]):
    """A fake subprocess.run for git: the first word after -C <root> picks the answer."""

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        assert argv[:2] == ["git", "-C"]
        key = " ".join(argv[3:])
        for prefix, answer in answers.items():
            if key.startswith(prefix):
                if answer is None:
                    return subprocess.CompletedProcess(argv, 128, "", "fatal")
                return subprocess.CompletedProcess(argv, 0, answer + "\n", "")
        raise AssertionError(f"unexpected git {key}")

    return run


class TestInstalledVersion:
    def test_from_the_package_metadata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(version_mod, "_dist_version", lambda name: "0.2.0")
        assert version_mod.installed_version() == "0.2.0"

    def test_unknown_without_metadata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def missing(name: str) -> str:
            raise version_mod.PackageNotFoundError(name)

        monkeypatch.setattr(version_mod, "_dist_version", missing)
        assert version_mod.installed_version() == "unknown"


class TestReleaseStatus:
    def test_on_its_release_tag(self) -> None:
        run = _git({"rev-parse": "true", "describe --tags --exact-match": "v0.2.0"})
        assert version_mod.release_status(version="0.2.0", run=run) == (
            "pass",
            "Entrabot 0.2.0, release v0.2.0",
        )

    def test_a_pre_release_tag_matches_its_python_version(self) -> None:
        run = _git({"rev-parse": "true", "describe --tags --exact-match": "v0.3.0-rc.1"})
        status, detail = version_mod.release_status(version="0.3.0rc1", run=run)
        assert status == "pass" and "v0.3.0-rc.1" in detail

    def test_ahead_of_a_tag_is_development_not_a_failure(self) -> None:
        run = _git(
            {
                "rev-parse": "true",
                "describe --tags --exact-match": None,
                "describe --tags": "v0.2.0-3-gabc1234",
            }
        )
        status, detail = version_mod.release_status(version="0.2.0", run=run)
        assert status == "warn"
        assert "3 commits ahead of v0.2.0" in detail and "development" in detail

    def test_no_release_tag_at_all(self) -> None:
        run = _git(
            {
                "rev-parse": "true",
                "describe --tags --exact-match": None,
                "describe --tags": None,
            }
        )
        status, detail = version_mod.release_status(version="0.2.0", run=run)
        assert status == "warn" and "not on a release tag" in detail

    def test_a_tag_the_installed_package_does_not_match(self) -> None:
        # pyproject was bumped by the checkout, but the editable install was not refreshed.
        run = _git({"rev-parse": "true", "describe --tags --exact-match": "v0.2.0"})
        status, detail = version_mod.release_status(version="0.1", run=run)
        assert status == "warn" and "v0.2.0" in detail and "0.1" in detail

    def test_not_a_git_checkout(self) -> None:
        run = _git({"rev-parse": None})
        assert version_mod.release_status(version="0.2.0", run=run)[0] == "skip"

    def test_no_git_installed(self) -> None:
        def no_git(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            raise FileNotFoundError("git")

        assert version_mod.release_status(version="0.2.0", run=no_git)[0] == "skip"
