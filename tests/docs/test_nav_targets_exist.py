"""Every path referenced in mkdocs.yml's nav: must exist under docs/.

A stale nav entry pointing at a moved/deleted file is a broken site
(404 on click-through) that MkDocs' non-strict mode silently tolerates.
This test is the enforcement mechanism `mkdocs build --strict` also
provides, kept here so it runs fast under plain pytest without invoking
a full site build.
"""

from tests.docs._helpers import DOCS_DIR, all_nav_paths


def test_every_nav_path_exists_on_disk():
    missing = [path for path in all_nav_paths() if not (DOCS_DIR / path).is_file()]
    assert missing == [], f"mkdocs.yml nav references missing files: {sorted(missing)}"
