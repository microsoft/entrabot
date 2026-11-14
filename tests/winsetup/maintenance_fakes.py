"""Shared fakes for the maintenance commands: rotate-cert, teardown, status.

A throwaway project folder, an in-memory certificate store with the ``wincert`` API, and
stand-ins for the child processes that talk to Microsoft Entra. Nothing reaches the network,
a tenant, the real ``.env`` or the real key store.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from entrabot.winsetup.console import RunRequest
from entrabot.winsetup.helpers import RESULT_MARKER, Paths
from entrabot.winsetup.wizard import World
from tests.winsetup.fakes import FakeSpawn, Reply, snippet, words

NOW = datetime(2026, 11, 11, 9, 30, 15, tzinfo=UTC)
TENANT = "aaaaaaaa-0000-0000-0000-000000000001"
BP_APP = "11111111-2222-3333-4444-555555555555"
BP_OBJECT = "bp-object"
UPN = "entrabot-agent-sati@werner.ac"
OLD_SHA1 = "B" * 36 + "01D1"
NEW_SHA1 = "A" * 36 + "2E3F"
PROV_SHA1 = "C" * 36 + "9A9A"
OLD_X5T = "o" * 43
NEW_X5T = "n" * 43

# Markers that pick out each child program (see entrabot.winsetup.directory).
# REMOVE_BLUEPRINT_CREDENTIAL matches both Blueprint removals; the second marker only the one
# that takes several certificates off in a single write (teardown's).
REMOVE_BLUEPRINT_CREDENTIAL = "remove this computer's certificate from the Blueprint"
REMOVE_BLUEPRINT_CREDENTIALS_IN_ONE_WRITE = "with any rotation leftover, in one write"
REMOVE_PROVISIONER_CREDENTIAL = "remove this computer's setup certificate"
REVOKE_OLD_SETUP_CERTIFICATE = "old setup certificates an earlier setup could not remove"
PROVE_CERTIFICATE = "prove the new certificate"


class WinCertError(Exception):
    pass


@dataclass(frozen=True)
class StoreCert:
    sha1_hex: str
    x5t_s256: str
    der: bytes = b"\x01\x02\x03"
    subject: str = "CN=x"
    friendly_name: str = "x"
    not_before: datetime = NOW - timedelta(days=300)
    not_after: datetime = NOW + timedelta(days=65)
    has_private_key: bool = True
    ksp: str = "tpm"


@dataclass
class FakeStore:
    """An in-memory certificate store with the wincert API."""

    certs: dict[str, StoreCert] = field(default_factory=dict)
    create_error: str | None = None
    delete_error: str | None = None
    # Thumbprints whose certificate comes out of the store but whose private key does not:
    # delete_cert then raises, as entrabot.platform.wincert does.
    key_stuck: set[str] = field(default_factory=set)
    # With key_stuck: leave the certificate in the store as well, which is what
    # entrabot.platform.wincert does today (the certificate is the handle for a retry).
    key_stuck_keeps_cert: bool = False
    unreadable: bool = False  # the store itself can't be read
    tpm_reason: str | None = None
    # What a created certificate says its key is kept in. None: as on Windows, the TPM or,
    # with tpm_reason, the software key store.
    created_ksp: str | None = None
    created: list[dict[str, object]] = field(default_factory=list)
    deleted: list[tuple[str, bool]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    WinCertError: type[Exception] = WinCertError

    def find_cert(self, sha1_hex: str) -> StoreCert | None:
        if self.unreadable:
            raise WinCertError("the certificate store could not be opened")
        return self.certs.get(sha1_hex.upper())

    def list_certs(self, *, friendly_name: str | None = None) -> list[StoreCert]:
        return [
            cert
            for cert in self.certs.values()
            if friendly_name is None or cert.friendly_name == friendly_name
        ]

    def test_sign(self, sha1_hex: str) -> None:
        return None

    def create_signing_cert(
        self, *, subject: str, friendly_name: str, days: int = 365, prefer_tpm: bool = True
    ) -> tuple[StoreCert, str | None]:
        if self.create_error:
            raise WinCertError(self.create_error)
        self.created.append({"subject": subject, "friendly_name": friendly_name, "days": days})
        self.events.append("create")
        cert = StoreCert(
            sha1_hex=NEW_SHA1,
            x5t_s256=NEW_X5T,
            subject=subject,
            friendly_name=friendly_name,
            not_before=NOW - timedelta(minutes=10),
            not_after=NOW + timedelta(days=days),
            ksp=self.created_ksp or ("software" if self.tpm_reason else "tpm"),
        )
        self.certs[NEW_SHA1] = cert
        return cert, self.tpm_reason

    def delete_cert(self, sha1_hex: str, *, delete_key: bool = True) -> bool:
        if self.delete_error:
            raise WinCertError(self.delete_error)
        if sha1_hex.upper() in self.key_stuck:
            self.events.append(f"key-stuck:{sha1_hex[-4:].upper()}")
            if self.key_stuck_keeps_cert:
                raise WinCertError(
                    "The private key could not be deleted (access denied), so the "
                    "certificate was left in the certificate store."
                )
            self.certs.pop(sha1_hex.upper(), None)
            raise WinCertError(
                "The certificate was removed, but its private key could not be deleted "
                "(access denied)."
            )
        self.deleted.append((sha1_hex.upper(), delete_key))
        self.events.append(f"delete:{sha1_hex[-4:].upper()}")
        return self.certs.pop(sha1_hex.upper(), None) is not None


def blueprint_cert(sha1: str = OLD_SHA1, x5t: str = OLD_X5T, **changes: object) -> StoreCert:
    values: dict[str, object] = {
        "sha1_hex": sha1,
        "x5t_s256": x5t,
        "subject": f"CN=Entrabot Blueprint {BP_APP[:8]}",
        "friendly_name": f"Entrabot Blueprint {BP_APP}",
    }
    values.update(changes)
    return StoreCert(**values)  # type: ignore[arg-type]


def provisioner_cert() -> StoreCert:
    return StoreCert(
        sha1_hex=PROV_SHA1,
        x5t_s256="p" * 43,
        subject="CN=entrabot-provisioner",
        friendly_name=f"Entrabot Provisioner {TENANT}",
    )


ENV_LINES = [
    f"ENTRABOT_TENANT_ID={TENANT}",
    f"ENTRABOT_BLUEPRINT_APP_ID={BP_APP}",
    f"ENTRABOT_BLUEPRINT_OBJECT_ID={BP_OBJECT}",
    f"ENTRABOT_BLUEPRINT_CERT_THUMBPRINT={OLD_X5T}",
    f"ENTRABOT_BLUEPRINT_CERT_SHA1={OLD_SHA1}",
    "ENTRABOT_BLUEPRINT_KSP=tpm",
    "ENTRABOT_AGENT_ID=agent-app",
    "ENTRABOT_AGENT_USER_ID=agent-user",
    f"ENTRABOT_AGENT_UPN={UPN}",
    "ENTRABOT_HUMAN_UPN=brandon@werner.ac",
    "ENTRABOT_KEEP_MEMORY_LOCAL=true",
]
STATE = {
    "TENANT_ID": TENANT,
    "PROVISIONER_CLIENT_ID": "prov-app",
    "PROVISIONER_CERT_THUMBPRINT": PROV_SHA1.lower(),
    "BLUEPRINT_APP_ID": BP_APP,
    "BLUEPRINT_OBJECT_ID": BP_OBJECT,
    "AGENT_USER_UPN": UPN,
}
# Setup moved an older install off its plaintext Provisioner key file but could not take that
# key's certificate off the Provisioner app, so it left this note in the state file and retries
# on its next run (scripts/entra_provisioning.py, _retire_old_certificates).
OLD_PROV_SHA1 = "F" * 36 + "C0DE"
OLD_PROV_EXPIRES = "2027-01-31"
REVOKE_PENDING = {
    "PROVISIONER_CERT_REVOKE_PENDING": OLD_PROV_SHA1.lower(),
    "PROVISIONER_CERT_REVOKE_PENDING_EXPIRES": OLD_PROV_EXPIRES,
}
ACCOUNT = {
    "tenantId": TENANT,
    "tenantDisplayName": "Werner",
    "tenantDefaultDomain": "werner.ac",
    "user": {"name": "admin@werner.ac", "type": "user"},
    "id": "",
    "name": "",
}


@dataclass
class Project:
    """A set-up project folder, its certificate store and its scripted child processes."""

    root: Path
    paths: Paths
    world: World
    store: FakeStore
    spawn: FakeSpawn
    env: dict[str, str]

    @property
    def env_file(self) -> Path:
        return self.paths.env_file

    def env_values(self) -> dict[str, str]:
        from entrabot.winsetup.envfile import read_env

        return read_env(self.paths.env_file)

    def ran(self, marker: str) -> list[RunRequest]:
        return [call for call in self.spawn.calls if snippet(marker)(call)]


def result(code: int = 0, **data: object) -> Reply:
    return Reply(returncode=code, stdout=RESULT_MARKER + json.dumps(data))


def make_project(
    tmp_path: Path,
    *,
    env_lines: list[str] | None = None,
    signed_in: bool = True,
    with_az: bool = True,
    certs: list[StoreCert] | None = None,
    state: dict[str, str] | None = None,
    platform: str = "win32",
    store: FakeStore | None = None,
) -> Project:
    root = tmp_path / "entrabot"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for name in (
        "register_windows_blueprint_cert.py",
        "deprovision_entra_agent_identity.py",
        "show_agent_status.py",
    ):
        (scripts / name).write_text("", encoding="utf-8")
    venv = root / ".venv" / "Scripts"
    venv.mkdir(parents=True)
    python = venv / "python.exe"
    python.write_text("", encoding="utf-8")
    (venv / "entrabot-mcp.exe").write_text("", encoding="utf-8")
    data_root = tmp_path / "data"
    data_root.mkdir()
    paths = Paths(project_root=root, python=python, data_root=data_root)

    lines = ENV_LINES if env_lines is None else env_lines
    if lines:
        paths.env_file.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    paths.state_file.write_text(json.dumps(STATE if state is None else state), encoding="utf-8")

    store = store or FakeStore()
    for cert in [blueprint_cert()] if certs is None else certs:
        store.certs[cert.sha1_hex] = cert

    env = {
        "PATH": str(tmp_path / "bin"),
        "SystemRoot": "C:/Windows",
        "LOCALAPPDATA": str(tmp_path / "localappdata"),
        "COPILOT_HOME": str(tmp_path / "copilot"),
    }
    az = tmp_path / "bin" / "az"
    az.parent.mkdir(exist_ok=True)
    az.write_text("", encoding="utf-8")

    def which(name: str) -> str | None:
        return str(az) if (name == "az" and with_az) else None

    spawn = FakeSpawn()
    spawn.on(
        words("account", "show"),
        Reply(stdout=json.dumps(ACCOUNT)) if signed_in else Reply(returncode=1, stderr="az login"),
    )

    world = World(env=env, platform=platform, now=lambda: NOW, which=which, wincert=store)
    return Project(root=root, paths=paths, world=world, store=store, spawn=spawn, env=env)


def sleeper() -> tuple[Callable[[float], None], list[float]]:
    waits: list[float] = []
    return waits.append, waits
