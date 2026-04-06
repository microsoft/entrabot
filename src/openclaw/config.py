"""Environment-based configuration for Openclaw.

Uses a simple dataclass with fallback defaults. Values are read from
environment variables prefixed with OPENCLAW_.  On import the module
looks for a ``.env`` file in the project root (best-effort, no hard
dependency on ``python-dotenv``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_dir(subdir: str) -> Path:
    return Path.home() / ".openclaw" / subdir


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
class OpenclawConfig:
    """Immutable configuration loaded from environment variables."""

    tenant_id: str | None = field(default=None)
    client_id: str | None = field(default=None)
    client_secret: str | None = field(default=None)
    agent_user_id: str | None = field(default=None)
    agent_upn: str | None = field(default=None)
    agent_password: str | None = field(default=None)
    human_user_id: str | None = field(default=None)
    human_upn: str | None = field(default=None)
    log_dir: Path = field(default_factory=lambda: _default_dir("logs"))
    audit_dir: Path = field(default_factory=lambda: _default_dir("audit"))
    data_dir: Path = field(default_factory=lambda: _default_dir("data"))
    log_level: str = field(default="INFO")

    @classmethod
    def from_env(cls) -> OpenclawConfig:
        """Build config from OPENCLAW_* environment variables."""
        return cls(
            tenant_id=os.environ.get("OPENCLAW_TENANT_ID"),
            client_id=os.environ.get("OPENCLAW_CLIENT_ID"),
            client_secret=os.environ.get("OPENCLAW_CLIENT_SECRET"),
            agent_user_id=os.environ.get("OPENCLAW_AGENT_USER_ID"),
            agent_upn=os.environ.get("OPENCLAW_AGENT_UPN"),
            agent_password=os.environ.get("OPENCLAW_AGENT_PASSWORD"),
            human_user_id=os.environ.get("OPENCLAW_HUMAN_USER_ID"),
            human_upn=os.environ.get("OPENCLAW_HUMAN_UPN"),
            log_dir=Path(os.environ.get("OPENCLAW_LOG_DIR", _default_dir("logs"))),
            audit_dir=Path(os.environ.get("OPENCLAW_AUDIT_DIR", _default_dir("audit"))),
            data_dir=Path(os.environ.get("OPENCLAW_DATA_DIR", _default_dir("data"))),
            log_level=os.environ.get("OPENCLAW_LOG_LEVEL", "INFO"),
        )


def get_config() -> OpenclawConfig:
    """Convenience accessor — returns config from current environment."""
    return OpenclawConfig.from_env()
