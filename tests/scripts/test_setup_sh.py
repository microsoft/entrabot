from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def read_script(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8-sig")


def test_setup_prompts_read_stdin_only_from_a_terminal() -> None:
    # setup.sh runs under `set -euo pipefail`, so a bare `read` with no terminal attached
    # (an agent or CI run) hits end of input and aborts setup before its summary and smoke test.
    lines = read_script("scripts/setup.sh").splitlines()
    unguarded = [
        number
        for number, line in enumerate(lines, start=1)
        if re.match(r"\s*read\b", line)
        and "<<<" not in line
        and not any("[ -t 0 ]" in before for before in lines[max(0, number - 4) : number - 1])
    ]

    assert unguarded == [], f"setup.sh reads stdin without a [ -t 0 ] check on lines {unguarded}"
