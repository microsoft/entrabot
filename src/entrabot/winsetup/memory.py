"""Where the agent's memory lives: this computer, or Azure Blob Storage.

At runtime ``ENTRABOT_KEEP_MEMORY_LOCAL`` (``true``, ``1`` or ``yes``, any case) outranks the
blob settings. So "on cloud memory" means both blob settings are present AND that flag is off.
Lines are replaced, never appended: the runtime keeps the first value of a repeated key.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from entrabot.winsetup.console import Console
from entrabot.winsetup.envfile import EnvGuard, read_env, update_env
from entrabot.winsetup.envstep import BLOB_KEYS, LOCAL_MEMORY_KEY
from entrabot.winsetup.helpers import (
    MEMORY_PROBE_SNIPPET,
    MEMORY_UPLOAD_SNIPPET,
    RESULT_MARKER,
    Helpers,
    marked_line,
)

_LEGACY_COMMENT = "# ADR-005:"
_ENDPOINT = re.compile(r"^BLOB_ENDPOINT=(.+?)\s*$", re.M)
_CONTAINER = re.compile(r"^BLOB_CONTAINER=(.+?)\s*$", re.M)

MemoryChoice = Literal["keep", "local", "cloud"]


class MemoryStopped(Exception):
    """Cloud memory could not be set up and the person chose not to continue on local."""


@dataclass(frozen=True)
class MemoryRequest:
    choice: MemoryChoice
    tenant_id: str
    agent_user_id: str
    storage_account: str = ""
    container: str = ""
    create_new_storage: bool = False
    location: str = ""
    subscription_id: str = ""


@dataclass(frozen=True)
class MemoryResult:
    mode: Literal["local", "cloud"]
    endpoint: str = ""
    container: str = ""
    provisioned: bool = False
    problem: str = ""  # why cloud memory was asked for and not set up


def local_flag_on(value: str | None) -> bool:
    return (value or "").strip().lower() in ("true", "1", "yes")


def current_memory_mode(env_values: Mapping[str, str]) -> Literal["cloud", "local", "unset"]:
    """What ``.env`` means for the runtime today."""
    if local_flag_on(env_values.get(LOCAL_MEMORY_KEY)):
        return "local"
    if all(env_values.get(key) for key in BLOB_KEYS):
        return "cloud"
    return "unset"


def _use_local(env_path: Path, guard: EnvGuard | None) -> None:
    update_env(
        env_path,
        {LOCAL_MEMORY_KEY: "true"},
        drop_comment_prefixes=[_LEGACY_COMMENT],
        guard=guard,
    )


def apply_memory(
    env_path: Path,
    request: MemoryRequest,
    *,
    same_agent: bool,
    helpers: Helpers,
    console: Console,
    guard: EnvGuard | None = None,
) -> MemoryResult:
    """Write the memory settings to ``.env``, provisioning blob storage when asked to.

    ``keep``   stay on whatever this agent uses now; a new machine gets local.
    ``local``  switch to local (the way back from cloud).
    ``cloud``  provision Azure Blob Storage for this Agent User and switch to it.
    """
    existing = read_env(env_path)
    on_cloud = same_agent and current_memory_mode(existing) == "cloud"
    kept_cloud = MemoryResult(
        mode="cloud",
        endpoint=existing.get(BLOB_KEYS[0], ""),
        container=existing.get(BLOB_KEYS[1], ""),
    )

    if request.choice == "local" or (request.choice == "keep" and not on_cloud):
        _use_local(env_path, guard)
        return MemoryResult(mode="local")
    if request.choice == "keep":
        return kept_cloud

    def could_not(problem: str) -> MemoryResult:
        """Cloud memory was asked for and isn't available. Never silently."""
        if on_cloud:
            console.warn(f"{problem}. Keeping the cloud memory this agent already uses.")
            return MemoryResult(
                mode="cloud",
                endpoint=kept_cloud.endpoint,
                container=kept_cloud.container,
                problem=problem,
            )
        console.warn(f"{problem}. Cloud memory was NOT set up.")
        console.detail(
            "The agent can keep its memory on this computer instead. You can switch to cloud "
            "memory later by running setup again."
        )
        if not console.confirm(
            "Continue with memory on this computer for now?", default=True, key="memory_fallback"
        ):
            raise MemoryStopped(problem)
        _use_local(env_path, guard)
        return MemoryResult(mode="local", problem=problem)

    if not request.agent_user_id:
        return could_not("There is no Agent User to give access to the storage")

    args = ["--tenant-id", request.tenant_id, "--agent-user-object-id", request.agent_user_id]
    if request.location:
        args += ["--location", request.location]
    if request.subscription_id:
        args += ["--subscription", request.subscription_id]
    if request.storage_account:
        args += ["--with-storage-account", request.storage_account]
    if request.container:
        args += ["--with-container", request.container]
    if request.create_new_storage:
        args.append("--create-new-storage")

    # The provisioner prints progress on stderr and KEY=value lines on stdout.
    result = helpers.script(
        "provision_blob_storage.py",
        args,
        capture=True,
        heartbeat="Still creating storage in Azure",
    )
    if result.returncode != 0:
        return could_not("Creating the storage in Azure failed (see above)")
    endpoint = _ENDPOINT.search(result.stdout)
    container = _CONTAINER.search(result.stdout)
    if not endpoint or not container:
        return could_not("Azure did not report where the storage is")

    update_env(
        env_path,
        {BLOB_KEYS[0]: endpoint.group(1), BLOB_KEYS[1]: container.group(1)},
        remove=[LOCAL_MEMORY_KEY],
        drop_comment_prefixes=[_LEGACY_COMMENT],
        guard=guard,
    )
    return MemoryResult(
        mode="cloud", endpoint=endpoint.group(1), container=container.group(1), provisioned=True
    )


def offer_memory_upload(helpers: Helpers, console: Console) -> bool | None:
    """After switching to cloud memory: offer to copy what is already on this computer.

    Returns True when it was uploaded, False when the upload failed, None when there was
    nothing to upload or the person said no. Local files are left in place either way.
    """
    probe = helpers.snippet(MEMORY_PROBE_SNIPPET, [str(helpers.paths.project_root)])
    raw = marked_line(probe.stdout, RESULT_MARKER)
    if probe.returncode != 0 or raw is None:
        return None
    try:
        found = json.loads(raw)
    except ValueError:
        return None
    data_bytes, persona_bytes = int(found.get("data_bytes", 0)), int(found.get("persona_bytes", 0))
    if not data_bytes and not persona_bytes:
        return None

    parts = [
        name
        for name, size in (
            ("agent data", data_bytes),
            ("Claude Code persona memory", persona_bytes),
        )
        if size
    ]
    kilobytes = max(1, (data_bytes + persona_bytes) // 1024)
    console.info(
        f"This computer already holds about {kilobytes} KB of memory ({' + '.join(parts)})."
    )
    if not console.confirm(
        "Copy it to the cloud storage now? (the local copy is kept)",
        default=False,
        key="memory_upload",
    ):
        console.detail("Skipped. Run setup again and choose cloud memory to be asked again.")
        return None

    upload = helpers.snippet(
        MEMORY_UPLOAD_SNIPPET,
        [found["data_dir"] if data_bytes else "", found["persona_dir"] if persona_bytes else ""],
    )
    raw = marked_line(upload.stdout, RESULT_MARKER)
    try:
        report = json.loads(raw) if raw else {}
    except ValueError:
        report = {}
    if upload.returncode != 0 or report.get("error_count"):
        console.fail(
            "Copying memory to the cloud failed. Cloud memory is not in sync with this computer.",
            fix="The most common cause is that the agent's access to the storage has not "
            "taken effect yet. Wait a few minutes and run setup again.",
        )
        for key, error in report.get("errors", []):
            console.detail(f"{key}: {error}")
        if not report:
            console.detail(upload.stderr.strip()[-400:])
        return False
    console.ok(
        f"Copied {report.get('copied', 0)} file(s) to the cloud "
        f"({report.get('skipped', 0)} were already there). Local files were left in place."
    )
    return True
