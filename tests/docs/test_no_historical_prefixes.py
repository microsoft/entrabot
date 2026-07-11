"""Public docs must never contain historical-artifact filenames.

Plans, specs, designs, and other point-in-time artifacts belong under
engineering-history/, not docs/. This test fails loudly (naming every
offending file) as long as any prefixed file remains under docs/, which
is true today until Phase 9 completes the migration.
"""

from tests.docs._helpers import DOCS_DIR, FORBIDDEN_PREFIXES


def test_no_forbidden_prefixes_under_docs():
    offenders = [
        str(p.relative_to(DOCS_DIR))
        for p in DOCS_DIR.rglob("*.md")
        if p.name.startswith(FORBIDDEN_PREFIXES)
    ]
    assert offenders == [], (
        "Historical-artifact files must move to engineering-history/, "
        f"not live under docs/: {sorted(offenders)}"
    )
