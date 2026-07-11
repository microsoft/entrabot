"""Public docs must read as neutral repository documentation, not as
notes authored/reviewed by a specific AI agent or model. Product names
used as client identifiers (e.g. 'Claude Code', 'Copilot CLI' as a
supported host) are fine; byline-style attribution or competitor
framing ("author: ChatGPT", "reviewed by Codex") is not.
"""

import re

from tests.docs._helpers import (
    DOCS_DIR,
    FORBIDDEN_ATTRIBUTION_PATTERNS,
    all_public_markdown_files,
)

COMPILED_PATTERNS = [
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in FORBIDDEN_ATTRIBUTION_PATTERNS
]


def test_no_agent_attribution_bylines():
    offenders: list[str] = []
    for relative_path in all_public_markdown_files():
        md_file = DOCS_DIR / relative_path
        text = md_file.read_text(encoding="utf-8")
        for pattern in COMPILED_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{relative_path}: {pattern.pattern}")
    assert offenders == [], (
        "Public docs must not carry agent-authorship attribution bylines: "
        f"{offenders}"
    )
