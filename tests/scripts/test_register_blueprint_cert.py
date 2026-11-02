"""Tests for scripts/register_blueprint_cert.py: the setup.sh Step 6 entry point.

Besides the #124 regression (attaching a machine must not remove other machines'
certificates) these cover the private key's journey: it stays under a pending
account until its certificate is confirmed on the Blueprint, and it is not thrown
away while a write may have landed. Losing the key after a --remove-all-existing-certs
write would lock out every machine.
"""

from __future__ import annotations

import base64
import json
import socket
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from tests.scripts._blueprint_cert_helpers import (
    FAKE_APP_ID,
    FAKE_OBJECT_ID,
    FAKE_TOKEN,
    FakeGraph,
    FakeKeyring,
    block_real_keyring_and_network,
    blueprint_certs,
    graph_entry,
    load_script,
    make_cert,
    raising,
    use_fake_graph,
)

reg = load_script("register_blueprint_cert")

FLAG = "--remove-all-existing-certs"
REAL = ("entrabot", "blueprint-private-key")
PENDING = ("entrabot", "blueprint-private-key.pending")

EXIT_OK = 0
EXIT_NOT_REGISTERED = 1
EXIT_USAGE = 2
EXIT_UNCONFIRMED = 3

HOST_A = "EntraBot Device Certificate — host-a"
HOST_B = "EntraBot Device Certificate — host-b"

CERT_A = make_cert("host-a")
CERT_B = make_cert("host-b")
RETAINED = make_cert("retained")
OLD_REAL_KEY = "old-real-private-key-placeholder"


@pytest.fixture(autouse=True)
def _no_real_keychain_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_real_keyring_and_network(monkeypatch)


class FakeStdin:
    def __init__(self, *, tty: bool, answer: str | None = None) -> None:
        self._tty = tty
        self._answer = answer

    def isatty(self) -> bool:
        return self._tty

    def readline(self) -> str:
        if self._answer is None:
            raise AssertionError("the script tried to read an answer it should not need")
        return self._answer


@dataclass
class Env:
    monkeypatch: pytest.MonkeyPatch
    state_file: Path
    keyring: FakeKeyring
    events: list[str]
    sleeps: list[float] = field(default_factory=list)

    def graph(self, *entries: dict[str, Any], **kwargs: Any) -> FakeGraph:
        return use_fake_graph(self.monkeypatch, FakeGraph(entries, events=self.events, **kwargs))

    def stdin(self, *, tty: bool, answer: str | None = None) -> None:
        self.monkeypatch.setattr(sys, "stdin", FakeStdin(tty=tty, answer=answer))

    def run(self, *extra: str) -> int:
        return reg.main(
            [
                "--blueprint-object-id",
                FAKE_OBJECT_ID,
                "--blueprint-app-id",
                FAKE_APP_ID,
                "--state-file",
                str(self.state_file),
                *extra,
            ],
            sleep=self.sleeps.append,
        )

    @property
    def real_key(self) -> str | None:
        return self.keyring.entries.get(REAL)

    @property
    def pending_key(self) -> str | None:
        return self.keyring.entries.get(PENDING)

    @property
    def state(self) -> dict[str, Any]:
        return json.loads(self.state_file.read_text())


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Env:
    events: list[str] = []
    fake_keyring = FakeKeyring({REAL: OLD_REAL_KEY}, events=events)
    monkeypatch.setattr(reg, "keyring", fake_keyring)

    def backend_check() -> str:
        events.append("backend-check")
        return "fake-backend"

    def token(*, wait_for_propagation: bool = True) -> str:
        assert wait_for_propagation is False
        events.append("token")
        return FAKE_TOKEN

    monkeypatch.setattr(reg, "assert_allowed_keyring_backend", backend_check)
    monkeypatch.setattr(reg, "get_bootstrap_graph_token", token)
    monkeypatch.setattr(socket, "gethostname", lambda: "fake-host.local")
    monkeypatch.setattr(sys, "stdin", FakeStdin(tty=False))

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"BLUEPRINT_APP_ID": FAKE_APP_ID, "BLUEPRINT_SECRET": "old-secret"})
    )
    return Env(monkeypatch, state_file, fake_keyring, events)


def _existing() -> list[dict[str, Any]]:
    return [
        graph_entry(
            CERT_A,
            key_id="00000000-0000-0000-0000-00000000000a",
            display_name=HOST_A,
            end="2027-01-02T00:00:00Z",
        ),
        graph_entry(
            CERT_B,
            key_id="00000000-0000-0000-0000-00000000000b",
            display_name=HOST_B,
            end="2027-03-04T00:00:00Z",
        ),
    ]


def _cert_from(entry: dict[str, Any]) -> x509.Certificate:
    return x509.load_der_x509_certificate(base64.b64decode(entry["key"]))


def _spki(cert: x509.Certificate) -> bytes:
    return cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _keyring_writes(env: Env) -> list[str]:
    """Every keyring write or delete the run attempted, in order."""
    return [event for event in env.events if event.startswith(("set:", "delete:"))]


class TestAddByDefault:
    def test_keeps_every_existing_certificate_and_prints_only_the_thumbprint(
        self, env: Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        graph = env.graph(*_existing())

        assert env.run() == EXIT_OK

        body = graph.patch_bodies[0]
        assert body[:2] == _existing()
        assert len(body) == 3
        out = capsys.readouterr().out
        assert out == blueprint_certs.thumbprint_of_der(base64.b64decode(body[2]["key"])) + "\n"
        assert len(out.strip()) == 43

    def test_says_how_many_it_is_adding_to_and_does_not_prompt(
        self, env: Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env.stdin(tty=True)  # readline() would raise
        env.graph(*_existing())

        assert env.run() == EXIT_OK

        assert "adding to 2 existing certificate(s)" in capsys.readouterr().err

    def test_a_first_certificate_on_an_empty_blueprint(
        self, env: Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        graph = env.graph()
        assert env.run() == EXIT_OK
        assert len(graph.patch_bodies[0]) == 1
        assert "adding to 0 existing certificate(s)" in capsys.readouterr().err

    def test_two_hosts_with_the_same_name_as_this_one_both_survive(self, env: Env) -> None:
        same_name = "EntraBot Device Certificate — fake-host"
        graph = env.graph(
            graph_entry(CERT_A, key_id="k-1", display_name=same_name),
            graph_entry(CERT_B, key_id="k-2", display_name=same_name),
        )

        assert env.run() == EXIT_OK

        assert [entry["displayName"] for entry in graph.entries] == [same_name] * 3
        assert {"k-1", "k-2"} <= {entry["keyId"] for entry in graph.entries}


class TestGeneratedCertificate:
    def test_matches_what_setup_has_always_generated(self, env: Env) -> None:
        graph = env.graph()

        assert env.run() == EXIT_OK

        entry = graph.patch_bodies[0][0]
        cert = _cert_from(entry)
        assert isinstance(cert.public_key(), rsa.RSAPublicKey)
        assert cert.public_key().key_size == 2048
        assert cert.public_key().public_numbers().e == 65537
        assert cert.signature_hash_algorithm.name == "sha256"
        assert cert.subject == cert.issuer
        assert (
            cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
            == f"entrabot-blueprint-{FAKE_APP_ID}"
        )
        assert (
            cert.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)[0].value
            == "EntraBot Device Agent"
        )
        assert cert.not_valid_after_utc - cert.not_valid_before_utc == timedelta(days=365)
        assert entry["displayName"] == "EntraBot Device Certificate — fake-host"
        assert entry["type"] == "AsymmetricX509Cert"
        assert entry["usage"] == "Verify"
        assert entry["startDateTime"].endswith(".0000000Z")
        assert entry["endDateTime"].endswith(".0000000Z")

    def test_stored_key_matches_the_registered_certificate(self, env: Env) -> None:
        graph = env.graph()
        assert env.run() == EXIT_OK
        cert = _cert_from(graph.patch_bodies[0][0])
        assert blueprint_certs.public_key_of_private_pem(env.real_key) == _spki(cert)

    def test_repr_of_generated_certificate_hides_the_private_key(self) -> None:
        generated = reg.GeneratedCertificate(
            private_key_pem="SECRET-PEM", der_bytes=b"der", thumbprint="thumb"
        )
        assert "SECRET-PEM" not in repr(generated)


class TestOrdering:
    def test_key_is_stored_and_read_back_then_registered_then_promoted(self, env: Env) -> None:
        env.graph(*_existing())

        assert env.run() == EXIT_OK

        assert env.events == [
            "backend-check",
            "token",
            "GET",
            "get:blueprint-private-key.pending",
            "set:blueprint-private-key.pending",
            "get:blueprint-private-key.pending",
            "GET",
            "PATCH",
            "GET",
            "set:blueprint-private-key",
            "get:blueprint-private-key",
            "delete:blueprint-private-key.pending",
        ]
        assert env.pending_key is None
        assert env.real_key != OLD_REAL_KEY

    def test_promotion_waits_for_verification_through_replication_lag(self, env: Env) -> None:
        env.graph(*_existing(), stale_reads=3)

        assert env.run() == EXIT_OK

        assert env.sleeps == [blueprint_certs.VERIFY_DELAY_SECONDS] * 3
        after_patch = env.events[env.events.index("PATCH") + 1 :]
        assert after_patch == [
            "GET",
            "GET",
            "GET",
            "GET",
            "set:blueprint-private-key",
            "get:blueprint-private-key",
            "delete:blueprint-private-key.pending",
        ]

    def test_backend_check_failure_stops_everything(self, env: Env) -> None:
        env.monkeypatch.setattr(
            reg, "assert_allowed_keyring_backend", raising(RuntimeError("insecure backend"))
        )
        env.monkeypatch.setattr(
            reg.rsa, "generate_private_key", raising(AssertionError("generated a key"))
        )
        graph = env.graph(*_existing())

        assert env.run() == EXIT_NOT_REGISTERED

        assert graph.requests == []
        assert env.events == []
        assert env.real_key == OLD_REAL_KEY


class TestStateFile:
    def test_records_the_thumbprint_and_removes_the_old_secret(
        self, env: Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env.graph(*_existing())

        assert env.run() == EXIT_OK

        assert env.state == {
            "BLUEPRINT_APP_ID": FAKE_APP_ID,
            "BLUEPRINT_CERT_THUMBPRINT": capsys.readouterr().out.strip(),
        }
        assert env.state_file.read_text().endswith("\n")

    def test_creates_a_missing_state_file(self, env: Env) -> None:
        env.state_file.unlink()
        env.graph()
        assert env.run() == EXIT_OK
        assert "BLUEPRINT_CERT_THUMBPRINT" in env.state

    def test_state_write_failure_is_reported_without_printing_a_thumbprint(
        self, env: Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env.state_file.unlink()
        env.state_file.mkdir()
        env.graph()

        assert env.run() == EXIT_UNCONFIRMED

        assert capsys.readouterr().out == ""
        assert env.real_key != OLD_REAL_KEY  # registered and promoted; only the record failed

    @pytest.mark.parametrize(
        "landed_earlier", [False, True], ids=["registered-now", "landed-earlier"]
    )
    def test_a_state_write_failure_keeps_the_pending_key_until_a_later_run_records_it(
        self, env: Env, capsys: pytest.CaptureFixture[str], landed_earlier: bool
    ) -> None:
        # The new key is already the active one. The pending entry is what lets the next run
        # find its certificate and record the thumbprint; deleting it first would strand the
        # key beside the old thumbprint.
        if landed_earlier:
            env.keyring.entries[PENDING] = RETAINED.private_key_pem
            graph = env.graph(
                *_existing(), graph_entry(RETAINED, key_id="k-retained", display_name=HOST_A)
            )
        else:
            graph = env.graph(*_existing())
        env.state_file.unlink()
        env.state_file.mkdir()  # the write fails: a directory is in the way

        assert env.run() == EXIT_UNCONFIRMED

        assert capsys.readouterr().out == ""
        assert env.real_key != OLD_REAL_KEY
        assert env.pending_key == env.real_key
        key_after_failure = env.real_key
        patches_after_failure = len(graph.patches)

        env.state_file.rmdir()
        assert env.run() == EXIT_OK

        assert len(graph.patches) == patches_after_failure  # recovered without another write
        assert env.real_key == key_after_failure
        assert env.pending_key is None
        match = blueprint_certs.find_credential_for_public_key(
            [blueprint_certs.KeyCredential(raw=entry) for entry in graph.entries],
            blueprint_certs.public_key_of_private_pem(env.real_key),
        )
        assert match is not None
        assert capsys.readouterr().out == match.thumbprint + "\n"
        assert env.state["BLUEPRINT_CERT_THUMBPRINT"] == match.thumbprint


class TestStdout:
    def test_noisy_token_helper_stays_off_stdout(
        self, env: Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def noisy(*, wait_for_propagation: bool = True) -> str:
            print("  Ensuring 25 Graph application permissions on provisioner app...")
            return FAKE_TOKEN

        env.monkeypatch.setattr(reg, "get_bootstrap_graph_token", noisy)
        env.graph()

        assert env.run() == EXIT_OK

        captured = capsys.readouterr()
        assert len(captured.out.strip()) == 43
        assert "\n" not in captured.out.strip()
        assert "Ensuring 25" in captured.err

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"get_script": [500]},
            {"patch_outcome": 403, "patch_applies": False},
            {"patch_outcome": 503, "patch_applies": False},
        ],
        ids=["success", "read-failure", "rejected", "unconfirmed"],
    )
    def test_no_path_prints_key_material_or_the_token(
        self, env: Env, capsys: pytest.CaptureFixture[str], kwargs: dict[str, Any]
    ) -> None:
        env.graph(*_existing(), **kwargs)
        env.run()
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "PRIVATE KEY" not in combined
        assert FAKE_TOKEN not in combined
        assert OLD_REAL_KEY not in combined


class TestFailClosed:
    @pytest.mark.parametrize(
        "outcome",
        [403, 500, 503, httpx.ConnectTimeout("x"), b"not json", b"{}"],
        ids=repr,
    )
    def test_failed_first_read_generates_nothing_and_never_patches(
        self, env: Env, capsys: pytest.CaptureFixture[str], outcome: Any
    ) -> None:
        env.monkeypatch.setattr(
            reg.rsa, "generate_private_key", raising(AssertionError("generated a key"))
        )
        graph = env.graph(*_existing(), get_script=[outcome])

        assert env.run() == EXIT_NOT_REGISTERED

        assert graph.patches == []
        assert graph.entries == _existing()
        assert env.real_key == OLD_REAL_KEY
        assert env.pending_key is None
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize("outcome", [403, 500, httpx.ReadTimeout("x"), b"{}"], ids=repr)
    def test_failed_read_just_before_the_write_never_patches(
        self, env: Env, capsys: pytest.CaptureFixture[str], outcome: Any
    ) -> None:
        graph = env.graph(*_existing())
        original = reg.fetch_key_credentials

        def first_read_then_break_graph(*args: Any) -> Any:
            credentials = original(*args)
            graph.get_script.append(outcome)
            return credentials

        env.monkeypatch.setattr(reg, "fetch_key_credentials", first_read_then_break_graph)

        assert env.run() == EXIT_NOT_REGISTERED

        assert graph.patches == []
        assert env.real_key == OLD_REAL_KEY
        assert env.pending_key is None
        assert capsys.readouterr().out == ""

    def test_token_failure_changes_nothing(self, env: Env) -> None:
        env.monkeypatch.setattr(
            reg, "get_bootstrap_graph_token", raising(RuntimeError("fake token failure"))
        )
        graph = env.graph(*_existing())

        assert env.run() == EXIT_NOT_REGISTERED

        assert graph.requests == []
        assert env.real_key == OLD_REAL_KEY
        assert env.pending_key is None


class TestPatchFailure:
    @pytest.mark.parametrize("status", [400, 401, 403, 409, 429])
    def test_rejection_keeps_the_real_key_and_removes_the_pending_one(
        self, env: Env, capsys: pytest.CaptureFixture[str], status: int
    ) -> None:
        before = env.state_file.read_text()
        graph = env.graph(*_existing(), patch_outcome=status, patch_applies=False)

        assert env.run() == EXIT_NOT_REGISTERED

        assert env.real_key == OLD_REAL_KEY
        assert env.pending_key is None
        assert graph.entries == _existing()
        assert env.state_file.read_text() == before
        captured = capsys.readouterr()
        assert captured.out == ""
        assert f"fake failure {status}" in captured.err


class TestAKeyIsNeverLostWhileAWriteMayHaveLanded:
    """The lockout #124's fix must not introduce."""

    @pytest.mark.parametrize("extra", [(), (FLAG,)], ids=["add", "remove-all"])
    def test_write_landed_but_could_not_be_verified_retains_the_pending_key(
        self, env: Env, capsys: pytest.CaptureFixture[str], extra: tuple[str, ...]
    ) -> None:
        # stale_reads never ends: the write is in Entra, but no read ever shows it.
        graph = env.graph(*_existing(), stale_reads=10_000)

        assert env.run(*extra) == EXIT_UNCONFIRMED

        assert graph.patches != []
        assert env.pending_key is not None
        assert env.real_key == OLD_REAL_KEY
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "re-run" in captured.err.lower()

    def test_missing_prior_certificate_is_reported_by_name_and_retains_the_key(
        self, env: Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env.graph(*_existing(), lost_key_ids=[_existing()[1]["keyId"]])

        assert env.run() == EXIT_UNCONFIRMED

        assert env.pending_key is not None
        assert env.real_key == OLD_REAL_KEY
        assert HOST_B in capsys.readouterr().err

    @pytest.mark.parametrize("outcome", [500, 503, 504, httpx.ReadTimeout("x")], ids=repr)
    def test_ambiguous_patch_retains_the_key_when_nothing_can_be_confirmed(
        self, env: Env, outcome: Any
    ) -> None:
        env.graph(*_existing(), patch_outcome=outcome, patch_applies=False)

        assert env.run() == EXIT_UNCONFIRMED

        assert env.pending_key is not None
        assert env.real_key == OLD_REAL_KEY

    @pytest.mark.parametrize("outcome", [503, httpx.ReadTimeout("x")], ids=repr)
    def test_ambiguous_patch_that_landed_is_a_success(self, env: Env, outcome: Any) -> None:
        env.graph(*_existing(), patch_outcome=outcome)

        assert env.run() == EXIT_OK

        assert env.pending_key is None
        assert env.real_key != OLD_REAL_KEY

    def test_promotion_failure_retains_the_pending_key(
        self, env: Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env.keyring.fail_on[("set", REAL[1])] = RuntimeError("keychain locked")
        env.graph(*_existing())

        assert env.run() == EXIT_UNCONFIRMED

        assert env.pending_key is not None
        assert env.real_key == OLD_REAL_KEY
        assert capsys.readouterr().out == ""

    def test_promotion_read_back_mismatch_retains_the_pending_key(self, env: Env) -> None:
        env.keyring.corrupt_reads.add(REAL)
        env.graph(*_existing())

        assert env.run() == EXIT_UNCONFIRMED

        assert env.pending_key is not None

    def test_failure_to_remove_the_pending_entry_after_promotion_is_only_a_warning(
        self, env: Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env.keyring.fail_on[("delete", PENDING[1])] = RuntimeError("keychain busy")
        env.graph(*_existing())

        assert env.run() == EXIT_OK

        captured = capsys.readouterr()
        assert len(captured.out.strip()) == 43
        assert env.real_key != OLD_REAL_KEY
        assert "pending" in captured.err.lower()


class TestPendingStorageFailure:
    def test_failure_to_store_means_nothing_is_registered(self, env: Env) -> None:
        env.keyring.fail_on[("set", PENDING[1])] = RuntimeError("keychain locked")
        graph = env.graph(*_existing())

        assert env.run() == EXIT_NOT_REGISTERED

        assert graph.patches == []
        assert env.real_key == OLD_REAL_KEY

    def test_failed_read_back_means_nothing_is_registered(self, env: Env) -> None:
        env.keyring.corrupt_reads.add(PENDING)
        graph = env.graph(*_existing())

        # The corrupt read looks like a retained key at first, and that key is
        # unusable, so it is discarded; the fresh key's read-back then fails too.
        assert env.run() == EXIT_NOT_REGISTERED

        assert graph.patches == []
        assert env.real_key == OLD_REAL_KEY
        assert env.pending_key is None

    def test_failure_to_read_a_possible_retained_key_stops_before_generating(
        self, env: Env
    ) -> None:
        env.keyring.fail_on[("get", PENDING[1])] = RuntimeError("keychain locked")
        env.monkeypatch.setattr(
            reg.rsa, "generate_private_key", raising(AssertionError("generated a key"))
        )
        graph = env.graph(*_existing())

        assert env.run() == EXIT_NOT_REGISTERED

        assert graph.patches == []


class TestRecoveryFromAnEarlierRun:
    def _retain(self, env: Env, pem: str = RETAINED.private_key_pem) -> None:
        env.keyring.entries[PENDING] = pem

    def test_retained_key_whose_cert_landed_is_promoted_without_a_new_write(
        self, env: Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._retain(env)
        graph = env.graph(
            *_existing(), graph_entry(RETAINED, key_id="k-retained", display_name=HOST_A)
        )

        assert env.run() == EXIT_OK

        assert graph.patches == []
        assert env.real_key == RETAINED.private_key_pem
        assert env.pending_key is None
        assert capsys.readouterr().out == RETAINED.thumbprint + "\n"
        assert env.state["BLUEPRINT_CERT_THUMBPRINT"] == RETAINED.thumbprint

    def test_retained_key_with_no_cert_is_reused_not_overwritten(self, env: Env) -> None:
        # An earlier write could still appear; a new key would orphan it.
        self._retain(env)
        env.monkeypatch.setattr(
            reg.rsa, "generate_private_key", raising(AssertionError("generated a key"))
        )
        graph = env.graph(*_existing())

        assert env.run() == EXIT_OK

        cert = _cert_from(graph.patch_bodies[0][-1])
        assert _spki(cert) == blueprint_certs.public_key_of_private_pem(RETAINED.private_key_pem)
        assert env.real_key == RETAINED.private_key_pem
        assert env.pending_key is None

    def test_retained_key_survives_a_later_rejection(self, env: Env) -> None:
        self._retain(env)
        env.graph(*_existing(), patch_outcome=403, patch_applies=False)

        assert env.run() == EXIT_NOT_REGISTERED

        assert env.pending_key == RETAINED.private_key_pem
        assert env.real_key == OLD_REAL_KEY

    def test_retained_key_survives_a_later_read_failure(self, env: Env) -> None:
        self._retain(env)
        env.graph(*_existing(), get_script=[503])

        assert env.run() == EXIT_NOT_REGISTERED

        assert env.pending_key == RETAINED.private_key_pem

    def test_unusable_retained_entry_is_discarded(self, env: Env) -> None:
        self._retain(env, pem="not a pem")
        graph = env.graph(*_existing())

        assert env.run() == EXIT_OK

        assert len(graph.patch_bodies[0]) == 3
        assert env.pending_key is None
        assert env.real_key not in (OLD_REAL_KEY, "not a pem")

    def _landed(self, env: Env, **graph_kwargs: Any) -> FakeGraph:
        """A retained pending key whose certificate is already on the Blueprint."""
        self._retain(env)
        return env.graph(
            *_existing(),
            graph_entry(RETAINED, key_id="k-retained", display_name=HOST_A),
            **graph_kwargs,
        )

    def test_a_remove_all_that_is_declined_leaves_a_landed_retained_key_alone(
        self, env: Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Promoting the key before the prompt would pair it with the old thumbprint.
        before = env.state_file.read_text()
        env.stdin(tty=True, answer="n\n")
        env.monkeypatch.setattr(
            reg.rsa, "generate_private_key", raising(AssertionError("generated a key"))
        )
        graph = self._landed(env)

        assert env.run(FLAG) == EXIT_NOT_REGISTERED

        assert graph.patches == []
        assert env.real_key == OLD_REAL_KEY
        assert env.pending_key == RETAINED.private_key_pem
        assert env.state_file.read_text() == before
        assert _keyring_writes(env) == []
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "nothing was changed" in captured.err.lower()

    @pytest.mark.parametrize("status", [403, 409])
    def test_a_remove_all_that_is_rejected_leaves_a_landed_retained_key_alone(
        self, env: Env, capsys: pytest.CaptureFixture[str], status: int
    ) -> None:
        before = env.state_file.read_text()
        graph = self._landed(env, patch_outcome=status, patch_applies=False)
        listed = [dict(entry) for entry in graph.entries]

        assert env.run(FLAG) == EXIT_NOT_REGISTERED

        assert len(graph.patches) == 1
        assert graph.entries == listed
        assert env.real_key == OLD_REAL_KEY
        assert env.pending_key == RETAINED.private_key_pem
        assert env.state_file.read_text() == before
        assert _keyring_writes(env) == []
        assert capsys.readouterr().out == ""

    def test_a_remove_all_reuses_a_landed_retained_key_for_the_replacement(
        self, env: Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env.monkeypatch.setattr(
            reg.rsa, "generate_private_key", raising(AssertionError("generated a key"))
        )
        graph = self._landed(env)

        assert env.run(FLAG) == EXIT_OK

        # Nothing is promoted until the replacement is on the Blueprint.
        assert env.events.index("PATCH") < env.events.index("set:blueprint-private-key")
        assert len(graph.patch_bodies) == 1
        assert len(graph.entries) == 1
        replacement = graph.entries[0]
        assert _spki(_cert_from(replacement)) == blueprint_certs.public_key_of_private_pem(
            RETAINED.private_key_pem
        )
        thumbprint = capsys.readouterr().out.strip()
        assert thumbprint == blueprint_certs.thumbprint_of_der(base64.b64decode(replacement["key"]))
        assert thumbprint != RETAINED.thumbprint  # a new certificate, for the same key
        assert env.real_key == RETAINED.private_key_pem
        assert env.pending_key is None
        assert env.state["BLUEPRINT_CERT_THUMBPRINT"] == thumbprint


class TestRemoveAll:
    def test_replaces_the_list_with_only_the_new_certificate(self, env: Env) -> None:
        graph = env.graph(*_existing())

        assert env.run(FLAG) == EXIT_OK

        assert len(graph.patch_bodies) == 1
        assert len(graph.patch_bodies[0]) == 1
        assert graph.entries[0]["key"] == graph.patch_bodies[0][0]["key"]
        assert env.pending_key is None
        assert env.real_key != OLD_REAL_KEY

    def test_lists_names_and_expiry_only(
        self, env: Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env.graph(*_existing())

        assert env.run(FLAG) == EXIT_OK

        err = capsys.readouterr().err
        for entry in _existing():
            assert entry["displayName"] in err
            assert entry["endDateTime"][:10] in err
            assert entry["keyId"] not in err
            assert entry["key"] not in err
            assert entry["customKeyIdentifier"] not in err

    def test_says_the_blueprint_itself_is_not_deleted(
        self, env: Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env.graph(*_existing())
        assert env.run(FLAG) == EXIT_OK
        assert "does not delete the Blueprint" in capsys.readouterr().err

    def test_without_a_terminal_the_flag_is_consent_and_says_so_loudly(
        self, env: Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env.stdin(tty=False)  # readline() would raise
        graph = env.graph(*_existing())

        assert env.run(FLAG) == EXIT_OK

        assert len(graph.patch_bodies[0]) == 1
        err = capsys.readouterr().err
        assert "explicit consent" in err
        assert "REMOVING ALL 2 CERTIFICATE(S)" in err

    @pytest.mark.parametrize("answer", ["y\n", "Y\n", "yes\n", "YES\n", "  y  \n"])
    def test_on_a_terminal_yes_proceeds(
        self, env: Env, capsys: pytest.CaptureFixture[str], answer: str
    ) -> None:
        env.stdin(tty=True, answer=answer)
        graph = env.graph(*_existing())

        assert env.run(FLAG) == EXIT_OK

        assert len(graph.patch_bodies[0]) == 1
        captured = capsys.readouterr()
        assert "[y/N]" in captured.err
        assert "[y/N]" not in captured.out

    @pytest.mark.parametrize("answer", ["n\n", "N\n", "\n", "", "maybe\n", "no\n"])
    def test_on_a_terminal_anything_else_changes_nothing(
        self, env: Env, capsys: pytest.CaptureFixture[str], answer: str
    ) -> None:
        before = env.state_file.read_text()
        env.stdin(tty=True, answer=answer)
        env.monkeypatch.setattr(
            reg.rsa, "generate_private_key", raising(AssertionError("generated a key"))
        )
        graph = env.graph(*_existing())

        assert env.run(FLAG) == EXIT_NOT_REGISTERED

        assert graph.patches == []
        assert graph.entries == _existing()
        assert env.real_key == OLD_REAL_KEY
        assert env.pending_key is None
        assert env.state_file.read_text() == before
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "nothing was changed" in captured.err.lower()

    def test_an_empty_blueprint_needs_no_confirmation(self, env: Env) -> None:
        env.stdin(tty=True)  # readline() would raise
        graph = env.graph()
        assert env.run(FLAG) == EXIT_OK
        assert len(graph.patch_bodies[0]) == 1

    @pytest.mark.parametrize("outcome", [403, 503, httpx.ConnectTimeout("x"), b"{}"], ids=repr)
    def test_failed_read_stops_before_any_prompt_or_write(self, env: Env, outcome: Any) -> None:
        env.stdin(tty=True)  # readline() would raise
        graph = env.graph(*_existing(), get_script=[outcome])

        assert env.run(FLAG) == EXIT_NOT_REGISTERED

        assert graph.patches == []
        assert env.real_key == OLD_REAL_KEY

    def test_warns_if_a_removed_certificate_is_still_listed(
        self, env: Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        graph = env.graph(*_existing())
        original = graph._handle_patch

        def another_writer_put_one_back(request: httpx.Request) -> httpx.Response:
            response = original(request)
            graph.entries.append(_existing()[0])
            return response

        env.monkeypatch.setattr(graph, "_handle_patch", another_writer_put_one_back)

        assert env.run(FLAG) == EXIT_OK

        err = capsys.readouterr().err
        assert "still listed" in err
        assert HOST_A in err


class TestCommandLine:
    @pytest.fixture(autouse=True)
    def _nothing_may_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("assert_allowed_keyring_backend", "get_bootstrap_graph_token"):
            monkeypatch.setattr(reg, name, raising(AssertionError(f"{name} ran")))

    def test_help_exits_before_any_work(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as caught:
            reg.main(["--help"])
        assert caught.value.code == 0
        out = capsys.readouterr().out
        assert FLAG in out
        assert "does not delete the Blueprint" in " ".join(out.split())

    @pytest.mark.parametrize(
        "argv",
        [
            [],
            ["--blueprint-object-id", "x"],
            ["--blueprint-object-id", "x", "--blueprint-app-id", "y"],
            ["--unknown"],
        ],
    )
    def test_bad_arguments_exit_2(self, argv: list[str]) -> None:
        with pytest.raises(SystemExit) as caught:
            reg.main(argv)
        assert caught.value.code == EXIT_USAGE
