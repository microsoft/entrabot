"""Shared helpers for the Blueprint certificate tests.

These tests run on machines that hold a live Blueprint private key in the OS
keychain, so nothing here may reach the real keychain or the network:

* every test module calls :func:`block_real_keyring_and_network` from an autouse
  fixture, which makes any real keyring or socket access raise;
* the code under test gets an in-memory :class:`FakeKeyring` and a
  :class:`FakeGraph` behind ``httpx.MockTransport``;
* scripts are loaded against a stub ``entra_provisioning`` whose token helpers
  raise, so a test that forgets to patch them fails instead of minting a token.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import socket
import sys
import types
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import keyring
import keyring.backends.macOS
import keyring.core
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

FAKE_OBJECT_ID = "00000000-0000-0000-0000-0000000000b1"
FAKE_APP_ID = "00000000-0000-0000-0000-0000000000a1"
FAKE_TOKEN = "fake-graph-token-value"


def block_real_keyring_and_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any real keychain or network access raise for the current test."""

    def refuse_keyring(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("test attempted to touch the real OS keychain")

    def refuse_network(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("test attempted real network access")

    for name in (
        "get_keyring",
        "get_password",
        "set_password",
        "delete_password",
        "get_credential",
    ):
        monkeypatch.setattr(keyring, name, refuse_keyring)
        monkeypatch.setattr(keyring.core, name, refuse_keyring)
    for name in ("get_password", "set_password", "delete_password", "get_credential"):
        monkeypatch.setattr(keyring.backends.macOS.Keyring, name, refuse_keyring)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse_network)
    monkeypatch.setattr(socket.socket, "connect", refuse_network)
    monkeypatch.setattr(socket, "getaddrinfo", refuse_network)


def _provisioning_stub() -> types.ModuleType:
    def refuse_token(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("test did not patch the Graph token helper")

    stub = types.ModuleType("entra_provisioning")
    stub.get_existing_graph_token = refuse_token
    stub.get_bootstrap_graph_token = refuse_token
    return stub


def load_script(name: str, *, alias: str | None = None) -> types.ModuleType:
    """Load ``scripts/<name>.py`` with ``entra_provisioning`` stubbed.

    The stub is only in ``sys.modules`` while the script is imported, so other
    test modules' own handling of ``entra_provisioning`` is left undisturbed.
    """
    module_name = alias or name
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    previous = sys.modules.get("entra_provisioning")
    sys.modules["entra_provisioning"] = _provisioning_stub()
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            del sys.modules["entra_provisioning"]
        else:
            sys.modules["entra_provisioning"] = previous
    return module


blueprint_certs = load_script("blueprint_certs")


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeCert:
    der: bytes
    private_key_pem: str = field(repr=False)

    @property
    def b64(self) -> str:
        return base64.b64encode(self.der).decode()

    @property
    def thumbprint(self) -> str:
        digest = hashlib.sha256(self.der).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def make_cert(common_name: str = "fake-device") -> FakeCert:
    """A real, throwaway self-signed certificate (EC, so tests stay fast)."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=30))
        .sign(private_key, hashes.SHA256())
    )
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return FakeCert(der=cert.public_bytes(serialization.Encoding.DER), private_key_pem=pem)


def graph_entry(
    cert: FakeCert,
    *,
    key_id: str,
    display_name: str,
    end: str = "2027-06-01T00:00:00Z",
    **extra: Any,
) -> dict[str, Any]:
    """A keyCredentials entry shaped the way Graph returns one."""
    return {
        "customKeyIdentifier": hashlib.sha1(cert.der).hexdigest().upper(),
        "displayName": display_name,
        "endDateTime": end,
        "key": cert.b64,
        "keyId": key_id,
        "startDateTime": "2026-06-01T00:00:00Z",
        "type": "AsymmetricX509Cert",
        "usage": "Verify",
        **extra,
    }


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


class FakeGraph:
    """An in-memory ``/applications/{id}`` endpoint that replaces on PATCH.

    Args:
        entries: the Blueprint's starting ``keyCredentials``.
        get_script: outcomes for upcoming GETs, consumed before normal reads. An
            int is an error status, an exception is raised, ``bytes`` is a raw
            200 body.
        patch_outcome: the PATCH response status, or an exception to raise.
        patch_applies: whether a PATCH takes effect (a 5xx or timeout may still
            have landed; a 4xx from real Graph has not).
        stale_reads: how many GETs after a PATCH still see the old list.
        lost_key_ids: entries that vanish after a PATCH, as if another writer
            raced this one.
        events: optional shared log, for asserting order across Graph and keyring.
    """

    def __init__(
        self,
        entries: Iterable[dict[str, Any]] = (),
        *,
        get_script: Iterable[int | Exception | bytes] = (),
        patch_outcome: int | Exception = 204,
        patch_applies: bool = True,
        stale_reads: int = 0,
        lost_key_ids: Iterable[str] = (),
        events: list[str] | None = None,
    ) -> None:
        self.entries = [dict(entry) for entry in entries]
        self.get_script = deque(get_script)
        self.patch_outcome = patch_outcome
        self.patch_applies = patch_applies
        self.stale_reads = stale_reads
        self.lost_key_ids = set(lost_key_ids)
        self.events = events if events is not None else []
        self.requests: list[httpx.Request] = []
        self.patch_bodies: list[list[dict[str, Any]]] = []
        self._stale_snapshot: list[dict[str, Any]] = []
        self._next_key_id = 0xF000

    @property
    def gets(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.method == "GET"]

    @property
    def patches(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.method == "PATCH"]

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.events.append(request.method)
        if request.method == "PATCH":
            return self._handle_patch(request)
        return self._handle_get()

    def _handle_get(self) -> httpx.Response:
        if self.get_script:
            outcome = self.get_script.popleft()
            if isinstance(outcome, Exception):
                raise outcome
            if isinstance(outcome, bytes):
                return httpx.Response(200, content=outcome)
            return httpx.Response(
                outcome,
                json={"error": {"code": "FakeError", "message": f"fake failure {outcome}"}},
            )
        if self.patch_bodies and self.stale_reads > 0:
            self.stale_reads -= 1
            return httpx.Response(200, json={"keyCredentials": self._stale_snapshot})
        return httpx.Response(200, json={"keyCredentials": self.entries})

    def _handle_patch(self, request: httpx.Request) -> httpx.Response:
        body = httpx.Response(200, content=request.content).json()["keyCredentials"]
        self.patch_bodies.append(body)
        if self.patch_applies:
            self._stale_snapshot = self.entries
            self.entries = [
                self._with_key_id(entry)
                for entry in body
                if entry.get("keyId") not in self.lost_key_ids
            ]
        if isinstance(self.patch_outcome, Exception):
            raise self.patch_outcome
        if self.patch_outcome < 400:
            return httpx.Response(self.patch_outcome)
        return httpx.Response(
            self.patch_outcome,
            json={"error": {"code": "FakeError", "message": f"fake failure {self.patch_outcome}"}},
        )

    def _with_key_id(self, entry: dict[str, Any]) -> dict[str, Any]:
        if entry.get("keyId"):
            return dict(entry)
        self._next_key_id += 1
        return {**entry, "keyId": f"00000000-0000-0000-0000-00000000{self._next_key_id:04x}"}


def use_fake_graph(monkeypatch: pytest.MonkeyPatch, graph: FakeGraph) -> FakeGraph:
    monkeypatch.setattr(blueprint_certs, "_new_graph_client", graph.client)
    return graph


# ---------------------------------------------------------------------------
# Keyring
# ---------------------------------------------------------------------------


class FakeKeyring:
    """In-memory stand-in for the ``keyring`` module, as scripts use it."""

    def __init__(
        self,
        entries: dict[tuple[str, str], str] | None = None,
        *,
        events: list[str] | None = None,
    ) -> None:
        self.entries = dict(entries or {})
        self.events = events if events is not None else []
        self.fail_on: dict[tuple[str, str], Exception] = {}
        self.corrupt_reads: set[tuple[str, str]] = set()

    def _check(self, operation: str, account: str) -> None:
        self.events.append(f"{operation}:{account}")
        error = self.fail_on.get((operation, account))
        if error is not None:
            raise error

    def get_password(self, service: str, account: str) -> str | None:
        self._check("get", account)
        if (service, account) in self.corrupt_reads:
            return "corrupted"
        return self.entries.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self._check("set", account)
        self.entries[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self._check("delete", account)
        if (service, account) not in self.entries:
            raise RuntimeError("no such entry")
        del self.entries[(service, account)]


def raising(error: Exception) -> Callable[..., Any]:
    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    return _raise
