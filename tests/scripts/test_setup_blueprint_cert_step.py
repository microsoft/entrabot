"""Tests for the shell side of the #124 fix: setup.sh and the Windows wrapper.

setup.sh is never run for real here: the tests use ``bash -n`` and ``--help`` (which exits
before any work). The certificate step itself is no longer shell. ``setup.sh`` starts the
setup wizard, whose certificate step (``entrabot.winsetup.certstep``) decides whether this
computer's certificate is reused, registered or created; ``tests/winsetup/test_certstep.py``
and ``tests/winsetup/test_wizard_posix.py`` hold what the Step 6 decision block used to be
tested for here, and ``tests/scripts/test_setup_sh_bootstrap.py`` runs the script itself.
"""

from __future__ import annotations

import ast
import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.scripts._blueprint_cert_helpers import (
    REPO_ROOT,
    block_real_keyring_and_network,
    blueprint_certs,
)

SETUP_SH = REPO_ROOT / "scripts" / "setup.sh"
SETUP_PS1 = REPO_ROOT / "scripts" / "setup-windows.ps1"
DEPLOY_PS1 = REPO_ROOT / "scripts" / "deploy-windows.ps1"
TEARDOWN_SH = REPO_ROOT / "scripts" / "teardown.sh"

FLAG = "--remove-all-existing-certs"

CACHED = "c" * 43
RECOVERED = "r" * 43
REGISTERED = "n" * 43

# A keyCredentials key followed directly by a list holding an object literal: the
# shape that replaced the whole list.
ONE_ELEMENT_LITERAL = re.compile(r"""['"]?keyCredentials['"]?\s*:\s*\[\s*\{""")

posix_shell_only = pytest.mark.skipif(sys.platform == "win32", reason="runs under POSIX bash")


@pytest.fixture(autouse=True)
def _no_real_keychain_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_real_keyring_and_network(monkeypatch)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _help_run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run setup.sh with --help, which exits before doing any work.

    Tests pass --help even when checking another outcome: if a check ever regressed,
    the script still stops at the help text rather than starting real provisioning.
    """
    return subprocess.run(
        ["bash", str(SETUP_SH), *args, "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@posix_shell_only
class TestSetupScriptArguments:
    def test_bash_syntax_is_valid(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(SETUP_SH)], capture_output=True, text=True, timeout=30, check=False
        )
        assert result.returncode == 0, result.stderr

    def test_flag_is_parsed(self) -> None:
        result = _help_run(FLAG)
        assert result.returncode == 0
        assert "Unknown option" not in result.stderr

    def test_an_unknown_flag_is_still_rejected(self) -> None:
        # Shows the parsing check above can fail. The unknown flag comes before --help.
        result = _help_run("--not-a-real-flag")
        assert result.returncode == 2
        assert "Unknown option: --not-a-real-flag" in result.stderr

    def test_help_documents_the_flag_and_says_what_it_removes(self) -> None:
        output = " ".join(_help_run().stdout.split())
        assert FLAG in output
        assert "EVERY certificate" in output
        assert "other computers' included" in output
        assert "Not with --new" in output  # the wizard refuses the two together (exit 2)

    def test_help_says_setup_otherwise_never_removes_a_certificate(self) -> None:
        output = " ".join(_help_run().stdout.split())
        assert "every other computer's is kept" in output
        assert "Without this flag setup never removes a certificate" in output


class TestNothingReplacesTheListByAccident:
    @pytest.mark.parametrize("path", [SETUP_SH, SETUP_PS1, DEPLOY_PS1], ids=lambda p: p.name)
    def test_no_one_element_key_credentials_literal_remains(self, path: Path) -> None:
        assert ONE_ELEMENT_LITERAL.search(_read(path)) is None

    def test_the_guard_regex_recognizes_the_old_shapes(self) -> None:
        assert ONE_ELEMENT_LITERAL.search("json={'keyCredentials': [{")
        assert ONE_ELEMENT_LITERAL.search("body = {'keyCredentials': [{\n    'type': 'x'")
        assert ONE_ELEMENT_LITERAL.search('{"keyCredentials": [\n  {')

    def test_the_old_auto_proceed_and_wording_are_gone(self) -> None:
        text = " ".join(_read(SETUP_SH).split())
        assert "non-interactive shell" not in text
        assert "machine at a time" not in text
        assert "Replace existing cert(s)" not in text
        assert "REPLACE" not in text

    def test_setup_sh_holds_no_key_or_graph_handling(self) -> None:
        # The wizard's certificate step does it, through the certificate store and the
        # registration helper; nothing of it is inlined in shell.
        text = _read(SETUP_SH)
        assert "-m entrabot.winsetup" in text
        assert "keyring.set_password" not in text
        assert "requests.patch" not in text
        assert "rsa.generate_private_key" not in text

    def test_the_shell_does_not_call_the_verify_script(self) -> None:
        # A cached thumbprint says nothing about whether this machine's key matches it. The
        # wizard's certificate step asks the script about the thumbprint of the certificate
        # that is actually in this computer's store; the shell never asks at all.
        code = [line for line in _read(SETUP_SH).splitlines() if not line.lstrip().startswith("#")]
        assert [line for line in code if "verify_blueprint_cert" in line] == []

    def test_windows_wrapper_holds_no_graph_code_of_its_own(self) -> None:
        # deploy-windows.ps1 used to carry an inline Python driver that PATCHed the list.
        # It is now a thin wrapper: rotation lives in entrabot.winsetup.rotation.
        text = _read(DEPLOY_PS1)
        assert "'rotate-cert', '--project-root', $ProjectRoot" in text
        assert "entrabot.winsetup" in text
        for gone in ("keyCredentials", "httpx", "graph.microsoft.com", "$driver", "swap_key"):
            assert gone not in text

    def test_windows_rotation_adds_and_removes_through_the_helpers(self) -> None:
        from entrabot.winsetup import directory, rotation

        rotation_source = Path(rotation.__file__).read_text(encoding="utf-8")
        directory_source = Path(directory.__file__).read_text(encoding="utf-8")
        # Added beside the others by the registration helper (register_key_credential) ...
        assert '"register_windows_blueprint_cert.py"' in rotation_source
        # ... and taken off one at a time by remove_key_credential. Neither module builds
        # a keyCredentials list or PATCHes the application itself.
        for source in (rotation_source, directory_source):
            assert "keyCredentials" not in source
            assert "httpx.patch" not in source
            assert "swap_key_credential" not in source

    def test_windows_removal_program_is_valid_python_that_matches_the_helper(self) -> None:
        # Nothing else executes this child program before it reaches a Windows machine.
        from entrabot.winsetup import directory

        for program in (
            directory.REMOVE_BLUEPRINT_CREDENTIAL_SNIPPET,
            directory.REMOVE_PROVISIONER_CREDENTIAL_SNIPPET,
        ):
            tree = ast.parse(program)
            call = next(
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and ast.unparse(node.func) == "blueprint_certs.remove_key_credential"
            )
            keywords = {keyword.arg: None for keyword in call.keywords}
            assert set(keywords) == {"object_id", "token", "sha1_hex", "must_keep"}
            inspect.signature(blueprint_certs.remove_key_credential).bind(**keywords)
            # The exceptions it sorts into "nothing changed" exist under those names.
            for name in ("KeyCredentialsReadError", "RegistrationRejectedError"):
                assert f"blueprint_certs.{name}" in program
                assert hasattr(blueprint_certs, name)

    def test_teardown_clears_a_pending_key_too(self) -> None:
        assert "blueprint-private-key.pending" in _read(TEARDOWN_SH)
