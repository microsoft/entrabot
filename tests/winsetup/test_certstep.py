"""The Blueprint certificate step: reuse, register, or generate.

These carry over every rule the tests of the PowerShell certificate step encoded (the step
this replaced), now against Python that runs on every OS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from entrabot.winsetup.certstep import CertOutcome, CertStepError, ensure_blueprint_cert
from entrabot.winsetup.console import RunRequest
from entrabot.winsetup.helpers import Helpers, Paths
from tests.winsetup.fakes import FakeSpawn, Reply, make_console, script

NOW = datetime(2026, 11, 11, tzinfo=UTC)
OLD_SHA1 = "B" * 40
OLD_X5T = "o" * 43
NEW_SHA1 = "A" * 40
NEW_X5T = "n" * 43
BP_OBJECT = "blueprint-object"
BP_APP = "11111111-2222-3333-4444-555555555555"
OLD_ENV = {
    "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT": OLD_X5T,
    "ENTRABOT_BLUEPRINT_CERT_SHA1": OLD_SHA1,
    "ENTRABOT_BLUEPRINT_KSP": "software",
}


class WinCertError(Exception):
    pass


@dataclass(frozen=True)
class StoreCert:
    sha1_hex: str
    x5t_s256: str
    der: bytes
    subject: str = "CN=x"
    friendly_name: str = "x"
    not_before: datetime = NOW - timedelta(days=1)
    not_after: datetime = NOW + timedelta(days=300)
    has_private_key: bool = True
    ksp: str = "software"


@dataclass
class FakeWinCert:
    """An in-memory certificate store with the wincert API."""

    store: dict[str, StoreCert] = field(default_factory=dict)
    sign_error: str | None = None
    create_error: str | None = None
    tpm_reason: str | None = None
    created: list[dict[str, object]] = field(default_factory=list)
    deleted: list[tuple[str, bool]] = field(default_factory=list)
    signed: list[str] = field(default_factory=list)
    WinCertError: type[Exception] = WinCertError

    def find_cert(self, sha1_hex: str) -> StoreCert | None:
        return self.store.get(sha1_hex)

    def test_sign(self, sha1_hex: str) -> None:
        self.signed.append(sha1_hex)
        if self.sign_error:
            raise WinCertError(self.sign_error)

    def create_signing_cert(
        self, *, subject: str, friendly_name: str, days: int = 365, prefer_tpm: bool = True
    ) -> tuple[StoreCert, str | None]:
        if self.create_error:
            raise WinCertError(self.create_error)
        self.created.append({"subject": subject, "friendly_name": friendly_name, "days": days})
        cert = StoreCert(
            sha1_hex=NEW_SHA1,
            x5t_s256=NEW_X5T,
            der=b"\x01\x02\x03",
            subject=subject,
            friendly_name=friendly_name,
            ksp="software" if self.tpm_reason else "tpm",
        )
        self.store[NEW_SHA1] = cert
        return cert, self.tpm_reason

    def delete_cert(self, sha1_hex: str, *, delete_key: bool = True) -> bool:
        self.deleted.append((sha1_hex, delete_key))
        return self.store.pop(sha1_hex, None) is not None


@dataclass
class Harness:
    spawn: FakeSpawn
    wincert: FakeWinCert
    tmp_path: Path
    register_exit: int = 0
    registered_bytes: list[bytes] = field(default_factory=list)
    der_paths: list[Path] = field(default_factory=list)

    def run(self, env: dict[str, str] | None = None) -> CertOutcome:
        console, self.out = make_console(spawn=self.spawn)
        paths = Paths(self.tmp_path, Path("/venv/python"), self.tmp_path / "data")
        return ensure_blueprint_cert(
            env_values=env or {},
            blueprint_object_id=BP_OBJECT,
            blueprint_app_id=BP_APP,
            helpers=Helpers(console, paths, {}),
            console=console,
            wincert=self.wincert,
            now=NOW,
        )

    def called(self, helper: str) -> bool:
        return any(script(helper)(call) for call in self.spawn.calls)


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    spawn = FakeSpawn()
    result = Harness(spawn=spawn, wincert=FakeWinCert(), tmp_path=tmp_path)

    def register(request: RunRequest) -> Reply:
        der = Path(request.argv[request.argv.index("--der") + 1])
        result.der_paths.append(der)
        result.registered_bytes.append(der.read_bytes())
        return Reply(returncode=result.register_exit)

    spawn.on(script("register_windows_blueprint_cert.py"), register)
    return result


def _in_store(harness: Harness, **overrides: object) -> None:
    harness.wincert.store[OLD_SHA1] = StoreCert(
        sha1_hex=OLD_SHA1, x5t_s256=OLD_X5T, der=b"\x01\x02\x03\x04", **overrides
    )


def _verify(harness: Harness, exit_code: int) -> None:
    harness.spawn.on(script("verify_blueprint_cert.py"), Reply(returncode=exit_code))


class TestFreshMachine:
    def test_a_certificate_is_generated_registered_and_reported(self, harness: Harness) -> None:
        outcome = harness.run()
        assert harness.wincert.created and harness.called("register_windows_blueprint_cert.py")
        assert harness.registered_bytes == [b"\x01\x02\x03"]
        assert (outcome.sha1_hex, outcome.x5t_s256, outcome.ksp) == (NEW_SHA1, NEW_X5T, "tpm")
        assert outcome.generated and not outcome.unconfirmed
        assert outcome.tpm_fallback_reason is None

    def test_the_registration_is_told_which_blueprint(self, harness: Harness) -> None:
        harness.run()
        argv = next(
            call.argv
            for call in harness.spawn.calls
            if script("register_windows_blueprint_cert.py")(call)
        )
        assert argv[argv.index("--blueprint-object-id") + 1] == BP_OBJECT

    def test_the_certificate_is_named_for_its_blueprint(self, harness: Harness) -> None:
        # Teardown and rotation must be able to tell this agent's certificate from another's.
        harness.run()
        created = harness.wincert.created[0]
        assert BP_APP in str(created["friendly_name"])
        assert BP_APP[:8] in str(created["subject"])

    def test_the_temporary_certificate_file_is_deleted_on_every_path(
        self, harness: Harness
    ) -> None:
        harness.run()
        harness.register_exit = 1
        with pytest.raises(CertStepError):
            harness.run()
        assert len(harness.der_paths) == 2
        assert not any(path.exists() or path.parent.exists() for path in harness.der_paths)

    def test_a_software_fallback_reports_why_the_tpm_was_not_used(self, harness: Harness) -> None:
        harness.wincert.tpm_reason = "This machine has no TPM."
        outcome = harness.run()
        assert outcome.ksp == "software"
        assert outcome.tpm_fallback_reason == "This machine has no TPM."

    def test_a_certificate_that_cannot_be_created_stops_setup(self, harness: Harness) -> None:
        harness.wincert.create_error = "The key store refused."
        with pytest.raises(CertStepError, match="The key store refused"):
            harness.run()
        assert not harness.called("register_windows_blueprint_cert.py")

    def test_no_full_thumbprint_reaches_the_screen(self, harness: Harness) -> None:
        harness.run()
        assert NEW_SHA1 not in harness.out.getvalue()

    def test_a_new_certificate_is_marked_as_registered_by_this_run(self, harness: Harness) -> None:
        # The end-to-end test gives Microsoft Entra time to accept a certificate that was only
        # just added to the Blueprint, and only then.
        assert harness.run().registered


class TestExistingCertificate:
    def test_a_registered_certificate_is_reused_and_nothing_is_written(
        self, harness: Harness
    ) -> None:
        _in_store(harness)
        _verify(harness, 0)
        outcome = harness.run(OLD_ENV)
        assert harness.called("verify_blueprint_cert.py")
        assert not harness.wincert.created
        assert not harness.called("register_windows_blueprint_cert.py")
        assert outcome.sha1_hex == OLD_SHA1 and not outcome.generated
        assert not outcome.registered  # it was on the Blueprint already
        assert harness.wincert.signed == [OLD_SHA1]  # reuse only what can actually sign

    def test_verification_is_asked_about_this_certificate(self, harness: Harness) -> None:
        _in_store(harness)
        _verify(harness, 0)
        harness.run(OLD_ENV)
        argv = next(
            call.argv for call in harness.spawn.calls if script("verify_blueprint_cert.py")(call)
        )
        assert argv[-2:] == [BP_OBJECT, OLD_X5T]

    def test_one_missing_from_the_blueprint_is_registered_without_a_new_one(
        self, harness: Harness
    ) -> None:
        _in_store(harness)
        _verify(harness, 1)
        outcome = harness.run(OLD_ENV)
        assert not harness.wincert.created
        assert harness.registered_bytes == [b"\x01\x02\x03\x04"]  # the store's own bytes
        assert (outcome.sha1_hex, outcome.x5t_s256) == (OLD_SHA1, OLD_X5T)
        assert outcome.ksp == "software"
        assert outcome.registered and not outcome.generated  # added by this run, not made

    def test_a_failed_check_changes_nothing(self, harness: Harness) -> None:
        # A failed check says nothing about the Blueprint, so nothing is generated.
        _in_store(harness)
        _verify(harness, 3)
        with pytest.raises(CertStepError, match="Nothing was generated or changed"):
            harness.run(OLD_ENV)
        assert not harness.wincert.created
        assert not harness.called("register_windows_blueprint_cert.py")

    def test_one_in_env_but_not_in_the_store_is_replaced(self, harness: Harness) -> None:
        outcome = harness.run(OLD_ENV)
        assert harness.wincert.created and outcome.sha1_hex == NEW_SHA1
        assert not harness.called("verify_blueprint_cert.py")

    @pytest.mark.parametrize(
        "broken",
        [
            {"not_after": NOW - timedelta(days=1)},
            {"not_before": NOW + timedelta(days=1)},
            {"has_private_key": False},
        ],
        ids=["expired", "not-yet-valid", "no-private-key"],
    )
    def test_one_that_cannot_sign_is_replaced(self, harness: Harness, broken: dict) -> None:
        # Registering or reusing it would report success while Hop 1 stays broken.
        _in_store(harness, **broken)
        outcome = harness.run(OLD_ENV)
        assert not harness.called("verify_blueprint_cert.py")
        assert harness.wincert.created
        assert harness.called("register_windows_blueprint_cert.py")
        assert outcome.sha1_hex == NEW_SHA1

    def test_a_key_that_fails_a_real_signature_is_replaced(self, harness: Harness) -> None:
        # HasPrivateKey is not proof: a roamed certificate's TPM key belongs to another machine.
        _in_store(harness)
        harness.wincert.sign_error = "The key is not usable on this machine."
        outcome = harness.run(OLD_ENV)
        assert outcome.sha1_hex == NEW_SHA1 and outcome.generated
        assert "not usable on this machine" in harness.out.getvalue()

    @pytest.mark.parametrize("sha1", ["", "not-hex", "b" * 39])
    def test_a_malformed_thumbprint_in_env_is_ignored(self, harness: Harness, sha1: str) -> None:
        _in_store(harness)
        outcome = harness.run({**OLD_ENV, "ENTRABOT_BLUEPRINT_CERT_SHA1": sha1})
        assert outcome.sha1_hex == NEW_SHA1

    def test_the_store_not_env_is_the_source_of_the_sha256_thumbprint(
        self, harness: Harness
    ) -> None:
        _in_store(harness)
        _verify(harness, 0)
        outcome = harness.run({**OLD_ENV, "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT": "stale"})
        assert outcome.x5t_s256 == OLD_X5T

    def test_a_lowercase_thumbprint_in_env_still_finds_the_certificate(
        self, harness: Harness
    ) -> None:
        _in_store(harness)
        _verify(harness, 0)
        outcome = harness.run({**OLD_ENV, "ENTRABOT_BLUEPRINT_CERT_SHA1": OLD_SHA1.lower()})
        assert outcome.sha1_hex == OLD_SHA1 and not harness.wincert.created


class TestRegistrationOutcomes:
    def test_a_refused_registration_removes_the_new_certificate_and_its_key(
        self, harness: Harness
    ) -> None:
        harness.register_exit = 1
        with pytest.raises(CertStepError, match="Nothing was changed"):
            harness.run()
        assert harness.wincert.deleted == [(NEW_SHA1, True)]  # the key too, not just the cert

    def test_a_refused_registration_keeps_a_certificate_it_did_not_generate(
        self, harness: Harness
    ) -> None:
        _in_store(harness)
        _verify(harness, 1)
        harness.register_exit = 1
        with pytest.raises(CertStepError):
            harness.run(OLD_ENV)
        assert harness.wincert.deleted == []

    def test_an_unconfirmed_registration_keeps_the_certificate_for_the_next_run(
        self, harness: Harness
    ) -> None:
        # The write may have landed. The caller records the certificate in .env and stops;
        # the next run checks the Blueprint again.
        harness.register_exit = 3
        outcome = harness.run()
        assert outcome.unconfirmed and outcome.sha1_hex == NEW_SHA1
        assert harness.wincert.deleted == []


def test_a_missing_blueprint_object_id_is_refused_before_anything_happens(
    harness: Harness,
) -> None:
    console, _ = make_console(spawn=harness.spawn)
    paths = Paths(harness.tmp_path, Path("/venv/python"), harness.tmp_path / "data")
    with pytest.raises(CertStepError, match="Blueprint"):
        ensure_blueprint_cert(
            env_values={},
            blueprint_object_id="",
            blueprint_app_id=BP_APP,
            helpers=Helpers(console, paths, {}),
            console=console,
            wincert=harness.wincert,
            now=NOW,
        )
    assert harness.spawn.calls == [] and not harness.wincert.created


def test_a_certificate_close_to_expiry_is_flagged(harness: Harness) -> None:
    _in_store(harness, not_after=NOW + timedelta(days=12))
    _verify(harness, 0)
    outcome = harness.run(OLD_ENV)
    assert outcome.days_left == 12
    assert "12 days" in harness.out.getvalue()
