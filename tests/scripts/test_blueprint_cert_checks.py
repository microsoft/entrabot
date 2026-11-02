"""Tests for the read-only certificate checks setup.sh depends on.

Regression context (#124): verify_blueprint_cert.py used to return "not registered"
on any Graph failure, so a transient error could trigger regeneration and a wipe of
the Blueprint's certificates. These scripts must keep "checked: not registered"
(exit 1) apart from "could not check" (exit 3).
"""

from __future__ import annotations

import runpy
import sys
from typing import Any

import httpx
import pytest

from tests.scripts._blueprint_cert_helpers import (
    FAKE_OBJECT_ID,
    FAKE_TOKEN,
    SCRIPTS_DIR,
    FakeGraph,
    FakeKeyring,
    block_real_keyring_and_network,
    graph_entry,
    load_script,
    make_cert,
    raising,
    use_fake_graph,
)

verify_mod = load_script("verify_blueprint_cert")
find_mod = load_script("find_local_blueprint_cert")
list_mod = load_script("list_blueprint_certs")

EXIT_OK = 0
EXIT_NOT_REGISTERED = 1
EXIT_USAGE = 2
EXIT_COULD_NOT_CHECK = 3

LOCAL_CERT = make_cert("this-machine")
OTHER_CERT = make_cert("other-machine")

KEY_ACCOUNT = ("entrabot", "blueprint-private-key")
PENDING_ACCOUNT = ("entrabot", "blueprint-private-key.pending")

GRAPH_FAILURES = [
    401,
    403,
    404,
    429,
    500,
    503,
    httpx.ConnectTimeout("x"),
    httpx.ReadTimeout("x"),
    b"not json",
    b"{}",
    b'{"keyCredentials": null}',
]


@pytest.fixture(autouse=True)
def _no_real_keychain_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_real_keyring_and_network(monkeypatch)


@pytest.fixture(autouse=True)
def _patched_token_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (verify_mod, find_mod, list_mod):
        monkeypatch.setattr(module, "get_existing_graph_token", lambda: FAKE_TOKEN)


def _entry(cert: Any, key_id: str = "k-1", name: str = "EntraBot Device Certificate — x") -> dict:
    return graph_entry(cert, key_id=key_id, display_name=name)


def _run(module: Any, monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", [f"{module.__name__}.py", *args])
    return module.main()


class TestVerifyBlueprintCert:
    def _verify(self, monkeypatch: pytest.MonkeyPatch, thumbprint: str) -> int:
        return _run(verify_mod, monkeypatch, FAKE_OBJECT_ID, thumbprint)

    def test_present_exits_0_and_prints_nothing_on_stdout(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        use_fake_graph(monkeypatch, FakeGraph([_entry(OTHER_CERT), _entry(LOCAL_CERT, "k-2")]))
        assert self._verify(monkeypatch, LOCAL_CERT.thumbprint) == EXIT_OK
        assert capsys.readouterr().out == ""

    def test_checked_and_absent_exits_1(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        use_fake_graph(monkeypatch, FakeGraph([_entry(OTHER_CERT)]))
        assert self._verify(monkeypatch, LOCAL_CERT.thumbprint) == EXIT_NOT_REGISTERED
        assert "cache-desync" in capsys.readouterr().err

    def test_an_empty_list_read_successfully_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_fake_graph(monkeypatch, FakeGraph([]))
        assert self._verify(monkeypatch, LOCAL_CERT.thumbprint) == EXIT_NOT_REGISTERED

    @pytest.mark.parametrize("outcome", GRAPH_FAILURES, ids=repr)
    def test_graph_failure_exits_could_not_check(
        self, monkeypatch: pytest.MonkeyPatch, outcome: Any
    ) -> None:
        use_fake_graph(monkeypatch, FakeGraph([_entry(LOCAL_CERT)], get_script=[outcome]))
        assert self._verify(monkeypatch, LOCAL_CERT.thumbprint) == EXIT_COULD_NOT_CHECK

    def test_token_failure_exits_could_not_check(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            verify_mod, "get_existing_graph_token", raising(RuntimeError("fake token failure"))
        )
        graph = use_fake_graph(monkeypatch, FakeGraph([]))
        assert self._verify(monkeypatch, LOCAL_CERT.thumbprint) == EXIT_COULD_NOT_CHECK
        assert graph.requests == []
        assert "fake token failure" in capsys.readouterr().err

    def test_unreadable_entry_with_no_match_exits_could_not_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The unreadable entry could be this machine's, so absence isn't proven.
        use_fake_graph(monkeypatch, FakeGraph([{**_entry(OTHER_CERT), "key": None}]))
        assert self._verify(monkeypatch, LOCAL_CERT.thumbprint) == EXIT_COULD_NOT_CHECK

    def test_a_match_wins_over_an_unreadable_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_fake_graph(
            monkeypatch,
            FakeGraph([{**_entry(OTHER_CERT), "key": None}, _entry(LOCAL_CERT, "k-2")]),
        )
        assert self._verify(monkeypatch, LOCAL_CERT.thumbprint) == EXIT_OK

    def test_a_non_certificate_entry_does_not_block_the_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        symmetric = {**_entry(OTHER_CERT), "key": None, "type": "Symmetric"}
        use_fake_graph(monkeypatch, FakeGraph([symmetric]))
        assert self._verify(monkeypatch, LOCAL_CERT.thumbprint) == EXIT_NOT_REGISTERED

    def test_usage_error_exits_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _run(verify_mod, monkeypatch, "only-one-arg") == EXIT_USAGE

    def test_noisy_token_helper_stays_off_stdout(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def noisy() -> str:
            print("diagnostic line from the token helper")
            return FAKE_TOKEN

        monkeypatch.setattr(verify_mod, "get_existing_graph_token", noisy)
        use_fake_graph(monkeypatch, FakeGraph([_entry(LOCAL_CERT)]))

        assert self._verify(monkeypatch, LOCAL_CERT.thumbprint) == EXIT_OK

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "diagnostic line" in captured.err


class TestFindLocalBlueprintCert:
    def _keyring(self, monkeypatch: pytest.MonkeyPatch, *, pem: str | None) -> FakeKeyring:
        fake = FakeKeyring({KEY_ACCOUNT: pem} if pem else {})
        monkeypatch.setattr(find_mod, "keyring", fake)
        return fake

    def _find(self, monkeypatch: pytest.MonkeyPatch) -> int:
        return _run(find_mod, monkeypatch, FAKE_OBJECT_ID)

    def test_match_exits_0_with_only_the_thumbprint_on_stdout(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._keyring(monkeypatch, pem=LOCAL_CERT.private_key_pem)
        use_fake_graph(monkeypatch, FakeGraph([_entry(OTHER_CERT), _entry(LOCAL_CERT, "k-2")]))

        assert self._find(monkeypatch) == EXIT_OK

        captured = capsys.readouterr()
        assert captured.out == LOCAL_CERT.thumbprint + "\n"
        assert "PRIVATE KEY" not in captured.out + captured.err

    def test_no_local_key_exits_1_without_calling_graph(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._keyring(monkeypatch, pem=None)
        graph = use_fake_graph(monkeypatch, FakeGraph([]))
        assert self._find(monkeypatch) == EXIT_NOT_REGISTERED
        assert graph.requests == []

    def test_checked_and_no_match_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._keyring(monkeypatch, pem=LOCAL_CERT.private_key_pem)
        use_fake_graph(monkeypatch, FakeGraph([_entry(OTHER_CERT)]))
        assert self._find(monkeypatch) == EXIT_NOT_REGISTERED

    def test_a_local_key_that_is_not_a_private_key_exits_1(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Nothing can authenticate with it, so there is nothing here to recover.
        self._keyring(monkeypatch, pem="not a pem")
        graph = use_fake_graph(monkeypatch, FakeGraph([_entry(LOCAL_CERT)]))
        assert self._find(monkeypatch) == EXIT_NOT_REGISTERED
        assert graph.requests == []
        assert "not a pem" not in capsys.readouterr().err

    @pytest.mark.parametrize("outcome", GRAPH_FAILURES, ids=repr)
    def test_graph_failure_exits_could_not_check(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        outcome: Any,
    ) -> None:
        self._keyring(monkeypatch, pem=LOCAL_CERT.private_key_pem)
        use_fake_graph(monkeypatch, FakeGraph([_entry(LOCAL_CERT)], get_script=[outcome]))
        assert self._find(monkeypatch) == EXIT_COULD_NOT_CHECK
        assert capsys.readouterr().out == ""

    def test_token_failure_exits_could_not_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._keyring(monkeypatch, pem=LOCAL_CERT.private_key_pem)
        monkeypatch.setattr(
            find_mod, "get_existing_graph_token", raising(RuntimeError("fake token failure"))
        )
        assert self._find(monkeypatch) == EXIT_COULD_NOT_CHECK

    def test_keyring_failure_exits_could_not_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self._keyring(monkeypatch, pem=LOCAL_CERT.private_key_pem)
        fake.fail_on[("get", "blueprint-private-key")] = RuntimeError("keychain locked")
        graph = use_fake_graph(monkeypatch, FakeGraph([]))
        assert self._find(monkeypatch) == EXIT_COULD_NOT_CHECK
        assert graph.requests == []

    @pytest.mark.parametrize(
        "pem", [LOCAL_CERT.private_key_pem, None], ids=["with-a-local-key", "without"]
    )
    def test_a_pending_key_exits_1_before_anything_else_is_read(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        pem: str | None,
    ) -> None:
        # Only register_blueprint_cert.py reconciles a pending key, so setup must go there
        # even when the active key matches a registered certificate.
        fake = self._keyring(monkeypatch, pem=pem)
        fake.entries[PENDING_ACCOUNT] = OTHER_CERT.private_key_pem
        monkeypatch.setattr(
            find_mod, "get_existing_graph_token", raising(AssertionError("asked for a token"))
        )
        graph = use_fake_graph(monkeypatch, FakeGraph([_entry(LOCAL_CERT)]))

        assert self._find(monkeypatch) == EXIT_NOT_REGISTERED

        captured = capsys.readouterr()
        assert fake.events == ["get:blueprint-private-key.pending"]
        assert graph.requests == []
        assert captured.out == ""
        assert len(captured.err.strip().splitlines()) == 1
        assert "pending" in captured.err
        assert "PRIVATE KEY" not in captured.err

    def test_a_keyring_failure_reading_the_pending_key_exits_could_not_check(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Not knowing whether a pending key exists is not the same as there being none.
        fake = self._keyring(monkeypatch, pem=LOCAL_CERT.private_key_pem)
        fake.fail_on[("get", "blueprint-private-key.pending")] = RuntimeError("keychain locked")
        graph = use_fake_graph(monkeypatch, FakeGraph([_entry(LOCAL_CERT)]))

        assert self._find(monkeypatch) == EXIT_COULD_NOT_CHECK

        assert graph.requests == []
        assert capsys.readouterr().out == ""

    def test_unreadable_entry_with_no_match_exits_could_not_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._keyring(monkeypatch, pem=LOCAL_CERT.private_key_pem)
        use_fake_graph(monkeypatch, FakeGraph([{**_entry(OTHER_CERT), "key": "bm90LWEtY2VydA=="}]))
        assert self._find(monkeypatch) == EXIT_COULD_NOT_CHECK

    def test_a_match_wins_over_an_unreadable_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._keyring(monkeypatch, pem=LOCAL_CERT.private_key_pem)
        use_fake_graph(
            monkeypatch,
            FakeGraph([{**_entry(OTHER_CERT), "key": None}, _entry(LOCAL_CERT, "k-2")]),
        )
        assert self._find(monkeypatch) == EXIT_OK

    def test_usage_error_exits_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _run(find_mod, monkeypatch) == EXIT_USAGE


class TestListBlueprintCerts:
    def _list(self, monkeypatch: pytest.MonkeyPatch) -> int:
        return _run(list_mod, monkeypatch, FAKE_OBJECT_ID)

    def test_prints_the_count_on_stdout_and_details_on_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        entries = [
            graph_entry(
                LOCAL_CERT, key_id="k-1", display_name="Device A", end="2027-01-02T00:00:00Z"
            ),
            graph_entry(
                OTHER_CERT, key_id="k-2", display_name="Device B", end="2027-03-04T00:00:00Z"
            ),
        ]
        use_fake_graph(monkeypatch, FakeGraph(entries))

        assert self._list(monkeypatch) == EXIT_OK

        captured = capsys.readouterr()
        assert captured.out == "2\n"
        assert "Device A" in captured.err and "2027-01-02" in captured.err
        assert "Device B" in captured.err and "2027-03-04" in captured.err
        assert LOCAL_CERT.b64 not in captured.err

    def test_an_empty_list_read_successfully_prints_0(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        use_fake_graph(monkeypatch, FakeGraph([]))
        assert self._list(monkeypatch) == EXIT_OK
        assert capsys.readouterr().out == "0\n"

    @pytest.mark.parametrize("outcome", GRAPH_FAILURES, ids=repr)
    def test_graph_failure_exits_nonzero_and_never_prints_a_count(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        outcome: Any,
    ) -> None:
        use_fake_graph(monkeypatch, FakeGraph([_entry(LOCAL_CERT)], get_script=[outcome]))

        assert self._list(monkeypatch) == EXIT_COULD_NOT_CHECK

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err != ""

    def test_token_failure_exits_nonzero_and_never_prints_a_count(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            list_mod, "get_existing_graph_token", raising(RuntimeError("fake token failure"))
        )
        assert self._list(monkeypatch) == EXIT_COULD_NOT_CHECK
        assert capsys.readouterr().out == ""

    def test_usage_error_exits_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _run(list_mod, monkeypatch) == EXIT_USAGE


class TestDependencyFailure:
    """A traceback exits 1, which for these scripts means "checked: not registered"."""

    @pytest.mark.parametrize(
        ("script", "args"),
        [
            ("verify_blueprint_cert", [FAKE_OBJECT_ID, "thumbprint"]),
            ("find_local_blueprint_cert", [FAKE_OBJECT_ID]),
        ],
    )
    def test_an_import_failure_exits_could_not_check(
        self, monkeypatch: pytest.MonkeyPatch, script: str, args: list[str]
    ) -> None:
        monkeypatch.setitem(sys.modules, "blueprint_certs", None)
        monkeypatch.setattr(sys, "argv", [f"{script}.py", *args])

        with pytest.raises(SystemExit) as caught:
            runpy.run_path(str(SCRIPTS_DIR / f"{script}.py"), run_name="__main__")

        assert caught.value.code == EXIT_COULD_NOT_CHECK

    @pytest.mark.parametrize("module", [verify_mod, find_mod])
    def test_an_unexpected_exception_exits_could_not_check(
        self, monkeypatch: pytest.MonkeyPatch, module: Any
    ) -> None:
        monkeypatch.setattr(module, "fetch_key_credentials", raising(ZeroDivisionError("bug")))
        if module is find_mod:
            monkeypatch.setattr(
                find_mod, "keyring", FakeKeyring({KEY_ACCOUNT: LOCAL_CERT.private_key_pem})
            )
            assert _run(module, monkeypatch, FAKE_OBJECT_ID) == EXIT_COULD_NOT_CHECK
        else:
            assert _run(module, monkeypatch, FAKE_OBJECT_ID, "thumbprint") == EXIT_COULD_NOT_CHECK
