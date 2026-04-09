"""Environment-based configuration for EntraClaw.

Uses a simple dataclass with fallback defaults. Values are read from
environment variables prefixed with ENTRACLAW_.  On import the module
looks for a ``.env`` file in the project root (best-effort, no hard
dependency on ``python-dotenv``).
"""

from __future__ import annotations

import os
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


def _default_dir(subdir: str) -> Path:
    return Path.home() / ".entraclaw" / subdir


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


@dataclass(frozen=True)
class EntraClawConfig:
    """Immutable configuration loaded from environment variables."""

    tenant_id: str | None = field(default=None)
    blueprint_app_id: str | None = field(default=None)
    blueprint_object_id: str | None = field(default=None)
    blueprint_cert_thumbprint: str | None = field(default=None)
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

    @classmethod
    def from_env(cls) -> EntraClawConfig:
        """Build config from ENTRACLAW_* environment variables."""
        return cls(
            tenant_id=os.environ.get("ENTRACLAW_TENANT_ID"),
            blueprint_app_id=os.environ.get("ENTRACLAW_BLUEPRINT_APP_ID"),
            blueprint_object_id=os.environ.get("ENTRACLAW_BLUEPRINT_OBJECT_ID"),
            blueprint_cert_thumbprint=os.environ.get("ENTRACLAW_BLUEPRINT_CERT_THUMBPRINT"),
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
        )


def get_config() -> EntraClawConfig:
    """Convenience accessor — returns config from current environment."""
    return EntraClawConfig.from_env()
