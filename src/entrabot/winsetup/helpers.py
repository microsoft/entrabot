"""Paths, and the child processes setup starts: the repo's helper scripts and short snippets.

Anything that needs ``entrabot.config`` runs in a child. Importing it loads ``.env`` into the
process environment and never overwrites a variable that is already set, so the wizard, which
rewrites ``.env``, must not import it, and a child that should see the new ``.env`` must not
inherit the old values. :meth:`Helpers.runtime_env` takes every key ``.env`` defines out of
the environment so the file is the only source.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from entrabot.winsetup.console import Console
from entrabot.winsetup.envfile import read_env

CHECKS_MARKER = "ENTRABOT_CHECKS="
RESULT_MARKER = "ENTRABOT_RESULT="


def default_data_root(
    env: Mapping[str, str], *, platform: str | None = None, home: Path | None = None
) -> Path:
    """The per-user Entrabot folder: ``%LOCALAPPDATA%\\entrabot`` on Windows, ``~/.entrabot``
    elsewhere. Matches ``entrabot.config`` without importing it."""
    home = home or Path.home()
    if (platform or sys.platform) == "win32":
        local = env.get("LOCALAPPDATA")
        return (Path(local) if local else home / "AppData" / "Local") / "entrabot"
    return home / ".entrabot"


@dataclass(frozen=True)
class Paths:
    project_root: Path
    python: Path  # the virtual-environment interpreter the wizard is running under
    data_root: Path

    @property
    def scripts(self) -> Path:
        return self.project_root / "scripts"

    @property
    def env_file(self) -> Path:
        return self.project_root / ".env"

    @property
    def state_file(self) -> Path:
        return self.project_root / ".entrabot-state.json"

    @property
    def claude_mcp(self) -> Path:
        return self.project_root / ".mcp.json"

    @property
    def mcp_binary(self) -> Path:
        name = "entrabot-mcp.exe" if self.python.suffix.lower() == ".exe" else "entrabot-mcp"
        return self.python.parent / name

    @property
    def logs(self) -> Path:
        return self.data_root / "logs"


class Helpers:
    """Starts the repo's helper scripts and Python snippets with the right environment."""

    def __init__(self, console: Console, paths: Paths, base_env: Mapping[str, str]) -> None:
        self.console = console
        self.paths = paths
        env = dict(base_env)
        # A child printing a check mark to a pipe on a cp1252 machine would otherwise raise.
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        self.base_env = env

    def provisioning_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return {**self.base_env, **(extra or {})}

    def runtime_env(self) -> dict[str, str]:
        """The environment for a child that should behave like the agent: ``.env`` decides."""
        owned = set(read_env(self.paths.env_file))
        return {key: value for key, value in self.base_env.items() if key not in owned}

    def script(
        self,
        name: str,
        args: Sequence[str] = (),
        *,
        env: Mapping[str, str] | None = None,
        capture: bool = False,
        quiet: bool = False,
        heartbeat: str | None = None,
        summary: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``scripts/<name>`` with the virtual-environment interpreter.

        *summary*: the helper's output goes to the log, not the screen (see ``Console.run``).
        """
        return self.console.run(
            [str(self.paths.python), str(self.paths.scripts / name), *args],
            env=env if env is not None else self.base_env,
            cwd=self.paths.project_root,
            capture=capture,
            quiet=quiet,
            heartbeat=heartbeat,
            summary=summary,
        )

    def snippet(
        self,
        code: str,
        args: Sequence[str] = (),
        *,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        quiet: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run a short program with the virtual-environment interpreter.

        Values go in as arguments or on stdin, never into the program text.
        """
        return self.console.run(
            [str(self.paths.python), "-c", code, *args],
            env=env if env is not None else self.runtime_env(),
            cwd=self.paths.project_root,
            capture=True,
            quiet=quiet,
            input_text=input_text,
        )


def marked_line(output: str, marker: str) -> str | None:
    """The text after *marker* on the last line that starts with it, or None."""
    found = None
    for line in output.splitlines():
        if line.startswith(marker):
            found = line[len(marker) :]
    return found


# ── Snippets (run in a child so entrabot.config never loads into the wizard) ─────────────────

SMOKE_SNIPPET = f"""
import dataclasses, json
from entrabot.config import EntraBotConfig
from entrabot.preflight import run_smoke_checks
checks = run_smoke_checks(EntraBotConfig.from_env())
print({CHECKS_MARKER!r} + json.dumps([dataclasses.asdict(check) for check in checks]))
"""

DIAGNOSE_SNIPPET = """
import sys
from pathlib import Path
from entrabot.config import EntraBotConfig
from entrabot.preflight import format_report, overall_exit_code, run_diagnostics
root, binary, copilot, color = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
checks = run_diagnostics(
    EntraBotConfig.from_env(),
    state_path=root / ".entrabot-state.json",
    expected_binary=binary,
    claude_mcp_path=root / ".mcp.json",
    copilot_mcp_path=copilot,
    work_iq_manifest=root / "ToolingManifest.json",
)
print(format_report(checks, color=color == "1"))
sys.exit(overall_exit_code(checks))
"""

# Setup's last step for Word, Excel and OneDrive files: does Microsoft honour them for the
# agent yet? Per audience, from the manifest given as the first argument. No token is printed.
WORK_IQ_SNIPPET = f"""
import json, sys
from pathlib import Path
from entrabot.config import EntraBotConfig
from entrabot.preflight import work_iq_token_scopes
results = work_iq_token_scopes(EntraBotConfig.from_env(), Path(sys.argv[1]))
print({RESULT_MARKER!r} + json.dumps(results))
"""

LICENSE_SNIPPET = f"""
import dataclasses, json, sys
from entrabot.preflight import check_copilot_license_availability, check_teams_license_availability
token = sys.stdin.read().strip()
checks = [check_teams_license_availability(token)]
if sys.argv[1] == "1":
    checks.append(check_copilot_license_availability(token))
print({CHECKS_MARKER!r} + json.dumps([dataclasses.asdict(check) for check in checks]))
"""

LEGACY_DATA_SNIPPET = f"""
from entrabot.config import migrate_legacy_data_dir
print({RESULT_MARKER!r} + ("moved" if migrate_legacy_data_dir() else "nothing"))
"""

MEMORY_PROBE_SNIPPET = f"""
import json, sys
from pathlib import Path
from entrabot.config import EntraBotConfig
from entrabot.storage.persona import claude_code_memory_dir

def size(path):
    if not path.is_dir():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())

data = EntraBotConfig.from_env().data_dir
persona = claude_code_memory_dir(Path(sys.argv[1]))
print({RESULT_MARKER!r} + json.dumps({{
    "data_dir": str(data), "data_bytes": size(data),
    "persona_dir": str(persona), "persona_bytes": size(persona),
}}))
"""

MEMORY_UPLOAD_SNIPPET = f"""
import json, sys
from pathlib import Path
from entrabot.storage.backend import get_backend
from entrabot.storage.migration import migrate_local_to_backend
sources = []
if sys.argv[1]:
    sources.append((Path(sys.argv[1]), ""))
if sys.argv[2]:
    sources.append((Path(sys.argv[2]), "claude_memory"))
report = migrate_local_to_backend(sources, get_backend())
print({RESULT_MARKER!r} + json.dumps({{
    "copied": report.copied, "skipped": report.skipped, "bytes": report.bytes_copied,
    "errors": [[key, str(error)] for key, error in report.errors[:5]],
    "error_count": len(report.errors),
}}))
sys.exit(2 if report.errors else 0)
"""

# Reads the sponsors on the Agent Identity and adds the object IDs given as arguments that are
# not there yet. Additive only. The Graph calls are add_agent_sponsor.py's own functions, with
# the provisioner's token: Agent Identity APIs refuse the signed-in user's CLI token.
# argv: scripts folder, agent object ID, then the user object IDs to add.
SPONSOR_SNIPPET = f"""
import contextlib, json, sys, time
sys.path.insert(0, sys.argv[1])
agent, wanted = sys.argv[2], sys.argv[3:]
result = {{"before": None, "added": [], "failed": {{}}, "after": None, "error": ""}}

def ids(sponsors):
    return {{str(item.get("id", "")).lower() for item in sponsors}}

with contextlib.redirect_stdout(sys.stderr):
    try:
        from add_agent_sponsor import _add_sponsor, _list_sponsors
        from entra_provisioning import get_existing_graph_token
        token = get_existing_graph_token()
        result["before"] = _list_sponsors(token, agent)
        present = ids(result["before"])
        for user_id in wanted:
            if user_id.lower() in present:
                continue
            try:
                _add_sponsor(token, agent, user_id)
                result["added"].append(user_id)
            except (SystemExit, Exception) as exc:
                result["failed"][user_id] = str(exc)[:400]
        result["after"] = _list_sponsors(token, agent)
        for _attempt in range(5):  # the directory can take a moment to show a new sponsor
            if all(item.lower() in ids(result["after"]) for item in result["added"]):
                break
            time.sleep(2)
            result["after"] = _list_sponsors(token, agent)
    except (SystemExit, Exception) as exc:
        result["error"] = str(exc)[:400] or type(exc).__name__
print({RESULT_MARKER!r} + json.dumps(result))
"""
