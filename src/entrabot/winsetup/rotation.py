"""Rotate this computer's Blueprint certificate.

    python -m entrabot.winsetup rotate-cert --project-root PATH [--yes] [--verbose]

The agent proves it is the Blueprint by signing with a private key held on this computer.
Rotation replaces that key and its certificate. The order is what makes it safe: the
certificate in use stays in use, untouched, until the new one is PROVEN to sign the agent in.

    1. create a new certificate (Windows: TPM first, otherwise the software key store;
       macOS and Linux: in the account's Keychain or keyring)
    2. add it to the Blueprint, beside the current one and every other computer's
    3. prove it: sign in with the new certificate, waiting for Microsoft Entra to take it up
    4. switch ``.env`` to the new certificate            <- the only point of no return
    5. take the old certificate off the Blueprint
    6. delete the old certificate and its private key from this computer

A failure in 1-3 undoes the new certificate and changes nothing else. A failure in 5-6
leaves a working agent on the new certificate and something to tidy; the old thumbprint is
remembered in ``.env`` (``ENTRABOT_BLUEPRINT_CERT_RETIRING_SHA1``) so running the command
again finishes the job instead of rotating a second time.

Step 5 replaces the Blueprint's whole certificate list with the one it read, minus the old
certificate. Microsoft Entra can take a minute to show a certificate that was just added,
so the removal is told the new certificate must stay: it will not write from a read that
does not show it (``blueprint_certs.remove_key_credential(must_keep=...)``).

Unattended (no terminal) it runs only with ``--yes``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from entrabot.winsetup import certstep
from entrabot.winsetup.certstep import CertStepError
from entrabot.winsetup.console import Cancelled, Console
from entrabot.winsetup.directory import (
    Directory,
    Proof,
    connect,
    delete_local_cert,
    is_sha1,
    key_removal_advice,
    last4,
)
from entrabot.winsetup.envfile import EnvGuard, EnvLockError, read_env, update_env
from entrabot.winsetup.helpers import Helpers, Paths
from entrabot.winsetup.wizard import World
from entrabot.winsetup.words import words_for

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_LEFTOVERS, EXIT_CANCELLED = 0, 1, 2, 3, 130

SHA1_KEY = "ENTRABOT_BLUEPRINT_CERT_SHA1"
X5T_KEY = "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT"
KSP_KEY = "ENTRABOT_BLUEPRINT_KSP"
RETIRING_KEY = "ENTRABOT_BLUEPRINT_CERT_RETIRING_SHA1"

REGISTER_OK = 0
# Microsoft Entra usually takes a new certificate up within a minute; this allows two.
PROOF_ATTEMPTS = 12
PROOF_DELAY_SECONDS = 10.0


@dataclass(frozen=True)
class _Leftover:
    """Something rotation could not finish. *retry*: running rotation again can finish it."""

    text: str
    retry: bool = True


@dataclass
class _Rotation:
    console: Console
    paths: Paths
    world: World
    sleep: Callable[[float], None]
    proof_attempts: int
    proof_delay_seconds: float
    guard: EnvGuard = field(init=False)

    def __post_init__(self) -> None:
        # .env holds IDs, not secrets: a lock that can't be applied is reported, and the
        # switch still happens. Stopping there would strand a proven certificate.
        self.guard = EnvGuard(
            console=self.console,
            platform=self.world.platform,
            env=self.world.env,
            tolerate_failure=True,
        )
        # The world says which system this is, so its commands are the ones messages name.
        self.console.words = words_for(self.world.platform)
        self.words = self.console.words

    # ── Entry ────────────────────────────────────────────────────────────────

    def run(self) -> int:
        console = self.console
        console.banner(
            "Entrabot certificate rotation",
            [
                "Replaces the certificate this computer uses to sign the agent in.",
                "The current one stays in use until the new one is proven to work.",
            ],
        )
        if not self.paths.env_file.is_file():
            console.fail(
                "This computer has not been set up yet.", fix=f"Run {self.words.setup} first."
            )
            return EXIT_FAILED
        values = read_env(self.paths.env_file)
        object_id = values.get("ENTRABOT_BLUEPRINT_OBJECT_ID", "").strip()
        app_id = values.get("ENTRABOT_BLUEPRINT_APP_ID", "").strip()
        current = values.get(SHA1_KEY, "").strip().upper()
        if not object_id or not app_id or not is_sha1(current):
            console.fail(
                "This computer's settings don't name a certificate to rotate.",
                fix=f"Run {self.words.setup}: it creates and registers one. Rotation replaces a "
                "certificate that is already working.",
            )
            return EXIT_FAILED
        try:
            wincert = self.world.wincert or certstep.load_cert_store(self.world.platform)
        except CertStepError as exc:
            console.fail(str(exc))
            return EXIT_FAILED

        retiring = values.get(RETIRING_KEY, "").strip().upper()
        if is_sha1(retiring) and retiring != current:
            return self._finish_earlier_rotation(wincert, object_id, retiring, current)

        try:
            old = wincert.find_cert(current)
        except wincert.WinCertError as exc:
            console.fail(f"Could not read this computer's certificate store: {exc}")
            return EXIT_FAILED
        if old is None or not old.has_private_key:
            console.fail(
                "The certificate named in this computer's settings is not in this computer's "
                "certificate store (or its private key is gone).",
                fix=f"Run {self.words.setup}: it creates and registers a new certificate. That "
                "is the repair for this; rotation needs a working certificate to start from.",
            )
            return EXIT_FAILED
        self._describe_current(old)
        if not console.interactive and not console.assume_yes:
            console.fail(
                "Rotation needs a yes before it changes anything.",
                fix=f"Run it in a terminal and answer the question, or add {self.words.yes_hint}.",
            )
            return EXIT_USAGE

        access = connect(
            console,
            self.paths,
            self.world.env,
            platform=self.world.platform,
            which=self.world.which,
        )
        if access.problem:
            console.fail(access.problem[0], fix=access.problem[1])
            return EXIT_FAILED
        directory = Directory(console, self.paths, access.helpers)

        try:
            go_ahead = console.confirm(
                "Replace this computer's certificate now?", default=True, key="rotate"
            )
        except (Cancelled, KeyboardInterrupt):
            go_ahead = False
        if not go_ahead:
            console.info("Nothing was changed.")
            return EXIT_CANCELLED
        return self._rotate(wincert, directory, access.helpers, object_id, app_id, old)

    def _describe_current(self, old: Any) -> None:
        now = self.world.now()
        when = f"{old.not_after:%d %b %Y}"
        if now > old.not_after:
            self.console.warn(f"The current certificate expired on {when}.")
        else:
            days = max(0, (old.not_after - now).days)
            self.console.info(f"Current certificate: valid until {when} ({days} days left).")

    # ── The rotation ─────────────────────────────────────────────────────────

    def _rotate(
        self,
        wincert: Any,
        directory: Directory,
        helpers: Helpers,
        object_id: str,
        app_id: str,
        old: Any,
    ) -> int:
        console = self.console

        console.step(1, 5, "Creating a new certificate on this computer")
        try:
            new, tpm_reason = wincert.create_signing_cert(
                subject=f"CN=Entrabot Blueprint {app_id[:8]}",
                friendly_name=f"Entrabot Blueprint {app_id}",
                days=365,
                prefer_tpm=True,
            )
        except wincert.WinCertError as exc:
            console.fail(
                f"Could not create a certificate on this computer: {exc}",
                fix="Nothing was changed. The current certificate is still in use.",
            )
            return EXIT_FAILED
        if new.ksp not in ("tpm", "software", "unknown"):
            console.ok(f"Created a certificate whose key is kept in the {self.words.key_store}")
        elif tpm_reason:
            console.ok("Created a certificate in the Windows software key store")
            console.detail(f"The hardware TPM was not used: {tpm_reason}")
        else:
            console.ok("Created a certificate whose key is held by this computer's TPM")

        registered = False
        try:
            console.step(2, 5, "Adding it to the agent's Blueprint")
            register = helpers.script(
                "register_windows_blueprint_cert.py",
                ["--blueprint-object-id", object_id, "--sha1", new.sha1_hex],
                heartbeat="Waiting for Microsoft Entra to confirm the certificate",
            )
            if register.returncode != REGISTER_OK:
                # Exit 1 says nothing was written. Anything else may have landed, so the
                # new certificate is taken back off the list before it is deleted.
                registered = register.returncode != 1
                self._undo(wincert, directory, object_id, new, registered=registered)
                console.fail(
                    "The new certificate could not be added to the Blueprint "
                    f"(exit {register.returncode}, see above).",
                    fix="The current certificate is still in use. Nothing else was changed.",
                )
                return EXIT_FAILED
            registered = True
            console.ok("Added. The current certificate and every other computer's were kept")

            console.step(3, 5, "Proving the new certificate signs the agent in")
            proof = self._prove(directory, new)
        except KeyboardInterrupt:
            console.blank()
            console.warn("Stopped. Undoing the new certificate.")
            self._undo(wincert, directory, object_id, new, registered=True)
            console.info("The current certificate is still in use. Nothing else was changed.")
            return EXIT_CANCELLED

        if not proof.accepted:
            self._undo(wincert, directory, object_id, new, registered=registered)
            reason = proof.code or "no reason was given"
            console.fail(
                f"Microsoft Entra did not accept the new certificate ({reason}).",
                fix="The current certificate is still in use. Nothing else was changed.\n"
                f"Try again in a few minutes. If it keeps failing, run {self.words.diagnose}.",
            )
            if proof.detail:
                console.debug(proof.detail)
            return EXIT_FAILED
        if proof.complete:
            console.ok("The agent signed in with the new certificate")
        else:
            console.ok("Microsoft Entra accepted the new certificate")
            console.warn(
                f"A later sign-in step failed ({proof.hop}: {proof.code}). The certificate "
                f"does not control that step. Check with {self.words.diagnose}."
            )

        console.step(4, 5, "Switching this computer to the new certificate")
        try:
            update_env(
                self.paths.env_file,
                {
                    X5T_KEY: new.x5t_s256,
                    SHA1_KEY: new.sha1_hex,
                    KSP_KEY: new.ksp,
                    RETIRING_KEY: old.sha1_hex,
                },
                guard=self.guard,
            )
        except (OSError, EnvLockError) as exc:
            self._undo(wincert, directory, object_id, new, registered=True)
            console.fail(
                f"Could not update this computer's settings (.env): {exc}",
                fix="The current certificate is still in use. Nothing else was changed.",
            )
            return EXIT_FAILED
        console.ok("Settings updated")
        self._report_lock_problems()

        console.step(5, 5, "Retiring the old certificate")
        leftovers = self._retire(
            wincert, directory, object_id, old.sha1_hex, keep_sha1=new.sha1_hex
        )
        return self._finish(new, leftovers)

    def _prove(self, directory: Directory, new: Any) -> Proof:
        console = self.console
        proof = Proof(accepted=False)
        for attempt in range(1, self.proof_attempts + 1):
            proof = directory.prove_certificate(
                sha1_hex=new.sha1_hex, x5t_s256=new.x5t_s256, ksp=new.ksp
            )
            if proof.accepted or not proof.retry or attempt == self.proof_attempts:
                break
            console.detail(
                f"Microsoft Entra has not taken up the new certificate yet ({proof.code}). "
                f"Trying again in {self.proof_delay_seconds:.0f}s "
                f"(attempt {attempt + 1} of {self.proof_attempts})"
            )
            self.sleep(self.proof_delay_seconds)
        return proof

    def _undo(
        self, wincert: Any, directory: Directory, object_id: str, new: Any, *, registered: bool
    ) -> None:
        """Take the unproven certificate back out: off the Blueprint, then off this computer.

        The local key is deleted even when the Blueprint could not be changed, so a
        certificate that is still listed there can no longer be used by anyone.
        """
        console = self.console
        still_listed = False
        if registered:
            removal = directory.remove_blueprint_credential(object_id, new.sha1_hex)
            still_listed = not removal.gone
            if still_listed and removal.detail:
                console.debug(removal.detail)
        gone = delete_local_cert(wincert, new.sha1_hex)
        if gone.status == "key-remains":
            console.warn(
                "The unused certificate was removed from this computer, but its private key "
                f"could not be deleted: {gone.detail} {key_removal_advice(gone.ksp)}"
            )
        elif gone.status == "failed":
            console.warn(
                f"Could not delete the unused certificate from this computer: {gone.detail} "
                f"It is named 'Entrabot Blueprint ...' and ends in {last4(new.sha1_hex)}."
            )
        if still_listed:
            console.warn(
                "The unused certificate is still listed on the Blueprint (thumbprint ending "
                f"{last4(new.sha1_hex)}). Its private key has been deleted, so it cannot be "
                "used. Remove it in the Microsoft Entra admin center > App registrations > "
                "the agent's Blueprint > Certificates & secrets, or run rotation again."
            )

    def _report_lock_problems(self) -> None:
        for problem in self.guard.problems:
            self.console.warn(problem)
        self.guard.problems.clear()

    # ── Retiring the old certificate ─────────────────────────────────────────

    def _retire(
        self,
        wincert: Any,
        directory: Directory | None,
        object_id: str,
        old_sha1: str,
        *,
        keep_sha1: str,
    ) -> list[_Leftover]:
        """Remove the old certificate from the Blueprint and from this computer.

        *keep_sha1* is the certificate the agent is on now: the removal refuses to write a
        list that does not show it. Returns what is left over. The local key goes even when
        the Blueprint entry could not be removed: without its key the listed certificate is
        unusable, and the new certificate has already been proven to sign the agent in.
        """
        console = self.console
        leftovers: list[_Leftover] = []
        tail = last4(old_sha1)

        removal = (
            directory.remove_blueprint_credential(object_id, old_sha1, must_keep=[keep_sha1])
            if directory is not None
            else None
        )
        if removal is not None and removal.gone:
            console.ok("Removed the old certificate from the agent's Blueprint")
        else:
            why = removal.detail if removal is not None else "not signed in to Microsoft"
            leftovers.append(
                _Leftover(
                    "The old certificate is still listed on the Blueprint (thumbprint ending "
                    f"{tail}): {why or 'the change could not be confirmed'}."
                )
            )

        gone = delete_local_cert(wincert, old_sha1)
        if gone.status == "deleted":
            console.ok("Deleted the old certificate and its private key from this computer")
        elif gone.status == "absent":
            console.detail("The old certificate was already gone from this computer.")
        elif gone.status == "key-remains":
            # The certificate is gone, so another run has nothing to find: it can't help.
            leftovers.append(
                _Leftover(
                    f"The old certificate (ending {tail}) was removed from this computer, but "
                    f"its private key could not be deleted: {gone.detail} The key is still in "
                    f"the {self.words.key_store}. {key_removal_advice(gone.ksp)}",
                    retry=False,
                )
            )
        else:
            leftovers.append(
                _Leftover(
                    f"The old certificate (ending {tail}) could not be deleted from this "
                    f"computer: {gone.detail}"
                )
            )

        if not any(item.retry for item in leftovers):
            # Nothing is left that running again could finish, so nothing to remember.
            try:
                update_env(self.paths.env_file, {}, remove=[RETIRING_KEY], guard=self.guard)
            except (OSError, EnvLockError) as exc:
                console.debug(f"Could not clear the retiring-certificate note in .env: {exc}")
            self._report_lock_problems()
        return leftovers

    def _finish_earlier_rotation(
        self, wincert: Any, object_id: str, retiring: str, current: str
    ) -> int:
        """An earlier rotation switched certificates but could not finish tidying up."""
        console = self.console
        console.info(
            "An earlier rotation already moved the agent to a new certificate. "
            "Finishing its cleanup; no new certificate is created."
        )
        access = connect(
            console,
            self.paths,
            self.world.env,
            platform=self.world.platform,
            which=self.world.which,
        )
        directory = None
        if access.problem:
            console.warn(access.problem[0])
            console.detail(access.problem[1])
        else:
            directory = Directory(console, self.paths, access.helpers)
        leftovers = self._retire(wincert, directory, object_id, retiring, keep_sha1=current)
        if leftovers:
            self._say_leftovers(leftovers)
            return EXIT_LEFTOVERS
        console.blank()
        console.ok("Cleanup finished. The agent is on its new certificate; nothing is left over.")
        return EXIT_OK

    def _say_leftovers(self, leftovers: list[_Leftover]) -> None:
        console = self.console
        console.blank()
        console.warn("The agent is working on the new certificate, but cleanup is not finished:")
        for item in leftovers:
            console.bullet(item.text)
        if any(item.retry for item in leftovers):
            console.info(
                "A certificate whose private key has been deleted cannot be used by anyone.\n"
                f"Run rotation again to finish the cleanup:  {self.words.rotate}"
            )

    def _finish(self, new: Any, leftovers: list[_Leftover]) -> int:
        console = self.console
        console.banner(
            "Certificate rotated" if not leftovers else "Certificate rotated, cleanup pending"
        )
        console.info(f"New certificate:  valid until {new.not_after:%d %b %Y}")
        console.info(f"Sign-in key:      {certstep.protection_label(new.ksp)}")
        if self.console.log_file is not None:
            console.info(f"Log:              {self.console.log_file}")
        console.blank()
        console.info(
            "Restart your assistant (close and reopen Claude Code or GitHub Copilot CLI).\n"
            "The agent reads its certificate when it starts."
        )
        if leftovers:
            self._say_leftovers(leftovers)
            return EXIT_LEFTOVERS
        return EXIT_OK


def run_rotate_cert(
    console: Console,
    paths: Paths,
    world: World,
    *,
    sleep: Callable[[float], None] = time.sleep,
    proof_attempts: int = PROOF_ATTEMPTS,
    proof_delay_seconds: float = PROOF_DELAY_SECONDS,
) -> int:
    """Rotate this computer's Blueprint certificate. Returns the process exit code:
    0 rotated (or an earlier rotation's cleanup finished), 1 failed with the current
    certificate still in use, 2 unattended without --yes, 3 rotated but something is left
    over (said precisely), 130 cancelled."""
    return _Rotation(
        console=console,
        paths=paths,
        world=world,
        sleep=sleep,
        proof_attempts=proof_attempts,
        proof_delay_seconds=proof_delay_seconds,
    ).run()
