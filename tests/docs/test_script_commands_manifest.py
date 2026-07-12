"""Validate docs/reference/scripts/commands.yml, the single source of
truth for the 42 supported operator-facing commands documented on the
public site. Each entry must:
  * declare all required manifest keys
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

# Keys every commands.yml entry must declare, per the Phase 8 schema.
REQUIRED_KEYS = {"id", "path", "category", "page", "platforms", "summary"}

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


def _entry_label(entry: dict, index: int) -> str:
    """Readable label for a manifest entry, falling back to its index
    when the entry is malformed and has no usable id."""
    entry_id = entry.get("id")
    if entry_id:
        return f"entry {index} (id={entry_id!r})"
    return f"entry {index}"


def _level_2_headings(text: str) -> set[str]:
    """Return the set of exact ``## `` heading lines in *text*, ignoring
    any lines that fall inside fenced code blocks (delimited by lines
    starting with triple backticks) and ignoring headings of any other
    level (e.g. ``### Purpose``)."""
    headings: set[str] = set()
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith("## "):
            headings.add(line.rstrip())
    return headings


def test_manifest_has_exactly_42_commands():
    commands = load_commands_manifest()
    assert len(commands) == EXPECTED_COMMAND_COUNT, (
        f"Expected exactly {EXPECTED_COMMAND_COUNT} supported commands, "
        f"found {len(commands)}"
    )


def test_every_manifest_entry_has_required_keys():
    commands = load_commands_manifest()
    problems = []
    for index, entry in enumerate(commands):
        missing = sorted(REQUIRED_KEYS - entry.keys())
        if missing:
            problems.append(f"{_entry_label(entry, index)}: missing keys {missing}")
    assert problems == [], "\n".join(problems)


def test_manifest_ids_and_paths_are_unique():
    commands = load_commands_manifest()
    ids = [c.get("id") for c in commands]
    paths = [c.get("path") for c in commands]
    assert len(ids) == len(set(ids)), "Duplicate command ids in commands.yml"
    assert len(paths) == len(set(paths)), "Duplicate command paths in commands.yml"


def test_every_manifest_path_exists_on_disk():
    commands = load_commands_manifest()
    missing = []
    for index, entry in enumerate(commands):
        path = entry.get("path")
        if not path or not (REPO_ROOT / path).is_file():
            missing.append(_entry_label(entry, index))
    assert missing == [], f"commands.yml references missing scripts: {missing}"


def test_every_manifest_category_is_known():
    commands = load_commands_manifest()
    bad = [
        _entry_label(entry, index)
        for index, entry in enumerate(commands)
        if entry.get("category") not in KNOWN_CATEGORIES
    ]
    assert bad == [], f"commands.yml entries with unknown category: {bad}"


def test_every_manifest_page_exists_and_has_required_headings():
    commands = load_commands_manifest()
    problems = []
    for index, entry in enumerate(commands):
        label = _entry_label(entry, index)
        page = entry.get("page")
        if not page:
            problems.append(f"{label}: no page declared")
            continue
        page_path = DOCS_DIR / page
        if not page_path.is_file():
            problems.append(f"{label}: page {page} missing")
            continue
        headings = _level_2_headings(page_path.read_text(encoding="utf-8"))
        missing_headings = [h for h in REQUIRED_HEADINGS if h not in headings]
        if missing_headings:
            problems.append(f"{label}: {page} missing headings {missing_headings}")
    assert problems == [], "\n".join(problems)


def test_level_2_heading_helper_recognizes_exact_heading():
    text = "## Purpose\n\nSome body text.\n"
    assert "## Purpose" in _level_2_headings(text)


def test_level_2_heading_helper_rejects_level_3_heading():
    text = "### Purpose\n\nSome body text.\n"
    assert "## Purpose" not in _level_2_headings(text)


def test_level_2_heading_helper_rejects_longer_heading_text():
    text = "## Purpose and Audience\n\nSome body text.\n"
    headings = _level_2_headings(text)
    assert "## Purpose and Audience" in headings
    assert "## Purpose" not in headings


def test_level_2_heading_helper_ignores_fenced_heading():
    text = "```\n## Purpose\n```\n\n## Requirements\n"
    headings = _level_2_headings(text)
    assert "## Purpose" not in headings
    assert "## Requirements" in headings
