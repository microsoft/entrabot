"""Command line for the setup wizard, on Windows, macOS and Linux.

    python -m entrabot.winsetup setup --project-root PATH [flags]
    python -m entrabot.winsetup diagnose --project-root PATH
    python -m entrabot.winsetup rotate-cert --project-root PATH [--yes] [--verbose]
    python -m entrabot.winsetup teardown --project-root PATH
        [--delete-agent] [--purge-data] [--keep-setup-credential] [--yes]
    python -m entrabot.winsetup status --project-root PATH [--json] [--health-only] [--strict]
        [--full-thumbprints]

``setup`` is the default subcommand. On Windows ``scripts/setup-windows.ps1`` passes its
parameters through one to one (``-NewChain`` -> ``--new-chain`` and so on), and
``deploy-windows.ps1``, ``teardown-windows.ps1`` and ``status-windows.ps1`` do the same for
the other three. On macOS and Linux ``scripts/setup.sh`` and ``scripts/teardown.sh`` do it,
translating the flags they have always taken (``--new`` -> ``--new-chain``,
``--with-upn-suffix=NAME`` -> ``--upn-suffix NAME``, ``--teams-user=EMAIL`` -> ``--sponsor``).
The package is still called ``winsetup``: it was written for Windows first.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from entrabot.winsetup import azcli, rotation, status, teardown
from entrabot.winsetup.console import Console
from entrabot.winsetup.helpers import Paths, default_data_root
from entrabot.winsetup.wizard import (
    EXIT_CANCELLED,
    EXIT_USAGE,
    Options,
    Wizard,
    World,
    run_diagnose,
)
from entrabot.winsetup.words import words_for

SUBCOMMANDS = ("setup", "diagnose", "rotate-cert", "teardown", "status")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m entrabot.winsetup",
        description="Set up Entrabot: an interactive wizard, or scripted with --yes.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    setup = commands.add_parser(
        "setup",
        help="Set up or repair Entrabot on this computer (the default).",
        description="Set up or repair Entrabot on this computer. Without flags it asks; every "
        "question has a default. Exit codes: 0 done, 1 failed, 2 usage or a missing answer "
        "under --yes, 3 set up but with something to check (said on the last screen), "
        "130 cancelled.",
    )
    setup.add_argument("--project-root", required=True, type=Path, help="The Entrabot folder.")

    identity = setup.add_argument_group("which agent")
    mode = identity.add_mutually_exclusive_group()
    mode.add_argument("--new-chain", action="store_true", help="Create a brand-new agent.")
    mode.add_argument(
        "--use-blueprint",
        default="",
        metavar="APP_ID",
        help="Join an existing agent by its Blueprint App ID.",
    )
    identity.add_argument(
        "--upn-suffix",
        default="",
        metavar="NAME",
        help="Short name: the agent becomes entrabot-agent-NAME@<domain>.",
    )
    identity.add_argument(
        "--agent-user-upn", default="", metavar="UPN", help="Use exactly this Agent User address."
    )
    identity.add_argument(
        "--sponsor",
        action="append",
        default=[],
        metavar="EMAIL",
        help="Who the agent works for (repeatable). Default: you.",
    )

    store = setup.add_argument_group("memory")
    store.add_argument(
        "--memory",
        choices=("local", "cloud"),
        default=None,
        help="Where the agent keeps its memory. Default: keep the current "
        "setting, or local on a new machine.",
    )
    store.add_argument("--storage-account", default="", metavar="NAME")
    store.add_argument("--container", default="", metavar="NAME")
    store.add_argument("--create-new-storage", action="store_true")
    store.add_argument("--location", default="", metavar="REGION")
    store.add_argument("--subscription", default="", metavar="ID_OR_NAME")

    sign_in = setup.add_argument_group("sign-in")
    sign_in.add_argument("--tenant", default="", help="Tenant to sign in to (domain or ID).")
    sign_in.add_argument(
        "--device-code",
        action="store_true",
        help="Sign in with a code instead of a browser window.",
    )
    sign_in.add_argument(
        "--switch-user",
        action="store_true",
        help="Ask for a new Microsoft sign-in even when an earlier one still works.",
    )

    extras = setup.add_argument_group("other")
    extras.add_argument(
        "--configure-a365-work-iq",
        action="store_true",
        help="Also switch on Word, Excel and OneDrive files (Microsoft Agent 365 Work IQ). "
        "Nothing else to install; the agent needs a Microsoft 365 Copilot licence.",
    )
    extras.add_argument(
        "--remove-all-existing-certs",
        action="store_true",
        help="Replace EVERY certificate on the agent's Blueprint, other computers' included, "
        "with a new one for this computer. Asks first at a terminal; unattended the flag is "
        "the consent. Not with --new-chain.",
    )
    extras.add_argument(
        "--hosts",
        default=None,
        metavar="LIST",
        help="Assistants to register with: claude,copilot (default: both).",
    )
    extras.add_argument("--skip-smoke", action="store_true", help="Skip the end-to-end test.")
    extras.add_argument(
        "--yes", action="store_true", help="Never prompt. Missing answers are an error (exit 2)."
    )
    extras.add_argument(
        "--verbose",
        action="store_true",
        help="Show everything the provisioning helpers print. By default it goes to the log "
        "only, and each step is summarised in a line or two.",
    )
    extras.add_argument("--log-file", default=None, type=Path, metavar="PATH")

    diagnose = commands.add_parser(
        "diagnose",
        help="Check an existing setup. Changes nothing.",
        description="Check an existing setup. Changes nothing. Exit 0 when every check passes.",
    )
    diagnose.add_argument("--project-root", required=True, type=Path)
    diagnose.add_argument("--log-file", default=None, type=Path, metavar="PATH")

    rotate = commands.add_parser(
        "rotate-cert",
        help="Replace the certificate this computer signs the agent in with.",
        description="Replace the certificate this computer signs the agent in with. The "
        "current one stays in use until the new one is proven to work. Exit codes: 0 rotated, "
        "1 failed with the current certificate still in use, 3 rotated but cleanup is pending "
        "(run it again), 130 cancelled.",
    )
    rotate.add_argument("--project-root", required=True, type=Path)
    rotate.add_argument("--yes", action="store_true", help="Don't ask before rotating.")
    rotate.add_argument("--verbose", action="store_true", help="Show everything the helpers print.")
    rotate.add_argument("--log-file", default=None, type=Path, metavar="PATH")

    down = commands.add_parser(
        "teardown",
        help="Disconnect this computer from the agent (and optionally delete the agent).",
        description="Disconnect this computer from the agent: its certificate comes off the "
        "agent's Blueprint and off this computer, the entrabot entry leaves the assistants' "
        "configs, .env and .entrabot-state.json are backed up and removed, and this computer's "
        "setup certificate is retired once no other agent here needs it. The agent "
        "itself is kept unless --delete-agent is given. Exit codes: 0 done or nothing to do, "
        "1 finished with something left to do, 2 usage, 130 cancelled.",
    )
    down.add_argument("--project-root", required=True, type=Path)
    down.add_argument(
        "--delete-agent",
        action="store_true",
        help="Also delete the agent from the tenant, for every computer. Cannot be undone.",
    )
    down.add_argument(
        "--purge-data",
        action="store_true",
        help="Also delete Entrabot's data folder on this computer (memory, logs, private "
        "Python and Azure CLI).",
    )
    down.add_argument(
        "--keep-setup-credential",
        action="store_true",
        help="Keep this computer's setup (Provisioner) certificate, for setting up again "
        "soon. By default it is retired once no other Entrabot agent here needs it: it can "
        "manage applications in the tenant.",
    )
    down.add_argument(
        "--yes",
        action="store_true",
        help="Don't ask. Only the scopes named by the other flags are removed.",
    )
    down.add_argument("--verbose", action="store_true", help="Show everything the helpers print.")
    down.add_argument("--log-file", default=None, type=Path, metavar="PATH")

    state = commands.add_parser(
        "status",
        help="Show the agent's status. Changes nothing.",
        description="Show the agent's status (scripts/show_agent_status.py). Changes nothing "
        "and installs nothing. The exit code is the status script's.",
    )
    state.add_argument("--project-root", required=True, type=Path)
    state.add_argument("--json", action="store_true", help="Machine-readable output.")
    state.add_argument("--health-only", action="store_true", help="Only the health checks.")
    state.add_argument(
        "--strict", action="store_true", help="Exit non-zero when a health check fails."
    )
    state.add_argument(
        "--full-thumbprints",
        action="store_true",
        help="Show certificate thumbprints in full. By default only their last four "
        "characters are shown.",
    )
    return parser


def options_from_args(args: argparse.Namespace) -> Options:
    return Options(
        project_root=args.project_root.resolve(),
        new_chain=args.new_chain,
        use_blueprint=args.use_blueprint.strip(),
        upn_suffix=args.upn_suffix.strip(),
        agent_user_upn=args.agent_user_upn.strip(),
        sponsors=[
            part.strip() for item in args.sponsor for part in item.split(",") if part.strip()
        ],
        memory=args.memory,
        storage_account=args.storage_account.strip(),
        container=args.container.strip(),
        create_new_storage=args.create_new_storage,
        location=args.location.strip(),
        subscription=args.subscription.strip(),
        configure_work_iq=args.configure_a365_work_iq,
        tenant=args.tenant.strip(),
        device_code=args.device_code,
        switch_user=args.switch_user,
        remove_all_existing_certs=args.remove_all_existing_certs,
        hosts=args.hosts,
        skip_smoke=args.skip_smoke,
        yes=args.yes,
        verbose=args.verbose,
        log_file=args.log_file,
    )


def code_is_from_another_copy(project_root: Path, *, code_root: Path | None = None) -> Path | None:
    """The folder this code is running from, when *project_root* is a copy of the project
    of its own and this code is not that copy's; otherwise None.

    The runtime reads ``.env`` from the folder its code is in (``entrabot.config``), never
    from ``--project-root``. Code from one copy pointed at another would write that copy's
    ``.env`` and then test the agent with its own, or with none: seen for real, as "Agent
    User credentials not configured" from a health check of a folder that was set up. A
    ``.venv`` whose editable install was re-pointed at a git worktree does the same without
    anyone asking for it. A folder with no code of its own (a test's scratch folder) is not
    such a copy, and is left to the checks that follow.
    """
    code_root = (code_root or Path(__file__).resolve().parents[3]).resolve()
    own_code = project_root / "src" / "entrabot" / "__init__.py"
    if not own_code.is_file() or project_root.resolve() == code_root:
        return None
    return code_root


def _with_default_command(argv: Sequence[str]) -> list[str]:
    args = list(argv)
    if not args or (args[0] not in SUBCOMMANDS and args[0] not in ("-h", "--help")):
        return ["setup", *args]
    return args


def main(argv: Sequence[str] | None = None, *, world: World | None = None) -> int:
    args = build_parser().parse_args(_with_default_command(sys.argv[1:] if argv is None else argv))
    world = world or World()
    # Every command signs in the same way: through the browser (see azcli.sign_in_env).
    world = replace(world, env=azcli.sign_in_env(world.env, platform=world.platform))
    # A console that can't draw a character gets a replacement, never a traceback.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]

    project_root = args.project_root.resolve()
    elsewhere = code_is_from_another_copy(project_root)
    if elsewhere is not None:
        setup = words_for(world.platform).setup
        print(
            f"ERROR: this is Entrabot's code from {elsewhere}, asked to work on {project_root}, "
            "which has its own copy of the code.\n"
            "The agent's settings (.env) are read from the folder the code is in, so the two "
            "have to be the same folder: nothing was changed.\n"
            f"Run it from that folder instead:  cd {project_root}  and then  {setup}\n"
            "If its .venv was built from another copy of the project (for example a git "
            "worktree), rebuild it there:  .venv/bin/python -m pip install -e .",
            file=sys.stderr,
        )
        return EXIT_USAGE
    paths = Paths(
        project_root=project_root,
        python=Path(sys.executable),
        data_root=default_data_root(world.env, platform=world.platform),
    )
    if args.command == "status":
        # No console and no log: the status script's output, --json included, stands alone.
        try:
            return status.run_status(
                paths,
                world,
                json_output=args.json,
                health_only=args.health_only,
                strict=args.strict,
                full_thumbprints=args.full_thumbprints,
            )
        except KeyboardInterrupt:
            return EXIT_CANCELLED
    log_file = args.log_file or paths.logs / f"{args.command}-{datetime.now(UTC):%Y%m%d-%H%M%S}.log"
    console = Console(
        assume_yes=getattr(args, "yes", False),
        log_file=log_file,
        env=world.env,
        verbose=getattr(args, "verbose", False),
        platform=world.platform,
    )
    if args.command == "diagnose":
        try:
            return run_diagnose(console, paths, world.env)
        except KeyboardInterrupt:
            return EXIT_CANCELLED
        finally:
            console.close()
    if args.command in ("rotate-cert", "teardown"):
        try:
            if args.command == "rotate-cert":
                return rotation.run_rotate_cert(console, paths, world)
            return teardown.run_teardown(
                console,
                paths,
                world,
                delete_agent=args.delete_agent,
                purge_data=args.purge_data,
                keep_setup_credential=args.keep_setup_credential,
            )
        except KeyboardInterrupt:
            return EXIT_CANCELLED
        finally:
            console.close()
    return Wizard(options_from_args(args), console, paths, world).run()
