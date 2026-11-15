"""What the contained Entrabot server may read, write and connect to.

Derived from configuration and from what the code calls, never from the model:

- read-only: the repository and its virtual environment (and anything in
  ``ENTRABOT_SANDBOX_EXTRA_READ``, ``:``-separated);
- read-write: Entrabot's own state (``~/.entrabot`` and any moved log, audit or data
  directory) and the delegated sign-in cache; the temp directory is inside the state;
- nothing else under the home directory (on macOS, nor under ``/Users`` or ``/Volumes``);
- network: only these hosts, on 443, through the launcher's allowlisting proxy:
  ``login.microsoftonline.com`` (sign-in, tenant discovery), ``graph.microsoft.com``,
  ``agent365.svc.cloud.microsoft`` (Work IQ and Agent 365 telemetry), ``*.sharepoint.com``
  (Graph file downloads redirect there), the Azure Blob endpoint when cloud memory is on,
  persona-sati's host when configured, and anything in ``ENTRABOT_SANDBOX_EXTRA_HOSTS``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from entrabot.config import EntraBotConfig

# From the code: entrabot.auth, tools.teams, graph_helpers, a365.catalog, tools.files and
# the Microsoft OpenTelemetry Distro's Agent 365 exporter (DEFAULT_ENDPOINT_URL).
BASE_HOSTS: tuple[str, ...] = (
    "login.microsoftonline.com",
    "graph.microsoft.com",
    "agent365.svc.cloud.microsoft",
    "*.sharepoint.com",
)
EXTRA_HOSTS_ENV = "ENTRABOT_SANDBOX_EXTRA_HOSTS"
EXTRA_READ_ENV = "ENTRABOT_SANDBOX_EXTRA_READ"


@dataclass(frozen=True)
class ContainmentPolicy:
    home: Path
    read_only: tuple[Path, ...]
    read_write: tuple[Path, ...]
    temp_dir: Path
    allowed_hosts: tuple[str, ...]


def host_allowed(host: str, allowed: Sequence[str]) -> bool:
    """Is *host* one of *allowed*? ``*.example.com`` covers subdomains, not the domain."""
    name = host.strip().lower().rstrip(".")
    if not name:
        return False
    for entry in allowed:
        pattern = entry.strip().lower()
        if pattern.startswith("*."):
            if name.endswith(pattern[1:]) and name != pattern[2:]:
                return True
        elif name == pattern:
            return True
    return False


def _host_of(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return parsed.hostname.lower() if parsed.hostname else None


def _unique(paths: list[Path]) -> tuple[Path, ...]:
    seen: list[Path] = []
    for path in paths:
        if path not in seen:
            seen.append(path)
    return tuple(seen)


def build_policy(
    config: EntraBotConfig,
    *,
    project_root: Path,
    venv: Path,
    home: Path,
    cache_dir: Path,
    env: Mapping[str, str],
) -> ContainmentPolicy:
    state_root = home / ".entrabot"
    read_write = [state_root, cache_dir]
    for directory in (config.log_dir, config.audit_dir, config.data_dir):
        if not Path(directory).is_relative_to(state_root):
            read_write.append(Path(directory))
    read_only = [project_root, venv]
    for item in env.get(EXTRA_READ_ENV, "").split(os.pathsep):
        if item.strip():
            read_only.append(Path(item.strip()))

    hosts = list(BASE_HOSTS)
    if config.blob_endpoint and config.blob_container:
        blob = _host_of(config.blob_endpoint)
        if blob:
            hosts.append(blob)
    persona = _host_of(env.get("PERSONA_SATI_MCP_URL"))
    if persona:
        hosts.append(persona)
    for item in env.get(EXTRA_HOSTS_ENV, "").split(","):
        if item.strip():
            hosts.append(item.strip().lower())

    return ContainmentPolicy(
        home=home,
        read_only=_unique(read_only),
        read_write=_unique(read_write),
        temp_dir=state_root / "tmp",
        allowed_hosts=tuple(dict.fromkeys(hosts)),
    )
