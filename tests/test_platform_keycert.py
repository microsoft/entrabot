"""The macOS and Linux certificate store: one keystore item per certificate, key included.

Nothing here touches a real Keychain or keyring: ``keycert._store`` is replaced by a
dictionary-backed fake with the ``CredentialStore`` shape.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from entrabot.platform import keycert
from entrabot.platform.keycert import CertStoreError

TENANT = "11111111-2222-3333-4444-555555555555"
APP_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
FRIENDLY = f"Entrabot Blueprint {APP_ID}"


class FakeStore:
    """A ``CredentialStore`` held in memory. *broken* makes every call raise."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.broken: Exception | None = None
        self.undeletable: set[tuple[str, str]] = set()

    def store(self, service: str, key: str, value: str) -> None:
        if self.broken:
            raise self.broken
        self.items[(service, key)] = value

    def retrieve(self, service: str, key: str) -> str | None:
        if self.broken:
            raise self.broken
        return self.items.get((service, key))

    def delete(self, service: str, key: str) -> None:
        if self.broken:
            raise self.broken
        if (service, key) in self.undeletable:
            return
        self.items.pop((service, key), None)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    fake = FakeStore()
    monkeypatch.setattr(keycert, "_store", lambda: fake)
    monkeypatch.setattr(keycert, "_platform", lambda: "darwin")
    keycert.forget_setup_certs()
    return fake


def _create(friendly: str = FRIENDLY, days: int = 365) -> keycert.StoreCert:
    cert, reason = keycert.create_signing_cert(
        subject=f"CN=Entrabot Blueprint {APP_ID[:8]}", friendly_name=friendly, days=days
    )
    assert reason is None
    return cert


def _key_and_cert(days: int = 365) -> tuple[str, bytes]:
    """A private key (PEM) and a matching self-signed certificate (DER), made outside the store."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "legacy")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pem, cert.public_bytes(serialization.Encoding.DER)


# ── create, find, sign ───────────────────────────────────────────────────────


def test_create_stores_one_item_named_by_the_thumbprint(store: FakeStore) -> None:
    cert = _create()

    assert len(cert.sha1_hex) == 40 and cert.sha1_hex == cert.sha1_hex.upper()
    assert len(cert.x5t_s256) == 43
    assert cert.friendly_name == FRIENDLY
    assert cert.has_private_key
    assert cert.ksp == keycert.KSP_KEYCHAIN
    assert (keycert.SERVICE, cert.sha1_hex) in store.items
    # The legacy single slot is never written: a second agent can't overwrite the first's key.
    assert ("entrabot", "blueprint-private-key") not in store.items


def test_linux_names_the_keyring(store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keycert, "_platform", lambda: "linux")
    assert _create().ksp == keycert.KSP_KEYRING


def test_created_certificate_is_signature_only_and_in_date(store: FakeStore) -> None:
    cert = _create(days=365)
    parsed = x509.load_der_x509_certificate(cert.der)

    usage = parsed.extensions.get_extension_for_class(x509.KeyUsage).value
    assert usage.digital_signature and not usage.key_encipherment
    with pytest.raises(x509.ExtensionNotFound):
        parsed.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    now = datetime.now(UTC)
    assert cert.not_before < now < cert.not_after
    assert 363 <= (cert.not_after - now).days <= 365


def test_find_returns_what_was_created_and_none_for_another(store: FakeStore) -> None:
    cert = _create()

    found = keycert.find_cert(cert.sha1_hex.lower())
    assert found is not None and found.der == cert.der and found.friendly_name == FRIENDLY
    assert keycert.find_cert("0" * 40) is None


def test_two_agents_keep_two_keys(store: FakeStore) -> None:
    first = _create("Entrabot Blueprint one")
    second = _create("Entrabot Blueprint two")

    assert first.sha1_hex != second.sha1_hex
    assert keycert.load_private_key_pem(first.sha1_hex) != keycert.load_private_key_pem(
        second.sha1_hex
    )
    assert {c.friendly_name for c in keycert.list_certs()} == {
        "Entrabot Blueprint one",
        "Entrabot Blueprint two",
    }
    assert [c.sha1_hex for c in keycert.list_certs(friendly_name="Entrabot Blueprint two")] == [
        second.sha1_hex
    ]


def test_test_sign_passes_for_a_created_certificate(store: FakeStore) -> None:
    keycert.test_sign(_create().sha1_hex)


def test_test_sign_fails_when_the_key_is_not_the_certificates(store: FakeStore) -> None:
    cert = _create()
    other_key, _ = _key_and_cert()
    bundle = store.items[(keycert.SERVICE, cert.sha1_hex)]
    certificate_part = bundle[bundle.index("-----BEGIN CERTIFICATE-----") :]
    store.items[(keycert.SERVICE, cert.sha1_hex)] = other_key + certificate_part

    with pytest.raises(CertStoreError, match="does not match"):
        keycert.test_sign(cert.sha1_hex)


def test_test_sign_fails_for_a_missing_certificate(store: FakeStore) -> None:
    with pytest.raises(CertStoreError, match="not in"):
        keycert.test_sign("A" * 40)


def test_a_bad_thumbprint_is_refused(store: FakeStore) -> None:
    with pytest.raises(CertStoreError, match="40 hexadecimal"):
        keycert.find_cert("not-a-thumbprint")


def test_load_private_key_pem_is_the_stored_key(store: FakeStore) -> None:
    cert = _create()
    pem = keycert.load_private_key_pem(cert.sha1_hex)

    assert pem is not None and pem.startswith("-----BEGIN PRIVATE KEY-----")
    assert keycert.load_private_key_pem("B" * 40) is None


# ── delete ───────────────────────────────────────────────────────────────────


def test_delete_removes_the_item_and_its_index_entry(store: FakeStore) -> None:
    cert = _create()

    assert keycert.delete_cert(cert.sha1_hex) is True
    assert keycert.find_cert(cert.sha1_hex) is None
    assert keycert.list_certs() == []
    assert keycert.delete_cert(cert.sha1_hex) is False
    # Seen on a real run: the index stayed behind, empty, after the last certificate went.
    assert store.items == {}


def test_the_index_stays_while_another_certificate_is_there(store: FakeStore) -> None:
    first = _create("Entrabot Blueprint one")
    second = _create("Entrabot Blueprint two")

    keycert.delete_cert(first.sha1_hex)

    assert json.loads(store.items[(keycert.SERVICE, keycert.INDEX_ACCOUNT)]) == [second.sha1_hex]


def test_delete_that_leaves_the_item_behind_raises(store: FakeStore) -> None:
    cert = _create()
    store.undeletable.add((keycert.SERVICE, cert.sha1_hex))

    with pytest.raises(CertStoreError, match="still"):
        keycert.delete_cert(cert.sha1_hex)
    assert keycert.find_cert(cert.sha1_hex) is not None


def test_delete_takes_the_legacy_slot_only_when_it_holds_the_same_key(store: FakeStore) -> None:
    pem, der = _key_and_cert()
    adopted = keycert.import_cert(private_key_pem=pem, der=der, friendly_name=FRIENDLY)
    store.items[(keycert.LEGACY_SERVICE, keycert.LEGACY_ACCOUNT)] = pem
    keycert.delete_cert(adopted.sha1_hex)
    assert (keycert.LEGACY_SERVICE, keycert.LEGACY_ACCOUNT) not in store.items

    other = _create()
    store.items[(keycert.LEGACY_SERVICE, keycert.LEGACY_ACCOUNT)] = pem  # another agent's key
    keycert.delete_cert(other.sha1_hex)
    assert store.items[(keycert.LEGACY_SERVICE, keycert.LEGACY_ACCOUNT)] == pem


# ── adopting a key from an older setup ───────────────────────────────────────


def test_import_cert_stores_the_pair_and_it_signs(store: FakeStore) -> None:
    pem, der = _key_and_cert()

    cert = keycert.import_cert(private_key_pem=pem, der=der, friendly_name=FRIENDLY)

    assert cert.der == der and cert.friendly_name == FRIENDLY
    keycert.test_sign(cert.sha1_hex)
    assert [c.sha1_hex for c in keycert.list_certs()] == [cert.sha1_hex]


def test_import_cert_refuses_a_key_that_is_not_the_certificates(store: FakeStore) -> None:
    pem, _ = _key_and_cert()
    _, other_der = _key_and_cert()

    with pytest.raises(CertStoreError, match="does not match"):
        keycert.import_cert(private_key_pem=pem, der=other_der, friendly_name=FRIENDLY)
    assert store.items == {}


def test_legacy_key_is_read_from_the_old_single_slot(store: FakeStore) -> None:
    assert keycert.legacy_private_key_pem() is None
    pem, _ = _key_and_cert()
    store.items[(keycert.LEGACY_SERVICE, keycert.LEGACY_ACCOUNT)] = pem

    assert keycert.legacy_private_key_pem() == pem
    assert len(keycert.public_key_der(pem)) > 200


# ── the setup (Provisioner) certificate, kept by scripts/entra_provisioning.py ──


def test_setup_certs_shows_the_provisioner_bundle_for_the_tenant(store: FakeStore) -> None:
    pem, der = _key_and_cert()
    cert_pem = x509.load_der_x509_certificate(der).public_bytes(serialization.Encoding.PEM).decode()
    store.items[(keycert.SETUP_SERVICE, TENANT)] = pem + cert_pem

    assert keycert.setup_certs("some-other-tenant") == []
    found = keycert.setup_certs(TENANT)

    assert [c.friendly_name for c in found] == [f"Entrabot Provisioner {TENANT}"]
    sha1 = found[0].sha1_hex
    assert sha1 not in [c.sha1_hex for c in keycert.list_certs()]  # not a Blueprint certificate
    assert keycert.find_cert(sha1) is not None
    keycert.test_sign(sha1)
    assert keycert.delete_cert(sha1) is True
    assert (keycert.SETUP_SERVICE, TENANT) not in store.items


# ── health ───────────────────────────────────────────────────────────────────


def test_cert_health_statuses(store: FakeStore) -> None:
    assert keycert.cert_health(None).status == "missing"
    assert keycert.cert_health("C" * 40).status == "missing"

    cert = _create(days=365)
    assert keycert.cert_health(cert.sha1_hex).status == "ok"
    soon = cert.not_after - timedelta(days=10)
    expiring = keycert.cert_health(cert.sha1_hex, now=soon)
    assert expiring.status == "expiring" and expiring.usable and expiring.days_left == 10
    late = cert.not_after + timedelta(days=1)
    assert keycert.cert_health(cert.sha1_hex, now=late).status == "expired"
    early = cert.not_before - timedelta(days=1)
    assert keycert.cert_health(cert.sha1_hex, now=early).status == "not_yet_valid"
    assert keycert.days_until_expiry(cert.sha1_hex, now=soon) == 10
    assert keycert.days_until_expiry("D" * 40) is None


def test_health_messages_name_the_posix_setup_command(store: FakeStore) -> None:
    message = keycert.cert_health(None).message
    assert "./scripts/setup.sh" in message and "setup-windows" not in message


# ── failures are said in plain words, and nothing secret is shown ────────────


def test_a_store_that_cannot_be_reached_is_one_error_type(store: FakeStore) -> None:
    store.broken = RuntimeError("org.freedesktop.DBus.Error.ServiceUnknown")

    with pytest.raises(CertStoreError, match="key store"):
        keycert.find_cert("E" * 40)
    with pytest.raises(CertStoreError):
        keycert.create_signing_cert(subject="CN=x", friendly_name="x")


def test_no_approved_keystore_is_said_with_what_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    from entrabot.errors import InsecureKeyringBackendError

    def refuse() -> None:
        raise InsecureKeyringBackendError("keyring.backends.fail.Keyring", ("SecretService",))

    monkeypatch.setattr(keycert, "get_credential_store", refuse)
    monkeypatch.setattr(keycert, "_platform", lambda: "linux")

    with pytest.raises(CertStoreError) as caught:
        keycert.list_certs()
    assert "Secret Service" in str(caught.value)


def test_repr_and_errors_never_show_a_full_thumbprint_or_key(store: FakeStore) -> None:
    cert = _create()

    assert cert.sha1_hex not in repr(cert)
    store.items[(keycert.SERVICE, cert.sha1_hex)] = "garbage"
    with pytest.raises(CertStoreError) as caught:
        keycert.test_sign(cert.sha1_hex)
    assert cert.sha1_hex not in str(caught.value)


def test_index_that_names_a_vanished_item_is_tolerated(store: FakeStore) -> None:
    cert = _create()
    del store.items[(keycert.SERVICE, cert.sha1_hex)]

    assert keycert.list_certs() == []
    assert json.loads(store.items[(keycert.SERVICE, keycert.INDEX_ACCOUNT)]) == [cert.sha1_hex]


def test_a_certificate_whose_read_back_fails_is_not_left_behind(store: FakeStore) -> None:
    real_store = store.store

    def corrupting(service: str, key: str, value: str) -> None:
        real_store(service, key, "garbage" if key != keycert.INDEX_ACCOUNT else value)

    store.store = corrupting  # type: ignore[method-assign]

    with pytest.raises(CertStoreError):
        keycert.create_signing_cert(subject="CN=x", friendly_name="x")
    assert [key for key in store.items if key[1] != keycert.INDEX_ACCOUNT] == []


# ── another agent on the account, from before this store ─────────────────────


def test_an_older_key_that_is_no_listed_certificates_is_someone_elses(store: FakeStore) -> None:
    mine = _create()
    assert keycert.older_key_is_someone_elses([mine.sha1_hex]) is False  # the slot is empty

    other_pem, _ = _key_and_cert()
    store.items[(keycert.LEGACY_SERVICE, keycert.LEGACY_ACCOUNT)] = other_pem
    assert keycert.older_key_is_someone_elses([mine.sha1_hex]) is True
    assert keycert.older_key_is_someone_elses([]) is True


def test_this_agents_own_older_copy_is_not_someone_elses(store: FakeStore) -> None:
    pem, der = _key_and_cert()
    adopted = keycert.import_cert(private_key_pem=pem, der=der, friendly_name=FRIENDLY)
    store.items[(keycert.LEGACY_SERVICE, keycert.LEGACY_ACCOUNT)] = pem

    assert keycert.older_key_is_someone_elses([adopted.sha1_hex]) is False


def test_something_in_the_older_slot_that_is_not_a_key_counts_for_nobody(store: FakeStore) -> None:
    store.items[(keycert.LEGACY_SERVICE, keycert.LEGACY_ACCOUNT)] = "not a key"
    assert keycert.older_key_is_someone_elses([]) is False
