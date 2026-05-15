"""Configuration helpers for Agent 365 / Work IQ runtime."""

from __future__ import annotations

import os
from pathlib import Path


def manifest_candidates(cwd: Path | None = None) -> list[Path]:
    """Return manifest paths in resolution order."""
    root = cwd or Path.cwd()
    candidates: list[Path] = []
    configured = os.environ.get("ENTRACLAW_A365_TOOLING_MANIFEST")
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend([root / "ToolingManifest.json", root / ".a365" / "ToolingManifest.json"])
    return candidates
