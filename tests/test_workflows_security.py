"""Security regression tests for GitHub Actions workflows."""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(".github/workflows/claude-pr-review.yml")


def test_claude_pr_review_uses_immutable_action_pin() -> None:
    workflow = WORKFLOW.read_text()

    match = re.search(r"uses:\s*anthropics/claude-code-action@([0-9a-f]{40})\b", workflow)

    assert match is not None


def test_claude_pr_review_does_not_request_oidc_token() -> None:
    workflow = WORKFLOW.read_text()

    assert not re.search(r"^\s*id-token:\s*write\s*$", workflow, re.MULTILINE)
