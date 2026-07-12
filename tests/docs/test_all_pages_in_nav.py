"""Every public Markdown file under docs/ must be reachable from nav.

MkDocs will build and publish orphan pages even if they are absent from
nav: they just won't be linked from the site chrome, and (critically)
they still get indexed by the search plugin and are still crawlable by
URL. The redesign spec requires every public page to be a deliberate,
navigable part of the site, so orphans are treated as a bug.
"""

from pathlib import PureWindowsPath

from tests.docs import _helpers


def test_every_markdown_file_is_in_nav():
    nav_paths = _helpers.all_nav_paths()
    all_files = _helpers.all_public_markdown_files()
    orphans = sorted(all_files - nav_paths)
    assert orphans == [], f"These docs/ pages exist but are not listed in mkdocs.yml nav: {orphans}"


def test_public_markdown_paths_use_posix_separators(monkeypatch):
    class FakePath:
        def relative_to(self, _root):
            return PureWindowsPath("architecture/identity.md")

    class FakeDocsDir:
        def rglob(self, _pattern):
            return [FakePath()]

    monkeypatch.setattr(_helpers, "DOCS_DIR", FakeDocsDir())
    result = _helpers.all_public_markdown_files()
    assert result == {"architecture/identity.md"}
