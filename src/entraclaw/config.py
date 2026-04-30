"""Environment-based configuration for EntraClaw.

Uses a simple dataclass with fallback defaults. Values are read from
environment variables prefixed with ENTRACLAW_.  On import the module
looks for a ``.env`` file in the project root (best-effort, no hard
dependency on ``python-dotenv``).
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path


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
    return base / "entraclaw"


def _default_dir(subdir: str) -> Path:
    if sys.platform == "win32":
        return _windows_root() / subdir
    return Path.home() / ".entraclaw" / subdir


def _has_content(path: Path) -> bool:
    """True when ``path`` exists and contains at least one entry."""
    return path.is_dir() and any(path.iterdir())


def migrate_legacy_data_dir(*, home: Path | None = None) -> bool:
    """One-shot move of legacy ``~/.entraclaw/`` to ``%LOCALAPPDATA%\\entraclaw\\``.

    Idempotent and Windows-only. Returns ``True`` when content was moved,
    ``False`` when no migration was needed (legacy missing/empty, or target
    already populated and legacy gone).

    Raises ``RuntimeError`` when both legacy and target contain data — that
    means the user has been running on two roots and needs manual triage.
    """
    if sys.platform != "win32":
        return False

    home = home or Path.home()
    legacy = home / ".entraclaw"
    target = _windows_root(home=home)

    if not _has_content(legacy):
        return False

    if _has_content(target):
        raise RuntimeError(
            "two entraclaw dirs detected: legacy "
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

    Raises ``RuntimeError`` on Windows when legacy ``~/.entraclaw/`` has
    content while target ``%LOCALAPPDATA%\\entraclaw\\`` is empty/missing.
    No-op on Mac/Linux.
    """
    if sys.platform != "win32":
        return

    home = home or Path.home()
    legacy = home / ".entraclaw"
    target = _windows_root(home=home)

    if _has_content(legacy) and not _has_content(target):
        raise RuntimeError(
            f"Legacy entraclaw data found at {legacy} but target "
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


VALID_MODES = {"auto", "bot", "delegated", "agent_user"}


def _validate_mode(value: str) -> str:
    """Return the mode if valid, otherwise default to 'auto'."""
    return value if value in VALID_MODES else "auto"


@dataclass(frozen=True)
class EntraClawConfig:
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
    bot_app_id: str | None = field(default=None)
    bot_cert_thumbprint: str | None = field(default=None)
    bot_tunnel_port: int = field(default=3978)
    blob_endpoint: str | None = field(default=None)
    blob_container: str | None = field(default=None)
    keep_memory_local: bool = field(default=False)

    @classmethod
    def from_env(cls) -> EntraClawConfig:
        """Build config from ENTRACLAW_* environment variables."""
        return cls(
            tenant_id=os.environ.get("ENTRACLAW_TENANT_ID"),
            blueprint_app_id=os.environ.get("ENTRACLAW_BLUEPRINT_APP_ID"),
            blueprint_object_id=os.environ.get("ENTRACLAW_BLUEPRINT_OBJECT_ID"),
            blueprint_cert_thumbprint=os.environ.get("ENTRACLAW_BLUEPRINT_CERT_THUMBPRINT"),
            blueprint_cert_sha1=os.environ.get("ENTRACLAW_BLUEPRINT_CERT_SHA1"),
            blueprint_ksp=os.environ.get("ENTRACLAW_BLUEPRINT_KSP"),
            agent_id=os.environ.get("ENTRACLAW_AGENT_ID"),
            agent_object_id=os.environ.get("ENTRACLAW_AGENT_OBJECT_ID"),
            agent_user_id=os.environ.get("ENTRACLAW_AGENT_USER_ID"),
            agent_user_upn=os.environ.get("ENTRACLAW_AGENT_USER_UPN"),
            human_user_id=os.environ.get("ENTRACLAW_HUMAN_USER_ID"),
            human_upn=os.environ.get("ENTRACLAW_HUMAN_UPN"),
            human_user_ids=_parse_csv(os.environ.get("ENTRACLAW_HUMAN_USER_IDS"))
            or _parse_csv(os.environ.get("ENTRACLAW_HUMAN_USER_ID")),
            human_upns=_parse_csv(os.environ.get("ENTRACLAW_HUMAN_UPNS"))
            or _parse_csv(os.environ.get("ENTRACLAW_HUMAN_UPN")),
            human_user_tenant_ids=_parse_csv_preserve_empty(
                os.environ.get("ENTRACLAW_HUMAN_USER_TENANT_IDS")
            ),
            human_user_mails=_parse_csv(os.environ.get("ENTRACLAW_HUMAN_USER_MAILS")),
            human_user_types=_parse_csv_preserve_empty(
                os.environ.get("ENTRACLAW_HUMAN_USER_TYPES")
            ),
            log_dir=Path(os.environ.get("ENTRACLAW_LOG_DIR", _default_dir("logs"))),
            audit_dir=Path(os.environ.get("ENTRACLAW_AUDIT_DIR", _default_dir("audit"))),
            data_dir=Path(os.environ.get("ENTRACLAW_DATA_DIR", _default_dir("data"))),
            log_level=os.environ.get("ENTRACLAW_LOG_LEVEL", "INFO"),
            client_id=os.environ.get("ENTRACLAW_CLIENT_ID"),
            skip_provisioning=os.environ.get("ENTRACLAW_SKIP_PROVISIONING", "").lower()
            in ("true", "1", "yes"),
            authority=os.environ.get(
                "ENTRACLAW_AUTHORITY", "https://login.microsoftonline.com/common"
            ),
            mode=_validate_mode(os.environ.get("ENTRACLAW_MODE", "auto")),
            bot_app_id=os.environ.get("ENTRACLAW_BOT_APP_ID"),
            bot_cert_thumbprint=os.environ.get("ENTRACLAW_BOT_CERT_THUMBPRINT"),
            bot_tunnel_port=int(os.environ.get("ENTRACLAW_BOT_TUNNEL_PORT", "3978")),
            blob_endpoint=os.environ.get("ENTRACLAW_BLOB_ENDPOINT"),
            blob_container=os.environ.get("ENTRACLAW_BLOB_CONTAINER"),
            keep_memory_local=os.environ.get("ENTRACLAW_KEEP_MEMORY_LOCAL", "").lower()
            in ("true", "1", "yes"),
        )


def get_config() -> EntraClawConfig:
    """Convenience accessor — returns config from current environment."""
    return EntraClawConfig.from_env()
