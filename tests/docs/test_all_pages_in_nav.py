"""Every public Markdown file under docs/ must be reachable from nav.

MkDocs will build and publish orphan pages even if they are absent from
nav: they just won't be linked from the site chrome, and (critically)
they still get indexed by the search plugin and are still crawlable by
URL. The redesign spec requires every public page to be a deliberate,
navigable part of the site, so orphans are treated as a bug.
"""

from tests.docs._helpers import all_nav_paths, all_public_markdown_files


def test_every_markdown_file_is_in_nav():
    nav_paths = all_nav_paths()
    all_files = all_public_markdown_files()
    orphans = sorted(all_files - nav_paths)
    assert orphans == [], (
        "These docs/ pages exist but are not listed in mkdocs.yml nav: "
        f"{orphans}"
    )
