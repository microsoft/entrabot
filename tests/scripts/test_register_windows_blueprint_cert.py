"""Tests for scripts/register_windows_blueprint_cert.py, the helper behind setup-windows.ps1 Step 6.

It adds this machine's certificate and keeps every other machine's (#124), and it leaves the
Blueprint alone whenever it can't be read.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.scripts._blueprint_cert_helpers import (
    FAKE_OBJECT_ID,
    FAKE_TOKEN,
    FakeGraph,
    block_real_keyring_and_network,
    graph_entry,
    load_script,
    make_cert,
    raising,
    use_fake_graph,
)

reg = load_script("register_windows_blueprint_cert")

OK, NOT_REGISTERED, USAGE, UNCONFIRMED = 0, 1, 2, 3
THIS = make_cert("this-machine")
EXISTING = [
    graph_entry(make_cert("a"), key_id="k-a", display_name="EntraBot Device Certificate — host-a"),
    graph_entry(make_cert("b"), key_id="k-b", display_name="EntraBot Device Certificate — host-b"),
]

Run = Callable[..., tuple[int, FakeGraph]]
Capture = pytest.CaptureFixture[str]
Patch = pytest.MonkeyPatch


@pytest.fixture(autouse=True)
def _no_real_keychain_or_network(monkeypatch: Patch) -> None:
    block_real_keyring_and_network(monkeypatch)


@pytest.fixture
def run(monkeypatch: Patch, tmp_path: Path) -> Run:
    """Run main() against a FakeGraph holding the given entries; returns (exit code, graph)."""
    der = tmp_path / "machine.cer"
    der.write_bytes(THIS.der)

    def token(*, wait_for_propagation: bool = True) -> str:
        assert wait_for_propagation is False  # the Provisioner token, with no propagation wait
        return FAKE_TOKEN

    monkeypatch.setattr(reg, "get_bootstrap_graph_token", token)
    monkeypatch.setattr(socket, "gethostname", lambda: "fake-host.local")

    def go(*entries: dict[str, Any], der_path: Path = der, **graph: Any) -> tuple[int, FakeGraph]:
        fake = use_fake_graph(monkeypatch, FakeGraph(entries, **graph))
        argv = ["--blueprint-object-id", FAKE_OBJECT_ID, "--der", str(der_path)]
        return reg.main(argv, sleep=lambda _seconds: None), fake

    return go


def test_adds_this_machine_and_keeps_every_other(run: Run, capsys: Capture) -> None:
    code, graph = run(*EXISTING)

    body = graph.patch_bodies[0]
    assert code == OK
    assert body[:2] == EXISTING and len(body) == 3 and body[2]["key"] == THIS.b64
    assert body[2]["displayName"] == "EntraBot Device Certificate — fake-host"
    assert "2 existing certificate(s) kept" in capsys.readouterr().err


@pytest.mark.parametrize("outcome", [403, 500, httpx.ConnectTimeout("x"), b"{}"], ids=repr)
def test_a_failed_read_means_no_patch(run: Run, outcome: Any) -> None:
    code, graph = run(*EXISTING, get_script=[outcome])
    assert (code, graph.patches, graph.entries) == (NOT_REGISTERED, [], EXISTING)


@pytest.mark.parametrize("status", [400, 403, 429])
def test_a_4xx_changes_nothing_and_says_why(run: Run, capsys: Capture, status: int) -> None:
    code, graph = run(*EXISTING, patch_outcome=status, patch_applies=False)
    assert code == NOT_REGISTERED and graph.entries == EXISTING
    assert f"fake failure {status}" in capsys.readouterr().err


def test_a_token_failure_reads_nothing(run: Run, monkeypatch: Patch) -> None:
    monkeypatch.setattr(reg, "get_bootstrap_graph_token", raising(RuntimeError("no token")))
    code, graph = run(*EXISTING)
    assert (code, graph.requests) == (NOT_REGISTERED, [])


@pytest.mark.parametrize(
    "graph",
    [
        {"stale_reads": 10_000},
        {"patch_outcome": 503, "patch_applies": False},
        {"lost_key_ids": ["k-b"]},
    ],
    ids=["never-visible", "ambiguous-patch", "other-cert-vanished"],
)
def test_a_write_that_cannot_be_confirmed_exits_3(run: Run, graph: dict[str, Any]) -> None:
    assert run(*EXISTING, **graph)[0] == UNCONFIRMED


def test_anything_unexpected_once_the_write_may_have_started_exits_3(
    run: Run, monkeypatch: Patch
) -> None:
    monkeypatch.setattr(reg, "register_key_credential", raising(RuntimeError("boom")))
    assert run(*EXISTING)[0] == UNCONFIRMED


@pytest.mark.parametrize("kind", ["empty", "junk", "missing"])
def test_a_bad_certificate_file_exits_2_before_any_token_or_graph_call(
    run: Run, monkeypatch: Patch, tmp_path: Path, capsys: Capture, kind: str
) -> None:
    path = tmp_path / "bad.cer"
    if kind != "missing":
        path.write_bytes(b"" if kind == "empty" else b"not a certificate")
    monkeypatch.setattr(reg, "get_bootstrap_graph_token", raising(AssertionError("no token")))

    code, graph = run(*EXISTING, der_path=path)

    captured = capsys.readouterr()
    assert (code, graph.requests, captured.out) == (USAGE, [], "")
    assert str(path) in captured.err


@pytest.mark.parametrize(
    "graph",
    [
        {},
        {"get_script": [500]},
        {"patch_outcome": 403, "patch_applies": False},
        {"stale_reads": 99},
    ],
    ids=["registered", "unreadable", "rejected", "unconfirmed"],
)
def test_stdout_stays_empty_and_the_token_never_appears(
    run: Run, monkeypatch: Patch, capsys: Capture, graph: dict[str, Any]
) -> None:
    def noisy(*, wait_for_propagation: bool = True) -> str:
        print("  Ensuring 25 Graph application permissions on provisioner app...")
        return FAKE_TOKEN

    monkeypatch.setattr(reg, "get_bootstrap_graph_token", noisy)

    run(*EXISTING, **graph)

    captured = capsys.readouterr()
    assert captured.out == "" and "Ensuring 25" in captured.err
    assert FAKE_TOKEN not in captured.err


# --sha1: the certificate is read from the certificate store in-process, so no DER file
# (and no path that could contain an apostrophe) is involved.

SHA1 = "0123456789ABCDEF0123456789ABCDEF01234567"


def _store_cert(der: bytes) -> Any:
    from datetime import UTC, datetime, timedelta

    from entrabot.platform import wincert

    now = datetime.now(UTC)
    return wincert.StoreCert(
        sha1_hex=SHA1,
        x5t_s256="x" * 43,
        der=der,
        subject="CN=entrabot-blueprint",
        friendly_name="Entrabot Blueprint",
        not_before=now - timedelta(minutes=10),
        not_after=now + timedelta(days=365),
        has_private_key=True,
        ksp="tpm",
    )


@pytest.fixture
def run_sha1(monkeypatch: Patch) -> Callable[..., tuple[int, FakeGraph]]:
    from entrabot.platform import wincert

    monkeypatch.setattr(
        reg, "get_bootstrap_graph_token", lambda *, wait_for_propagation=True: FAKE_TOKEN
    )
    monkeypatch.setattr(socket, "gethostname", lambda: "fake-host.local")
    monkeypatch.setattr(reg, "_cert_store", lambda: wincert)  # as on Windows

    def go(*entries: dict[str, Any], found: Any = "default", **graph: Any) -> tuple[int, FakeGraph]:
        cert = _store_cert(THIS.der) if found == "default" else found
        if isinstance(cert, Exception):
            monkeypatch.setattr(wincert, "find_cert", raising(cert))
        else:
            monkeypatch.setattr(wincert, "find_cert", lambda sha1: cert)
        fake = use_fake_graph(monkeypatch, FakeGraph(entries, **graph))
        argv = ["--blueprint-object-id", FAKE_OBJECT_ID, "--sha1", SHA1]
        return reg.main(argv, sleep=lambda _seconds: None), fake

    return go


def test_sha1_registers_the_certificate_from_the_store(
    run_sha1: Callable[..., tuple[int, FakeGraph]], capsys: Capture
) -> None:
    code, graph = run_sha1(*EXISTING)

    assert code == OK
    assert graph.patch_bodies[0][2]["key"] == THIS.b64
    assert SHA1 not in capsys.readouterr().err


def test_sha1_not_in_the_store_is_a_usage_error_and_nothing_is_sent(
    run_sha1: Callable[..., tuple[int, FakeGraph]], capsys: Capture
) -> None:
    code, graph = run_sha1(*EXISTING, found=None)

    assert code == USAGE
    assert graph.requests == []
    err = capsys.readouterr().err
    assert "certificate store" in err and SHA1 not in err


def test_sha1_store_failure_is_a_usage_error(
    run_sha1: Callable[..., tuple[int, FakeGraph]], capsys: Capture
) -> None:
    from entrabot.platform import wincert

    code, graph = run_sha1(*EXISTING, found=wincert.WinCertError("Windows only"))

    assert code == USAGE
    assert graph.requests == []
    assert "Windows only" in capsys.readouterr().err


def test_exactly_one_of_der_and_sha1_is_required(tmp_path: Path) -> None:
    der = tmp_path / "machine.cer"
    der.write_bytes(THIS.der)
    with pytest.raises(SystemExit) as neither:
        reg.main(["--blueprint-object-id", FAKE_OBJECT_ID])
    with pytest.raises(SystemExit) as both:
        reg.main(["--blueprint-object-id", FAKE_OBJECT_ID, "--der", str(der), "--sha1", SHA1])
    assert neither.value.code == 2 and both.value.code == 2


# ── macOS and Linux: the same script, reading the Keychain or keyring ────────


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_sha1_reads_the_keystore_off_windows(monkeypatch: Patch, platform: str) -> None:
    from entrabot.platform import keycert

    asked: list[str] = []

    def find(sha1: str) -> Any:
        asked.append(sha1)
        return _store_cert(THIS.der)

    monkeypatch.setattr(reg.sys, "platform", platform)
    monkeypatch.setattr(keycert, "find_cert", find)
    monkeypatch.setattr(
        reg, "get_bootstrap_graph_token", lambda *, wait_for_propagation=True: FAKE_TOKEN
    )
    monkeypatch.setattr(socket, "gethostname", lambda: "fake-host.local")
    fake = use_fake_graph(monkeypatch, FakeGraph(EXISTING))

    code = reg.main(
        ["--blueprint-object-id", FAKE_OBJECT_ID, "--sha1", SHA1], sleep=lambda _seconds: None
    )

    assert code == OK and asked == [SHA1]
    assert fake.patch_bodies[0][2]["key"] == THIS.b64


def test_sha1_missing_from_the_keystore_names_the_keystore(
    monkeypatch: Patch, capsys: Capture
) -> None:
    from entrabot.platform import keycert

    monkeypatch.setattr(reg.sys, "platform", "darwin")
    monkeypatch.setattr(keycert, "find_cert", lambda sha1: None)
    fake = use_fake_graph(monkeypatch, FakeGraph(EXISTING))

    code = reg.main(["--blueprint-object-id", FAKE_OBJECT_ID, "--sha1", SHA1])

    assert code == USAGE and fake.requests == []
    err = capsys.readouterr().err
    assert "Keychain or keyring" in err and "CurrentUser" not in err and SHA1 not in err


# ── --remove-all-existing-certs: every other certificate goes ───────────────


def test_remove_all_replaces_every_certificate_and_lists_what_went(
    run: Run, monkeypatch: Patch, tmp_path: Path, capsys: Capture
) -> None:
    der = tmp_path / "machine.cer"
    fake = use_fake_graph(monkeypatch, FakeGraph(EXISTING))

    code = reg.main(
        ["--blueprint-object-id", FAKE_OBJECT_ID, "--der", str(der), "--remove-all-existing-certs"],
        sleep=lambda _seconds: None,
    )

    assert code == OK
    body = fake.patch_bodies[0]
    assert len(body) == 1 and body[0]["key"] == THIS.b64
    err = capsys.readouterr().err
    assert "2 existing certificate(s) removed" in err
    assert "host-a" in err and "host-b" in err  # by name and expiry, never by thumbprint


def test_without_the_flag_nothing_is_ever_removed(run: Run, capsys: Capture) -> None:
    code, graph = run(*EXISTING)

    assert code == OK and len(graph.patch_bodies[0]) == 3
    assert "kept" in capsys.readouterr().err
