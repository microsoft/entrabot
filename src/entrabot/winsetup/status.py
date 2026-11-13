"""Show the agent's status: a thin runner for ``scripts/show_agent_status.py``.

    python -m entrabot.winsetup status --project-root PATH [--json] [--health-only] [--strict]
        [--full-thumbprints]

The status script does the work. This starts it with the virtual-environment interpreter,
from the project folder, with the Azure CLI setup used first on PATH (the script needs it to
reach Microsoft Entra, and Entrabot's private copy is not on the user's PATH). Nothing is
installed, and nothing is printed around the script's own output, so ``--json`` stays
parseable. The exit code is the script's.

Two cases are answered here instead, both with exit 1 and, under ``--json``, a small JSON
object saying the same:

* a folder with neither ``.env`` nor a state file, which is what a teardown leaves and what a
  fresh checkout is. The status script would report that the Provisioner app is not
  bootstrapped; the plain fact is that this folder is not connected to an agent.
* a folder with no ``.env`` whose state file holds only setup's note about an older setup key
  it could not switch off in Microsoft Entra (and which tenant's Provisioner app that is
  about). Teardown with ``--keep-setup-credential`` leaves exactly that, so that setup's next
  run retries the key. The status script would report a missing agent; what is said is that
  this folder is not connected to an agent, and which key is still waiting.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from entrabot.winsetup import azcli
from entrabot.winsetup.directory import SETUP_COMMAND
from entrabot.winsetup.helpers import Paths
from entrabot.winsetup.oldkeys import (
    REVOKE_PENDING,
    REVOKE_PENDING_EXPIRES,
    OldSetupKeys,
    old_setup_key_waiting,
    old_setup_keys,
)
from entrabot.winsetup.state import IdentityError, read_state
from entrabot.winsetup.wizard import World

STATUS_SCRIPT = "show_agent_status.py"
NOT_SET_UP = f"This folder is not connected to an agent. Run {SETUP_COMMAND} to set one up."
# All a state file holds when teardown left it only for setup's retry of an older setup key
# (teardown.py, _settle_old_setup_certs). Anything else recorded means more than that note.
_NOTE_ONLY = frozenset(
    {"TENANT_ID", "PROVISIONER_CLIENT_ID", REVOKE_PENDING, REVOKE_PENDING_EXPIRES}
)

Runner = Callable[..., int]


def _run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> int:
    """Start the script with this process's own terminal, and wait for it."""
    return subprocess.call(argv, cwd=cwd, env=env)  # noqa: S603 - fixed program, no shell


def _child_env(paths: Paths, world: World) -> dict[str, str]:
    env = dict(world.env)
    location = azcli.find_az(
        env=world.env, data_dir=paths.data_root, platform=world.platform, which=world.which
    )
    if location is not None:
        key = next((name for name in env if name.upper() == "PATH"), "PATH")
        current = env.get(key, "")
        env[key] = f"{location.bin_dir}{os.pathsep}{current}" if current else str(location.bin_dir)
    # The script prints check marks; on a cp1252 console that would otherwise raise.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _only_setups_note(paths: Paths) -> OldSetupKeys | None:
    """The older setup keys a folder still has a note about, when that note is all there is:
    no ``.env``, and a state file that records nothing else (a key left empty records
    nothing). None for any other folder, and for a state file that can't be read: the status
    script reports those."""
    if paths.env_file.is_file() or not paths.state_file.is_file():
        return None
    try:
        recorded = read_state(paths.state_file)
    except IdentityError:
        return None
    if not {key for key, value in recorded.items() if value.strip()} <= _NOTE_ONLY:
        return None
    note = old_setup_keys(recorded)
    return note if note.pending else None


def _report_setups_note(note: OldSetupKeys, *, json_output: bool) -> None:
    waiting = old_setup_key_waiting(note)
    if not json_output:
        print(NOT_SET_UP)
        print(waiting)
        return
    keys = [{"thumbprint_ends_in": key.ending, "expires": key.expires or None} for key in note.keys]
    print(
        json.dumps(
            {
                "configured": False,
                "message": NOT_SET_UP,
                "old_setup_keys": {
                    "waiting": keys,
                    "record_unreadable": note.unreadable,
                    "message": waiting,
                },
            }
        )
    )


def run_status(
    paths: Paths,
    world: World,
    *,
    json_output: bool = False,
    health_only: bool = False,
    strict: bool = False,
    full_thumbprints: bool = False,
    run: Runner = _run,
) -> int:
    """Run the consolidated status script and return its exit code.

    The script shows a certificate thumbprint by its last four characters; *full_thumbprints*
    (``--full-thumbprints``) has it show them in full.
    """
    if not paths.env_file.is_file() and not paths.state_file.is_file():
        # Nothing is set up here. Under --json the answer is still JSON, on stdout, so a
        # machine reader gets something it can parse.
        print(
            json.dumps({"configured": False, "message": NOT_SET_UP}) if json_output else NOT_SET_UP
        )
        return 1
    note = _only_setups_note(paths)
    if note is not None:
        _report_setups_note(note, json_output=json_output)
        return 1
    script = paths.scripts / STATUS_SCRIPT
    if not script.is_file():
        print(
            f"ERROR: {script} is missing. Update the project folder, then run {SETUP_COMMAND}.",
            file=sys.stderr,
        )
        return 1
    argv = [str(paths.python), str(script)]
    if json_output:
        argv.append("--json")
    if health_only:
        argv.append("--health-only")
    if strict:
        argv.append("--strict")
    if full_thumbprints:
        argv.append("--full-thumbprints")
    return run(argv, cwd=paths.project_root, env=_child_env(paths, world))
