"""Show the agent's status: a thin runner for ``scripts/show_agent_status.py``.

    python -m entrabot.winsetup status --project-root PATH [--json] [--health-only] [--strict]

The status script does the work. This starts it with the virtual-environment interpreter,
from the project folder, with the Azure CLI setup used first on PATH (the script needs it to
reach Microsoft Entra, and Entrabot's private copy is not on the user's PATH). Nothing is
installed, and nothing is printed around the script's own output, so ``--json`` stays
parseable. The exit code is the script's.

One case is answered here instead: a folder with neither ``.env`` nor a state file, which
is what a teardown leaves and what a fresh checkout is. The status script would report that
the Provisioner app is not bootstrapped; the plain fact is that this folder is not connected
to an agent, so that is what is said (as a small JSON object under ``--json``), with exit 1.
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
from entrabot.winsetup.wizard import World

STATUS_SCRIPT = "show_agent_status.py"
NOT_SET_UP = f"This folder is not connected to an agent. Run {SETUP_COMMAND} to set one up."

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


def run_status(
    paths: Paths,
    world: World,
    *,
    json_output: bool = False,
    health_only: bool = False,
    strict: bool = False,
    run: Runner = _run,
) -> int:
    """Run the consolidated status script and return its exit code."""
    if not paths.env_file.is_file() and not paths.state_file.is_file():
        # Nothing is set up here. Under --json the answer is still JSON, on stdout, so a
        # machine reader gets something it can parse.
        print(
            json.dumps({"configured": False, "message": NOT_SET_UP}) if json_output else NOT_SET_UP
        )
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
    return run(argv, cwd=paths.project_root, env=_child_env(paths, world))
