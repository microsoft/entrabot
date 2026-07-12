"""Once Phase 9 migrates historical content out of docs/, the specific
legacy paths named in the approved redesign spec must be gone from the
published tree entirely (not merely absent from nav — see the docs_dir
note in the implementation plan for why omission-from-nav is not enough).
"""

from tests.docs._helpers import DOCS_DIR

LEGACY_PATHS_THAT_MUST_NOT_EXIST = (
    "architecture/PLAN-windows-port.md",
    "architecture/next-mcp-server-design.md",
    "claude-copilot-cli-channel-port.md",
    "architecture/NEXT-WhatsApp-lightweight-teams-chat.md",
    "architecture/PLAN-agent-identity-by-upn.md",
    "architecture/PLAN-xpia-content-wrapping.md",
    "runbooks/mcp-disconnect-investigation.md",
    "decisions",
    "platform-learnings",
)


def test_legacy_paths_are_gone():
    still_present = [
        path
        for path in LEGACY_PATHS_THAT_MUST_NOT_EXIST
        if (DOCS_DIR / path).exists()
    ]
    assert still_present == [], (
        "These legacy docs/ paths must be migrated to engineering-history/ "
        f"or renamed and are still present: {still_present}"
    )
