"""Optional: Word, Excel and OneDrive files (Microsoft Agent 365 Work IQ).

Setup switches them on by itself, with no Agent 365 CLI, no .NET, no PowerShell 7 and no
Windows sign-in window, so it works over SSH and unattended. It does what the CLI's commands
did, in three steps:

1. Write the three Work IQ servers into the project's ``ToolingManifest.json``, as
   ``a365 develop add-mcp-servers`` did, from the package's catalog
   (:mod:`entrabot.a365.catalog`). Entries setup does not own are kept.
2. Run ``scripts/ensure_a365_work_iq_permissions.py`` with the setup (Provisioner) credential:
   the OAuth2 grants and the Blueprint's inheritable permissions, which is what
   ``a365 setup permissions mcp`` did. (``a365 setup requirements`` only checked the CLI's own
   prerequisites; nothing replaces it.)
3. Check, as the agent and the way the runtime asks, that its token for each audience in the
   manifest carries the scopes the manifest needs. Microsoft can take a little while to honour
   new permissions (20 to 45 seconds on a real tenant), so the check is tried for about two
   minutes before it is left as "set up, not honoured yet".

A failure in step 1 or 2 puts the manifest back the way it was, so nothing (setup's next run,
the agent) takes Work IQ to be on. No token is ever printed or logged.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from entrabot.a365.catalog import WORD_SERVER_NAME, manifest_entry, setup_servers
from entrabot.winsetup.console import Console
from entrabot.winsetup.helpers import RESULT_MARKER, WORK_IQ_SNIPPET, Helpers, Paths, marked_line

MANIFEST = "ToolingManifest.json"
PERMISSIONS_SCRIPT = "ensure_a365_work_iq_permissions.py"
# Between checks that Microsoft honours the new permissions: about two minutes in all.
CHECK_WAITS: tuple[float, ...] = (10, 10, 15, 15, 20, 20, 20, 20)
PERMISSIONS_REASON = "setup could not give the agent's Blueprint the permissions they need"
MANIFEST_REASON = f"setup could not write this folder's {MANIFEST}"

Outcome = Literal["on", "pending"]


class WorkIqError(Exception):
    """Switching Word, Excel and OneDrive files on failed. ``str(error)`` says which step, for
    the screen at that point and the log; :attr:`reason` says why in plain words, for the
    last screen."""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason or message


def write_manifest(path: Path) -> None:
    """Make *path* name the Work IQ servers setup switches on, exactly as Microsoft's tool
    writes them. Entries setup does not own are kept; its own are corrected in place, and a
    second copy of one is dropped (the runtime refuses a manifest that names a server twice).
    A manifest that cannot be read is left alone and raises :class:`WorkIqError`."""
    body: dict[str, Any] = {"mcpServers": []}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise WorkIqError(
                f"{path.name} could not be read: {exc}", reason=MANIFEST_REASON
            ) from exc
        if not isinstance(loaded, dict) or not isinstance(loaded.get("mcpServers", []), list):
            raise WorkIqError(
                f"{path.name} is not a Work IQ manifest (no mcpServers list)",
                reason=MANIFEST_REASON,
            )
        body = loaded
    ours = {entry["mcpServerName"]: entry for entry in map(manifest_entry, setup_servers())}
    servers: list[Any] = []
    written: set[str] = set()
    for item in body.get("mcpServers", []):
        name = item.get("mcpServerName") if isinstance(item, dict) else None
        if name in ours:
            if name not in written:
                servers.append(ours[name])
                written.add(name)
            continue
        servers.append(item)
    servers.extend(entry for name, entry in ours.items() if name not in written)
    body["mcpServers"] = servers
    try:
        path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise WorkIqError(
            f"{path.name} could not be written: {exc}", reason=MANIFEST_REASON
        ) from exc


def word_tools_configured(project_root: Path) -> bool:
    """Has an earlier run given this agent its Word tools? Read from the tooling manifest.
    Anything unreadable counts as no: the answer only decides which suggestions setup's last
    screen makes."""
    try:
        return WORD_SERVER_NAME in (project_root / MANIFEST).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return False


def _saved(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _put_back(path: Path, content: bytes | None, console: Console) -> None:
    try:
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(content)
    except OSError as exc:
        console.warn(f"Could not put {path.name} back the way it was: {exc}")


def _honoured(helpers: Helpers, manifest: Path) -> bool:
    """One check, as the agent: do its tokens carry every scope the manifest needs? A check
    that could not run counts as not yet."""
    result = helpers.snippet(WORK_IQ_SNIPPET, [str(manifest)])
    raw = marked_line(result.stdout, RESULT_MARKER)
    try:
        rows = json.loads(raw) if raw else None
    except ValueError:
        rows = None
    if not isinstance(rows, list) or not rows:
        helpers.console.debug(f"The Work IQ check could not run: {result.stderr.strip()[-300:]}")
        return False
    for row in rows:
        if isinstance(row, dict) and not row.get("ok"):
            helpers.console.debug(
                f"Not yet for {row.get('audience')}: "
                + (str(row.get("error")) or "missing " + " ".join(row.get("missing") or []))
            )
    return all(isinstance(row, dict) and row.get("ok") for row in rows)


def configure_work_iq(
    *,
    paths: Paths,
    helpers: Helpers,
    console: Console,
    blueprint_app_id: str,
    sleep: Callable[[float], None],
    waits: tuple[float, ...] = CHECK_WAITS,
) -> Outcome:
    """Switch Word, Excel and OneDrive files on for the agent of *blueprint_app_id*. Returns
    "on" when Microsoft honours them for the agent, "pending" when everything is set up but
    the agent's tokens do not carry the scopes yet. Raises :class:`WorkIqError` when a step
    failed, after putting the manifest back the way it was."""
    if not blueprint_app_id:
        raise WorkIqError("The agent must be created before Work IQ can be configured.")
    manifest = paths.project_root / MANIFEST
    saved = _saved(manifest)
    try:
        write_manifest(manifest)
        console.ok(f"Listed the Word, Excel and OneDrive tools for the agent ({MANIFEST})")
        granted = helpers.script(
            PERMISSIONS_SCRIPT,
            ["--blueprint-app-id", blueprint_app_id],
            capture=True,
            quiet=True,
            heartbeat="Still giving the agent's Blueprint its permissions",
        )
        if granted.returncode != 0:
            console.show_tail(granted, lines=6)
            raise WorkIqError(
                "Giving the agent's Blueprint the permissions failed.", reason=PERMISSIONS_REASON
            )
        console.ok("Gave the agent's Blueprint the permissions they need")
    except BaseException:  # a failure, or Ctrl+C: either way it did not finish
        _put_back(manifest, saved, console)
        raise
    console.info("Checking that Microsoft lets the agent use them (it can take a minute)...")
    if _honoured(helpers, manifest):
        console.ok("Word, Excel and OneDrive files are switched on")
        return "on"
    for wait in waits:
        console.detail(
            f"Still waiting for Microsoft to honour them. Checking again in {wait:.0f}s."
        )
        sleep(wait)
        if _honoured(helpers, manifest):
            console.ok("Word, Excel and OneDrive files are switched on")
            return "on"
    console.warn("Word, Excel and OneDrive files are set up, but Microsoft does not honour them")
    console.detail("for the agent yet. What to do is at the end.")
    return "pending"
