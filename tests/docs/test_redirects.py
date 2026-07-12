"""Every mkdocs-redirects target must point at a page in the current nav.

The redirects plugin is configured with a non-empty redirect_maps table.
"""

from tests.docs._helpers import all_nav_paths, redirect_map


def test_redirects_plugin_is_configured():
    assert redirect_map(), (
        "mkdocs.yml must configure the `redirects` plugin with a non-empty "
        "redirect_maps table"
    )


def test_every_redirect_target_is_a_current_nav_page():
    nav_paths = all_nav_paths()
    mapping = redirect_map()
    bad_targets = {old: new for old, new in mapping.items() if new not in nav_paths}
    assert bad_targets == {}, (
        "Redirect targets must point at pages currently in nav (not at "
        f"unpublished engineering-history URLs): {bad_targets}"
    )


def test_no_redirect_source_is_also_a_current_nav_page():
    nav_paths = all_nav_paths()
    mapping = redirect_map()
    overlap = sorted(set(mapping.keys()) & nav_paths)
    assert overlap == [], (
        f"Redirect sources must be removed pages, not current nav pages: {overlap}"
    )
