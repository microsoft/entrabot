"""Environment-based configuration for EntraBot.

Uses a simple dataclass with fallback defaults. Values are read from
environment variables prefixed with ENTRABOT_.  On import the module
looks for a ``.env`` file in the project root (best-effort, no hard
dependency on ``python-dotenv``).
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from entrabot.errors import RemovedModeError


def _parse_csv(value: str | None) -> list[str]:
    """Parse a comma-separated string into a list, filtering empty strings."""
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _parse_csv_preserve_empty(value: str | None) -> list[str]:
    """Parse a comma-separated string, preserving empty entries.

    Unlike ``_parse_csv``, empty strings between commas are kept so that
    the result stays index-aligned with parallel CSV lists (e.g. user IDs
    and their corresponding tenant IDs).
    """
    if not value:
        return []
    return [v.strip() for v in value.split(",")]


def _windows_root(home: Path | None = None) -> Path:
    """Return the per-user data root on Windows.

    Prefers ``%LOCALAPPDATA%``; falls back to ``<home>/AppData/Local`` when
    the env var is missing (rare on stripped CI runners).
    """
    home = home or Path.home()
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else home / "AppData" / "Local"
    return base / "entrabot"


def _default_dir(subdir: str) -> Path:
    if sys.platform == "win32":
        return _windows_root() / subdir
    return Path.home() / ".entrabot" / subdir


def _path_from_env(name: str, default_subdir: str) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else _default_dir(default_subdir)


def _has_content(path: Path) -> bool:
    """True when ``path`` exists and contains at least one entry."""
    return path.is_dir() and any(path.iterdir())


def migrate_legacy_data_dir(*, home: Path | None = None) -> bool:
    """One-shot move of legacy ``~/.entrabot/`` to ``%LOCALAPPDATA%\\entrabot\\``.

    Idempotent and Windows-only. Returns ``True`` when content was moved,
    ``False`` when no migration was needed (legacy missing/empty, or target
    already populated and legacy gone).

    Raises ``RuntimeError`` when both legacy and target contain data — that
    means the user has been running on two roots and needs manual triage.
    """
    if sys.platform != "win32":
        return False

    home = home or Path.home()
    legacy = home / ".entrabot"
    target = _windows_root(home=home)

    if not _has_content(legacy):
        return False

    if _has_content(target):
        raise RuntimeError(
            "two entrabot dirs detected: legacy "
            f"{legacy} and current {target} both contain data. "
            "Manual triage needed — pick one and remove the other."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.rmdir()
    shutil.move(str(legacy), str(target))
    return True


def check_legacy_data_dir(*, home: Path | None = None) -> None:
    """MCP-boot guard: halt loud when migration is owed.

    Raises ``RuntimeError`` on Windows when legacy ``~/.entrabot/`` has
    content while target ``%LOCALAPPDATA%\\entrabot\\`` is empty/missing.
    No-op on Mac/Linux.
    """
    if sys.platform != "win32":
        return

    home = home or Path.home()
    legacy = home / ".entrabot"
    target = _windows_root(home=home)

    if _has_content(legacy) and not _has_content(target):
        raise RuntimeError(
            f"Legacy entrabot data found at {legacy} but target "
            f"{target} is empty. Run setup-windows.cmd --migrate to move it."
        )


def _load_dotenv() -> None:
    """Best-effort load of ``.env`` file from the project root."""
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Don't overwrite values already in the environment
        if key not in os.environ:
            os.environ[key] = value


# Load .env on first import so all downstream code sees the values.
_load_dotenv()


VALID_MODES = {"auto", "delegated", "agent_user"}

# Modes that once existed but were removed. Setting one of these is a
# hard error (fail loud) rather than a silent fallback to ``auto`` — see
# RemovedModeError and ADR-006.
_REMOVED_MODES = {
    "bot": (
        "ENTRABOT_MODE=bot was removed. Bot mode used the Bot Framework "
        "gateway, which bypasses the Agent Identity this project is built "
        "around. Use 'agent_user' (recommended) or 'delegated' instead."
    ),
}


def _validate_mode(value: str) -> str:
    """Return the mode if valid.

    Raises ``RemovedModeError`` for a mode that was removed (e.g. ``bot``),
    so an existing config never silently switches identity modes. Any other
    unrecognized value falls back to ``auto``.
    """
    if value in _REMOVED_MODES:
        raise RemovedModeError(_REMOVED_MODES[value])
    return value if value in VALID_MODES else "auto"


@dataclass(frozen=True)
class EntraBotConfig:
    """Immutable configuration loaded from environment variables."""

    tenant_id: str | None = field(default=None)
    blueprint_app_id: str | None = field(default=None)
    blueprint_object_id: str | None = field(default=None)
    blueprint_cert_thumbprint: str | None = field(default=None)
    blueprint_cert_sha1: str | None = field(default=None)
    blueprint_ksp: str | None = field(default=None)
    agent_id: str | None = field(default=None)
    agent_object_id: str | None = field(default=None)
    agent_user_id: str | None = field(default=None)
    agent_user_upn: str | None = field(default=None)
    human_user_id: str | None = field(default=None)
    human_upn: str | None = field(default=None)
    human_user_ids: list[str] = field(default_factory=list)
    human_upns: list[str] = field(default_factory=list)
    human_user_tenant_ids: list[str] = field(default_factory=list)
    human_user_mails: list[str] = field(default_factory=list)
    human_user_types: list[str] = field(default_factory=list)
    log_dir: Path = field(default_factory=lambda: _default_dir("logs"))
    audit_dir: Path = field(default_factory=lambda: _default_dir("audit"))
    data_dir: Path = field(default_factory=lambda: _default_dir("data"))
    log_level: str = field(default="INFO")
    client_id: str | None = field(default=None)
    skip_provisioning: bool = field(default=False)
    authority: str = field(default="https://login.microsoftonline.com/common")
    mode: str = field(default="auto")
    blob_endpoint: str | None = field(default=None)
    blob_container: str | None = field(default=None)
    keep_memory_local: bool = field(default=False)
    # XPIA content-wrapping rollback flag. Default True so protection
    # is on unless an operator explicitly opts out.
    # ``ENTRABOT_XPIA_WRAP_ENABLE=false`` disables the wrap without a
    # code revert; see docs/architecture/security-boundaries.md.
    xpia_wrap_enable: bool = field(default=True)

    @classmethod
    def from_env(cls) -> EntraBotConfig:
        """Build config from ENTRABOT_* environment variables."""
        return cls(
            tenant_id=os.environ.get("ENTRABOT_TENANT_ID"),
            blueprint_app_id=os.environ.get("ENTRABOT_BLUEPRINT_APP_ID"),
            blueprint_object_id=os.environ.get("ENTRABOT_BLUEPRINT_OBJECT_ID"),
            blueprint_cert_thumbprint=os.environ.get("ENTRABOT_BLUEPRINT_CERT_THUMBPRINT"),
            blueprint_cert_sha1=os.environ.get("ENTRABOT_BLUEPRINT_CERT_SHA1"),
            blueprint_ksp=os.environ.get("ENTRABOT_BLUEPRINT_KSP"),
            agent_id=os.environ.get("ENTRABOT_AGENT_ID"),
            agent_object_id=os.environ.get("ENTRABOT_AGENT_OBJECT_ID"),
            agent_user_id=os.environ.get("ENTRABOT_AGENT_USER_ID"),
            # Canonical machine identity for the self-authored filter
            # (Learning #69). Accepts either ``ENTRABOT_AGENT_UPN`` (the
            # rename-safe canonical name from the 2026-07-09 fix) or the
            # historical ``ENTRABOT_AGENT_USER_UPN``. Prefer the new name;
            # keep the old for existing ``.env`` files.
            agent_user_upn=(
                os.environ.get("ENTRABOT_AGENT_UPN")
                or os.environ.get("ENTRABOT_AGENT_USER_UPN")
            ),
            human_user_id=os.environ.get("ENTRABOT_HUMAN_USER_ID"),
            human_upn=os.environ.get("ENTRABOT_HUMAN_UPN"),
            human_user_ids=_parse_csv(os.environ.get("ENTRABOT_HUMAN_USER_IDS"))
            or _parse_csv(os.environ.get("ENTRABOT_HUMAN_USER_ID")),
            human_upns=_parse_csv(os.environ.get("ENTRABOT_HUMAN_UPNS"))
            or _parse_csv(os.environ.get("ENTRABOT_HUMAN_UPN")),
            human_user_tenant_ids=_parse_csv_preserve_empty(
                os.environ.get("ENTRABOT_HUMAN_USER_TENANT_IDS")
            ),
            human_user_mails=_parse_csv(os.environ.get("ENTRABOT_HUMAN_USER_MAILS")),
            human_user_types=_parse_csv_preserve_empty(
                os.environ.get("ENTRABOT_HUMAN_USER_TYPES")
            ),
            log_dir=_path_from_env("ENTRABOT_LOG_DIR", "logs"),
            audit_dir=_path_from_env("ENTRABOT_AUDIT_DIR", "audit"),
            data_dir=_path_from_env("ENTRABOT_DATA_DIR", "data"),
            log_level=os.environ.get("ENTRABOT_LOG_LEVEL", "INFO"),
            client_id=os.environ.get("ENTRABOT_CLIENT_ID"),
            skip_provisioning=os.environ.get("ENTRABOT_SKIP_PROVISIONING", "").lower()
            in ("true", "1", "yes"),
            authority=os.environ.get(
                "ENTRABOT_AUTHORITY", "https://login.microsoftonline.com/common"
            ),
            mode=_validate_mode(os.environ.get("ENTRABOT_MODE", "auto")),
            blob_endpoint=os.environ.get("ENTRABOT_BLOB_ENDPOINT"),
            blob_container=os.environ.get("ENTRABOT_BLOB_CONTAINER"),
            keep_memory_local=os.environ.get("ENTRABOT_KEEP_MEMORY_LOCAL", "").lower()
            in ("true", "1", "yes"),
            # Default True — the flag is a rollback path, not an opt-in.
            # Any non-empty value that is not explicitly falsy stays enabled.
            xpia_wrap_enable=(
                os.environ.get("ENTRABOT_XPIA_WRAP_ENABLE", "").strip().lower()
                not in ("false", "0", "no", "off")
            ),
        )


def get_config() -> EntraBotConfig:
    """Convenience accessor — returns config from current environment."""
    return EntraBotConfig.from_env()
