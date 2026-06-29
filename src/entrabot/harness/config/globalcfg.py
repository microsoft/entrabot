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

# Keys unique to one agent (its Entra identity + Teams user). One set per directory. The shared
# root keys (tenant/blueprint/cert) are simply "everything that isn't an AGENT_KEY" — see split().
AGENT_KEYS = (
    "ENTRABOT_AGENT_ID",
    "ENTRABOT_AGENT_OBJECT_ID",
    "ENTRABOT_AGENT_USER_ID",
    "ENTRABOT_AGENT_USER_UPN",
)

GLOBAL_ENV_FILENAME = "global.env"
AGENT_ENV_FILENAME = ".env"


def global_dir() -> str:
    """Directory holding the shared global config. Delegates to the core `entrabot_home()` so it
    matches the platform data root (`%LOCALAPPDATA%\\entrabot` on Windows, `~/.entrabot` else,
    `$ENTRABOT_HOME` override) and never collides with the legacy `~/.entrabot` migration."""
    from entrabot import config as _config

    return str(_config.entrabot_home())


def global_env_path() -> str:
    return os.path.join(global_dir(), GLOBAL_ENV_FILENAME)


def agent_env_path(root: str) -> str:
    return os.path.join(root, ".entrabot", AGENT_ENV_FILENAME)


def home_agent_env_path() -> str:
    """The default agent's ``.env``, alongside global.env in the home config dir (``~/.entrabot``,
    or ``$ENTRABOT_HOME``). This is what bare ``entrabot`` from home runs."""
    return os.path.join(global_dir(), AGENT_ENV_FILENAME)


def _is_valid_env_line(line: str) -> bool:
    """True for a non-blank, non-comment ``KEY=VALUE`` line."""
    return bool(line) and not line.startswith("#") and "=" in line


def read_env(path: str) -> dict[str, str]:
    """Parse a KEY=VALUE ``.env`` file (ignores blanks/comments). Missing file → {}."""
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (FileNotFoundError, OSError):
        return out
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not _is_valid_env_line(line):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # First occurrence wins — matches the dotenv loader (config._overlay), which never
        # overwrites an already-set key. A combined .env that accumulated duplicate keys across
        # setup runs (e.g. rotated cert thumbprints) must resolve to the SAME value the runtime
        # uses, or migrate would capture a stale/unregistered one.
        out.setdefault(key, value.strip())
    return out


def write_env(path: str, mapping: dict[str, str], header: str = "") -> None:
    """Write a ``.env`` file (creating parent dirs). Sorted for stable diffs."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lines = []
    if header:
        lines += ["# " + header_line for header_line in header.splitlines()]
    for key in sorted(mapping):
        lines.append(f"{key}={mapping[key]}")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def split(env: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Partition a combined env into (global, per-agent). Per-agent = the agent identity keys;
    global = everything else (tenant/blueprint/cert, shared HUMAN_*, and any runtime prefs), so
    no key is ever dropped on the round-trip."""
    agent = {key: value for key, value in env.items() if key in AGENT_KEYS and value}
    glob = {key: value for key, value in env.items() if key not in AGENT_KEYS and value}
    return glob, agent


def read_global() -> dict[str, str]:
    return read_env(global_env_path())


def global_exists() -> bool:
    """True once the shared tenant + blueprint have been provisioned."""
    global_env = read_global()
    return bool(
        global_env.get("ENTRABOT_TENANT_ID") and global_env.get("ENTRABOT_BLUEPRINT_APP_ID")
    )


def agent_exists(root: str) -> bool:
    """True once an agent identity is provisioned in ``root`` (its .env carries an Agent User
    UPN). Idempotent ``init`` keys off this to resume instead of minting another agent."""
    return bool(read_env(agent_env_path(root)).get("ENTRABOT_AGENT_USER_UPN"))


def blueprint_app_id() -> str:
    return read_global().get("ENTRABOT_BLUEPRINT_APP_ID", "")
