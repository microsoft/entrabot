"""Validate docs/reference/scripts/commands.yml, the single source of
truth for the 42 supported operator-facing commands documented on the
public site. Each entry must:
  * reference a script that exists on disk (relative to repo root)
  * declare a category from the known set
  * declare a docs page under docs/reference/scripts/ that exists and
    contains all six required section headings
  * be one of exactly 42 entries, with unique ids and unique paths
"""

from tests.docs._helpers import DOCS_DIR, REPO_ROOT, load_commands_manifest

REQUIRED_HEADINGS = (
    "## Purpose",
    "## Requirements",
    "## Usage",
    "## Effects",
    "## Exit behavior",
    "## Related commands",
)

KNOWN_CATEGORIES = {
    "setup",
    "provisioning",
    "auth-and-certs",
    "storage",
    "operations",
    "diagnostics",
    "teardown",
}

EXPECTED_COMMAND_COUNT = 42


def test_manifest_has_exactly_42_commands():
    commands = load_commands_manifest()
    assert len(commands) == EXPECTED_COMMAND_COUNT, (
        f"Expected exactly {EXPECTED_COMMAND_COUNT} supported commands, "
        f"found {len(commands)}"
    )


def test_manifest_ids_and_paths_are_unique():
    commands = load_commands_manifest()
    ids = [c["id"] for c in commands]
    paths = [c["path"] for c in commands]
    assert len(ids) == len(set(ids)), "Duplicate command ids in commands.yml"
    assert len(paths) == len(set(paths)), "Duplicate command paths in commands.yml"


def test_every_manifest_path_exists_on_disk():
    commands = load_commands_manifest()
    missing = [c["path"] for c in commands if not (REPO_ROOT / c["path"]).is_file()]
    assert missing == [], f"commands.yml references missing scripts: {missing}"


def test_every_manifest_category_is_known():
    commands = load_commands_manifest()
    bad = [c["id"] for c in commands if c.get("category") not in KNOWN_CATEGORIES]
    assert bad == [], f"commands.yml entries with unknown category: {bad}"


def test_every_manifest_page_exists_and_has_required_headings():
    commands = load_commands_manifest()
    problems = []
    for c in commands:
        page_path = DOCS_DIR / c["page"]
        if not page_path.is_file():
            problems.append(f"{c['id']}: page {c['page']} missing")
            continue
        text = page_path.read_text(encoding="utf-8")
        missing_headings = [h for h in REQUIRED_HEADINGS if h not in text]
        if missing_headings:
            problems.append(f"{c['id']}: {c['page']} missing headings {missing_headings}")
    assert problems == [], "\n".join(problems)
