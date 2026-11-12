"""REGRESSION-CRITICAL: cert rotation rollback on Windows.

The plan flags rotation rollback as the single most dangerous Windows-
specific code path (D7). If a cert rotates and the smoke test then
fails, the agent identity must end up exactly where it started:

1. Original public DER reposted to the Blueprint app via Graph PATCH.
2. ``.env`` thumbprints restored.
3. MSAL token cache invalidated (D13) so the next call doesn't try
   to use a token signed under the now-invalidated new key.

These tests drive ``rotate_cert_windows.py`` with mocked Graph PATCH +
mocked smoke-test outcomes. They run on every host.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_THIS = Path(__file__).resolve()
_SPEC = importlib.util.spec_from_file_location(
    "rotate_cert_windows",
    _THIS.parents[1] / "scripts" / "rotate_cert_windows.py",
)
rotate_cert_windows = importlib.util.module_from_spec(_SPEC)
sys.modules["rotate_cert_windows"] = rotate_cert_windows
_SPEC.loader.exec_module(rotate_cert_windows)


def _make_state(tmp_path: Path) -> rotate_cert_windows.RotationState:
    env = tmp_path / ".env"
    env.write_text(
        "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT=ORIG_X5T\n"
        "ENTRABOT_BLUEPRINT_CERT_SHA1=" + "0" * 40 + "\n"
    )
    msal = tmp_path / ".msal-cache.bin"
    msal.write_bytes(b"old-cache")
    return rotate_cert_windows.RotationState(
        env_path=env,
        msal_cache_path=msal,
        blueprint_object_id="bp-obj",
    )


class TestPatchOkSmokeOk:
    def test_no_rollback_when_smoke_passes(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        graph_patch = MagicMock(return_value=200)
        smoke_test = MagicMock(return_value=True)
        delete_old = MagicMock()

        rotate_cert_windows.rotate(
            state=state,
            old_der=b"OLD-DER",
            new_thumbprint="A" * 40,
            new_x5t_s256="NEW_X5T",
            new_der=b"NEW-DER",
            graph_patch=graph_patch,
            smoke_test=smoke_test,
            delete_old_cert=delete_old,
            graph_token_provider=lambda: "tok",
        )

        graph_patch.assert_called_once()
        smoke_test.assert_called_once()
        delete_old.assert_called_once()
        # MSAL cache untouched on happy path.
        assert state.msal_cache_path.read_bytes() == b"old-cache"
        # .env updated.
        env_text = state.env_path.read_text()
        assert "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT=NEW_X5T" in env_text
        assert "ENTRABOT_BLUEPRINT_CERT_SHA1=" + "A" * 40 in env_text


class TestPatchOkSmokeFail:
    def test_rollback_repatches_and_restores_env_and_invalidates_msal(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        graph_patch = MagicMock(return_value=200)
        smoke_test = MagicMock(return_value=False)
        delete_old = MagicMock()

        with pytest.raises(rotate_cert_windows.RotationRolledBack):
            rotate_cert_windows.rotate(
                state=state,
                old_der=b"OLD-DER",
                new_thumbprint="A" * 40,
                new_x5t_s256="NEW_X5T",
                new_der=b"NEW-DER",
                graph_patch=graph_patch,
                smoke_test=smoke_test,
                delete_old_cert=delete_old,
                graph_token_provider=lambda: "tok",
            )

        # Two PATCH calls: one with new DER, one rollback with old DER.
        assert graph_patch.call_count == 2
        first_call_der = graph_patch.call_args_list[0].kwargs["der_bytes"]
        second_call_der = graph_patch.call_args_list[1].kwargs["der_bytes"]
        assert first_call_der == b"NEW-DER"
        assert second_call_der == b"OLD-DER"

        # Old cert NOT deleted from Cert: store.
        delete_old.assert_not_called()

        # MSAL cache invalidated.
        assert not state.msal_cache_path.exists()

        # .env restored to ORIG values.
        env_text = state.env_path.read_text()
        assert "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT=ORIG_X5T" in env_text
        assert "ENTRABOT_BLUEPRINT_CERT_SHA1=" + "0" * 40 in env_text


class TestPatchFails:
    def test_no_rollback_needed_when_initial_patch_fails(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        graph_patch = MagicMock(return_value=500)
        smoke_test = MagicMock()
        delete_old = MagicMock()

        with pytest.raises(rotate_cert_windows.RotationFailed):
            rotate_cert_windows.rotate(
                state=state,
                old_der=b"OLD-DER",
                new_thumbprint="A" * 40,
                new_x5t_s256="NEW_X5T",
                new_der=b"NEW-DER",
                graph_patch=graph_patch,
                smoke_test=smoke_test,
                delete_old_cert=delete_old,
                graph_token_provider=lambda: "tok",
            )

        # Only one PATCH attempted; smoke test never ran.
        assert graph_patch.call_count == 1
        smoke_test.assert_not_called()
        delete_old.assert_not_called()
        # No rollback needed: nothing changed in the agent identity.
        # .env still has original values.
        env_text = state.env_path.read_text()
        assert "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT=ORIG_X5T" in env_text


class TestRollbackPatchAlsoFails:
    def test_halts_loud_when_rollback_patch_fails(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        # First PATCH succeeds, smoke fails, rollback PATCH fails.
        responses = [200, 500]

        def fake_patch(**kwargs):
            return responses.pop(0)

        smoke_test = MagicMock(return_value=False)
        delete_old = MagicMock()

        with pytest.raises(
            rotate_cert_windows.ManualInterventionRequired,
            match="MANUAL INTERVENTION",
        ):
            rotate_cert_windows.rotate(
                state=state,
                old_der=b"OLD-DER",
                new_thumbprint="A" * 40,
                new_x5t_s256="NEW_X5T",
                new_der=b"NEW-DER",
                graph_patch=fake_patch,
                smoke_test=smoke_test,
                delete_old_cert=delete_old,
                graph_token_provider=lambda: "tok",
            )

        delete_old.assert_not_called()


class TestSupportedEntryPoint:
    """Running the script is the supported rotation: `python -m entrabot.winsetup rotate-cert`.

    The transactional ``rotate()`` above is kept as a library for its callers. It removes
    the old certificate in the same write that adds the new one, so it is not what
    ``scripts/deploy-windows.ps1`` runs any more.
    """

    def test_main_runs_the_rotation_for_this_project_folder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from entrabot.winsetup import cli

        seen: list[list[str]] = []

        def fake_main(argv: list[str]) -> int:
            seen.append(list(argv))
            return 3

        monkeypatch.setattr(cli, "main", fake_main)

        code = rotate_cert_windows.main(["--yes", "--verbose"])

        assert code == 3
        repo_root = str(_THIS.parents[1])
        assert seen == [["rotate-cert", "--project-root", repo_root, "--yes", "--verbose"]]

    def test_main_takes_only_the_rotation_flags(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            rotate_cert_windows.main(["--delete-agent"])
        assert exit_info.value.code == 2


class TestEnvFileIsUtf8OnAnyLocale:
    """The settings file is UTF-8 (setup writes it that way). Read with the Windows ANSI code
    page, a non-ASCII address elsewhere in the file stopped the rotation: the UTF-8 bytes of
    an accented capital such as A-acute are not valid cp1252."""

    def test_other_lines_survive_a_patch_on_a_cp1252_machine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.locale_codec import force_locale_codec

        env = tmp_path / ".env"
        env.write_bytes(
            "ENTRABOT_HUMAN_UPN=Ángel.Íñigo@zürich.example\n"
            "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT=ORIG\n"
            "ENTRABOT_BLUEPRINT_CERT_SHA1=0000\n".encode()
        )
        force_locale_codec(monkeypatch)

        rotate_cert_windows._patch_env(env, x5t="NEW", sha1="1111")

        assert env.read_bytes() == (
            "ENTRABOT_HUMAN_UPN=Ángel.Íñigo@zürich.example\n"
            "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT=NEW\n"
            "ENTRABOT_BLUEPRINT_CERT_SHA1=1111\n".encode()
        )
        assert rotate_cert_windows._read_orig_thumbprints(env) == ("NEW", "1111")

    def test_a_byte_order_mark_does_not_hide_the_first_key(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_bytes(b"\xef\xbb\xbfENTRABOT_BLUEPRINT_CERT_THUMBPRINT=ORIG\n")

        assert rotate_cert_windows._read_orig_thumbprints(env) == ("ORIG", "")
