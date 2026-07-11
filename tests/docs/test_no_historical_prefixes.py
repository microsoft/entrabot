"""Public docs must never contain historical-artifact filenames.

Plans, specs, designs, and other point-in-time artifacts belong under
engineering-history/, not docs/. This test fails loudly (naming every
offending file) as long as any prefixed file remains under docs/, which
is true today until Phase 9 completes the migration.
"""

from pathlib import Path

from tests.docs._helpers import FORBIDDEN_PREFIXES, all_public_markdown_files


def test_no_forbidden_prefixes_under_docs():
    offenders = [
        path
        for path in all_public_markdown_files()
        if Path(path).name.startswith(FORBIDDEN_PREFIXES)
    ]
    assert offenders == [], (
        "Historical-artifact files must move to engineering-history/, "
        f"not live under docs/: {sorted(offenders)}"
    )
