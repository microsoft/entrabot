"""The setup wizard: ask, review, then do.

The run has two halves. The first only looks and asks: this computer, the sign-in, which
agent, who it works for, where its memory lives. Nothing in the tenant or on disk changes
until the person confirms the review screen, apart from an Azure CLI the person agrees to
install. The second half does the work, one step at a time, and every step is safe to run
again: re-running setup is the repair path. After the last step, and before the summary,
Word, Excel and OneDrive files are switched on when they were asked for (``workiq``), and the
licence walkthrough (``licences``) can get the agent the Microsoft 365 licences it still lacks.

Exit codes: 0 done, 1 failed, 2 usage or a missing answer under ``--yes``, 3 set up but with
something to check (the end-to-end test failed, a sponsor could not be confirmed, Claude Code
is too old for the project's safety checks, an older setup key could not be switched off in
Microsoft Entra, or Word, Excel and OneDrive files were asked for and do not work yet: a step
that switches them on failed, Microsoft does not honour them for the agent yet, or the agent
has no Microsoft 365 Copilot licence), 130 cancelled.
"""

from __future__ import annotations

import json
import os
import platform as platform_module
import re
import shutil
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from entrabot.licensing import Report, friendly_name
from entrabot.winsetup import (
    agentsponsors,
    azcli,
    certstep,
    hosts,
    licences,
    memory,
    sponsors,
    state,
    workiq,
)
from entrabot.winsetup.agentsponsors import DirectorySponsor, SponsorSync
from entrabot.winsetup.azcli import Account, AzCli, AzError, DirectoryUser, Subscription
from entrabot.winsetup.certstep import CertOutcome, CertStepError
from entrabot.winsetup.console import Cancelled, Choice, Console, MissingAnswer
from entrabot.winsetup.envfile import EnvGuard, lock_problem, read_env
from entrabot.winsetup.envstep import (
    EnvResult,
    Identity,
    identity_from_state,
    write_identity_env,
)
from entrabot.winsetup.helpers import (
    CHECKS_MARKER,
    DIAGNOSE_SNIPPET,
    LEGACY_DATA_SNIPPET,
    LICENSE_SNIPPET,
    RESULT_MARKER,
    SMOKE_SNIPPET,
    Helpers,
    Paths,
    marked_line,
)
from entrabot.winsetup.memory import MemoryRequest, MemoryResult, MemoryStopped
from entrabot.winsetup.oldkeys import (
    PROVISIONER_APP_NAME,
    OldSetupKeys,
    listed,
    old_setup_key_lines,
    read_old_setup_keys,
)
from entrabot.winsetup.sponsors import Sponsor, SponsorError
from entrabot.winsetup.state import IdentityChoice, IdentityError

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_SMOKE_FAILED, EXIT_CANCELLED = 0, 1, 2, 3, 130
TOTAL_STEPS = 11
DEFAULT_LOCATION = "eastus"
RERUN = "Run setup again. It continues from here and keeps what already works."

# What scripts/entra_provisioning.py prints after RESULT_MARKER, on a line of its own, when it
# would not take over an existing app registration with the Provisioner's name. Tests on both
# sides pin the whole line.
PROVISIONER_TAKEOVER_REFUSED = "provisioner-takeover-refused"
_TAKEOVER_REFUSED_FIX = (
    "The app registrations it found, and why it did not use them, are listed above and in\n"
    "the setup log. To go on, delete or rename the one you don't recognise (Entra admin\n"
    "center > App registrations > All applications), or, once you have checked that it is\n"
    "yours, set ENTRABOT_PROVISIONER_APP_ID_VERIFIED to its application ID in this window:\n"
    '  cmd.exe:     set "ENTRABOT_PROVISIONER_APP_ID_VERIFIED=<application ID>"\n'
    "  PowerShell:  $env:ENTRABOT_PROVISIONER_APP_ID_VERIFIED = '<application ID>'\n"
    "Then run setup again from the same window (scripts\\setup-windows.cmd)."
)
# Printed the same way, just before the script's error: the app named in
# ENTRABOT_PROVISIONER_APP_ID_VERIFIED was not accepted, or setup could not read from Microsoft
# Entra what it needed to decide. Nothing in Entra changed in either case.
PROVISIONER_APP_CHOICE_REJECTED = "provisioner-app-choice-rejected"
PROVISIONER_COULD_NOT_CHECK = "provisioner-could-not-check"
_APP_CHOICE_REJECTED = (
    "The value given in ENTRABOT_PROVISIONER_APP_ID_VERIFIED was not accepted. Nothing was "
    "changed in Microsoft Entra."
)
_APP_CHOICE_REJECTED_FIX = (
    "The reason is in the lines above and in the setup log. Set it, in this window, to the\n"
    "application ID of the Provisioner app you have checked is yours:\n"
    '  cmd.exe:     set "ENTRABOT_PROVISIONER_APP_ID_VERIFIED=<application ID>"\n'
    "  PowerShell:  $env:ENTRABOT_PROVISIONER_APP_ID_VERIFIED = '<application ID>'\n"
    "or clear it:\n"
    '  cmd.exe:     set "ENTRABOT_PROVISIONER_APP_ID_VERIFIED="\n'
    "  PowerShell:  Remove-Item Env:ENTRABOT_PROVISIONER_APP_ID_VERIFIED\n"
    "Then run setup again from the same window (scripts\\setup-windows.cmd)."
)
_COULD_NOT_READ_ENTRA = "Setup could not read what it needed from Microsoft Entra."
_PROVISIONER_COULD_NOT_CHECK_FIX = (
    "This is usually temporary, or a problem with the sign-in or an admin role (if you activate\n"
    "admin roles on demand, activate them first). What Microsoft Entra said is in the lines\n"
    f"above and in the setup log.\n{RERUN}"
)

# What scripts/create_entra_agent_ids.py prints after RESULT_MARKER, on a line of its own, just
# before it stops. Tests on both sides pin the whole line.
BLUEPRINT_NOT_FOUND = "blueprint-not-found"
BLUEPRINT_COULD_NOT_CHECK = "blueprint-could-not-check"
BLUEPRINT_AMBIGUOUS = "blueprint-ambiguous"
AGENT_USER_WRONG_BLUEPRINT = "agent-user-wrong-blueprint"
AGENT_LOOKUP_COULD_NOT_CHECK = "agent-lookup-could-not-check"
_DETAILS_ABOVE = "The details are in the lines above and in the setup log."
_BLUEPRINT_NOT_FOUND_FIX = (
    "Check the Blueprint App ID and that you signed in to the agent's tenant, then run setup\n"
    "again with the right one (-UseBlueprint APP_ID), or create a new agent instead\n"
    f"(-NewChain -UpnSuffix NAME).\n{_DETAILS_ABOVE}"
)
# The helper asks about the Blueprint before it records or creates anything (in
# blueprint_for_this_run), so "nothing was changed" holds for that stop.
_BLUEPRINT_COULD_NOT_CHECK_FIX = (
    f"This is usually temporary: run setup again in a few minutes.\n{_DETAILS_ABOVE}"
)
# Not for the agent lookups: they run after the Blueprint is recorded (or, for a new agent,
# created), and the Agent User's can run after a new Agent Identity was created. The helper's
# own lines say what was and was not created.
_AGENT_LOOKUP_COULD_NOT_CHECK_FIX = (
    "The lines above say what was and was not created.\n"
    "This is usually temporary: run setup again in a few minutes.\n"
    f"It continues from where it stopped.\n{_DETAILS_ABOVE}"
)
_BLUEPRINT_AMBIGUOUS_FIX = (
    "Run setup again with the App ID of the right one (the candidates are listed in the lines\n"
    "above):\n"
    "  scripts\\setup-windows.cmd -UseBlueprint <App ID>\n"
    f"{_DETAILS_ABOVE}"
)
_AGENT_USER_WRONG_BLUEPRINT_FIX = (
    "Join that agent instead (run setup with -UseBlueprint and its Blueprint App ID), or\n"
    f"choose another name for this one (-UpnSuffix NAME).\n{_DETAILS_ABOVE}"
)

# What a failed sign-in test looks like when the only problem is the kind of session.
_KEYRING_MARKERS = (
    "keyring",
    "credential manager",
    "credentials cannot be saved",
    "logon session",
    "1312",
)


# The preflight module names its checks for engineers; these are the same checks in plain words.
_TOKEN_CHECK = "Token mint (three-hop)"
_CHECK_LABELS = {
    _TOKEN_CHECK: "The agent can sign in",
    "Graph /me identity": "The agent is who it should be",
    "Teams scope (/me/chats)": "The agent can reach Teams",
}

# Microsoft Entra's sign-in can lag behind Microsoft Graph by tens of seconds after a
# certificate is added to an application: Graph lists it (the registration helper reads it
# back) while sign-in still answers that the key is not there. When this run registered the
# certificate, the end-to-end test waits for that the way certificate rotation does
# (rotation.PROOF_ATTEMPTS, PROOF_DELAY_SECONDS): up to twelve tries, ten seconds apart, about
# two minutes in all.
NEW_CERT_ATTEMPTS = 12
NEW_CERT_RETRY_SECONDS = 10.0
# Microsoft Entra's answer at hop 1 (the only step the Blueprint certificate signs) when it does
# not know the certificate that signed the request ("[Reason - The key was not found.]").
CERT_NOT_ACCEPTED_CODE = "AADSTS700027"
_AADSTS = re.compile(r"AADSTS\d+")

# A device-code sign-in that nobody finished before the code ran out.
_CODE_NOT_USED = "The sign-in code was not used in time. Codes last about 15 minutes."
_CODE_NOT_USED_FIX = "Run setup again to get a new code."


LicenceOutcome = Literal["assigned", "existing", "none", "unknown"]


def licence_outcome(
    output: str, before: Mapping[str, str], after: Mapping[str, str]
) -> LicenceOutcome:
    """What happened to the agent's Teams licence, from what ``create_entra_agent_ids.py``
    printed and recorded.

    ``assigned``  given one in this run (Teams needs 10-15 minutes to catch up).
    ``existing``  already had one.
    ``none``      the tenant has no free licence that includes Teams, so it has none.
    ``unknown``   the helper did not say.
    """
    if "No Teams-capable licenses found" in output or "No subscribed SKUs found" in output:
        return "none"
    if "already has Teams" in output:
        return "existing"
    if "Teams/mailbox" in output and "provisioning can take" in output:
        return "assigned"
    sku = after.get("AGENT_USER_LICENSE_SKU", "")
    if sku:
        return "existing" if before.get("AGENT_USER_LICENSE_SKU", "") == sku else "assigned"
    return "unknown"


def copilot_outcome(
    output: str, before: Mapping[str, str], after: Mapping[str, str]
) -> LicenceOutcome:
    """What happened to the agent's Microsoft 365 Copilot licence, when Work IQ asked
    ``create_entra_agent_ids.py`` for one: the same four answers as :func:`licence_outcome`."""
    if "No Microsoft 365 Copilot licenses found" in output or "No subscribed SKUs found" in output:
        return "none"
    if "already has Work IQ license" in output or "already has Teams and Work IQ" in output:
        return "existing"
    if "Work IQ license assigned" in output:
        return "assigned"
    sku = after.get("AGENT_USER_WORK_IQ_LICENSE_SKU", "")
    if sku:
        return "existing" if before.get("AGENT_USER_WORK_IQ_LICENSE_SKU", "") == sku else "assigned"
    return "unknown"


class SetupFailed(Exception):
    """A step failed. *fix* says what to do about it."""

    def __init__(self, message: str, *, fix: str | None = None) -> None:
        super().__init__(message)
        self.fix = fix


class UsageError(Exception):
    """The command line asks for something that can't be done as written."""


@dataclass
class Options:
    """What the command line asked for. Everything is optional; the wizard asks for the rest."""

    project_root: Path
    new_chain: bool = False
    use_blueprint: str = ""
    upn_suffix: str = ""
    agent_user_upn: str = ""
    sponsors: list[str] = field(default_factory=list)
    memory: str | None = None
    storage_account: str = ""
    container: str = ""
    create_new_storage: bool = False
    location: str = ""
    subscription: str = ""
    configure_work_iq: bool = False
    tenant: str = ""
    device_code: bool = False
    hosts: str | None = None
    skip_smoke: bool = False
    yes: bool = False
    verbose: bool = False
    log_file: Path | None = None


# What Microsoft Entra says when a sign-in has to be made again: the session was revoked
# (continuous access evaluation), or its refresh token expired or was invalidated.
_SESSION_ENDED = re.compile(
    r"InteractionRequired|TokenIssuedBeforeRevocationTimestamp|AADSTS(?:50173|700082|70043)"
    r"|interaction_required",
)
START_SCRIPT = "start-entrabot.cmd"
# End-to-end test results after which the agent is not offered a start, or things to try.
_CANNOT_START = ("sign-in", "keyring", "crashed", "certificate-pending")
# What Claude Code asks the first time it opens this folder, in the order it asks. Worded
# by what each question is about: Claude Code's own wording changes between versions.
FIRST_START_QUESTIONS = (
    "if this PC has not used it before, then whether to trust this folder, whether to use",
    'the "entrabot" MCP server, and whether to load a development channel. Say yes to each:',
    "the folder is the one you just set up, the server is your agent, and the channel is how",
    "Teams messages reach it.",
)
# Characters cmd.exe gives a meaning to even inside quotes. A launcher under such a folder is
# left for the person to double-click (Explorer starts it correctly).
_CMD_SPECIAL = re.compile(r"[&^%!()]")


def _wrapped(text: str) -> list[str]:
    """*text* as lines that fit under a thing to check on the last screen."""
    return textwrap.wrap(text, width=86, break_on_hyphens=False, break_long_words=False)


def launch_in_new_console(argv: Sequence[str], cwd: Path) -> None:
    """Start *argv* in its own console window and return at once. Windows only."""
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
    subprocess.Popen(  # noqa: S603 - the project's own launcher, by full path
        list(argv), cwd=cwd, creationflags=flags, close_fds=True
    )


def _os_description() -> str:
    return (
        f"{platform_module.system()} {platform_module.release()} "
        f"({platform_module.version()}), {platform_module.machine()}"
    )


@dataclass
class World:
    """Everything outside the wizard, so tests can swap it."""

    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    platform: str = sys.platform
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    which: Callable[[str], str | None] = shutil.which
    wincert: Any = None  # None: the real entrabot.platform.wincert, loaded when first needed
    http_get: sponsors.HttpGet = sponsors.http_get_json
    os_description: Callable[[], str] = _os_description
    launch: Callable[[Sequence[str], Path], None] = launch_in_new_console
    sleep: Callable[[float], None] = time.sleep


@dataclass
class Plan:
    """What the person decided, before anything is changed."""

    identity: IdentityChoice = field(default_factory=lambda: IdentityChoice(mode="continue"))
    explicit_sponsors: list[Sponsor] | None = None
    sponsor_label: str = ""
    memory_choice: memory.MemoryChoice = "keep"
    memory_label: str = ""
    subscription: Subscription | None = None
    location: str = ""
    work_iq: bool = False  # to be switched on in this run (off again if a step fails)
    work_iq_asked: bool = False  # asked for, on the command line or at the question
    hosts: list[str] = field(default_factory=lambda: list(hosts.KNOWN_HOSTS))


def run_diagnose(console: Console, paths: Paths, env: Mapping[str, str]) -> int:
    """Health check of an existing setup. Changes nothing."""
    console.banner(
        "Entrabot diagnostics", ["Checking the setup on this computer. Nothing is changed."]
    )
    if not paths.env_file.is_file():
        console.fail("This computer has not been set up yet.", fix="Run setup first.")
        return EXIT_FAILED
    helpers = Helpers(console, paths, env)
    result = console.run(
        [
            str(paths.python),
            "-c",
            DIAGNOSE_SNIPPET,
            str(paths.project_root),
            str(paths.mcp_binary),
            str(hosts.copilot_mcp_path(env)),
            "1" if console.color else "0",
        ],
        env=helpers.runtime_env(),
        cwd=paths.project_root,
    )
    console.blank()
    if result.returncode == 0:
        # The health check reports an older setup key as a warning, which fails no check; the
        # last line must not read as if nothing were left to do.
        if read_old_setup_keys(paths.state_file).pending:
            console.warn(
                "No check failed, but an older setup key still has to be switched off in "
                "Microsoft Entra (see above)."
            )
        else:
            console.ok("All checks passed.")
        return EXIT_OK
    console.fail("One or more checks failed (see above).", fix=RERUN)
    return EXIT_FAILED


class Wizard:
    def __init__(self, options: Options, console: Console, paths: Paths, world: World) -> None:
        self.options = options
        self.console = console
        self.paths = paths
        self.world = world
        self.plan = Plan()
        self._step = 0
        self.az: AzCli | None = None
        self.helpers = Helpers(console, paths, world.env)
        self.account: Account | None = None
        self.me: DirectoryUser | None = None
        self.subscriptions: list[Subscription] = []
        self.state: dict[str, str] = {}
        self.env_values: dict[str, str] = {}
        self.licence: LicenceOutcome = "unknown"
        self.copilot: LicenceOutcome = "unknown"  # Microsoft 365 Copilot, for Work IQ
        self.work_iq_failure: workiq.WorkIqError | None = None  # why switching it on failed
        # Set up, but the agent's tokens did not carry the scopes within setup's wait.
        self.work_iq_pending = False
        # What setup did on this PC before the review screen, in words: the review says it.
        self.done_before_review: list[str] = []
        # What the organization has, as the licence walkthrough last read it: the last
        # screen's advice is for what the organization has when this says.
        self.licence_report: Report | None = None
        # The kinds of licence ("teams", "copilot") whose advice the walkthrough showed: the
        # last screen points back to it rather than printing it again.
        self.licence_explained: set[str] = set()
        # Who the agent takes instructions from in Teams, as the directory says: before
        # anything changes (None when it can't be known yet), and after the sync.
        self.current_sponsors: list[DirectorySponsor] | None = None
        self.sponsor_sync: SponsorSync | None = None
        self.sponsor_attention: list[list[str]] = []  # what the person still has to do
        self._sponsor_names: dict[str, str] = {}
        # Older setup keys Entra still trusts, as the record stood after the provisioning step.
        self.old_keys = OldSetupKeys()
        console.verbose = console.verbose or options.verbose
        # Every write of .env goes through this: locked before it lands, and setup carries
        # on (with a precise warning) if a lock can't be applied. The file holds IDs only.
        self.guard = EnvGuard(
            console=console, platform=world.platform, env=world.env, tolerate_failure=True
        )

    # ── Plumbing ─────────────────────────────────────────────────────────────

    def _next_step(self, title: str) -> None:
        self._step += 1
        self.console.step(self._step, TOTAL_STEPS, title)

    def run(self) -> int:
        console = self.console
        try:
            self._validate_options()
            self._welcome()
            if self._this_computer() == "diagnose":
                return run_diagnose(console, self.paths, self.world.env)
            if console.missing:
                return self._report_missing(console.missing)
            self._sign_in()
            self._choices()
            if console.missing:
                return self._report_missing(console.missing)
            self._review()
            return self._execute()
        except UsageError as exc:
            console.fail(str(exc))
            return EXIT_USAGE
        except (Cancelled, KeyboardInterrupt):
            console.blank()
            console.warn("Setup was stopped. Nothing is left half-written.")
            console.detail(RERUN)
            return EXIT_CANCELLED
        except SetupFailed as exc:
            console.blank()
            console.fail(str(exc), fix=exc.fix or RERUN)
            if console.log_file:
                console.detail(f"Details are in the setup log: {console.log_file}")
            return EXIT_FAILED
        finally:
            console.close()

    def _report_missing(self, missing: list[MissingAnswer]) -> int:
        self.console.fail("Setup was asked not to prompt (-Yes), but needs these answers:")
        for item in missing:
            self.console.bullet(f"{item.prompt}  ->  {item.flag}" if item.flag else item.prompt)
        return EXIT_USAGE

    def _validate_options(self) -> None:
        opts = self.options
        if opts.new_chain and opts.use_blueprint:
            raise UsageError(
                "Choose one: create a new agent (-NewChain) or join an existing one "
                "(-UseBlueprint). They can't be combined."
            )
        checks = [
            (opts.use_blueprint, state.validate_app_id, "-UseBlueprint"),
            (opts.upn_suffix, state.validate_suffix, "-UpnSuffix"),
            (opts.agent_user_upn, state.validate_upn, "-AgentUserUpn"),
            (opts.tenant, azcli.validate_tenant, "-Tenant"),
        ]
        for value, validate, flag in checks:
            problem = validate(value) if value else None
            if problem:
                raise UsageError(f"{flag} {value!r}: {problem}")
        for sponsor in opts.sponsors:
            problem = sponsors.validate_sponsor(sponsor)
            if problem:
                raise UsageError(f"-Sponsor {sponsor!r}: {problem}")
        if opts.memory not in (None, "local", "cloud"):
            raise UsageError("Memory is either 'local' or 'cloud'.")
        if opts.create_new_storage and opts.storage_account:
            raise UsageError("-CreateNewStorage and -WithStorageAccount are mutually exclusive.")
        storage_flags = (
            opts.storage_account
            or opts.container
            or opts.create_new_storage
            or opts.location
            or opts.subscription
        )
        if storage_flags and opts.memory != "cloud":
            raise UsageError("The storage options only apply to cloud memory. Add -UseCloudMemory.")
        try:
            self.plan.hosts = hosts.parse_hosts(opts.hosts)
        except hosts.HostError as exc:
            raise UsageError(str(exc)) from exc

    # ── Looking and asking ───────────────────────────────────────────────────

    def _welcome(self) -> None:
        self.console.banner(
            "Entrabot setup",
            [
                "This gives you an AI agent with its own Microsoft Teams identity.",
                "It takes about ten minutes. Press Enter to accept the suggested answer.",
                "",
                "You need one of:",
                "  - a Microsoft work account that administers your Microsoft 365 tenant, or",
                "  - an agent's Blueprint App ID from someone who does.",
                "An Azure subscription is only needed if you choose cloud memory.",
            ],
        )

    def _this_computer(self) -> str:
        """System check, and which agent this run is for. Returns "setup" or "diagnose"."""
        console, paths, world = self.console, self.paths, self.world
        self._next_step("Checking this computer")
        if world.platform != "win32":
            raise SetupFailed(
                "This wizard sets up Entrabot on Windows.",
                fix="On macOS or Linux, run ./scripts/setup.sh instead.",
            )
        if not (paths.scripts / "entra_provisioning.py").is_file():
            raise SetupFailed(
                f"The Entrabot project folder looks incomplete ({paths.scripts} has no "
                "setup scripts).",
                fix="Download or clone the project again, then run setup from that folder.",
            )
        console.ok(world.os_description())

        root = str(paths.project_root)
        onedrive = [
            world.env[name]
            for name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")
            if world.env.get(name)
        ]
        if any(root.lower().startswith(base.lower()) for base in onedrive) or (
            "onedrive" in root.lower()
        ):
            console.warn("This folder is synced by OneDrive.")
            console.detail(
                "Setup works here, but sync can lock files while the agent runs. A folder "
                "outside OneDrive (for example C:\\dev\\entrabot) is more reliable."
            )
        if len(root) > 120:
            console.warn("This folder's path is very long, which some Windows tools can't handle.")
            console.detail("If setup fails with a path error, move the project somewhere shorter.")

        self._migrate_legacy_data()

        try:
            self.state = state.read_state(paths.state_file)
        except IdentityError as exc:
            raise SetupFailed(str(exc)) from exc
        self.env_values = read_env(paths.env_file)
        return self._decide_identity()

    def _migrate_legacy_data(self) -> None:
        """Older versions kept data in ``~\\.entrabot``; it now lives under LOCALAPPDATA."""
        result = self.helpers.snippet(LEGACY_DATA_SNIPPET)
        outcome = marked_line(result.stdout, RESULT_MARKER)
        if outcome == "moved":
            self.console.ok("Moved data from an older Entrabot version to its new location")
        elif result.returncode != 0:
            if "two entrabot dirs" in result.stderr:
                self.console.warn(
                    "Entrabot data exists in two places: an old folder (.entrabot in your "
                    "user folder) and the current one."
                )
                self.console.detail(
                    "The agent uses the current one. Compare them and delete the old folder "
                    "when you are sure nothing in it is needed."
                )
            else:
                self.console.warn("Could not check for data from an older Entrabot version.")
                self.console.detail(result.stderr.strip()[-300:])

    def _existing_label(self) -> str:
        return (
            self.env_values.get("ENTRABOT_AGENT_UPN")
            or self.env_values.get("ENTRABOT_AGENT_USER_UPN")
            or self.state.get("AGENT_USER_UPN")
            or "the agent with Blueprint "
            + (
                self.state.get("BLUEPRINT_APP_ID")
                or self.env_values.get("ENTRABOT_BLUEPRINT_APP_ID", "")
            )
        )

    def _decide_identity(self) -> str:
        console, opts = self.console, self.options
        recorded = self.state.get("BLUEPRINT_APP_ID", "")
        # A machine whose state file was lost still names its agent in .env.
        existing = recorded or self.env_values.get("ENTRABOT_BLUEPRINT_APP_ID", "")

        if opts.new_chain:
            self.plan.identity = self._new_agent()
            return "setup"
        if opts.use_blueprint:
            self.plan.identity = self._join(opts.use_blueprint)
            return "setup"

        if existing:
            label = self._existing_label()
            console.ok(f"This computer is already set up for {label}")
            choice = console.ask_choice(
                "What would you like to do?",
                [
                    Choice(
                        "repair",
                        f"Continue with {label}",
                        "Checks everything and repairs what is broken.",
                    ),
                    Choice("different", "Set up a different agent on this computer"),
                    Choice("diagnose", "Only run a health check", "Changes nothing."),
                ],
                default="repair",
                key="existing",
            )
            if choice == "diagnose":
                return "diagnose"
            if choice == "repair":
                self.plan.identity = (
                    IdentityChoice(mode="continue")
                    if recorded
                    else IdentityChoice(mode="join", blueprint_app_id=existing)
                )
                return "setup"

        if not console.interactive and "identity" not in console.answers:
            console.missing.append(
                MissingAnswer(
                    key="identity",
                    prompt="Which agent to set up",
                    flag="-NewChain -UpnSuffix NAME   or   -UseBlueprint APP_ID",
                )
            )
            return "setup"
        choice = console.ask_choice(
            "How do you want to set up your agent?",
            [
                Choice(
                    "new",
                    "Create a new agent",
                    "You need to be an administrator of your Microsoft 365 tenant.",
                ),
                Choice(
                    "join",
                    "Join an agent that already exists",
                    "For a second computer. Someone gives you its Blueprint App ID.",
                ),
            ],
            default="new",
            key="identity",
        )
        if choice == "new":
            self.plan.identity = self._new_agent()
        else:
            app_id = console.ask_text(
                "Blueprint App ID",
                validate=state.validate_app_id,
                key="blueprint",
                flag="-UseBlueprint APP_ID",
            ).strip()
            self.plan.identity = self._join(app_id)
        return "setup"

    def _new_agent(self) -> IdentityChoice:
        console, opts = self.console, self.options
        if opts.agent_user_upn:
            return IdentityChoice(mode="new", agent_user_upn=opts.agent_user_upn)
        suffix = opts.upn_suffix
        if not suffix:
            console.info("Your agent gets its own address: entrabot-agent-<name>@<your domain>.")
            username = self.world.env.get("USERNAME") or self.world.env.get("USER") or ""
            suffix = console.ask_text(
                "Short name for your agent",
                # A name is part of the agent's identity: never invented in an unattended run.
                default=state.default_suffix(username) if console.interactive else None,
                validate=state.validate_suffix,
                key="upn_suffix",
                flag="-UpnSuffix NAME",
            )
        return IdentityChoice(mode="new", upn_suffix=suffix)

    def _join(self, app_id: str) -> IdentityChoice:
        opts = self.options
        current = self.state.get("BLUEPRINT_APP_ID", "")
        upn = opts.agent_user_upn
        if not upn and not opts.upn_suffix and self.console.interactive and not opts.use_blueprint:
            upn = self.console.ask_text(
                "The agent's address, if you know it (Enter lets setup find it)",
                default="",
                validate=state.validate_upn,
                key="agent_user_upn",
            ).strip()
        return IdentityChoice(
            mode="join",
            blueprint_app_id=app_id,
            upn_suffix="" if upn else opts.upn_suffix,
            agent_user_upn=upn,
            switching=bool(current) and current.lower() != app_id.lower(),
        )

    def _sign_in(self) -> None:
        console, opts, world = self.console, self.options, self.world
        self._next_step("Signing in to Microsoft")

        location = azcli.find_az(
            env=world.env, data_dir=self.paths.data_root, platform=world.platform, which=world.which
        )
        if location is None:
            console.info(
                "The Azure CLI (Microsoft's sign-in and management tool) is not installed."
            )
            if not console.confirm("Install it now?", default=True, key="install_az"):
                raise SetupFailed(
                    "Setup needs the Azure CLI.",
                    fix="Install it from https://aka.ms/installazurecliwindows, then run "
                    "setup again.",
                )
            try:
                location = azcli.install_az(
                    console,
                    data_dir=self.paths.data_root,
                    python=self.paths.python,
                    env=world.env,
                    platform=world.platform,
                    which=world.which,
                    use_winget=azcli.is_local_admin(console, world.env),
                )
            except AzError as exc:
                raise SetupFailed(str(exc)) from exc
            self.done_before_review.append("installed the Azure CLI")
        az = self.az = AzCli(location, console, env=world.env, platform=world.platform)
        self.helpers = Helpers(console, self.paths, az.child_env(world.env))

        account = az.account()
        if account is not None and opts.tenant and not self._in_tenant(account, opts.tenant):
            account = None  # signed in somewhere else than the tenant that was asked for
        # Is the earlier sign-in still accepted? Only a real request to the directory tells:
        # az goes on listing an account, and handing out its cached token, after the
        # organization has ended the session (or when it was made through the Windows account
        # broker, which setup no longer uses). Offering that account fails steps later.
        me = az.me() if account is not None else None
        if account is not None and me is None:
            console.info(
                f"The earlier sign-in as {account.user} can no longer be used, so setup asks "
                "you to sign in again."
            )
            # Signing in again is not enough: az would go on sending the old token.
            az.forget(account.user)
            account = None
        if account is not None:
            console.info(
                f"Already signed in as {account.user} ({account.tenant_name or account.tenant_id})."
            )
            if not console.confirm("Use this account?", default=True, key="use_account"):
                account = None
        if account is None:
            account = self._login()
            me = az.me()
            if me is None:
                # A sign-in made this minute that the directory refuses: something older is
                # still in az's cache. One clean start, then stop: nothing has been changed.
                console.info(
                    "Microsoft did not accept that sign-in for the directory. Setup clears "
                    "what the Azure CLI remembered and asks you to sign in once more."
                )
                az.forget_everyone()
                account = self._login()
                me = az.me()
            if me is None:
                raise SetupFailed(
                    "Microsoft is not accepting this sign-in for requests to your directory.",
                    fix="Nothing has been changed. Wait a few minutes and run setup again. If "
                    "it keeps happening, your organization may be ending the session (a "
                    "sign-in risk or Conditional Access policy): ask your administrator.",
                )
        self.account = account
        console.ok(f"Signed in as {account.user} ({account.tenant_name or account.tenant_id})")

        self.me = me
        self.subscriptions = az.subscriptions(account.tenant_id)
        self._check_tenant(account)
        self._check_privileges()

    def _in_tenant(self, account: Account, wanted: str) -> bool:
        """Is *account* signed in to the tenant named *wanted* (a tenant ID or any of its
        domains)? A custom domain is resolved through the tenant's public sign-in metadata;
        when that can't be read the answer is no, and the person signs in again."""
        wanted = wanted.strip().lower()
        if wanted in (account.tenant_id.lower(), account.tenant_domain.lower()):
            return True
        if state.validate_app_id(wanted) is None:
            return False  # a different tenant ID
        document = self.world.http_get(
            f"https://login.microsoftonline.com/{wanted}/.well-known/openid-configuration"
        )
        return sponsors.tenant_id_from_discovery(document).lower() == account.tenant_id.lower()

    def _login(self) -> Account:
        console, opts, world = self.console, self.options, self.world
        assert self.az is not None
        tenant = opts.tenant or None
        device_code = opts.device_code or not azcli.has_interactive_desktop(
            world.env, platform=world.platform
        )
        if device_code:
            console.info("You will get a short code. Open the page shown below on any device,")
            console.info("enter the code, and sign in with your work account.")
        else:
            console.info("Your browser is opening so you can sign in to Microsoft.")
            console.detail("If you don't see it, look for a new browser tab or window.")
        outcome = self.az.login(tenant=tenant, device_code=device_code)
        if outcome == "failed" and not device_code:
            console.warn("That sign-in did not complete.")
            if console.confirm(
                "Try again with a sign-in code instead?", default=True, key="retry_device_code"
            ):
                console.info("Open the page shown below on any device and enter the code.")
                outcome = self.az.login(tenant=tenant, device_code=True)
        while outcome == "code-expired":
            # A new code is offered only to someone at the keyboard: unattended, it would run
            # out unused too.
            if not console.interactive:
                raise SetupFailed(_CODE_NOT_USED, fix=_CODE_NOT_USED_FIX)
            console.warn(_CODE_NOT_USED)
            if not console.confirm("Get a new code and try again?", default=True):
                raise SetupFailed("Sign-in did not complete.", fix=_CODE_NOT_USED_FIX)
            console.info("Open the page shown below on any device and enter the new code.")
            outcome = self.az.login(tenant=tenant, device_code=True)
        account = self.az.account() if outcome == "signed-in" else None
        if account is None:
            raise SetupFailed(
                "Sign-in did not complete.",
                fix="Run setup again. If your organization only allows sign-in from managed "
                "devices, run setup on one of those, or ask your administrator.",
            )
        return account

    def _same_agent(self) -> bool:
        """Is this run for the agent this computer is already set up for?"""
        identity = self.plan.identity
        if identity.mode == "continue":
            return True
        if identity.mode != "join" or identity.switching:
            return False
        known = self.state.get("BLUEPRINT_APP_ID") or self.env_values.get(
            "ENTRABOT_BLUEPRINT_APP_ID", ""
        )
        return known.lower() == identity.blueprint_app_id.lower()

    def _check_tenant(self, account: Account) -> None:
        """A repair must talk to the tenant the agent lives in."""
        recorded = self.env_values.get("ENTRABOT_TENANT_ID") or self.state.get("TENANT_ID", "")
        if self._same_agent() and recorded and recorded.lower() != account.tenant_id.lower():
            raise SetupFailed(
                "You are signed in to a different tenant "
                f"({account.tenant_name or account.tenant_id}) "
                f"than the one this computer's agent lives in ({recorded}). Nothing was changed.",
                fix=f"Sign in to the agent's tenant (run setup with -Tenant {recorded}), or run "
                "setup again and choose to set up a different agent.",
            )

    def _check_privileges(self) -> None:
        console = self.console
        assert self.az is not None
        verdict = azcli.assess_privileges(
            self.az.directory_role_template_ids(), mode=self.plan.identity.mode
        )
        if verdict.level == "enough":
            console.ok(verdict.message)
            return
        if verdict.level == "unknown":
            console.detail(f"{verdict.message} Setup will say so if a step needs more rights.")
            return
        console.warn(verdict.message)
        console.detail(
            "If you activate admin roles on demand (PIM), activate them and run setup again."
        )
        if (
            verdict.level == "none"
            and console.interactive
            and not console.confirm("Continue anyway?", default=False, key="continue_without_admin")
        ):
            raise Cancelled("no admin rights")

    def _choices(self) -> None:
        self._next_step("A few choices")
        if not self.console.interactive:
            self.console.info(
                "Using the answers given on the command line, and the defaults for the rest."
            )
        self._read_current_sponsors()
        self._decide_sponsor()
        self._decide_memory()
        self._decide_work_iq()

    def _self_sponsor(self) -> Sponsor | None:
        if self.me is None or self.account is None:
            return None
        return sponsors.sponsor_from_user(
            self.me, entered=self.account.user or self.me.upn, http_get=self.world.http_get
        )

    def _resolve_sponsors(self, identifiers: list[str]) -> list[Sponsor]:
        assert self.az is not None
        try:
            return sponsors.resolve_sponsors(identifiers, self.az, http_get=self.world.http_get)
        except SponsorError as exc:
            raise SetupFailed(
                str(exc), fix="Nothing was changed. Run setup again with the corrected sponsor."
            ) from exc

    def _tenant_env(self) -> dict[str, str]:
        """The environment for a helper that talks to this tenant with the provisioner."""
        env = dict(self.helpers.base_env)
        if self.account is not None:
            env["ENTRABOT_TENANT_ID"] = self.account.tenant_id
        return env

    def _read_current_sponsors(self) -> None:
        """Before anything changes: who does the agent on this computer already obey?

        Only possible for an agent this computer is already connected to. Reading changes
        nothing; when it can't be read yet the answer stays unknown.
        """
        agent_object_id = self.state.get("AGENT_OBJECT_ID") or self.env_values.get(
            "ENTRABOT_AGENT_OBJECT_ID", ""
        )
        if not self._same_agent() or not agent_object_id:
            return
        sync = agentsponsors.sync_sponsors(
            self.helpers, agent_object_id, [], env=self._tenant_env()
        )
        if sync.read:
            self.current_sponsors = list(sync.after)
        else:
            self.console.debug(f"The agent's sponsors could not be read yet: {sync.error}")

    def _sponsor_name(self, sponsor: DirectorySponsor) -> str:
        """A directory sponsor as the person knows them: the address they typed, their own
        sign-in name, or what the directory says."""
        key = sponsor.object_id.lower()
        for chosen in self.plan.explicit_sponsors or []:
            if chosen.object_id.lower() == key:
                return chosen.upn
        if self.me is not None and self.me.object_id.lower() == key and self.account:
            return self.account.user or sponsor.label
        if sponsor.mail or sponsor.upn:
            return sponsor.label
        if key not in self._sponsor_names:
            found = None
            if self.az is not None:
                try:
                    found = self.az.search_user(sponsor.object_id).user
                except AzError:
                    found = None
            self._sponsor_names[key] = (
                DirectorySponsor(found.object_id, found.display_name, found.upn, found.mail).label
                if found is not None
                else sponsor.label
            )
        return self._sponsor_names[key]

    def _ordered(self, listed: Sequence[DirectorySponsor]) -> list[DirectorySponsor]:
        """The people chosen in this run first, then everyone else as the directory lists them."""
        chosen = [item.object_id.lower() for item in self.plan.explicit_sponsors or []]

        def rank(sponsor: DirectorySponsor) -> int:
            key = sponsor.object_id.lower()
            return chosen.index(key) if key in chosen else len(chosen)

        return sorted(listed, key=rank)

    def _decide_sponsor(self) -> None:
        console, plan = self.console, self.plan
        me = self.account.user if self.account else "you"
        if self.options.sponsors:
            plan.explicit_sponsors = self._resolve_sponsors(self.options.sponsors)
            return
        if not console.interactive and "sponsor" not in console.answers:
            return  # a new agent obeys its creator; an existing one keeps the sponsors it has

        console.info("Who does this agent take instructions from in Teams? (its sponsors)")
        if plan.identity.mode == "new":
            choice = console.ask_choice(
                "The agent's sponsor",
                [Choice("me", f"Me ({me})"), Choice("other", "Someone else")],
                default="me",
                key="sponsor",
            )
        else:
            current = self.current_sponsors
            names = ", ".join(self._sponsor_name(item) for item in current or [])
            options = [
                Choice(
                    "keep",
                    f"Keep the current sponsors: {names}"
                    if names
                    else "Keep the agent's current sponsors",
                )
            ]
            already = self.me is not None and any(
                item.object_id.lower() == self.me.object_id.lower() for item in current or []
            )
            if not already:
                options.append(Choice("me", f"Add me ({me})"))
            options.append(Choice("other", "Add someone else"))
            choice = console.ask_choice(
                "The agent's sponsors", options, default="keep", key="sponsor"
            )

        if choice == "other":
            entered = console.ask_text(
                "Their work email (several: separate with commas)",
                validate=lambda value: next(
                    (
                        problem
                        for part in value.split(",")
                        if (problem := sponsors.validate_sponsor(part.strip()))
                    ),
                    None,
                ),
                key="sponsor_emails",
                flag="-Sponsor EMAIL",
            )
            plan.explicit_sponsors = self._resolve_sponsors(
                [part.strip() for part in entered.split(",") if part.strip()]
            )
        elif choice == "me" and plan.identity.mode != "new":
            mine = self._self_sponsor()
            if mine is None:
                raise SetupFailed("Could not look up your account in the directory.")
            plan.explicit_sponsors = [mine]

    def _sponsor_line(self) -> str:
        """The effect on who the agent obeys, for the review screen."""
        plan = self.plan
        me = self.account.user if self.account else "you"
        chosen = plan.explicit_sponsors or []
        chosen_ids = {item.object_id.lower() for item in chosen}
        current = self.current_sponsors
        current_ids = {item.object_id.lower() for item in current or []}
        new_agent = plan.identity.mode == "new"
        parts: list[str] = []
        for sponsor in chosen:
            if new_agent:
                is_creator = self.me is not None and sponsor.object_id.lower() == (
                    self.me.object_id.lower()
                )
                tag = "you" if is_creator else "added"
            elif current is None:
                tag = "added if not there already"
            else:
                tag = "already" if sponsor.object_id.lower() in current_ids else "added"
            parts.append(f"{sponsor.upn} ({tag})")
        if new_agent:
            creator_chosen = self.me is not None and self.me.object_id.lower() in chosen_ids
            if not chosen:
                parts.append(f"you ({me})")
            elif not creator_chosen:
                parts.append(f"{me} (you, because you create the agent)")
        elif current is None:
            parts.append("its current sponsors (listed once this computer is connected)")
        else:
            parts += [
                f"{self._sponsor_name(item)} (already)"
                for item in current
                if item.object_id.lower() not in chosen_ids
            ]
        return ", ".join(parts)

    def _decide_memory(self) -> None:
        console, opts, plan = self.console, self.options, self.plan
        current = memory.current_memory_mode(self.env_values) if self._same_agent() else "unset"
        on_cloud = current == "cloud"

        if opts.memory:
            choice = opts.memory
        elif console.interactive or "memory" in console.answers:
            console.info("Where should the agent keep its memory (conversation history, notes)?")
            if on_cloud:
                options = [
                    Choice("keep", "Keep it in Azure (current)"),
                    Choice(
                        "local",
                        "Move it to this computer",
                        "The agent starts with an empty memory here.",
                    ),
                ]
                default = "keep"
            else:
                options = [Choice("local", "On this computer", "Simplest. Nothing else to set up.")]
                if self.subscriptions:
                    options.append(
                        Choice(
                            "cloud",
                            "In your Azure subscription",
                            "Survives a lost or replaced computer. Costs a few cents a month.",
                        )
                    )
                else:
                    console.detail(
                        "Cloud memory is not offered: this account has no Azure subscription "
                        "in this tenant."
                    )
                default = "local"
            choice = console.ask_choice("Memory", options, default=default, key="memory")
        else:
            choice = "keep"

        if choice == "keep" or (choice == "local" and not opts.memory and not on_cloud):
            plan.memory_choice = "keep"
            plan.memory_label = "in Azure (unchanged)" if on_cloud else "on this computer"
            return
        if choice == "local":
            plan.memory_choice = "local"
            plan.memory_label = "on this computer"
            return

        if not self.subscriptions:
            raise SetupFailed(
                "Cloud memory needs an Azure subscription, and this account has none in this "
                "tenant. Nothing was changed.",
                fix="Run setup again and keep memory on this computer, or sign in with an "
                "account that has a subscription.",
            )
        plan.memory_choice = "cloud"
        plan.subscription = self._pick_subscription()
        plan.location = opts.location or console.ask_text(
            "Azure region for the storage",
            default=DEFAULT_LOCATION,
            validate=lambda value: (
                None
                if value.isalnum() and value.islower()
                else "A region looks like eastus or westeurope."
            ),
            key="location",
        )
        plan.memory_label = (
            f"in Azure (subscription '{plan.subscription.name}', region {plan.location})"
        )

    def _pick_subscription(self) -> Subscription:
        wanted = self.options.subscription
        if wanted:
            for item in self.subscriptions:
                if wanted.lower() in (item.id.lower(), item.name.lower()):
                    return item
            names = ", ".join(item.name for item in self.subscriptions)
            raise SetupFailed(
                f"No enabled Azure subscription called '{wanted}' in this tenant.",
                fix=f"Available: {names}.",
            )
        default = next(
            (item for item in self.subscriptions if item.is_default), self.subscriptions[0]
        )
        if len(self.subscriptions) == 1:
            return default
        picked = self.console.ask_choice(
            "Which Azure subscription should hold the storage?",
            [Choice(item.id, item.name) for item in self.subscriptions],
            default=default.id,
            key="subscription",
        )
        return next(item for item in self.subscriptions if item.id == picked)

    def _decide_work_iq(self) -> None:
        """Word, Excel and OneDrive files: explained first (they need a Microsoft 365 Copilot
        licence for the agent), then a yes or no. Nothing to install: setup switches them on
        itself after the end-to-end test, over SSH and unattended too."""
        console, plan = self.console, self.plan
        wanted = self.options.configure_work_iq
        if not wanted and (console.interactive or "work_iq" in console.answers):
            console.info(
                "The agent can also read and edit Word, Excel and OneDrive files for you (Work IQ)."
            )
            console.info("For that it needs one more licence of its own: Microsoft 365 Copilot.")
            console.info("Most people skip this.")
            wanted = console.confirm(
                "Switch on Word, Excel and OneDrive files?", default=False, key="work_iq"
            )
        if wanted:
            plan.work_iq_asked = plan.work_iq = True

    def _work_iq_label(self) -> str:
        """Work IQ, for the review screen."""
        plan = self.plan
        return "yes (Word, Excel and OneDrive files)" if plan.work_iq else "no"

    def _review(self) -> None:
        console, plan = self.console, self.plan
        self._next_step("Review")
        identity = plan.identity
        if identity.mode == "new":
            agent = (
                f"new agent {identity.agent_user_upn}"
                if identity.agent_user_upn
                else f"new agent entrabot-agent-{identity.upn_suffix}"
            )
        elif identity.mode == "join" and not self._same_agent():
            agent = f"join the agent with Blueprint {identity.blueprint_app_id}"
        else:
            agent = f"continue with {self._existing_label()}"
        names = {"claude": "Claude Code", "copilot": "GitHub Copilot CLI"}
        account = self.account
        console.info(f"Account:     {account.user if account else ''}")
        console.info(f"Agent:       {agent}")
        console.info(f"Will take instructions in Teams from: {self._sponsor_line()}")
        console.info(f"Memory:      {plan.memory_label}")
        console.info(f"Work IQ:     {self._work_iq_label()}")
        console.info(f"Assistants:  {', '.join(names[host] for host in plan.hosts)}")
        console.blank()
        if self.done_before_review:
            # True to what happened: the tenant is untouched, this PC has new tools.
            for line in textwrap.wrap(
                "Nothing has been changed in your tenant yet. On this PC setup has only "
                f"{listed(self.done_before_review)}.",
                width=88,
            ):
                console.info(line)
        else:
            console.info("Nothing has been changed yet.")
        if not console.confirm("Start setting up?", default=True, key="start"):
            raise Cancelled("declined at review")

    # ── Doing ────────────────────────────────────────────────────────────────

    def _execute(self) -> int:
        identity = self._create_agent()
        cert = self._certificate(identity)
        env_result = self._save_settings(identity, cert)
        memory_result, upload_failed = self._memory(identity, env_result)
        host_report = self._assistants()
        smoke = self._smoke(cert)
        # Optional, so after everything the agent needs is in place and tested.
        self._work_iq(identity)
        self._licences(identity, smoke)
        return self._finish(
            identity, cert, env_result, memory_result, upload_failed, host_report, smoke
        )

    def _provisioning_env(self) -> dict[str, str]:
        assert self.account is not None
        try:
            env = state.identity_environment(self.plan.identity, base=self.helpers.base_env)
        except IdentityError as exc:
            raise SetupFailed(str(exc)) from exc
        env["ENTRABOT_TENANT_ID"] = self.account.tenant_id
        # A Copilot licence is assigned only when this run asked for Work IQ. The variable is
        # dropped first: the old PowerShell setup left it set in the calling session.
        env.pop("ENTRABOT_ASSIGN_WORK_IQ_LICENSE", None)
        if self.plan.work_iq:
            env["ENTRABOT_ASSIGN_WORK_IQ_LICENSE"] = "1"
        return env

    def _expected_blueprint(self) -> str:
        """The Blueprint App ID the agent step must come back with: the one this run joins,
        or the one this computer is recorded for. Empty for a new agent."""
        identity = self.plan.identity
        if identity.mode == "join":
            return identity.blueprint_app_id
        if identity.mode == "continue":
            return self.state.get("BLUEPRINT_APP_ID", "")
        return ""

    def _create_agent(self) -> Identity:
        console, paths = self.console, self.paths
        assert self.account is not None and self.az is not None
        already_here = self._same_agent()  # asked now: the state file is about to change
        expected_blueprint = self._expected_blueprint()  # likewise

        self._next_step("Connecting setup to your tenant")
        try:
            previous = state.read_state(paths.state_file)
            notes = state.apply_identity_choice(
                paths.state_file,
                self.plan.identity,
                now=self.world.now(),
                env_values=read_env(paths.env_file),
            )
        except IdentityError as exc:
            raise SetupFailed(str(exc)) from exc
        for note in notes:
            console.info(note)
        env = self._provisioning_env()
        try:
            before = state.read_state(paths.state_file)  # as the identity choice left it
        except IdentityError as exc:
            raise SetupFailed(str(exc)) from exc
        bootstrap = self.helpers.script(
            "entra_provisioning.py",
            env=env,
            heartbeat="Still working with Microsoft Entra",
            summary=True,
        )
        if bootstrap.returncode != 0:
            console.show_tail(bootstrap)
            outcome = (marked_line(bootstrap.stdout, RESULT_MARKER) or "").strip()
            if outcome == PROVISIONER_TAKEOVER_REFUSED:
                raise SetupFailed(
                    "An app registration with the Provisioner's name already exists in this "
                    "tenant, and setup would not take it over.",
                    fix=_TAKEOVER_REFUSED_FIX,
                )
            if outcome == PROVISIONER_APP_CHOICE_REJECTED:
                raise SetupFailed(_APP_CHOICE_REJECTED, fix=_APP_CHOICE_REJECTED_FIX)
            if outcome == PROVISIONER_COULD_NOT_CHECK:
                raise SetupFailed(
                    f"{_COULD_NOT_READ_ENTRA} Nothing was changed in Microsoft Entra.",
                    fix=_PROVISIONER_COULD_NOT_CHECK_FIX,
                )
            if _SESSION_ENDED.search(bootstrap.stdout + bootstrap.stderr):
                raise SetupFailed(
                    "Your Microsoft sign-in is no longer accepted: your organization ended "
                    "the session while setup was running.",
                    fix="Run setup again. It will ask you to sign in again, then continue from "
                    "here and keep what already works.",
                )
            raise SetupFailed(
                "Setup could not get the access it needs in your tenant.",
                fix="This step needs an administrator of the tenant. If you activate admin "
                f"roles on demand, activate them first. {RERUN}",
            )
        console.ok("Setup has the access it needs")
        if "Created provisioner app" in bootstrap.stdout:
            console.detail(
                "First time in this tenant: created the "
                f"'{PROVISIONER_APP_NAME}' app registration."
            )
        # The helper's own warning went to the log, so what it recorded is said here.
        self.old_keys = self._read_old_setup_keys()
        if self.old_keys.pending:
            self._say_old_setup_keys(self.old_keys)
            if console.log_file:
                console.detail(f"The reason is in the setup log: {console.log_file}")

        self._next_step("Creating your agent")
        self._licence_preflight()
        created = self.helpers.script(
            "create_entra_agent_ids.py",
            env=env,
            heartbeat="Still creating your agent. This step can take a couple of minutes",
            summary=True,
        )
        if created.returncode != 0:
            console.show_tail(created)
            outcome = (marked_line(created.stdout, RESULT_MARKER) or "").strip()
            if outcome == BLUEPRINT_NOT_FOUND and self.plan.identity.mode == "join":
                self._forget_missing_blueprint(previous=previous, applied=before)
            raise self._agent_step_stopped(
                outcome, blueprint=expected_blueprint, recorded=already_here
            )
        try:
            self.state = state.read_state(paths.state_file)
        except IdentityError as exc:
            raise SetupFailed(str(exc)) from exc
        identity = identity_from_state(self.state, tenant_id=self.account.tenant_id)
        returned = identity.blueprint_app_id
        if expected_blueprint and returned and returned.lower() != expected_blueprint.lower():
            # Defence in depth: the next steps would register this computer's certificate on
            # the Blueprint just recorded, and add sponsors to its agent.
            console.show_tail(created)
            asked = (
                "This computer is set up for the agent"
                if already_here
                else "You asked to join the agent"
            )
            raise SetupFailed(
                f"{asked} with Blueprint {expected_blueprint}, but the agent step "
                f"(create_entra_agent_ids.py) returned Blueprint {returned}. Setup stopped "
                "before this computer's certificate step. Nothing further was changed.",
                fix=f"Run setup again with -UseBlueprint {expected_blueprint}, or check that "
                f"{expected_blueprint} is the right Blueprint App ID.",
            )
        if not identity.blueprint_app_id or not identity.agent_id:
            console.show_tail(created)
            raise SetupFailed(
                "The agent was created, but its IDs were not recorded on this computer."
            )
        name = identity.agent_user_upn or identity.agent_id
        mode = self.plan.identity.mode
        if mode == "new":
            console.ok(f"Created your agent: {name}")
        elif already_here:
            console.ok(f"Your agent: {name} (already set up)")
        else:
            console.ok(f"Connected this computer to the agent: {name}")
        if not identity.agent_user_id:
            console.warn("The agent's Teams user was not created.")
            console.show_tail(created)

        self.licence = (
            licence_outcome(created.stdout + created.stderr, before, self.state)
            if identity.agent_user_id
            else "unknown"
        )
        sku = self.state.get("AGENT_USER_LICENSE_SKU", "")
        if self.licence == "assigned":
            # By a name a person knows; the part number goes to the log only.
            named = f" ({friendly_name(sku)})" if sku else ""
            console.ok(f"Teams licence assigned{named}")
            console.debug(f"Teams licence part number: {sku or 'not recorded'}")
            console.detail("Teams and the mailbox take 10-15 minutes to become available.")
        elif self.licence == "existing":
            console.ok("The agent already has a Teams licence")
        elif self.licence == "none":
            console.warn("The agent has no Teams licence: none is free in this tenant.")
            console.detail(
                "Setup carries on. The agent will work, but not in Teams, until it has one. "
                "What to do is at the end."
            )
        if self.plan.work_iq and identity.agent_user_id:
            self.copilot = copilot_outcome(created.stdout + created.stderr, before, self.state)
            copilot_sku = self.state.get("AGENT_USER_WORK_IQ_LICENSE_SKU", "")
            if self.copilot == "assigned":
                console.ok("Microsoft 365 Copilot licence assigned")
                console.debug(
                    f"Microsoft 365 Copilot licence part number: {copilot_sku or 'not recorded'}"
                )
            elif self.copilot == "existing":
                console.ok("The agent already has a Microsoft 365 Copilot licence")
            elif self.copilot == "none":
                console.warn(
                    "The agent has no Microsoft 365 Copilot licence: none is free in this tenant."
                )
                console.detail("Word, Excel and OneDrive files need it. What to do is at the end.")

        self._sync_sponsors(identity)
        return identity

    def _work_iq(self, identity: Identity) -> None:
        """After the end-to-end test, not a numbered step: switch Word, Excel and OneDrive
        files on (:mod:`workiq`). A failure never stops setup: they stay off, and the last
        screen says why and what to do."""
        plan, console = self.plan, self.console
        if not plan.work_iq:
            return
        console.section("Word, Excel and OneDrive files")
        try:
            outcome = workiq.configure_work_iq(
                paths=self.paths,
                helpers=self.helpers,
                console=console,
                blueprint_app_id=identity.blueprint_app_id,
                sleep=self.world.sleep,
            )
        except workiq.WorkIqError as exc:
            plan.work_iq = False
            self.work_iq_failure = exc
            console.warn("Word, Excel and OneDrive files could not be switched on.")
            console.detail(str(exc))
            console.detail("Setup carries on without them. What to do is at the end.")
            return
        self.work_iq_pending = outcome == "pending"

    def _agent_step_stopped(self, outcome: str, *, blueprint: str, recorded: bool) -> SetupFailed:
        """Why the agent step stopped, from the result line create_entra_agent_ids.py printed
        before it did (*outcome*, "" when it printed none). *blueprint* is the Blueprint App ID
        this run was for ("" for a new agent); *recorded* says it is the one this computer is
        set up for, not one given in this run."""
        account = self.account
        where = (account.tenant_name or account.tenant_id) if account else ""
        tenant = f"this tenant ({where})" if where else "this tenant"
        if outcome == BLUEPRINT_NOT_FOUND:
            if blueprint:
                whose = "recorded on this computer" if recorded else "you gave"
                message = f"The Blueprint App ID {whose} ({blueprint}) does not exist in {tenant}."
            else:
                message = f"The agent's Blueprint does not exist in {tenant}."
            return SetupFailed(message, fix=_BLUEPRINT_NOT_FOUND_FIX)
        if outcome == BLUEPRINT_COULD_NOT_CHECK:
            return SetupFailed(
                f"{_COULD_NOT_READ_ENTRA} Nothing was changed.", fix=_BLUEPRINT_COULD_NOT_CHECK_FIX
            )
        if outcome == AGENT_LOOKUP_COULD_NOT_CHECK:
            return SetupFailed(_COULD_NOT_READ_ENTRA, fix=_AGENT_LOOKUP_COULD_NOT_CHECK_FIX)
        if outcome == BLUEPRINT_AMBIGUOUS:
            return SetupFailed(
                "Several applications in this tenant have the agent's name, and none is recorded "
                "on this computer, so setup can't tell which one is your agent.",
                fix=_BLUEPRINT_AMBIGUOUS_FIX,
            )
        if outcome == AGENT_USER_WRONG_BLUEPRINT:
            return SetupFailed(
                "The name chosen for the agent already belongs to a different agent.",
                fix=_AGENT_USER_WRONG_BLUEPRINT_FIX,
            )
        return SetupFailed("The agent could not be created.")

    def _forget_missing_blueprint(
        self, *, previous: Mapping[str, str], applied: Mapping[str, str]
    ) -> None:
        """The Blueprint this run joins does not exist: put the state file's record of the
        agent back as it was before this run named that Blueprint (*previous*; *applied* is
        the file as the identity choice left it). Otherwise the next run, with the right App
        ID, would be a switch away from an agent that was never there. What the provisioning
        step recorded in this run stays. A record that can't be put back is left as it is."""
        try:
            changed = state.undo_join(self.paths.state_file, before=previous, applied=applied)
        except (IdentityError, OSError) as exc:
            self.console.debug(f"Could not put this computer's record back: {exc}")
            return
        if changed:
            self.console.detail(
                "Setup did not keep that Blueprint App ID: this computer's record of its agent "
                "is as it was before this run."
            )

    def _sync_sponsors(self, identity: Identity) -> None:
        """Make the directory agree with the choice: add the chosen sponsors who are missing
        from the Agent Identity, read the list back, and say what is true now.

        Additive only. Nobody is ever removed here, and nothing is claimed that the read-back
        does not show.
        """
        console, paths = self.console, self.paths
        chosen = self.plan.explicit_sponsors or []
        sync = agentsponsors.sync_sponsors(
            self.helpers,
            identity.agent_object_id,
            [item.object_id for item in chosen],
            env=self._tenant_env(),
        )
        self.sponsor_sync = sync
        failed = {key.lower(): reason for key, reason in sync.failed.items()}

        if not sync.read:
            console.warn(f"Could not read who the agent takes instructions from: {sync.error}")
        for sponsor in chosen:
            key = sponsor.object_id.lower()
            add = agentsponsors.add_command(paths, sponsor.upn, object_id=sponsor.object_id)
            if sync.read and key in {item.lower() for item in sync.added}:
                console.ok(f"{sponsor.upn} can now give the agent instructions in Teams")
            elif sync.read and sync.was_sponsor(sponsor.object_id):
                console.ok(f"{sponsor.upn} is already a sponsor")
            elif key in {item.lower() for item in sync.unconfirmed}:
                console.warn(f"{sponsor.upn} was added, but the directory does not list them yet.")
                self.sponsor_attention.append(
                    [
                        f"{sponsor.upn} was added as a sponsor, but the directory does not "
                        "list them yet.",
                        "Check in a few minutes with:",
                        f"  {agentsponsors.list_command(paths)}",
                    ]
                )
            else:
                reason = failed.get(key) or sync.error or "the directory did not accept it"
                console.warn(f"Could not add {sponsor.upn} as a sponsor: {reason}")
                self.sponsor_attention.append(
                    [
                        f"The agent will not take instructions from {sponsor.upn} in Teams "
                        "until they are added as a sponsor.",
                        "An administrator can add them from this folder with:",
                        f"  {add}",
                    ]
                )
        if not sync.read:
            return

        names = [self._sponsor_name(item) for item in self._ordered(sync.after)]
        if not chosen:
            console.info(f"Takes instructions in Teams from: {', '.join(names) or 'nobody yet'}")
        chosen_ids = {item.object_id.lower() for item in chosen}
        if (
            self.plan.identity.mode == "new"
            and chosen
            and self.me is not None
            and self.account is not None
            and self.me.object_id.lower() not in chosen_ids
            and sync.is_sponsor(self.me.object_id)
        ):
            me = self.account.user
            step_back = agentsponsors.remove_command(paths, me, object_id=self.me.object_id)
            console.info(f"You ({me}) are a sponsor too, because you created the agent.")
            console.detail(f"To step back later:  {step_back}")

    def _read_old_setup_keys(self) -> OldSetupKeys:
        """What the state file says now about older setup keys Entra may still trust. A file
        that can't be read is treated like a record that can't: setup can't tell, so it asks
        the person to check."""
        return read_old_setup_keys(self.paths.state_file)

    def _say_old_setup_keys(self, note: OldSetupKeys) -> None:
        lines = old_setup_key_lines(note)
        if not lines:
            return
        self.console.warn(lines[0])
        for line in lines[1:]:
            self.console.info(f"     {line}")

    def _claude_command(self, host_report: hosts.HostReport) -> str:
        """The command that starts the agent by hand. Claude Code's installer does not always
        put it on PATH, so the full path is used when plain ``claude`` would not be found."""
        program = hosts.claude_program(host_report.claude, self.world.which)
        return f"{program} {hosts.START_ARGUMENTS}"

    def _things_to_try(self) -> None:
        """What to ask the agent once it answers, by what this agent can actually do: Teams
        and mail follow its licence, and the Word tools exist only with Work IQ."""
        console = self.console

        def under(text: str) -> None:  # a line under a suggestion, below its first word
            console.detail(f" {text}")  # detail indents five; a suggestion's words start at six

        def note(text: str) -> None:  # a line of its own, level with the suggestions' dashes
            console.info(f"  {text}")

        console.blank()
        console.info("Things to try next. Say them to the agent:")
        if self.licence == "none":
            # Mail needs a licence as much as Teams does: one line for both, for later.
            note(
                "- Once the agent has its licence (see above), ask it to message you in Teams "
                "and email you."
            )
        else:
            note("- Send me a Teams message to say hello.")
            under("Then answer it in Teams. The agent sees your reply.")
            if self.licence == "assigned":
                under("If Teams doesn't answer straight away: a new licence takes 10-15 minutes.")
            note("- Send me an email that says what you can do.")
            under("Needs a licence that includes Outlook mail.")
        plan = self.plan
        if plan.work_iq and self.copilot in ("assigned", "existing"):
            note("- Create a Word document called Hello and share it with me.")
            if self.work_iq_pending:
                under("If the agent says it cannot reach Word yet, try again in a few minutes.")
            elif self.copilot == "assigned":
                under("A new Microsoft 365 Copilot licence can take 10-15 minutes.")
        elif plan.work_iq:
            note("Word, Excel and OneDrive files are switched on, but they need the agent's")
            note("Microsoft 365 Copilot licence first (see above).")
        elif plan.work_iq_asked:
            note(
                "Word, Excel and OneDrive files were asked for but are not switched on yet (see "
                "above)."
            )
        elif workiq.word_tools_configured(self.paths.project_root):
            note("- Create a Word document called Hello and share it with me.")
        else:
            note(
                "Word, Excel and OneDrive files are not switched on. To add them, run setup "
                "again and"
            )
            note("answer yes to that question.")
        console.info(
            "  The agent can use only the Microsoft 365 products it has a licence for. An "
            "administrator"
        )
        console.info(
            "  gives it one in the Microsoft 365 admin center:  https://admin.microsoft.com"
        )

    def _offer_to_start(self, host_report: hosts.HostReport, smoke: str) -> None:
        """Interactively, offer to open the agent in its own window. Never unattended, and
        only when it can actually start: the launcher is there, nothing is left to install,
        the agent can sign in, and there is a desktop to open a window on."""
        console, world = self.console, self.world
        launcher = self.paths.project_root / START_SCRIPT
        if (
            not console.interactive
            or "claude" not in self.plan.hosts
            or not launcher.is_file()
            or host_report.todo
            or smoke in _CANNOT_START
            or not azcli.has_interactive_desktop(world.env, platform=world.platform)
            or _CMD_SPECIAL.search(str(launcher))
        ):
            return
        console.blank()
        if not console.confirm("Start your agent now?", default=True, key="start_agent"):
            return
        comspec = world.env.get("ComSpec") or world.env.get("COMSPEC")
        if not comspec:
            root = world.env.get("SystemRoot") or world.env.get("SYSTEMROOT") or r"C:\Windows"
            comspec = str(Path(root) / "System32" / "cmd.exe")
        try:
            world.launch([comspec, "/c", str(launcher)], self.paths.project_root)
        except (OSError, ValueError) as exc:
            console.warn(f"Could not open it ({exc}). Double-click {START_SCRIPT} instead.")
            return
        console.ok("Your agent is starting in its own window")

    def _works_for(self, env_result: EnvResult) -> str:
        """Who the agent obeys, from the directory read-back; never from what was asked for."""
        sync = self.sponsor_sync
        if sync is not None and sync.read:
            names = [self._sponsor_name(item) for item in self._ordered(sync.after)]
            return ", ".join(names) or "nobody yet (the agent has no sponsor in the directory)"
        named = env_result.sponsor_label
        return "could not be read from the directory" + (
            f" (this computer's settings name {named})" if named else ""
        )

    def _licence_preflight(self) -> None:
        """Is there a Teams licence to give the agent? Informational; never blocks."""
        assert self.az is not None
        token = self.az.graph_token()
        if not token:
            return
        result = self.helpers.snippet(
            LICENSE_SNIPPET, ["1" if self.plan.work_iq else "0"], input_text=token
        )
        # The log always gets this; the screen only in verbose mode. What the person needs to
        # know is what happened to the licence once the agent exists, and that comes next.
        for check in _checks(result.stdout):
            self.console.debug(
                f"Before creating the agent - {check.get('name')}: "
                f"{check.get('status')}: {check.get('detail')}"
            )

    def _certificate(self, identity: Identity) -> CertOutcome:
        self._next_step("Securing the agent's sign-in on this computer")
        try:
            wincert = self.world.wincert or certstep.load_wincert()
            return certstep.ensure_blueprint_cert(
                env_values=read_env(self.paths.env_file),
                blueprint_object_id=identity.blueprint_object_id,
                blueprint_app_id=identity.blueprint_app_id,
                helpers=self.helpers,
                console=self.console,
                wincert=wincert,
                now=self.world.now(),
            )
        except CertStepError as exc:
            raise SetupFailed(str(exc)) from exc

    def _save_settings(self, identity: Identity, cert: CertOutcome) -> EnvResult:
        console, paths = self.console, self.paths
        self._next_step("Saving settings")
        result = write_identity_env(
            paths.env_file,
            identity=identity,
            cert=cert,
            explicit_sponsors=self.plan.explicit_sponsors,
            default_sponsor=self._self_sponsor,
            guard=self.guard,
        )
        console.ok("Settings saved to .env")
        if result.sponsor == "none":
            console.warn("Could not look up your account, so no sponsor was recorded.")
            console.detail("Run setup again with -Sponsor you@yourcompany.com.")
        elif result.sponsor == "kept":
            console.detail(f"Contact in this computer's settings unchanged: {result.sponsor_label}")
        sync = self.sponsor_sync
        if (
            result.sponsor == "set"
            and not self.plan.explicit_sponsors
            and sync is not None
            and sync.read
            and self.me is not None
            and self.account is not None
            and not sync.is_sponsor(self.me.object_id)
        ):
            me = self.account.user
            console.warn(
                f"This computer's settings name you ({me}) as the agent's contact, but the "
                "agent will not take instructions from you in Teams: you are not one of its "
                "sponsors."
            )
            console.detail(f"To become one, run setup again with -Sponsor {me}.")
        if cert.unconfirmed:
            raise SetupFailed(
                "Microsoft Entra has not confirmed this computer's certificate yet. Your "
                "settings were saved.",
                fix="Wait a minute, then run setup again. It checks again and registers the "
                "certificate only if it is missing.",
            )
        return result

    def _memory(self, identity: Identity, env_result: EnvResult) -> tuple[MemoryResult, bool]:
        console, plan = self.console, self.plan
        self._next_step("Setting up the agent's memory")
        request = MemoryRequest(
            choice=plan.memory_choice,
            tenant_id=identity.tenant_id,
            agent_user_id=identity.agent_user_id,
            storage_account=self.options.storage_account,
            container=self.options.container,
            create_new_storage=self.options.create_new_storage,
            location=plan.location,
            subscription_id=plan.subscription.id if plan.subscription else "",
        )
        try:
            result = memory.apply_memory(
                self.paths.env_file,
                request,
                same_agent=env_result.same_agent,
                helpers=self.helpers,
                console=console,
                guard=self.guard,
            )
        except MemoryStopped as exc:
            raise SetupFailed(
                f"{exc}. Setup stopped because you chose not to continue without cloud memory.",
                fix="Your agent and settings are saved. Fix the problem above and run setup again.",
            ) from exc
        if result.mode == "cloud":
            console.ok(f"Memory: Azure Blob Storage ({result.endpoint}/{result.container})")
        else:
            console.ok("Memory: on this computer")
        self._confirm_settings_are_private()
        upload_failed = False
        if result.provisioned:
            upload_failed = memory.offer_memory_upload(self.helpers, console) is False
        return result, upload_failed

    def _confirm_settings_are_private(self) -> None:
        """After the last write of .env: read its permissions back, and only say "only your
        account" when that is what the file system says."""
        console = self.console
        problem = lock_problem(
            self.paths.env_file, console=console, platform=self.world.platform, env=self.world.env
        )
        if problem is None:
            console.ok("Settings file .env: only your account can read or change it")
            return
        console.warn(f"The settings file .env is NOT restricted to your account: {problem}.")
        for attempt in self.guard.problems[-1:]:
            console.detail(f"When setup tried to restrict it: {attempt}")
        console.detail("It holds IDs, not passwords or keys, so setup continues.")

    def _assistants(self) -> hosts.HostReport:
        console = self.console
        self._next_step("Connecting your assistants")
        try:
            written = hosts.register_mcp(self.paths, self.plan.hosts, env=self.world.env)
        except hosts.HostError as exc:
            raise SetupFailed(str(exc)) from exc
        for target in written:
            console.ok(f"Registered the agent in {target}")
        report = hosts.check_hosts(
            self.paths,
            self.plan.hosts,
            console,
            env=self.helpers.base_env,
            platform=self.world.platform,
            which=self.world.which,
        )
        report.registered = written
        return report

    def _smoke(self, cert: CertOutcome) -> str:
        """End-to-end test. Returns "pass", "skipped", or why it failed, as a short code.

        When this run registered the certificate on the Blueprint (*cert*), a sign-in that
        fails at hop 1 because Microsoft Entra does not accept that certificate yet is tried
        again for about two minutes (NEW_CERT_ATTEMPTS) before the result is reported. Any
        other result is reported at once.
        """
        console = self.console
        self._next_step("Testing the agent end to end")
        if self.options.skip_smoke:
            console.warn("Test skipped (-SkipSmoke).")
            return "skipped"
        console.info("Signing in as the agent and reaching Teams...")
        result = self.helpers.snippet(SMOKE_SNIPPET)
        checks = _checks(result.stdout)
        waiting = cert.registered and _certificate_not_accepted_yet(checks)
        attempt = 1
        while waiting and attempt < NEW_CERT_ATTEMPTS:
            if attempt == 1:
                console.detail(
                    "Microsoft Entra has not started accepting the certificate just registered. "
                    "That can take a minute or two."
                )
            attempt += 1
            console.detail(
                f"Still waiting for Microsoft Entra ({CERT_NOT_ACCEPTED_CODE}). Trying again in "
                f"{NEW_CERT_RETRY_SECONDS:.0f}s (attempt {attempt} of {NEW_CERT_ATTEMPTS})"
            )
            self.world.sleep(NEW_CERT_RETRY_SECONDS)
            result = self.helpers.snippet(SMOKE_SNIPPET)
            checks = _checks(result.stdout)
            waiting = _certificate_not_accepted_yet(checks)
        if not checks:
            console.fail("The test could not run.")
            console.detail(result.stderr.strip()[-600:])
            return "crashed"
        for check in checks:
            name = str(check.get("name"))
            label = _CHECK_LABELS.get(name, name)
            status = check.get("status")
            if status == "pass":
                console.ok(label)
            elif status == "fail":
                console.fail(f"{label}: no")
                console.detail(f"{name}: {check.get('detail')}")
            elif status == "skip":
                console.detail(f"{label}: not checked")
            else:
                console.warn(f"{label}: {check.get('detail')}")
        if not any(check.get("status") == "fail" for check in checks):
            return "pass"
        if waiting:
            return "certificate-pending"  # still not accepted after the wait
        if checks[0].get("status") == "fail":
            detail = str(checks[0].get("detail", "")).lower()
            return "keyring" if any(marker in detail for marker in _KEYRING_MARKERS) else "sign-in"
        return "teams-not-ready"

    def _licences(self, identity: Identity, smoke: str) -> None:
        """After the end-to-end test, before the summary: the licence walkthrough (not a
        numbered step), when the agent lacks a licence that includes Teams, or the Microsoft
        365 Copilot licence that Word, Excel and OneDrive files need, once they are switched
        on in this setup (asked for but left off, Copilot is not mentioned). Offered only to
        someone at the keyboard (or by its answer key) and only when the agent could sign in.
        Otherwise the organization's licences are only read, so the last screen can say what
        to do about what it has."""
        if not identity.agent_user_id:
            return
        need_teams = smoke != "pass" and (
            self.licence == "none" or (self.licence == "unknown" and smoke == "teams-not-ready")
        )
        need_copilot = self.plan.work_iq and self.copilot not in ("assigned", "existing")
        if not (need_teams or need_copilot):
            return
        console, env = self.console, self._tenant_env()
        offered = smoke in ("pass", "teams-not-ready", "skipped") and (
            console.interactive or licences.OFFER_KEY in console.answers
        )
        if not offered:
            report = self.licence_report = licences.read_quietly(self.helpers, env)
            if report is not None and need_teams and report.agent_has("teams"):
                self.licence = "existing"
            if report is not None and need_copilot and report.agent_has("copilot"):
                self.copilot = "existing"
            return
        outcome = licences.walk_through(
            console, self.helpers, env, teams=need_teams, copilot=need_copilot
        )
        self.licence_report = outcome.report
        self.licence_explained = set(outcome.explained)
        known: dict[str, LicenceOutcome] = {"assigned": "assigned", "had": "existing"}
        self.licence = known.get(outcome.teams, self.licence)
        self.copilot = known.get(outcome.copilot, self.copilot)
        try:  # what the walkthrough recorded, for anything that reads it after this
            self.state = state.read_state(self.paths.state_file)
        except IdentityError as exc:
            console.debug(f"Could not read the state file again: {exc}")

    def _work_iq_did_not_finish(self, failure: workiq.WorkIqError) -> tuple[str, list[str]]:
        """The last screen's reminder after a step that switches them on failed: why in plain
        words, what to do, and where the details are."""
        reason = failure.reason[:1].upper() + failure.reason[1:]
        if not reason.endswith("."):
            reason += "."
        body = [
            *_wrapped(reason),
            *_wrapped("Run setup again and answer yes to the Word, Excel and OneDrive question."),
        ]
        if self.console.log_file:
            body += ["The details are in the setup log:", f"  {self.console.log_file}"]
        return (
            "Word, Excel and OneDrive files were asked for, but they are not switched on.",
            body,
        )

    def _work_iq_attention(self) -> list[tuple[str, list[str]]]:
        """Word, Excel and OneDrive files were asked for and do not work yet: why, and what
        to do. Empty when they work, or were not asked for."""
        plan = self.plan
        if plan.work_iq_asked and not plan.work_iq:
            failure = self.work_iq_failure or workiq.WorkIqError(
                "not switched on", reason=workiq.PERMISSIONS_REASON
            )
            return [self._work_iq_did_not_finish(failure)]
        items: list[tuple[str, list[str]]] = []
        if plan.work_iq and self.work_iq_pending:
            items.append(
                (
                    "Word, Excel and OneDrive files are set up, but Microsoft has not started "
                    "honouring them for the agent yet.",
                    _wrapped(
                        "That usually takes a minute or two after setup gives the permissions. "
                        "Check with:  scripts\\setup-windows.cmd -Diagnose. If it still says so "
                        "later, run setup again and answer yes to the Word, Excel and OneDrive "
                        "question."
                    ),
                )
            )
        if plan.work_iq and self.copilot not in ("assigned", "existing"):
            if self.copilot == "unknown" and self.licence_report is None:
                headline = (
                    "Word, Excel and OneDrive files are switched on, but setup could not check "
                    f"that the agent has the {licences.whole('Microsoft 365 Copilot')} licence "
                    "they need."
                )
            else:
                headline = (
                    "Word, Excel and OneDrive files are switched on, but the agent has no "
                    f"{licences.whole('Microsoft 365 Copilot')} licence, which they need."
                )
            items.append(
                (
                    headline,
                    licences.last_screen_lines(
                        self.licence_report, "copilot", shown="copilot" in self.licence_explained
                    ),
                )
            )
        return items

    def _finish(
        self,
        identity: Identity,
        cert: CertOutcome,
        env_result: EnvResult,
        memory_result: MemoryResult,
        upload_failed: bool,
        host_report: hosts.HostReport,
        smoke: str,
    ) -> int:
        console, paths = self.console, self.paths
        # Read again: the certificate step's helper also asks for the provisioner, and that
        # retries the removal (or makes the first attempt) after the provisioning step. An
        # entry that could not be read stays a thing to check: the script drops it from the
        # record without knowing what it named.
        old_keys = self._read_old_setup_keys()
        if self.old_keys.unreadable and not old_keys.unreadable:
            old_keys = replace(old_keys, unreadable=True)
        work_iq_attention = self._work_iq_attention()
        healthy = (
            smoke in ("pass", "skipped")
            and not upload_failed
            and not self.sponsor_attention
            and not host_report.attention
            and not old_keys.pending
            and not work_iq_attention
        )
        console.banner(
            "Entrabot is set up" if healthy else "Setup finished, with something to check"
        )
        console.info(f"Agent:        {identity.agent_user_upn or identity.agent_id}")
        console.info(f"Works for:    {self._works_for(env_result)}")
        if memory_result.mode == "cloud":
            console.info(f"Memory:       Azure Blob Storage ({memory_result.endpoint})")
        else:
            console.info("Memory:       on this computer")
        if memory_result.problem:
            console.warn(f"Cloud memory was asked for but not set up: {memory_result.problem}.")
        console.info(f"Sign-in key:  {cert.protection}")
        if cert.tpm_fallback_reason:
            console.detail(f"The hardware TPM was not used: {cert.tpm_fallback_reason}")
        console.info(f"Certificate:  valid until {cert.not_after:%d %b %Y}")
        if console.log_file:
            console.info(f"Setup log:    {console.log_file}")

        if old_keys.pending:
            console.blank()
            self._say_old_setup_keys(old_keys)
        elif self.old_keys.keys:
            endings = listed([key.ending for key in self.old_keys.keys])
            console.blank()
            console.ok(
                f"The older setup key ending in {endings} is switched off now: a later step in "
                "this run removed it."
                if len(self.old_keys.keys) == 1
                else f"The older setup keys ending in {endings} are switched off now: a later "
                "step in this run removed them."
            )

        if smoke == "keyring":
            console.blank()
            console.warn(
                "The test could not use Windows Credential Manager from this kind of session "
                "(a remote or SSH session). That is a limit of the session, not of the setup."
            )
            console.detail(
                "Sign in at the computer's own desktop and run:  "
                "scripts\\setup-windows.cmd -Diagnose"
            )
        elif smoke == "sign-in":
            console.blank()
            console.warn("The agent could not sign in. This is not a licence delay.")
            console.detail(RERUN)
        elif smoke == "certificate-pending":
            console.blank()
            console.warn(
                "The agent could not sign in yet: this computer's certificate was registered on "
                "the agent's Blueprint moments ago, and Microsoft Entra has not started accepting "
                "it yet."
            )
            console.detail(
                "Run setup again in a few minutes. It continues from here and keeps what "
                "already works."
            )
        elif smoke == "teams-not-ready" and self.licence == "none":
            pass  # explained below: there is no licence to wait for
        elif smoke == "teams-not-ready" and self.licence in ("assigned", "existing"):
            console.blank()
            console.warn("The agent signed in, but Teams is not ready for it yet.")
            console.detail(
                "A newly assigned licence takes 10-15 minutes to reach Teams. Check later with:  "
                "scripts\\setup-windows.cmd -Diagnose"
            )
        elif smoke == "teams-not-ready":
            console.blank()
            console.warn("The agent signed in, but Teams did not let it in.")
            console.detail(
                "Check that the agent has a licence that includes Teams (Microsoft 365 admin "
                "center, https://admin.microsoft.com), then run:  "
                "scripts\\setup-windows.cmd -Diagnose"
            )
        elif smoke == "crashed":
            console.blank()
            console.warn(
                "The end-to-end test could not run. Check with:  "
                "scripts\\setup-windows.cmd -Diagnose"
            )
        if self.licence == "none" and smoke != "pass":
            console.blank()
            if smoke == "teams-not-ready":  # the test saw it sign in; only Teams turned it away
                console.warn(
                    "The agent is set up and can sign in, but it has no Teams licence, so it "
                    "cannot use Teams yet."
                )
            elif smoke == "skipped":  # nothing was tested, so nothing is said about signing in
                console.warn(
                    "The agent is set up, but it has no Teams licence, so it cannot use Teams yet."
                )
            else:  # it could not sign in, or the test did not get that far: said just above
                console.warn("The agent also has no Teams licence, so it cannot use Teams yet.")
            # For what this organization has, when setup could read it (licences.py); in a
            # line or two when the walkthrough above already said it.
            shown = "teams" in self.licence_explained
            for line in licences.last_screen_lines(self.licence_report, "teams", shown=shown):
                console.info(f"     {line}")
        for headline, body in work_iq_attention:
            console.blank()
            licences.say(console, headline, body)
        for reason in host_report.attention:
            console.blank()
            console.warn(reason)
            console.info("     What to do is the first step below.")
        for first, *rest in self.sponsor_attention:
            console.blank()
            console.warn(first)
            for line in rest:
                console.info(f"     {line}")
        if upload_failed:
            console.blank()
            console.warn("Existing memory was NOT copied to the cloud. Run setup again to retry.")

        console.blank()
        console.info("To start your agent:")
        number = 0
        for item in host_report.todo:
            number += 1
            console.info(f"  {number}. {item}")
        launcher = paths.project_root / START_SCRIPT
        if "claude" not in self.plan.hosts:
            console.info(
                f"  {number + 1}. Open GitHub Copilot CLI in {paths.project_root}. "
                "The agent's tools load by themselves."
            )
            number += 1
        else:
            if launcher.is_file():
                console.info(f"  {number + 1}. Double-click {START_SCRIPT} in {paths.project_root}")
                # Under the step's words: detail indents five, the words start at seven.
                console.detail(f"  (or, in a terminal there:  {self._claude_command(host_report)})")
            else:
                console.info(f"  {number + 1}. Open a terminal in {paths.project_root} and run:")
                console.info(f"       {self._claude_command(host_report)}")
            console.info(
                f"  {number + 2}. Answer Claude Code's questions. The first time, it asks you to "
                "sign in to Claude"
            )
            for line in FIRST_START_QUESTIONS:
                console.info(f"     {line}")
            number += 2
        console.info(f"  {number + 1}. Then say:  Who are you?")
        if smoke not in _CANNOT_START:
            self._things_to_try()
        console.blank()
        console.info("Later:")
        console.info("  Health check:       scripts\\setup-windows.cmd -Diagnose")
        console.info(
            "  Change or repair:   run scripts\\setup-windows.cmd again. It keeps what works."
        )
        console.detail(
            "Optional: to give the agent a persona and long-term memory (persona-sati), see "
            "docs/clients/persona-sati-host-bootstrap.md."
        )
        self._offer_to_start(host_report, smoke)
        if upload_failed:
            return EXIT_FAILED
        if (
            smoke not in ("pass", "skipped")
            or self.sponsor_attention
            or host_report.attention
            or old_keys.pending
            or work_iq_attention
        ):
            return EXIT_SMOKE_FAILED  # set up, with something to check
        return EXIT_OK


def _certificate_not_accepted_yet(checks: Sequence[Mapping[str, Any]]) -> bool:
    """Did the sign-in fail at hop 1 because Microsoft Entra does not accept the certificate
    that signed it (yet)?

    preflight's token check reports a failed hop as ``"<hop> failed - <error>: <description>"``,
    with the hop as entrabot.tools.teams names it (hop 1 is ``hop1:blueprint``) and the AADSTS
    code Microsoft Entra starts its description with. Only that code at hop 1 counts: never
    the words around it, and never the same code at a later hop.
    """
    if not checks:
        return False
    first = checks[0]
    detail = str(first.get("detail") or "")
    return (
        first.get("name") == _TOKEN_CHECK
        and first.get("status") == "fail"
        and detail.startswith("hop1:")
        and CERT_NOT_ACCEPTED_CODE in _AADSTS.findall(detail)
    )


def _checks(output: str) -> list[dict[str, Any]]:
    """The checks a preflight snippet printed, or [] when it printed none."""
    raw = marked_line(output, CHECKS_MARKER)
    if raw is None:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
