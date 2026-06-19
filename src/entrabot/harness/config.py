"""HarnessConfig + on-disk store (port of Config/TeammateConfig.cs + ConfigStore.cs).

Single-agent: the multi-agent ``workspace`` / ``workspaces`` / ``teamLead`` fields are
dropped. Adds Teams binding (``watched_chats``) and a per-caller ``permissions`` block.

Stored as ``<root>/.entrabot/harness.json`` in camelCase, indented, null fields omitted
(matching the .NET serializer conventions).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

CONFIG_DIR = ".entrabot"
CONFIG_FILE = "harness.json"

# field name -> json (camelCase) name
_CAMEL = {
    "version": "version",
    "name": "name",
    "description": "description",
    "model": "model",
    "reasoning_effort": "reasoningEffort",
    "context_tier": "contextTier",
    "agent_id": "agentId",
    "created_utc": "createdUtc",
    "watched_chats": "watchedChats",
    "permissions": "permissions",
}
_SNAKE = {v: k for k, v in _CAMEL.items()}


@dataclass
class HarnessConfig:
    name: str
    description: str
    version: int = 1
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    context_tier: Optional[str] = None  # "default" | "long_context"
    agent_id: Optional[str] = None
    created_utc: Optional[str] = None  # ISO-8601 UTC
    # Teams binding: chat IDs this ENTRABOT listens to (ingress). May be empty and
    # discovered/added at runtime.
    watched_chats: List[str] = field(default_factory=list)
    # Per-caller permission policy (see permissions.py). Opaque here; parsed there.
    permissions: Dict[str, Any] = field(default_factory=dict)

    # ---- serialization ---------------------------------------------------------------
    def to_json_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for f in fields(self):
            val = getattr(self, f.name)
            # null-omit, and omit empty collections to keep files tidy
            if val is None:
                continue
            if isinstance(val, (list, dict)) and not val:
                continue
            out[_CAMEL[f.name]] = val
        return out

    @classmethod
    def from_json_dict(cls, raw: Dict[str, Any]) -> "HarnessConfig":
        kwargs: Dict[str, Any] = {}
        for k, v in raw.items():
            snake = _SNAKE.get(k)
            if snake:
                kwargs[snake] = v
        # required fields fall back to safe defaults if a hand-edited file omits them
        kwargs.setdefault("name", kwargs.get("name", "entrabot"))
        kwargs.setdefault("description", kwargs.get("description", ""))
        return cls(**kwargs)

    def ensure_identity(self) -> bool:
        """Fill agent_id / created_utc if missing. Returns True if anything changed."""
        changed = False
        if not self.agent_id:
            self.agent_id = uuid.uuid4().hex
            changed = True
        if not self.created_utc:
            self.created_utc = datetime.now(timezone.utc).isoformat()
            changed = True
        return changed


# ---- store -------------------------------------------------------------------------------
def config_dir(root: str) -> str:
    return os.path.join(root, CONFIG_DIR)


def config_path(root: str) -> str:
    return os.path.join(root, CONFIG_DIR, CONFIG_FILE)


def exists(root: str) -> bool:
    return os.path.isfile(config_path(root))


def try_load(root: str) -> Optional[HarnessConfig]:
    path = config_path(root)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return HarnessConfig.from_json_dict(raw)


def save(root: str, cfg: HarnessConfig) -> None:
    os.makedirs(config_dir(root), exist_ok=True)
    with open(config_path(root), "w", encoding="utf-8") as fh:
        json.dump(cfg.to_json_dict(), fh, indent=2)
        fh.write("\n")
