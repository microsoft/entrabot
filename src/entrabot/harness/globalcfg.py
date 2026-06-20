"""Global (tenant + blueprint) vs per-agent (agent identity) config split.

The identity chain has a shared root and a per-agent leaf:

    Tenant ─ Blueprint app (+ cert)        → GLOBAL, provisioned once  → ~/.entrabot/global.env
       └─ Agent Identity ─ Agent User      → PER-AGENT, one per name   → <dir>/.entrabot/.env

So a second agent that "goes by a different name" reuses the global blueprint and only mints a
new Agent User — no re-init. This module is the pure read/write/split plumbing; the wizard and
``entrabot migrate`` drive it. Single-tenant by design (one global block); multi-tenant would be
additional named blocks here.
"""

from __future__ import annotations

import os
from typing import Dict, Tuple

# Keys that belong to the shared root (tenant + blueprint + cert). Provisioned once.
GLOBAL_KEYS = (
    "ENTRABOT_TENANT_ID",
    "ENTRABOT_BLUEPRINT_APP_ID",
    "ENTRABOT_BLUEPRINT_OBJECT_ID",
    "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT",
    "ENTRABOT_BLUEPRINT_CERT_SHA1",
    "ENTRABOT_BLUEPRINT_KSP",
    "ENTRABOT_AUTHORITY",
)
# Keys unique to one agent (its Entra identity + Teams user). One set per directory.
AGENT_KEYS = (
    "ENTRABOT_AGENT_ID",
    "ENTRABOT_AGENT_OBJECT_ID",
    "ENTRABOT_AGENT_USER_ID",
    "ENTRABOT_AGENT_USER_UPN",
)
# The humans an agent may talk to are shared across agents → kept global.
HUMAN_PREFIX = "ENTRABOT_HUMAN_"

GLOBAL_ENV_FILENAME = "global.env"
AGENT_ENV_FILENAME = ".env"


def global_dir() -> str:
    """Directory holding the shared global config (`$ENTRABOT_HOME` or `~/.entrabot`)."""
    return os.environ.get("ENTRABOT_HOME") or os.path.join(os.path.expanduser("~"), ".entrabot")


def global_env_path() -> str:
    return os.path.join(global_dir(), GLOBAL_ENV_FILENAME)


def agent_env_path(root: str) -> str:
    return os.path.join(root, ".entrabot", AGENT_ENV_FILENAME)


def home_agent_env_path() -> str:
    """The default agent's ``.env``, alongside global.env in the home config dir (``~/.entrabot``,
    or ``$ENTRABOT_HOME``). This is what bare ``entrabot`` from home runs."""
    return os.path.join(global_dir(), AGENT_ENV_FILENAME)


def read_env(path: str) -> Dict[str, str]:
    """Parse a KEY=VALUE ``.env`` file (ignores blanks/comments). Missing file → {}."""
    out: Dict[str, str] = {}
    try:
        text = open(path, encoding="utf-8").read()
    except (FileNotFoundError, OSError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def write_env(path: str, mapping: Dict[str, str], header: str = "") -> None:
    """Write a ``.env`` file (creating parent dirs). Sorted for stable diffs."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lines = []
    if header:
        lines += ["# " + h for h in header.splitlines()]
    for key in sorted(mapping):
        lines.append(f"{key}={mapping[key]}")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def split(env: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Partition a combined env into (global, per-agent). Per-agent = the agent identity keys;
    global = everything else (tenant/blueprint/cert, shared HUMAN_*, and any runtime prefs), so
    no key is ever dropped on the round-trip."""
    agent = {k: v for k, v in env.items() if k in AGENT_KEYS and v}
    glob = {k: v for k, v in env.items() if k not in AGENT_KEYS and v}
    return glob, agent


def read_global() -> Dict[str, str]:
    return read_env(global_env_path())


def global_exists() -> bool:
    """True once the shared tenant + blueprint have been provisioned."""
    g = read_global()
    return bool(g.get("ENTRABOT_TENANT_ID") and g.get("ENTRABOT_BLUEPRINT_APP_ID"))


def blueprint_app_id() -> str:
    return read_global().get("ENTRABOT_BLUEPRINT_APP_ID", "")
