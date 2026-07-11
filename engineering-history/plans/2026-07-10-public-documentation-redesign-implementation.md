# Public Documentation Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the published MkDocs site (`docs/`, `mkdocs.yml`) so it contains only current, neutral, present-tense product and engineering documentation, move every plan/spec/design/investigation/prompt/ADR/QA-log artifact into the non-published `engineering-history/` tree (deleting true duplicates), replace grouped script docs with one page per supported operator-facing command driven by a manifest, and add automated repository tests that make the old structure mechanically impossible to reintroduce — per the approved design in `engineering-history/specs/2026-07-10-public-documentation-redesign.md`.

**Architecture:** `docs/` becomes the exclusive source for MkDocs's `docs_dir` and contains only pages listed in `mkdocs.yml`'s `nav:` (a new repository test enforces 1:1 coverage in both directions). Historical material moves to `engineering-history/{architecture,decisions,investigations,plans,prompts,research,specs}/`, a plain directory tree with no build step, so it is never processed by MkDocs. A new `docs/reference/scripts/commands.yml` manifest is the single source of truth for the 42 supported operator-facing commands; a repository test cross-checks the manifest against the filesystem, the per-command pages, and the MkDocs nav. `mkdocs-redirects` maps every removed public URL to a canonical current page. `.github/workflows/docs.yml` gains a `validate` job that runs on every pull request (docs pytest + `mkdocs build --strict`) and keeps the `deploy` job gated to `push` on `main`.

**Tech Stack:** Python 3.12, `pytest` (existing dev dependency), `PyYAML` (new), `mkdocs-material` (new declared dependency, already used ad hoc), `mkdocs-redirects` (new), MkDocs's built-in `pymdownx.snippets` extension (already enabled) for the root-`CHANGELOG.md` embed. No new documentation framework, static-site generator, or JS toolchain is introduced.

---

## How this plan is organized

Phases 1–13 below match the phase numbering in the task brief exactly. Each phase contains one or more numbered tasks; each task is a sequence of checkbox steps with exact file paths, exact code, and exact commands. Commit after every task (not just every phase) — the commit commands are included as the final step of each task.

Run every command from the worktree root:

```bash
cd "/Volumes/Development HD/entraclaw-identity-research/.worktrees/docs-public-site"
```

All work happens on branch `docs/public-site-restructure` inside this worktree. Do not switch branches or touch other worktrees.

### One-time environment setup (do this before Task 1.1)

Per `AGENTS.md` Learning #36, worktree installs must use a worktree-local venv, never the parent repo's venv.

- [ ] **Step 1: Create a worktree-local virtual environment**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
```

- [ ] **Step 2: Verify the venv resolves inside this worktree**

```bash
.venv/bin/python3 -c "from entrabot import config; print(config.__file__)"
```

Expected: the printed path contains `.worktrees/docs-public-site`, not the parent repo's `.venv`. (This import will fail with `ModuleNotFoundError` until Step 3 installs the package — that is expected the first time.)

- [ ] **Step 3: Install the package in editable mode with dev + docs extras (docs extra is added in Task 2.1; until then use `dev` only)**

```bash
pip install -e ".[dev]"
```

---

## Phase 1: Failing structural docs tests + docs tooling dependency

**Files:**
- Create: `tests/docs/__init__.py`
- Create: `tests/docs/test_no_historical_prefixes.py`
- Create: `tests/docs/test_no_agent_attribution.py`
- Create: `tests/docs/test_nav_targets_exist.py`
- Create: `tests/docs/test_all_pages_in_nav.py`
- Create: `tests/docs/test_redirects.py`
- Create: `tests/docs/test_script_commands_manifest.py`
- Create: `tests/docs/test_legacy_paths_removed.py`
- Modify: `pyproject.toml`

These tests must be written and run **red** first (Task 1.1–1.8), against the *current* `docs/` tree, before any content moves. Later phases turn them green incrementally. This satisfies the plan's TDD requirement for docs structure.

### Task 1.1: Add docs/PyYAML dependencies to pyproject.toml

- [ ] **Step 1: Add a `docs` optional-dependency group**

Edit `pyproject.toml`, inside `[project.optional-dependencies]` (after the existing `dev = [...]` block and before `provisioning = [...]`):

```toml
docs = [
    "mkdocs-material>=9.5",
    "mkdocs-redirects>=1.2",
    "PyYAML>=6.0",
]
```

- [ ] **Step 2: Install the new extras**

```bash
pip install -e ".[dev,docs]"
```

- [ ] **Step 3: Verify PyYAML is importable**

```bash
python3 -c "import yaml; print(yaml.__version__)"
```

Expected: a version string, no `ModuleNotFoundError`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "docs: add mkdocs-material, mkdocs-redirects, PyYAML docs extras"
```

### Task 1.2: Shared docs-test helpers

**Files:**
- Create: `tests/docs/__init__.py`
- Create: `tests/docs/_helpers.py`

- [ ] **Step 1: Create the empty package marker file**

Create `tests/docs/__init__.py` with empty content (mirrors the convention in `tests/scripts/__init__.py`).

- [ ] **Step 2: Create shared helpers used by every test in this phase**

Create `tests/docs/_helpers.py`:

```python
"""Shared helpers for docs-structure tests.

These parse ``mkdocs.yml`` and ``docs/reference/scripts/commands.yml`` once
per test session and expose plain Python data structures (sets of relative
paths, flattened nav entries) so each test file stays a short list of
assertions.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"
MKDOCS_YML = REPO_ROOT / "mkdocs.yml"
COMMANDS_YML = DOCS_DIR / "reference" / "scripts" / "commands.yml"

# Historical-artifact filename prefixes that must never appear under docs/.
FORBIDDEN_PREFIXES = (
    "PLAN-",
    "SPEC-",
    "DESIGN-",
    "NEXT-",
    "TODO-",
    "AGENT-PROMPT-",
    "FOURPAGER-",
)

# Case-insensitive regex fragments that indicate agent-authorship or
# personal-review attribution rather than neutral repository voice.
FORBIDDEN_ATTRIBUTION_PATTERNS = (
    r"author:\s*(claude|codex|copilot|chatgpt|gpt|openai)",
    r"written by\s*(claude|codex|copilot|chatgpt|gpt|openai)",
    r"^\s*openai\s*:",
    r"generated by\s*(claude|codex|copilot|chatgpt|gpt|openai)",
)


def load_mkdocs_config() -> dict:
    with MKDOCS_YML.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def flatten_nav(nav_entry) -> list[str]:
    """Flatten a MkDocs ``nav:`` structure into a list of docs_dir-relative paths.

    MkDocs nav entries are either a bare string path, or a one-key mapping
    whose value is either a path or a nested list of the same shape.
    """
    paths: list[str] = []
    if isinstance(nav_entry, str):
        paths.append(nav_entry)
    elif isinstance(nav_entry, dict):
        for value in nav_entry.values():
            paths.extend(flatten_nav(value))
    elif isinstance(nav_entry, list):
        for item in nav_entry:
            paths.extend(flatten_nav(item))
    return paths


def all_nav_paths() -> set[str]:
    config = load_mkdocs_config()
    paths: list[str] = []
    for entry in config.get("nav", []):
        paths.extend(flatten_nav(entry))
    return set(paths)


def all_public_markdown_files() -> set[str]:
    """Every ``*.md`` file under ``docs/``, as a path relative to ``docs/``."""
    return {
        str(p.relative_to(DOCS_DIR)) for p in DOCS_DIR.rglob("*.md")
    }


def load_commands_manifest() -> list[dict]:
    if not COMMANDS_YML.exists():
        return []
    with COMMANDS_YML.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("commands", [])


def redirect_map() -> dict[str, str]:
    config = load_mkdocs_config()
    for plugin_entry in config.get("plugins", []):
        if isinstance(plugin_entry, dict) and "redirects" in plugin_entry:
            redirects_cfg = plugin_entry["redirects"] or {}
            return redirects_cfg.get("redirect_maps", {}) or {}
    return {}

- [ ] **Step 3: Run pytest to confirm the helper module imports cleanly (no tests yet)**

```bash
pytest tests/docs -v
```

Expected: `collected 0 items` (no test functions exist yet — this just proves `_helpers.py` has no syntax errors and `commands.yml` does not exist yet, so `load_commands_manifest()` returning `[]` is fine).

- [ ] **Step 4: Commit**

```bash
git add tests/docs/__init__.py tests/docs/_helpers.py
git commit -m "test: add shared helpers for docs structure tests"
```

### Task 1.3: Forbidden historical-prefix test (red)

**Files:**
- Create: `tests/docs/test_no_historical_prefixes.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run it and confirm it fails against the current tree**

```bash
pytest tests/docs/test_no_historical_prefixes.py -v
```

Expected: FAIL, listing files such as `architecture/PLAN-windows-port.md`, `architecture/NEXT-WhatsApp-lightweight-teams-chat.md`, etc. (This is the correct red state — Phase 9 turns it green.)

- [ ] **Step 3: Commit**

```bash
git add tests/docs/test_no_historical_prefixes.py
git commit -m "test: add failing check for historical-artifact filenames under docs/"
```

### Task 1.4: No agent-authorship attribution test (red or green)

**Files:**
- Create: `tests/docs/test_no_agent_attribution.py`

- [ ] **Step 1: Write the test**

```python
"""Public docs must read as neutral repository documentation, not as
notes authored/reviewed by a specific AI agent or model. Product names
used as client identifiers (e.g. 'Claude Code', 'Copilot CLI' as a
supported host) are fine; byline-style attribution or competitor
framing ("author: ChatGPT", "reviewed by Codex") is not.
"""

import re

from tests.docs._helpers import DOCS_DIR, FORBIDDEN_ATTRIBUTION_PATTERNS

COMPILED_PATTERNS = [
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in FORBIDDEN_ATTRIBUTION_PATTERNS
]


def test_no_agent_attribution_bylines():
    offenders: list[str] = []
    for md_file in DOCS_DIR.rglob("*.md"):
        text = md_file.read_text(encoding="utf-8")
        for pattern in COMPILED_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{md_file.relative_to(DOCS_DIR)}: {pattern.pattern}")
    assert offenders == [], (
        "Public docs must not carry agent-authorship attribution bylines: "
        f"{offenders}"
    )
```

- [ ] **Step 2: Run it**

```bash
pytest tests/docs/test_no_agent_attribution.py -v
```

Expected: PASS today (no existing docs page currently carries a byline pattern) — this is a regression guard, not a migration blocker. If it unexpectedly fails, inspect the listed file/pattern pair and remove the byline before proceeding.

- [ ] **Step 3: Commit**

```bash
git add tests/docs/test_no_agent_attribution.py
git commit -m "test: guard against agent-authorship attribution bylines in docs"
```

### Task 1.5: Nav-target-existence test (red)

**Files:**
- Create: `tests/docs/test_nav_targets_exist.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run it against the current nav**

```bash
pytest tests/docs/test_nav_targets_exist.py -v
```

Expected: PASS today (the current `mkdocs.yml` nav already matches the current tree). This test exists to stay green through every later phase — rerun it after every nav edit in Phases 3–10 to catch typos immediately.

- [ ] **Step 3: Commit**

```bash
git add tests/docs/test_nav_targets_exist.py
git commit -m "test: verify every mkdocs.yml nav path exists on disk"
```

### Task 1.6: All-pages-in-nav test (red)

**Files:**
- Create: `tests/docs/test_all_pages_in_nav.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run it against the current tree**

```bash
pytest tests/docs/test_all_pages_in_nav.py -v
```

Expected: FAIL, listing every current `docs/reference/scripts/*.md` and any other page missing from nav (check current `mkdocs.yml` — most existing pages are already listed, but confirm by running the command; whatever the tool reports is the authoritative red-state list, not a pre-enumerated one, since the exact orphan set depends on the tree at execution time).

- [ ] **Step 3: Commit**

```bash
git add tests/docs/test_all_pages_in_nav.py
git commit -m "test: require every public docs page to be listed in nav"
```

### Task 1.7: Redirect-mapping validation test (red until Phase 11)

**Files:**
- Create: `tests/docs/test_redirects.py`

- [ ] **Step 1: Write the failing test**

```python
"""Every mkdocs-redirects target must point at a page that is in the
current nav, and the plugin config itself must be present once Phase 2
adds it. Until Phase 2/11 land, this test fails on the missing plugin
config, which is the correct red state.
"""

from tests.docs._helpers import all_nav_paths, redirect_map


def test_redirects_plugin_is_configured():
    assert redirect_map(), (
        "mkdocs.yml must configure the `redirects` plugin with a non-empty "
        "redirect_maps table (see Phase 2 and Phase 11 of the implementation plan)"
    )


def test_every_redirect_target_is_a_current_nav_page():
    nav_paths = all_nav_paths()
    mapping = redirect_map()
    bad_targets = {
        old: new for old, new in mapping.items() if new not in nav_paths
    }
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
```

- [ ] **Step 2: Run it**

```bash
pytest tests/docs/test_redirects.py -v
```

Expected: FAIL on `test_redirects_plugin_is_configured` (no `redirects` plugin entry exists in `mkdocs.yml` yet). This turns green in Phase 11.

- [ ] **Step 3: Commit**

```bash
git add tests/docs/test_redirects.py
git commit -m "test: validate mkdocs-redirects mapping targets and sources"
```

### Task 1.8: Script-commands manifest validation test (red until Phase 8)

**Files:**
- Create: `tests/docs/test_script_commands_manifest.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run it**

```bash
pytest tests/docs/test_script_commands_manifest.py -v
```

Expected: FAIL on `test_manifest_has_exactly_42_commands` (`commands.yml` does not exist yet, so `load_commands_manifest()` returns `[]`). Turns green in Phase 8.

- [ ] **Step 3: Commit**

```bash
git add tests/docs/test_script_commands_manifest.py
git commit -m "test: validate commands.yml manifest against filesystem and pages"
```

### Task 1.9: Legacy-paths-removed test (red until Phase 9)

**Files:**
- Create: `tests/docs/test_legacy_paths_removed.py`

- [ ] **Step 1: Write the failing test**

```python
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
    "decisions",  # entire decisions/ directory moves to engineering-history/decisions
    "platform-learnings",  # entire directory renamed to platform-docs/
)


def test_legacy_paths_are_gone():
    still_present = [p for p in LEGACY_PATHS_THAT_MUST_NOT_EXIST if (DOCS_DIR / p).exists()]
    assert still_present == [], (
        f"These legacy docs/ paths must be migrated to engineering-history/ "
        f"or renamed and are still present: {still_present}"
    )
```

- [ ] **Step 2: Run it**

```bash
pytest tests/docs/test_legacy_paths_removed.py -v
```

Expected: FAIL, listing every path in `LEGACY_PATHS_THAT_MUST_NOT_EXIST` that currently exists (all of them, today). Turns green as Phases 6 and 9 complete.

- [ ] **Step 3: Confirm the full Phase 1 red suite together**

```bash
pytest tests/docs -v
```

Expected: several failures (prefixes, all-pages-in-nav, redirects, manifest, legacy-paths) and several passes (attribution, nav-targets-exist). This mixed red/green state is correct — it is the TDD baseline the rest of the plan turns fully green.

- [ ] **Step 4: Commit**

```bash
git add tests/docs/test_legacy_paths_removed.py
git commit -m "test: require legacy historical docs paths to be removed"
```

---

## Phase 2: PR docs validation; deploy only on main push

**Files:**
- Modify: `.github/workflows/docs.yml`
- Modify: `mkdocs.yml` (add `strict: true` builder-level setting is not a real MkDocs key; strictness is controlled by the `--strict` CLI flag used in CI, so no `mkdocs.yml` change is required here beyond what Phase 11 already does for the `redirects` plugin)

### Task 2.1: Restructure docs.yml into validate (PR+push) and deploy (main push only)

- [ ] **Step 1: Read the current workflow to confirm the trigger and job names before editing**

```bash
cat .github/workflows/docs.yml
```

- [ ] **Step 2: Replace the entire file content**

Replace the full contents of `.github/workflows/docs.yml` with:

```yaml
name: Docs

on:
  push:
    branches: [main]
    paths:
      - "docs/**"
      - "engineering-history/**"
      - "mkdocs.yml"
      - "pyproject.toml"
      - ".github/workflows/docs.yml"
  pull_request:
    paths:
      - "docs/**"
      - "engineering-history/**"
      - "mkdocs.yml"
      - "pyproject.toml"
      - ".github/workflows/docs.yml"

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: docs-${{ github.ref }}
  cancel-in-progress: true

jobs:
  validate:
    name: Validate docs (tests + strict build)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install package with docs + dev extras
        run: pip install -e ".[dev,docs]"
      - name: Run docs structure tests
        run: pytest tests/docs -v
      - name: Build site in strict mode
        run: mkdocs build --strict --site-dir site

  deploy:
    name: Deploy to GitHub Pages
    needs: validate
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install package with docs extras
        run: pip install -e ".[docs]"
      - name: Build site in strict mode
        run: mkdocs build --strict --site-dir site
      - name: Upload Pages artifact
        uses: actions/upload-pages-artifact@v3
        with:
          path: site
      - name: Deploy to GitHub Pages
        id: deployment
        uses: actions/deploy-pages@v4
```

- [ ] **Step 3: Validate the YAML is well-formed**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/docs.yml'))" && echo OK
```

Expected: `OK`.

- [ ] **Step 4: Run the docs tests locally exactly as CI will**

```bash
pytest tests/docs -v
```

Expected: same mixed red/green state as the end of Phase 1 (this task does not change docs content, only CI wiring).

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/docs.yml
git commit -m "ci: validate docs on every PR, deploy only on main push"
```

### Task 2.2: Confirm strict build fails today (expected — proves the gate works)

- [ ] **Step 1: Attempt a strict build against the unmigrated tree**

```bash
mkdocs build --strict --site-dir site 2>&1 | tail -n 40
```

Expected: this may currently pass or fail depending on whether today's `mkdocs.yml`/`docs/` tree already has broken links; either result is acceptable at this point in the plan — the goal of this step is only to record the current baseline so later phases' `mkdocs build --strict` runs can be compared against it. Do not attempt to fix any strict-mode warnings here; that happens naturally as Phases 3–11 rebuild the tree.

- [ ] **Step 2: Remove the local site/ build artifact (never commit it)**

```bash
rm -rf site
```

- [ ] **Step 3: Confirm `site/` is already git-ignored**

```bash
git check-ignore -v site || echo "site/ NOT ignored -- add it to .gitignore before proceeding"
```

If the `echo` branch prints, add `site/` to `.gitignore`, then:

```bash
git add .gitignore
git commit -m "chore: ignore local mkdocs build output"
```

---

## Phase 3: Build current public entry points, getting-started, guides, project pages

This phase rewrites/creates the pages that are not client docs, architecture docs, platform docs, script reference, or troubleshooting (those are Phases 4–8). It does not yet touch `mkdocs.yml` nav (Phase 10) — pages are created first and wired into nav together in Phase 10 so nav edits happen exactly once.

### Task 3.1: Rewrite docs/index.md as the neutral public Home page

**Files:**
- Modify: `docs/index.md`

- [ ] **Step 1: Replace the "Where to Start" and "Open Research Questions" sections**

The current `docs/index.md` links to historical/internal pages (`architecture/DESIGN-persona-sati-integration.md` which archives to `engineering-history/architecture/`, `decisions/006-remove-bot-gateway-mode.md` which archives to `engineering-history/decisions/` (ADRs are not published — see Task 3.5), `platform-learnings/*` at paths that rename, `SECURITY-DEBT-PROVISIONER-SECRET.md` which archives, `runbooks/hard-won-learnings.md` which splits into troubleshooting). Replace the whole file content with:

```markdown
# Entrabot Identity Research

**Source:** <https://github.com/microsoft/entrabot> · **License:** MIT

Entrabot is a Python MCP server that gives a device-local agent its own Entra **Agent ID** and **Agent User**. The agent signs in autonomously, sends and receives Teams messages from its own account, uses its mailbox and Microsoft 365 files, and writes audit events against its own object ID. It runs on macOS, Linux, and Windows and works with Claude Code, Copilot CLI, or any MCP-speaking client.

**All you need to get started is:**

- A Microsoft 365 development tenant where you can create app registrations and grant admin consent
- A license that includes Teams and Outlook (E3 or E5 dev tenant licenses work)
- Python 3.12 installed locally

The scripts take care of the rest: provisioning the Agent Identity Blueprint, Agent Identity, and Agent User in Entra; uploading a self-signed certificate; assigning the license; and configuring the local MCP server.

**Microsoft Entra Agent ID** and **Microsoft Agent 365** — which enable these experiences — went GA on 2026-05-01. Entrabot is the reference implementation that pulls those primitives together on a real device, today.

## Where to Start

- **New to the project?** Start with the [Quickstart](getting-started/quickstart.md)
- **Which client do I use?** See [Clients Overview](clients/overview.md)
- **Current status / what's shipped / what's next?** [Project Status](project/status.md)
- **Customizing the agent's prompt?** Read [Customizing the Body Prompt](guides/customizing-the-body-prompt.md)
- **How the system fits together?** Read [System Overview](architecture/system-overview.md)
- **Local vs. cloud storage?** Read [Storage Configuration and Migration](guides/storage-configuration.md)
- **MCP tool reference?** See [MCP Tools](reference/mcp-tools.md)
- **Script reference?** Browse [Scripts Overview](reference/scripts/index.md)
- **How tokens flow?** See [Identity and Token Flow](architecture/identity-and-token-flow.md)
- **Something not working?** Check [Troubleshooting](troubleshooting/index.md)
- **Platform deep dives (Agent IDs, Agent 365, Teams/Files Graph, OS keystores)?** Browse [Platform Docs](platform-docs/agent-id-blueprints-and-users.md)

## Supported Platforms and Clients

- **Operating systems:** macOS, Linux, Windows
- **MCP clients:** Claude Code, GitHub Copilot CLI, and any other MCP-speaking host — see [Clients Overview](clients/overview.md)
- **Microsoft 365 surfaces:** Teams chat, Outlook mail, OneDrive/SharePoint files (via Microsoft Agent 365 Work IQ)
```

- [ ] **Step 2: Confirm no forbidden-prefix or legacy links remain in the new file**

```bash
grep -nE "PLAN-|DESIGN-|NEXT-|TODO-|AGENT-PROMPT-|SECURITY-DEBT|runbooks/|platform-learnings/" docs/index.md
```

Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add docs/index.md
git commit -m "docs: rewrite Home page with current, neutral links"
```

### Task 3.2: Build the five approved Getting Started pages

The approved spec (`engineering-history/specs/2026-07-10-public-documentation-redesign.md`, "Getting Started") requires exactly five pages: Quickstart, Prerequisites, macOS and Linux installation, Windows installation, and First identity and health verification. Use these exact paths — there is no `installation.md` or `first-agent-user.md` page.

**Files:**
- Create: `docs/getting-started/prerequisites.md`
- Create: `docs/getting-started/macos-linux.md`
- Create: `docs/getting-started/windows.md`
- Create: `docs/getting-started/verify.md`
- Modify: `docs/getting-started/quickstart.md` (existing; fix two stale links — see Step 5)

- [ ] **Step 1: Create docs/getting-started/prerequisites.md**

```markdown
# Prerequisites

Entrabot requires:

- A Microsoft 365 development tenant where you can create app registrations and grant admin consent
- A license that includes Teams and Outlook (E3 or E5 dev tenant licenses work)
- Python 3.12 or newer
- `git`

## Clone and install

```bash
git clone https://github.com/microsoft/entrabot.git
cd entrabot
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

## Verify the install

```bash
pytest -v --tb=short
ruff check .
```

Both commands must pass before you provision an Agent Identity.

## Next step

Continue to platform-specific setup: [macOS and Linux](macos-linux.md) or [Windows](windows.md).
```

- [ ] **Step 2: Create docs/getting-started/macos-linux.md**

```markdown
# macOS and Linux Installation

Assumes you have completed [Prerequisites](prerequisites.md).

## 1. Install platform prerequisites

=== "macOS"

    ```bash
    ./scripts/prereqs-macos.sh
    ```

    Installs the Azure CLI and confirms Keychain access for certificate storage.

=== "Linux"

    Install Python 3.12+, the Azure CLI, `git`, and a Secret Service–compatible keyring (e.g. `gnome-keyring` or KWallet).

## 2. Create a fresh identity chain

```bash
# Replace "workstation" with a short unique label for this Agent User.
./scripts/setup.sh --new --with-upn-suffix=workstation
```

To attach this device to an existing Blueprint instead:

```bash
./scripts/setup.sh --use-blueprint=<blueprint-app-id>
```

Use `--agent-user-upn=<existing-upn>` or `--with-upn-suffix=<label>` when the Blueprint has multiple Agent Users and auto-discovery would be ambiguous. Run `./scripts/setup.sh --help` for storage, Work IQ, migration, and status options — see [scripts/setup.sh reference](../reference/scripts/setup/setup-sh.md) for the full option list.

## Next step

Continue to [Verify Your Agent Identity](verify.md).
```

- [ ] **Step 3: Create docs/getting-started/windows.md**

```markdown
# Windows Installation

Assumes you have completed [Prerequisites](prerequisites.md). Use PowerShell 7 (`pwsh`), not Windows PowerShell 5.1.

## 1. Install platform prerequisites

```powershell
.\scripts\prereqs-windows.ps1
```

Windows setup prefers a TPM-backed CNG key and falls back to a software-protected key when TPM provisioning is unavailable.

## 2. Create a fresh identity chain

```powershell
pwsh -File scripts/setup-windows.ps1 -NewChain -UpnSuffix workstation
```

For an existing Blueprint:

```powershell
pwsh -File scripts/setup-windows.ps1 -UseBlueprint <blueprint-app-id>
```

See [scripts/setup-windows.ps1 reference](../reference/scripts/setup/setup-windows-ps1.md) for the full option list.

## Next step

Continue to [Verify Your Agent Identity](verify.md).
```

- [ ] **Step 4: Create docs/getting-started/verify.md**

```markdown
# Verify Your Agent Identity

## What gets created in Entra

1. **Agent Identity Blueprint** — a certificate-backed app registration that represents the *kind* of agent (e.g., "entrabot on this device").
2. **BlueprintPrincipal** — a service principal for the Blueprint. Not auto-created by Entra; the setup script creates it explicitly.
3. **Agent Identity** — a federated-identity-credential (FIC) child of the Blueprint, representing this specific agent instance.
4. **Agent User** — a real Entra user object with a Teams and Outlook license, linked to the Agent Identity via a `user_fic` grant. This is the identity your agent authenticates as.

## Run the health check

```bash
./status.sh --health-only --strict
```

On Windows:

```powershell
pwsh -File status-windows.ps1 -HealthOnly -Strict
```

A healthy Agent User token has `idtyp=user`, the Agent User's `oid`, and Microsoft Graph as its audience. The Agent Identity and Agent User are separate objects and should both appear in status output.

## Inspect status directly

```bash
python3 scripts/show_agent_status.py
```

This prints the Agent Identity's object ID, the Agent User's UPN, license assignment status, and certificate expiry. See [scripts/show_agent_status.py reference](../reference/scripts/operations/show-agent-status-py.md) for the full output shape.

## Start the MCP host and send your first message

```bash
claude --dangerously-load-development-channels server:entrabot
```

Then ask the host to call `whoami` and send a Teams message. Both should report and act as the Agent User identity, not your human account. See [Identity and Token Flow](../architecture/identity-and-token-flow.md) for how the three-hop token exchange makes this possible.

## If something goes wrong

See [Troubleshooting](../troubleshooting/index.md).
```

- [ ] **Step 5: Fix the two stale links in the existing docs/getting-started/quickstart.md**

`quickstart.md` predates this redesign and still links to a pre-restructure script-docs path and the old monolithic runbook. Update its "Next steps" section:

```markdown
Old:
- [Setup script options](../reference/scripts/setup.md)
- [Token flow](../reference/token-flows.md)
- [Troubleshooting](../runbooks/hard-won-learnings.md)

New:
- [Prerequisites](prerequisites.md)
- [Setup script options](../reference/scripts/setup/setup-sh.md)
- [Token flow](../reference/token-flows.md)
- [Troubleshooting](../troubleshooting/index.md)
```

- [ ] **Step 6: Confirm all new/modified files parse as valid Markdown (no unbalanced code fences)**

```bash
python3 - << 'PY'
from pathlib import Path
for name in ("prerequisites.md", "macos-linux.md", "windows.md", "verify.md", "quickstart.md"):
    text = Path(f"docs/getting-started/{name}").read_text()
    assert text.count("```") % 2 == 0, f"{name} has unbalanced code fences"
print("OK")
PY
```

Expected: `OK`.

- [ ] **Step 7: Commit**

```bash
git add docs/getting-started/prerequisites.md docs/getting-started/macos-linux.md docs/getting-started/windows.md docs/getting-started/verify.md docs/getting-started/quickstart.md
git commit -m "docs: add prerequisites, macos-linux, windows, and verify getting-started pages"
```

### Task 3.3: Add docs/guides/configuration.md (source-mapped to src/entrabot/config.py)

**Files:**
- Create: `docs/guides/configuration.md`

**Source mapping:** every row below is read directly from `os.environ.get("ENTRABOT_*")` calls in `src/entrabot/config.py`.

- [ ] **Step 1: Create the page**

```markdown
# Configuration Reference

Entrabot is configured entirely through `ENTRABOT_*` environment variables, loaded once at MCP server boot by `src/entrabot/config.py`. There is no config file format beyond the `.env` written by `scripts/setup.sh`.

## Identity and tenant

| Variable | Purpose |
| --- | --- |
| `ENTRABOT_TENANT_ID` | Entra tenant GUID the Agent Identity and Agent User live in |
| `ENTRABOT_BLUEPRINT_APP_ID` | Application (client) ID of the Agent Identity Blueprint |
| `ENTRABOT_BLUEPRINT_OBJECT_ID` | Object ID of the Blueprint's service principal |
| `ENTRABOT_BLUEPRINT_CERT_THUMBPRINT` | SHA-1 thumbprint of the Blueprint's signing certificate (Windows Certificate Store lookup) |
| `ENTRABOT_BLUEPRINT_CERT_SHA1` | SHA-1 thumbprint used on macOS/Linux keystore lookups |
| `ENTRABOT_BLUEPRINT_KSP` | Windows Key Storage Provider name for the Blueprint certificate |
| `ENTRABOT_AGENT_ID` | Agent Identity's application ID |
| `ENTRABOT_AGENT_OBJECT_ID` | Agent Identity's service principal object ID |
| `ENTRABOT_AGENT_USER_ID` | Agent User's object ID |
| `ENTRABOT_AGENT_UPN` / `ENTRABOT_AGENT_USER_UPN` | Agent User's UPN (either name is accepted; `AGENT_UPN` is checked first) |
| `ENTRABOT_CLIENT_ID` | OAuth client ID used for delegated-mode MSAL flows |

## Sponsor / human owner

| Variable | Purpose |
| --- | --- |
| `ENTRABOT_HUMAN_USER_ID` / `ENTRABOT_HUMAN_USER_IDS` | Object ID(s) of the human sponsor(s) who receive DMs and approve promises |
| `ENTRABOT_HUMAN_UPN` / `ENTRABOT_HUMAN_UPNS` | UPN(s) of the sponsor(s); comma-separated for multiple |
| `ENTRABOT_HUMAN_USER_TENANT_IDS` | Tenant ID(s) for cross-tenant sponsors |
| `ENTRABOT_HUMAN_USER_MAILS` | Mail address(es) for the sponsor(s), used for email-based flows |
| `ENTRABOT_HUMAN_USER_TYPES` | Member vs. guest classification per sponsor, comma-separated in the same order as the ID/UPN lists |

## Runtime mode and behavior

| Variable | Purpose |
| --- | --- |
| `ENTRABOT_MODE` | `auto` (default), `delegated`, or `agent_user` — selects the auth flow at boot |
| `ENTRABOT_SKIP_PROVISIONING` | `true`/`false` — when true, skips the provisioning check at boot (used in tests and CI) |
| `ENTRABOT_LOG_LEVEL` | Python logging level, default `INFO` |
| `ENTRABOT_XPIA_WRAP_ENABLE` | `true`/`false` — enables cross-prompt-injection content wrapping (see [Security Boundaries](../architecture/security-boundaries.md)) |

## Storage backend

| Variable | Purpose |
| --- | --- |
| `ENTRABOT_KEEP_MEMORY_LOCAL` | `true` forces `LocalBackend` even if blob settings are present |
| `ENTRABOT_BLOB_ENDPOINT` | Azure Blob Storage account endpoint URL |
| `ENTRABOT_BLOB_CONTAINER` | Blob container name for operational memory |

See [Storage Configuration and Migration](storage-configuration.md) for how these three combine to select a backend at call time, and [Reference: Configuration](../reference/configuration.md) for the same table cross-linked from the API reference.
```

- [ ] **Step 2: Cross-check every variable name against config.py**

```bash
python3 - << 'PY'
import re
from pathlib import Path

config_text = Path("src/entrabot/config.py").read_text()
env_vars = set(re.findall(r'os\.environ\.get\("(ENTRABOT_[A-Z_]+)"', config_text))

doc_text = Path("docs/guides/configuration.md").read_text()
doc_vars = set(re.findall(r"`(ENTRABOT_[A-Z_]+)`", doc_text))

missing_from_doc = env_vars - doc_vars
extra_in_doc = doc_vars - env_vars
assert not missing_from_doc, f"config.py vars missing from doc: {missing_from_doc}"
assert not extra_in_doc, f"doc has vars not in config.py: {extra_in_doc}"
print("OK", len(env_vars), "vars matched")
PY
```

Expected: `OK 26 vars matched` (or the current count if `config.py` has changed since this plan was written — the assertion, not the printed number, is the pass/fail signal).

- [ ] **Step 3: Commit**

```bash
git add docs/guides/configuration.md
git commit -m "docs: add configuration guide sourced from config.py env vars"
```

### Task 3.4: Add remaining new guide pages (Teams/Chat, Email, Files/Work IQ, Identity Lifecycle)

**Files:**
- Create: `docs/guides/teams-and-chat-workflows.md`
- Create: `docs/guides/email-workflows.md`
- Create: `docs/guides/files-and-work-iq.md`
- Create: `docs/guides/identity-lifecycle.md`

**Source mapping:** `teams-and-chat-workflows.md` ← `src/entrabot/tools/teams.py`, `chat_cursors.py`, `dispatch.py`, `promises.py`, `wait_tool.py`; `email-workflows.md` ← `src/entrabot/tools/email.py`, `email_poll.py`, `daily_summary.py`; `files-and-work-iq.md` ← `src/entrabot/tools/files.py`, `src/entrabot/a365/*`; `identity-lifecycle.md` ← `src/entrabot/identity/` state machine + `scripts/create_entra_agent_ids.py` / `scripts/deprovision_entra_agent_identity.py`.

- [ ] **Step 1: Create docs/guides/teams-and-chat-workflows.md**

```markdown
# Teams and Chat Workflows

Entrabot has no default group chat. Every Teams tool requires an explicit `chat_id`, sourced from `create_chat`, the persisted `watched_chats` file, or the auto-discovery sweep over `/me/chats` (every 120 seconds).

## Sending and receiving messages

`send_teams_message` (see [MCP Tools](../reference/mcp-tools.md)) sends as the Agent User and, on non-channel-push hosts (Copilot CLI, Codex), blocks for a sponsor reply by default. On Claude Code, the reply arrives on the next turn via the background poll (every 5 seconds) pushing a `notifications/claude/channel` message. See [Sponsor DM Wait Pattern](../clients/overview.md#sponsor-dm-wait-pattern) for the host-gated behavior.

## Promises (deferred commitments)

`src/entrabot/tools/promises.py` tracks commitments the agent makes in chat ("I'll check on this and get back to you") so they survive across sessions and can be resolved or escalated later.

## Cursors and dedup

`src/entrabot/tools/chat_cursors.py` tracks the last-seen message per chat so the background poll and `watch_teams_replies` do not re-deliver the same message twice. The background poll and `watch_teams_replies` use **separate** dedup state (see [Platform Docs: Teams Graph API](../platform-docs/teams-graph-api.md)).

## Related reference

- [MCP Tools](../reference/mcp-tools.md)
- [Messaging and Channel Delivery](../architecture/messaging-and-delivery.md)
- [Troubleshooting: Teams and Email](../troubleshooting/teams-and-email.md)
```

- [ ] **Step 2: Create docs/guides/email-workflows.md**

```markdown
# Email Workflows

The email poll (every 60 seconds, `src/entrabot/tools/email_poll.py`) reads `/me/messages`, filters out Teams/M365 automated noise, and detects Purview-encrypted mail it cannot open. `src/entrabot/tools/email.py` exposes the read/send tools; `daily_summary.py` sends a 5pm PDT triage email of the day's interactions.

## Reading mail

Use the `read_email` MCP tool, or run `scripts/read_email.py` directly for a one-off check outside the MCP session — see [scripts/read_email.py reference](../reference/scripts/read-email-py.md).

## Daily summaries

The scheduler in `daily_summary.py` runs once per day and composes a digest from the interaction log (`src/entrabot/tools/interaction_log.py`). It does not require any additional configuration beyond a working Agent User mailbox.

## Related reference

- [MCP Tools](../reference/mcp-tools.md)
- [Troubleshooting: Teams and Email](../troubleshooting/teams-and-email.md)
```

- [ ] **Step 3: Create docs/guides/files-and-work-iq.md**

```markdown
# Files and Microsoft Agent 365 Work IQ

`src/entrabot/tools/files.py` exposes OneDrive/SharePoint file and comment operations over Microsoft Graph. `src/entrabot/a365/` implements the Microsoft Agent 365 Work IQ MCP provider boundary (`provider.py`, `catalog.py`, `manifest.py`, `mcp_client.py`) and a Word document adapter (`word.py`) that lets the agent read and comment on Word documents via Work IQ rather than raw Graph calls.

## When to use Files vs. Work IQ

Use `src/entrabot/tools/files.py` for direct Graph drive/item/comment operations. Use the Work IQ provider (`src/entrabot/a365/`) when the target document type has a Work IQ adapter (currently Word) — it handles the Agent 365 auth token exchange (`a365/tokens.py`) and error normalization (`a365/errors.py`) for you.

## Consent

Files-scope consent is granted via `scripts/grant_files_consent.py` — see its [reference page](../reference/scripts/grant-files-consent-py.md).

## Related reference

- [Platform Docs: Microsoft Agent 365](../platform-docs/microsoft-agent-365.md)
- [Platform Docs: Files Graph API](../platform-docs/files-graph-api.md)
```

- [ ] **Step 4: Create docs/guides/identity-lifecycle.md**

```markdown
# Identity Lifecycle and Deprovisioning

The Agent Identity progresses through a state machine in `src/entrabot/identity/`: `UNAUTHENTICATED` → `DELEGATED` → `PROVISIONING` → `AGENT_USER`. Provisioning is driven by `scripts/create_entra_agent_ids.py` (see its [reference page](../reference/scripts/create-entra-agent-ids-py.md)), which creates the Blueprint, BlueprintPrincipal, Agent Identity, and Agent User in order.

## Checking current state

```bash
python3 scripts/show_agent_status.py
```

## Rotating certificates

See [scripts/rotate_cert_windows.py reference](../reference/scripts/auth-and-certs/rotate-cert-windows-py.md) (Windows) or re-run `scripts/find_local_blueprint_cert.py` / `scripts/verify_blueprint_cert.py` (macOS/Linux) to confirm the current certificate before rotating manually.

## Deprovisioning

```bash
python3 scripts/deprovision_entra_agent_identity.py
```

This removes the Agent Identity and its Agent User from Entra. It does **not** delete local operational memory — clean that up separately with `scripts/teardown.sh` or `scripts/teardown-windows.ps1`. See both [reference pages](../reference/scripts/index.md) and [Troubleshooting: Migrations and Upgrades](../troubleshooting/migrations-and-upgrades.md) if deprovisioning fails partway through.
```

- [ ] **Step 5: Verify all four files have balanced code fences**

```bash
python3 - << 'PY'
from pathlib import Path
for name in ("teams-and-chat-workflows.md", "email-workflows.md", "files-and-work-iq.md", "identity-lifecycle.md"):
    text = Path(f"docs/guides/{name}").read_text()
    assert text.count("```") % 2 == 0, f"{name} has unbalanced code fences"
print("OK")
PY
```

- [ ] **Step 6: Commit**

```bash
git add docs/guides/teams-and-chat-workflows.md docs/guides/email-workflows.md docs/guides/files-and-work-iq.md docs/guides/identity-lifecycle.md
git commit -m "docs: add Teams, email, files/Work IQ, and identity lifecycle guides"
```

### Task 3.5: Add Project section pages (Status and Changelog only) and archive ADRs

Per the approved spec, the public Project section contains exactly two pages: Current Status and Changelog. ADRs are historical, point-in-time rationale — they move to `engineering-history/decisions/` alongside plans, specs, and investigations, and are never published. Their durable rationale is distilled into the functional architecture pages (Tasks 5.2 and 5.5 carry ADR-003 and ADR-005 forward as inline prose, not links).

**Files:**
- Move: `docs/engineering-status.md` → `docs/project/status.md`
- Create: `docs/project/changelog.md`
- Move: `docs/decisions/README.md`, `docs/decisions/001-obo-flows-for-device-agents.md`, `docs/decisions/002-agent-user-over-obo.md`, `docs/decisions/003-certificate-auth-over-client-secrets.md`, `docs/decisions/005-cloud-hosted-memory.md`, `docs/decisions/006-remove-bot-gateway-mode.md` → `engineering-history/decisions/` (same filenames; not published)

- [ ] **Step 1: Move engineering-status.md into the new project/ directory as status.md**

```bash
mkdir -p docs/project engineering-history/decisions
git mv docs/engineering-status.md docs/project/status.md
```

- [ ] **Step 2: Move ADRs into engineering-history/decisions/ — these are NOT published; they never appear under docs_dir or in mkdocs.yml's nav**

```bash
git mv docs/decisions/README.md engineering-history/decisions/README.md
git mv docs/decisions/001-obo-flows-for-device-agents.md engineering-history/decisions/001-obo-flows-for-device-agents.md
git mv docs/decisions/002-agent-user-over-obo.md engineering-history/decisions/002-agent-user-over-obo.md
git mv docs/decisions/003-certificate-auth-over-client-secrets.md engineering-history/decisions/003-certificate-auth-over-client-secrets.md
git mv docs/decisions/005-cloud-hosted-memory.md engineering-history/decisions/005-cloud-hosted-memory.md
git mv docs/decisions/006-remove-bot-gateway-mode.md engineering-history/decisions/006-remove-bot-gateway-mode.md
rmdir docs/decisions
```

- [ ] **Step 3: Create docs/project/changelog.md, embedding the root CHANGELOG.md via pymdownx.snippets**

```markdown
# Changelog

--8<-- "CHANGELOG.md"
```

- [ ] **Step 4: Confirm `pymdownx.snippets` has `base_path` set so the embed resolves from repo root (Phase 10 rebuilds all of mkdocs.yml, but note this requirement now)**

```bash
grep -A3 "pymdownx.snippets" mkdocs.yml
```

Expected: the current config lacks a `base_path` under `pymdownx.snippets`; record this — Task 10.1 must add:

```yaml
  - pymdownx.snippets:
      base_path: ["."]
```

- [ ] **Step 5: Confirm no public page links into the moved ADRs**

```bash
grep -rn "docs/decisions/\|project/decisions" docs/ mkdocs.yml
```

Expected: no output (Tasks 5.2 and 5.5 already carry ADR-003/ADR-005 rationale forward as prose, not links).

- [ ] **Step 6: Commit**

```bash
git add docs/project engineering-history/decisions
git commit -m "docs: move engineering status to project/status.md, archive ADRs (not published)"
```

---

## Phase 4: Replace client proposals with neutral client docs

**Files:**
- Create: `docs/clients/overview.md`
- Create: `docs/clients/claude-code.md`
- Create: `docs/clients/copilot-cli.md`
- Create: `docs/clients/other-hosts.md`
- Modify: `docs/clients/persona-sati-host-bootstrap.md`
- Removed from `docs/` (required historical migration; archived to `engineering-history/plans/claude-copilot-cli-channel-port.md` in Phase 9, not deleted — see Phase 9 for the exact `git mv` command; this phase only creates the replacement pages): `docs/claude-copilot-cli-channel-port.md`
- Delete (superseded duplicate working notes, after unique facts are folded into the replacement pages — see Phase 9 disposition table): `docs/claude-windows-port.md`, `docs/openai-copilot-cli-notifications.md`, `docs/openai-windows-agent-identity-port.md`

**Constraint carried over from research:** `tests/test_prompt_doctrine.py::test_bootstrap_doctrine_file_mentions_markers` (parametrized over `BOOTSTRAP_DOCTRINE_FILES`, which includes `docs/clients/persona-sati-host-bootstrap.md`) requires this file to keep containing the literal strings `bootstrap_session`, `reflect`, `recall`, `observe`, `FastMCP instructions`, `mind_contract_available`. Task 4.5 preserves all six.

### Task 4.1: Create docs/clients/overview.md

- [ ] **Step 1: Write the page**

```markdown
# Clients Overview

Entrabot's MCP server works with any MCP-speaking client. Product names below (Claude Code, GitHub Copilot CLI) identify *supported hosts*, not endorsements or comparisons — each page documents the host-specific behavior you need to know, not a ranking.

## Supported hosts

| Host | Channel-push replies | Sponsor DM wait behavior | Page |
| --- | --- | --- | --- |
| Claude Code | Yes — background poll pushes `notifications/claude/channel` | Never call `wait_for_sponsor_dm`; end the turn after `send_teams_message` | [Claude Code](claude-code.md) |
| GitHub Copilot CLI | No | `send_teams_message` auto-blocks and returns the reply inline as `sponsor_reply` | [Copilot CLI](copilot-cli.md) |
| Any other MCP client (Codex, custom hosts) | Host-dependent | Treated as non-channel-push by default (auto-block behavior) unless the host implements channel push itself | [Other Hosts](other-hosts.md) |

## Sponsor DM Wait Pattern

When a human sponsor says "ping me when X is done" or an equivalent, the agent: (1) confirms via `send_teams_message`, (2) does the work, (3) sends the completion update via `send_teams_message`. What happens next is **host-gated**, not a parameter the agent chooses:

- **Claude Code**: end the turn. The sponsor's reply arrives on the next turn through the background poll's channel push. Calling `wait_for_sponsor_dm` here blocks the session and is never correct.
- **Non-channel-push hosts** (Copilot CLI and others): `send_teams_message` blocks automatically and returns the sponsor's reply inline as `sponsor_reply`. No extra tool call is needed.
- `wait_for_sponsor_dm` exists only for the rare case where the operator explicitly says "block until they reply" mid-task.

This behavior is enforced server-side by host detection in `src/entrabot/mcp_server.py`, not by a tool parameter the model can override — see [Security Boundaries](../architecture/security-boundaries.md).

## Setting up a client

1. Provision an Agent Identity (see [Quickstart](../getting-started/quickstart.md)).
2. Point your MCP client at the local `entrabot` server per its `.mcp.json` (or equivalent) configuration.
3. If you also run persona-sati as a mind server, see [Persona-Sati Host Bootstrap](persona-sati-host-bootstrap.md).
```

- [ ] **Step 2: Commit**

```bash
git add docs/clients/overview.md
git commit -m "docs: add clients overview with host comparison table"
```

### Task 4.2: Create docs/clients/claude-code.md

- [ ] **Step 1: Write the page**

```markdown
# Claude Code

Claude Code is a channel-push host: the entrabot MCP server's background poll (every 5 seconds) delivers new inbound Teams messages via `notifications/claude/channel`, which Claude Code surfaces as a system reminder on your next turn.

## Sponsor DM wait pattern

Send your update with `send_teams_message`, then **end the turn**. Do not call `wait_for_sponsor_dm` — it blocks the session and freezes the conversation waiting for a reply that channel-push will deliver anyway. See [Clients Overview](overview.md#sponsor-dm-wait-pattern).

## Mind attachment (persona-sati)

If persona-sati is listed in `.mcp.json`, call `bootstrap_session()` before your first substantive answer or external tool call each session. See [Persona-Sati Host Bootstrap](persona-sati-host-bootstrap.md) for the full protocol and degraded-mode handling.

## Configuration

No Claude-Code-specific environment variables are required beyond the standard [Configuration Reference](../guides/configuration.md). Connect via `.mcp.json` pointing at the `entrabot` server.
```

- [ ] **Step 2: Commit**

```bash
git add docs/clients/claude-code.md
git commit -m "docs: add Claude Code client page"
```

### Task 4.3: Create docs/clients/copilot-cli.md

- [ ] **Step 1: Write the page**

```markdown
# GitHub Copilot CLI

Copilot CLI is not a channel-push host: it has no mechanism to receive an out-of-band `notifications/claude/channel` push between turns. Entrabot compensates with automatic blocking inline.

## Sponsor DM wait pattern

Send your update with `send_teams_message`. On Copilot CLI, this call **auto-blocks** and returns the sponsor's reply inline as the `sponsor_reply` field of the tool result — no separate wait call is needed. See [Clients Overview](overview.md#sponsor-dm-wait-pattern).

## Mind attachment (persona-sati)

If persona-sati is configured, call `bootstrap_session()` before your first substantive answer or external tool call. Copilot CLI does not automatically inject FastMCP `instructions=` into the system prompt, so the bootstrap call is the only reliable way the mind contract reaches the model. See [Persona-Sati Host Bootstrap](persona-sati-host-bootstrap.md).

## Configuration

No Copilot-CLI-specific environment variables are required beyond the standard [Configuration Reference](../guides/configuration.md).
```

- [ ] **Step 2: Commit**

```bash
git add docs/clients/copilot-cli.md
git commit -m "docs: add Copilot CLI client page"
```

### Task 4.4: Create docs/clients/other-hosts.md

- [ ] **Step 1: Write the page**

```markdown
# Other MCP Hosts

Entrabot is a standard MCP server and works with any spec-compliant MCP client (Codex, custom internal hosts, etc.), subject to two host-detection behaviors:

## Channel push

If your host has no mechanism analogous to Claude Code's `notifications/claude/channel`, entrabot treats it as a non-channel-push host by default: `send_teams_message` auto-blocks and returns the sponsor's reply inline as `sponsor_reply`, matching the [Copilot CLI](copilot-cli.md) behavior. If your host does implement a channel-push mechanism, entrabot detects host identity server-side (`src/entrabot/mcp_server.py`) — hosts not explicitly recognized default to the safer, auto-blocking path rather than assuming push support.

## Mind attachment (persona-sati)

The persona-sati bootstrap protocol is host-agnostic: call `bootstrap_session()` before your first substantive answer or external tool call, exactly as described in [Persona-Sati Host Bootstrap](persona-sati-host-bootstrap.md). It does not assume Claude-Code-specific behavior.

## Adding support for a new host

There is no host allowlist to edit — entrabot's behavior degrades safely (auto-block) for any unrecognized host. If your host needs channel-push detection added, see [MCP Runtime](../architecture/mcp-runtime.md) for where host detection lives.
```

- [ ] **Step 2: Commit**

```bash
git add docs/clients/other-hosts.md
git commit -m "docs: add other MCP hosts client page"
```

### Task 4.5: Update the DESIGN- link in persona-sati-host-bootstrap.md ahead of Phase 9's move

**Files:**
- Modify: `docs/clients/persona-sati-host-bootstrap.md`

- [ ] **Step 1: Confirm the required bootstrap markers are present before editing (regression baseline)**

```bash
python3 - << 'PY'
from pathlib import Path
text = Path("docs/clients/persona-sati-host-bootstrap.md").read_text()
markers = ["bootstrap_session", "reflect", "recall", "observe", "FastMCP instructions", "mind_contract_available"]
missing = [m for m in markers if m not in text]
assert not missing, f"Missing required bootstrap markers before edit: {missing}"
print("OK: all markers present")
PY
```

- [ ] **Step 2: Repoint the DESIGN- link to its Phase 9 destination (`architecture/system-overview.md` gains the mind-body split content as part of Phase 5; the historical DESIGN doc itself moves to `engineering-history/architecture/`)**

```
Old: For the design discussion of the body-vs-mind split — what changes when an agent's identity, memory, and behavioral rules live in a separate process, and why that's load-bearing for agent autonomy — see [`DESIGN: Persona-Sati Integration`](../architecture/DESIGN-persona-sati-integration.md).

New: For the design discussion of the body-vs-mind split — what changes when an agent's identity, memory, and behavioral rules live in a separate process, and why that's load-bearing for agent autonomy — see [System Overview](../architecture/system-overview.md#mind-body-split).
```

- [ ] **Step 3: Re-run the marker check to confirm the edit did not remove any required string**

```bash
python3 - << 'PY'
from pathlib import Path
text = Path("docs/clients/persona-sati-host-bootstrap.md").read_text()
markers = ["bootstrap_session", "reflect", "recall", "observe", "FastMCP instructions", "mind_contract_available"]
missing = [m for m in markers if m not in text]
assert not missing, f"Missing required bootstrap markers after edit: {missing}"
assert "DESIGN-persona-sati-integration" not in text, "stale DESIGN- link still present"
print("OK")
PY
```

- [ ] **Step 4: Run the existing prompt-doctrine test to confirm it still passes**

```bash
pytest tests/test_prompt_doctrine.py -v -k persona_sati
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add docs/clients/persona-sati-host-bootstrap.md
git commit -m "docs: repoint persona-sati bootstrap link ahead of DESIGN doc archival"
```

---

## Phase 5: Present-tense architecture pages from source

**Files:**
- Modify: `docs/architecture/system-overview.md` (add a `## Mind-Body Split` section, linked from Task 4.5)
- Keep as-is (already current, already under `docs/architecture/layers/`): `docs/architecture/layers/audit.md`, `auth.md`, `platform.md`, `teams.md`
- Create: `docs/architecture/identity-and-token-flow.md`
- Create: `docs/architecture/mcp-runtime.md`
- Create: `docs/architecture/messaging-and-delivery.md`
- Create: `docs/architecture/storage-and-memory.md`
- Create: `docs/architecture/security-boundaries.md` (merges `docs/architecture/enforcement-flow.md`, archived in Phase 9)
- Create: `docs/architecture/windows-and-platforms.md`

This produces exactly 8 architecture nav entries (System Overview; Components — a 4-page subgroup; Identity and Token Flow; MCP Runtime; Messaging and Channel Delivery; Storage and Memory; Security Boundaries; Cross-Platform and Windows Architecture), matching the spec's Architecture section item count.

### Task 5.1: Add the Mind-Body Split section to system-overview.md

**Files:**
- Modify: `docs/architecture/system-overview.md`

- [ ] **Step 1: Append a new section at the end of the file**

```markdown

## Mind-Body Split

Entrabot is the **body**: the Teams/email/files interface, identity and audit layers, and MCP tool surface. Personality, long-term memory, and behavioral cognition are served by a separate MCP server, **persona-sati**, when configured. The body prompt (`prompts/agent_system.md` + `prompts/anatomy/*.md`) loads first and is non-overridable; persona-sati's output layers on top and can never override body security or channel-discipline rules. Without persona-sati attached, entrabot runs in body-only mode: Teams tools, identity, and audit work normally, but personality and memory features are unavailable. See [Persona-Sati Host Bootstrap](../clients/persona-sati-host-bootstrap.md) for the session-start protocol a host must follow to reach the mind.
```

- [ ] **Step 2: Confirm the anchor `#mind-body-split` resolves (MkDocs auto-generates anchors from headings; verify the heading text matches exactly what Task 4.5 links to)**

```bash
grep -n "^## Mind-Body Split" docs/architecture/system-overview.md
```

Expected: one match.

- [ ] **Step 3: Commit**

```bash
git add docs/architecture/system-overview.md
git commit -m "docs: add mind-body split section to system overview"
```

### Task 5.2: Create docs/architecture/identity-and-token-flow.md

**Source mapping:** `src/entrabot/auth/` (certificate JWT builder, MSAL delegated), `src/entrabot/identity/` (state machine), `src/entrabot/tools/teams.py::acquire_agent_user_token` (three-hop flow), `src/entrabot/a365/tokens.py` (parallel Agent 365 hop), `engineering-history/decisions/003-certificate-auth-over-client-secrets.md` (historical ADR-003, not published — its rationale is distilled inline below).

- [ ] **Step 1: Write the page**

```markdown
# Identity and Token Flow

## The three-hop flow

Entrabot's Agent User authentication is a single function, `acquire_agent_user_token` in `src/entrabot/tools/teams.py`, chaining three token acquisitions with no human in the loop:

1. **Hop 1 — Blueprint token.** The Agent Identity Blueprint authenticates with a certificate-based JWT assertion (`src/entrabot/auth/`), private key held in the OS keystore (see [Windows and Platforms](windows-and-platforms.md)). This is a `client_credentials` grant producing a Blueprint app token.
2. **Hop 2 — Agent Identity token.** The Agent Identity exchanges the Blueprint token as a federated-identity-credential (FIC) assertion for its own `client_credentials` token.
3. **Hop 3 — Agent User token.** The Agent Identity token is exchanged via a `user_fic` grant for a delegated token with `idtyp=user`, scoped to Teams/Exchange/OneDrive.

A parallel fourth hop (`acquire_agent_user_storage_token` in `src/entrabot/a365/tokens.py` and the storage backend) exchanges against `https://storage.azure.com/.default` for Azure Blob Storage access when cloud memory is enabled.

## Certificate-based JWT assertions

Client secrets are never used for Hop 1. `src/entrabot/auth/` builds a JWT assertion signed by a private key that never leaves the OS keystore (Keychain on macOS, Certificate Store/TPM on Windows, Secret Service/Keyring on Linux). This design was chosen over client secrets (historical ADR-003, archived at `engineering-history/decisions/003-certificate-auth-over-client-secrets.md`, not published) because secrets are long-lived, transferable, and routinely leak via logs, config files, and CI variables, whereas a private key bound to the OS keystore never leaves the device and cannot be exfiltrated by copying a string.

## Token refresh

`src/entrabot/mcp_server.py` implements two refresh strategies:

- `_ensure_valid_token()` — eager refresh at a 55-minute threshold, called before scheduled background work.
- `_with_token_retry()` — lazy refresh, catches a 401 response and retries once with a fresh token.

## Error handling

Every token response is checked for an `"error"` key before `"access_token"` is accessed — Entra returns error dictionaries, not exceptions, on failure. This check is load-bearing across all three hops and the storage hop.

## Identity state machine

`src/entrabot/identity/` models progressive identity: `UNAUTHENTICATED` → `DELEGATED` → `PROVISIONING` → `AGENT_USER`. See [Identity Lifecycle and Deprovisioning](../guides/identity-lifecycle.md) for the operator-facing walkthrough of moving through these states.

## Delegated mode (MSAL)

When `ENTRABOT_MODE=delegated`, `src/entrabot/auth/` falls back to interactive MSAL auth using the human's own token, and outbound messages are prefixed `[EntraBot]` to distinguish agent-sent from human-sent messages within the same identity. This mode exists for tenants without Agent User provisioning access.
```

- [ ] **Step 2: Commit**

```bash
git add docs/architecture/identity-and-token-flow.md
git commit -m "docs: add identity and token flow architecture page"
```

### Task 5.3: Create docs/architecture/mcp-runtime.md

**Source mapping:** `src/entrabot/mcp_server.py` (FastMCP server, background tasks, host detection), `src/entrabot/efferent_copy.py` (opt-in observe side-channel).

- [ ] **Step 1: Write the page**

```markdown
# MCP Runtime

`src/entrabot/mcp_server.py` boots a FastMCP server exposing entrabot's Teams/email/files/identity tools, and starts background tasks eagerly in `agent_user` mode:

- Teams chat poll — every 5 seconds
- Email poll — every 60 seconds
- Chat auto-discovery over `/me/chats` — every 120 seconds
- Daily summary scheduler — 5pm PDT

## Host detection

`send_teams_message`'s auto-block-vs-channel-push behavior (see [Clients Overview](../clients/overview.md)) is determined by server-side host detection in `mcp_server.py`, never by a tool parameter — the codebase's rule is that LLMs will override behavioral switches exposed as parameters, so any such switch must be an env var or server-side detection, not a callable argument.

## Efferent-copy dispatch (opt-in)

When `EFFERENT_COPY_ENABLE=1` is set, `src/entrabot/efferent_copy.py`'s `discover_sinks()` enumerates MCP peers listed alongside entrabot and filters to those exposing a schema-compatible `observe(tool_name, args[, result])` tool. `install_into_fastmcp` then wraps every registered tool's function with pre/post `observe` calls, fire-and-forget with a 250ms per-sink timeout; failures are logged and swallowed. `observe` itself is never wrapped, and tool return values are byte-for-byte unchanged regardless of how many sinks are attached. Setting `EFFERENT_COPY_DISABLE=1` forces registration off even when the enable flag is present. Discovery is schema-based — there are no peer-specific names, URLs, or tokens in the middleware.

## Instructions loading

`mcp_server.py::_load_agent_instructions` composes the FastMCP `instructions=` field from the body prompt (`prompts/agent_system.md` + `@include`d `prompts/anatomy/*.md`) plus an optional persona fetched from a remote MCP when `PERSONA_SATI_MCP_URL` and `PERSONA_SATI_MCP_TOKEN_COMMAND` are set. Because most MCP clients do not inject `instructions=` into the LLM system prompt, this composed text alone is not sufficient — see [Persona-Sati Host Bootstrap](../clients/persona-sati-host-bootstrap.md) for the explicit bootstrap-call protocol that compensates.
```

- [ ] **Step 2: Commit**

```bash
git add docs/architecture/mcp-runtime.md
git commit -m "docs: add MCP runtime architecture page"
```

### Task 5.4: Create docs/architecture/messaging-and-delivery.md

**Source mapping:** `docs/architecture/NEXT-WhatsApp-lightweight-teams-chat.md` (landed feature, archived in Phase 9 — its accurate content becomes this page's source), `src/entrabot/tools/teams.py`, `dispatch.py`, `chat_cursors.py`.

- [ ] **Step 1: Read the source material before writing (confirm present-tense facts, not proposal language)**

```bash
sed -n '1,80p' docs/architecture/NEXT-WhatsApp-lightweight-teams-chat.md
```

- [ ] **Step 2: Write the page, converting the landed proposal into present-tense fact**

```markdown
# Messaging and Channel Delivery

Entrabot has no default group chat. Every Teams tool requires an explicit `chat_id` — this is deliberate: it prevents an agent from broadcasting to an unintended chat by falling back to a hardcoded default. Chats become known to entrabot through exactly three paths:

1. `create_chat` — the agent creates a new 1:1 or group chat and its ID is returned immediately.
2. The persisted `watched_chats` file — chats explicitly registered for polling.
3. Auto-discovery — a sweep over `/me/chats` every 120 seconds registers any chat not already in `watched_chats`.

## Multi-tenant lightweight chat

Entrabot supports lightweight, delegated-mode chat across tenant boundaries without provisioning a full Agent User in each tenant. `src/entrabot/tools/dispatch.py` routes outbound messages appropriately for the current auth mode; delegated-mode messages are prefixed `[EntraBot]` so recipients can distinguish agent-originated from human-originated messages sent from the same underlying account.

## Delivery and dedup

The background poll and `watch_teams_replies` intentionally use **separate** dedup state in `src/entrabot/tools/chat_cursors.py` — merging them caused missed and duplicate deliveries in earlier iterations (see [Platform Docs: Teams Graph API](../platform-docs/teams-graph-api.md) for the underlying Graph API constraint that makes client-side filtering necessary in the first place).

## Channel push to Claude Code

The background poll pushes new inbound messages to Claude Code via `notifications/claude/channel`. Other hosts do not receive this push and instead rely on `send_teams_message`'s auto-blocking behavior — see [Clients Overview](../clients/overview.md).
```

- [ ] **Step 3: Commit**

```bash
git add docs/architecture/messaging-and-delivery.md
git commit -m "docs: add messaging and channel delivery architecture page"
```

### Task 5.5: Create docs/architecture/storage-and-memory.md

**Source mapping:** `src/entrabot/storage/{backend,blob,migration,persona}.py`, `docs/guides/storage-configuration.md` (existing, stays — cross-link, do not duplicate its full content), `engineering-history/decisions/005-cloud-hosted-memory.md` (historical ADR-005, not published — its phase history is distilled inline below).

- [ ] **Step 1: Write the page**

```markdown
# Storage and Memory

Two independent memory systems exist side by side:

1. **Agent operational memory** — interaction log, daily summaries, watched-chats list, email cursor. Written by the entrabot MCP server itself (`src/entrabot/tools/interaction_log.py` and related modules).
2. **Mind memory** (persona-sati) — personality, relationships, philosophy, running context. Owned entirely by persona-sati when attached; entrabot never writes to it directly.

## Backend resolution (operational memory only)

`src/entrabot/storage/backend.py`'s `get_backend()` resolves a `MemoryBackend` at call time, not at boot, from three env vars in this precedence order:

1. `ENTRABOT_KEEP_MEMORY_LOCAL=true` → `LocalBackend`
2. `ENTRABOT_BLOB_ENDPOINT` + `ENTRABOT_BLOB_CONTAINER` set → `BlobBackend`
3. Neither → `LocalBackend` (the default; cloud storage is opt-in)

## Cloud memory

When enabled via `./scripts/setup.sh --use-cloud-memory`, operational memory is written to Azure Blob Storage through a parallel storage-scope token hop (`acquire_agent_user_storage_token`, see [Identity and Token Flow](identity-and-token-flow.md)). `src/entrabot/storage/blob.py` implements the async client (put/get/list/delete/exists, ETag-based optimistic concurrency, 401 → `TokenExpiredError`). `src/entrabot/storage/migration.py` provides an idempotent, source-preserving migration from local files to blob storage, run automatically by `scripts/setup.sh` when switching backends. This capability shipped in phases (resource provisioning, RBAC scoped to the Agent User, idempotent migration with a fallback to local storage on failure — historical ADR-005, archived at `engineering-history/decisions/005-cloud-hosted-memory.md`, not published); see [Storage Configuration and Migration](../guides/storage-configuration.md) for the operator walkthrough.

## Persona-sati memory (mind, not body)

`src/entrabot/storage/persona.py`'s `PersonaBackend` exists for historical migration purposes only (`scripts/claude_memory_sync.py` remains as a manual one-off migration tool, not part of the supported command catalog — see [Reference: Scripts Overview](../reference/scripts/index.md)). Ongoing mind-memory reads/writes go through persona-sati's own MCP tools (`write_memory_file`, `read_memory_file`, `refresh_persona`), never through entrabot's storage backends.
```

- [ ] **Step 2: Commit**

```bash
git add docs/architecture/storage-and-memory.md
git commit -m "docs: add storage and memory architecture page"
```

### Task 5.6: Create docs/architecture/security-boundaries.md (merges enforcement-flow.md)

**Source mapping:** `docs/architecture/enforcement-flow.md` (existing, accurate — becomes source material, archived in Phase 9), `src/entrabot/audit/`, `src/entrabot/security/xpia.py`, `ENTRABOT_XPIA_WRAP_ENABLE` from Task 3.3.

- [ ] **Step 1: Read the existing enforcement-flow.md before merging its content**

```bash
cat docs/architecture/enforcement-flow.md
```

- [ ] **Step 2: Write the merged page (fold in enforcement-flow.md's accurate content plus XPIA content wrapping)**

```markdown
# Security Boundaries

## Audit-first design

Every agent action that touches a resource emits an audit event, in `src/entrabot/audit/`, before the action returns to the caller. If the audit write fails, the action does not proceed — security paths fail closed. This is enforced structurally: tool functions call the audit layer before performing the side-effecting Graph/Teams call, not after.

## Attribution

Every agent resource access is attributed to the Agent Identity's object ID, never to the human sponsor's identity, even in delegated mode (where outbound messages carry a `[EntraBot]` prefix specifically so audit trails and recipients can distinguish agent-originated activity from the human's own).

## Cross-prompt injection (XPIA) content wrapping

`src/entrabot/security/xpia.py` wraps untrusted external content (inbound Teams messages, email bodies, file contents fetched via Graph or Work IQ) before it reaches the model context, marking it as data rather than instructions. This is controlled by `ENTRABOT_XPIA_WRAP_ENABLE` (see [Configuration Reference](../guides/configuration.md)).

## Secrets and logging

Certificate private keys never leave the OS keystore (see [Windows and Platforms](windows-and-platforms.md)). Tokens and secrets never appear in logs — sensitive dataclass/pydantic fields override `__repr__` to redact their values.

## Behavioral switches are never model-controlled

Any behavior that affects blocking, waiting, or validation (see [MCP Runtime](mcp-runtime.md)'s host-detection discussion) is controlled server-side by env var or host detection, never exposed as a tool parameter an LLM could set to bypass it.
```

- [ ] **Step 3: Commit**

```bash
git add docs/architecture/security-boundaries.md
git commit -m "docs: add security boundaries page, merging enforcement-flow content"
```

### Task 5.7: Create docs/architecture/windows-and-platforms.md

**Source mapping:** `src/entrabot/platform/` (`CredentialStore` protocol + macOS/Linux/Windows implementations), `docs/architecture/PLAN-windows-port.md` (archived in Phase 9 — landed content becomes source material here), `docs/platform-learnings/platform-{macos,linux,windows}.md` (renamed in Phase 6, cross-linked not duplicated).

**Important constraint (from the task brief):** this page must **not** claim the MXC (Microsoft Execution Containers) sandbox is shipped — confirmed by research that no `src/entrabot/sandbox` package exists in this branch.

- [ ] **Step 1: Confirm the sandbox package does not exist, to avoid an inaccurate claim**

```bash
test -d src/entrabot/sandbox && echo "EXISTS -- update this page's sandbox claim" || echo "confirmed absent"
```

Expected: `confirmed absent`.

- [ ] **Step 2: Write the page**

```markdown
# Cross-Platform and Windows Architecture

Entrabot runs on macOS, Linux, and Windows. Platform differences are isolated behind a single abstraction: the `CredentialStore` protocol in `src/entrabot/platform/`, exposing `store()`, `retrieve()`, and `delete()` for certificate private keys.

## Credential storage per OS

| OS | Backend | Notes |
| --- | --- | --- |
| macOS | Keychain | via `keyring` |
| Linux | Secret Service (gnome-keyring, KWallet) | via `keyring`; requires a running Secret Service provider |
| Windows | Certificate Store / TPM | via the Windows Key Storage Provider named in `ENTRABOT_BLUEPRINT_KSP` |

See [Platform Docs](../platform-docs/agent-id-blueprints-and-users.md) for OS-specific setup and troubleshooting detail, and [scripts/prereqs-windows.ps1](../reference/scripts/prereqs-windows-ps1.md) / [scripts/prereqs-macos.sh](../reference/scripts/prereqs-macos-sh.md) for the setup scripts.

## Windows-specific tooling

Windows has parallel setup, teardown, and certificate scripts (`scripts/setup-windows.ps1`, `scripts/setup-windows.cmd`, `scripts/teardown-windows.ps1`, `scripts/deploy-windows.ps1`, `scripts/generate_windows_cert.py`, `scripts/rotate_cert_windows.py`) because certificate handling and service installation differ fundamentally from the Unix keychain model. See [Reference: Scripts Overview](../reference/scripts/index.md) for the full list.

## Sandboxing status

Microsoft Execution Containers (MXC), an OS-level sandboxing mechanism, is **research territory only** on this branch — there is no `src/entrabot/sandbox` package, and no shipped code path routes agent actions through an MXC container. Do not assume sandboxing is enforced; the audit and attribution guarantees in [Security Boundaries](security-boundaries.md) do not depend on it.
```

- [ ] **Step 3: Confirm the "no sandbox package" claim stays true at doc-review time (rerun before merging the PR in Phase 13, not just now)**

```bash
test -d src/entrabot/sandbox && echo "FAIL: sandbox now exists, rewrite this page" || echo "OK"
```

- [ ] **Step 4: Commit**

```bash
git add docs/architecture/windows-and-platforms.md
git commit -m "docs: add cross-platform and Windows architecture page"
```

---

## Phase 6: Rename/rewrite Platform Learnings into Platform Docs

**Files:**
- `git mv docs/platform-learnings/` → `docs/platform-docs/` (directory rename)
- Keep as-is (already current, already neutral) inside the renamed directory: `agent-id-blueprints-and-users.md`, `entra-agent-users.md`, `microsoft-agent-365.md`, `teams-graph-api.md`, `files-graph-api.md`, `platform-macos.md`, `platform-linux.md`, `platform-windows.md`
- Create: `docs/platform-docs/delegated-msal-auth.md` (replaces `msal-entra-agent-ids.md`)
- Create: `docs/platform-docs/mcp-hosts-and-transports.md` (replaces `mcp-messaging-servers.md` + `mcp-close-the-loop.md`)
- Archive to `engineering-history/research/`: `agent-memory-systems.md`, `copilot-extensibility.md`, `github-cli.md`, `github-copilot-extensions.md`, `teams-bot-framework.md`, `teams-toolkit.md`, `mxc-windows-sandbox.md` (exact `git mv` commands are in Phase 9, which performs all archival moves together so nothing is moved twice)

### Task 6.1: Rename the directory and the 8 kept files (git mv preserves history; content unchanged in this task)

- [ ] **Step 1: Rename the directory**

```bash
git mv docs/platform-learnings docs/platform-docs
```

This single `git mv` on the directory carries all 18 files along, including the ones this phase will further transform or Phase 9 will archive out — subsequent steps operate on the new `docs/platform-docs/` path.

- [ ] **Step 2: Confirm the 8 files that need no content change are present at their new path**

```bash
for f in agent-id-blueprints-and-users.md entra-agent-users.md microsoft-agent-365.md teams-graph-api.md files-graph-api.md platform-macos.md platform-linux.md platform-windows.md; do
  test -f "docs/platform-docs/$f" && echo "OK: $f" || echo "MISSING: $f"
done
```

Expected: `OK` for all 8.

- [ ] **Step 3: Commit**

```bash
git add -A docs/platform-docs docs/platform-learnings 2>/dev/null
git commit -m "docs: rename platform-learnings/ to platform-docs/"
```

### Task 6.2: Create docs/platform-docs/delegated-msal-auth.md (replaces msal-entra-agent-ids.md)

- [ ] **Step 1: Read the file being replaced for accurate source content**

```bash
cat docs/platform-docs/msal-entra-agent-ids.md
```

- [ ] **Step 2: Write the replacement page, keeping every load-bearing constraint from the original but in the Platform Docs voice (current-state facts, not a supplementary "reading before X" framing)**

```markdown
# Delegated Mode: MSAL Authentication

Delegated mode (`ENTRABOT_MODE=delegated`) uses MSAL for interactive, human-in-the-loop authentication instead of the certificate-based three-hop Agent User flow. It exists for tenants where Agent User provisioning is unavailable or not yet granted.

## Token acquisition

`src/entrabot/auth/` performs an MSAL interactive auth against the human's own account, using a localhost redirect by default and falling back to device code flow when a browser cannot be opened (headless environments, remote sessions).

## Provisioning differences vs. Agent User mode

Delegated mode does not create a Blueprint, BlueprintPrincipal, Agent Identity, or Agent User — it reuses the human's existing identity and app registration. Outbound Teams messages are prefixed `[EntraBot]` so recipients and audit logs can distinguish agent-originated messages from the human's own, since both share the same underlying account.

## When to use delegated mode over Agent User mode

Use delegated mode only when Agent User provisioning access is unavailable. It provides weaker attribution guarantees (see [Security Boundaries](../architecture/security-boundaries.md)) because actions are not tied to a dedicated object ID.

## Related

- [Identity and Token Flow](../architecture/identity-and-token-flow.md) — the certificate-based Agent User flow this mode is an alternative to.
```

- [ ] **Step 3: Delete the replaced file (its accurate content is now folded into the new page; nothing left to archive separately)**

```bash
git rm docs/platform-docs/msal-entra-agent-ids.md
```

- [ ] **Step 4: Commit**

```bash
git add docs/platform-docs/delegated-msal-auth.md
git commit -m "docs: replace msal-entra-agent-ids.md with delegated-msal-auth.md"
```

### Task 6.3: Create docs/platform-docs/mcp-hosts-and-transports.md (replaces mcp-messaging-servers.md + mcp-close-the-loop.md)

- [ ] **Step 1: Read both files being replaced for accurate source content**

```bash
cat docs/platform-docs/mcp-messaging-servers.md docs/platform-docs/mcp-close-the-loop.md
```

- [ ] **Step 2: Write the merged replacement page**

```markdown
# MCP Hosts and Transports

Entrabot's MCP server (`src/entrabot/mcp_server.py`) is transport-agnostic at the FastMCP layer, but host behavior differs in two ways that matter operationally: whether the host supports channel-push notifications, and how the host's lifecycle handles a dropped connection.

## Transports

FastMCP supports stdio and SSE transports. Local single-user sessions (Claude Code, Copilot CLI) use stdio; remote/shared deployments (e.g., a persona-sati mind server) use SSE. See `.mcp.json` / `.mcp.json.example` for the dual-transport configuration pattern used when both entrabot and persona-sati are attached.

## Closing the loop: background poll vs. request/response

Background tasks (Teams poll, email poll, chat auto-discovery, daily summary) run independently of any single request/response cycle. On channel-push hosts, their output reaches the model via `notifications/claude/channel`; on non-channel-push hosts, the same information is not proactively pushed and only surfaces the next time a tool call returns it (e.g., `send_teams_message`'s auto-block `sponsor_reply`). This is why host detection (see [MCP Runtime](../architecture/mcp-runtime.md)) exists: the same background work needs a different delivery mechanism depending on what the host can receive.

## Known operational issue: MCP disconnects under sustained load

Entrabot's MCP server has been observed to disconnect after 2–10 minutes of sustained activity. Two amplifying causes have been fixed; the root cause is not yet identified. See [Troubleshooting: MCP Connectivity](../troubleshooting/mcp-connectivity.md) for current mitigation steps and the open-investigation status.
```

- [ ] **Step 3: Delete the two replaced files**

```bash
git rm docs/platform-docs/mcp-messaging-servers.md docs/platform-docs/mcp-close-the-loop.md
```

- [ ] **Step 4: Commit**

```bash
git add docs/platform-docs/mcp-hosts-and-transports.md
git commit -m "docs: replace mcp-messaging-servers.md and mcp-close-the-loop.md with mcp-hosts-and-transports.md"
```

---

## Phase 7: Rebuild functional reference; task-oriented troubleshooting

**Files:**
- Move: `docs/reference/api/mcp-tools.md` → `docs/reference/mcp-tools.md` (canonical path; see Task 7.0 — the existing `docs/reference/mcp-tools.md` is only a 5-line redirect stub today and is overwritten by this move)
- Create: `docs/reference/configuration.md` (functional reference version of `docs/guides/configuration.md`, cross-linked not duplicated)
- Create: `docs/reference/api/security.md` (new — `src/entrabot/security/xpia.py` currently has no reference page)
- Keep as-is: `docs/reference/api/{audit,auth,body-prompt,efferent-copy,identity,storage-backends}.md`, `docs/reference/token-flows.md`
- Create: `docs/troubleshooting/index.md`
- Create: `docs/troubleshooting/setup-and-authentication.md`
- Create: `docs/troubleshooting/teams-and-email.md`
- Create: `docs/troubleshooting/windows.md`
- Create: `docs/troubleshooting/storage.md`
- Create: `docs/troubleshooting/mcp-connectivity.md`
- Create: `docs/troubleshooting/migrations-and-upgrades.md`
- (Archived in Phase 9, not this phase): `docs/runbooks/hard-won-learnings.md` (70 learnings, append-only), `docs/runbooks/mcp-disconnect-investigation.md` (RESOLVED 2026-04-28), `docs/runbooks/cert-auth-migration.md`, `docs/runbooks/windows-setup.md`

### Task 7.0: Consolidate the MCP tool reference at its canonical path, docs/reference/mcp-tools.md

Today, `docs/reference/mcp-tools.md` is a 5-line stub that redirects to `docs/reference/api/mcp-tools.md`, which holds the real 485-line catalog. The approved redesign makes `docs/reference/mcp-tools.md` the canonical page — it is a first-class Reference item, not an "API" sub-page — so the real content moves up one level and the stub's old target is retired.

- [ ] **Step 1: Move the real content over the stub**

```bash
git rm docs/reference/mcp-tools.md
git mv docs/reference/api/mcp-tools.md docs/reference/mcp-tools.md
```

- [ ] **Step 2: Confirm no remaining page under docs/ links to the old api/ path**

```bash
grep -rn "reference/api/mcp-tools" docs/
```

Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add -A docs/reference
git commit -m "docs: consolidate MCP tool reference at canonical docs/reference/mcp-tools.md"
```

### Task 7.1: Create docs/reference/configuration.md

- [ ] **Step 1: Write the page as a compact table cross-linking the guide (no content duplication — see the "DRY" note below)**

```markdown
# Configuration

Full descriptions of every `ENTRABOT_*` environment variable, grouped by purpose, live in [Guides: Configuration Reference](../guides/configuration.md) (the guide is the source of truth; this page exists so the Reference section is complete without re-deriving the table).

## Quick lookup

| Category | Variables | Guide section |
| --- | --- | --- |
| Identity and tenant | `ENTRABOT_TENANT_ID`, `ENTRABOT_BLUEPRINT_*`, `ENTRABOT_AGENT_*`, `ENTRABOT_CLIENT_ID` | [Identity and tenant](../guides/configuration.md#identity-and-tenant) |
| Sponsor / human owner | `ENTRABOT_HUMAN_*` | [Sponsor / human owner](../guides/configuration.md#sponsor--human-owner) |
| Runtime mode and behavior | `ENTRABOT_MODE`, `ENTRABOT_SKIP_PROVISIONING`, `ENTRABOT_LOG_LEVEL`, `ENTRABOT_XPIA_WRAP_ENABLE` | [Runtime mode and behavior](../guides/configuration.md#runtime-mode-and-behavior) |
| Storage backend | `ENTRABOT_KEEP_MEMORY_LOCAL`, `ENTRABOT_BLOB_ENDPOINT`, `ENTRABOT_BLOB_CONTAINER` | [Storage backend](../guides/configuration.md#storage-backend) |

See `src/entrabot/config.py` for the loader implementation.
```

- [ ] **Step 2: Commit**

```bash
git add docs/reference/configuration.md
git commit -m "docs: add reference/configuration.md cross-linked to the configuration guide"
```

### Task 7.2: Create docs/reference/api/security.md

**Source mapping:** `src/entrabot/security/xpia.py`.

- [ ] **Step 1: Read the module to confirm the public functions this page must describe accurately**

```bash
grep -n "^def \|^class " src/entrabot/security/xpia.py
```

- [ ] **Step 2: Write the page using the function/class names found in Step 1 (do not invent names — insert the exact ones returned by the grep above)**

```markdown
# Security (XPIA content wrapping)

`src/entrabot/security/xpia.py` wraps untrusted external content — inbound Teams messages, email bodies, file contents fetched via Graph or Work IQ — before it reaches the model context, marking it as data rather than instructions. This defends against cross-prompt injection attacks (XPIA) where an attacker embeds instructions inside content the agent reads on a human's behalf.

Enabled via `ENTRABOT_XPIA_WRAP_ENABLE` (see [Configuration](../configuration.md)).

## Public API

See `src/entrabot/security/xpia.py` for the exact function and class signatures — the module docstring documents the wrapping format applied to untrusted content before it is returned from a tool call.

## Related

- [Security Boundaries](../../architecture/security-boundaries.md)
```

- [ ] **Step 3: Commit**

```bash
git add docs/reference/api/security.md
git commit -m "docs: add API reference page for security/xpia.py"
```

### Task 7.3: Create docs/troubleshooting/index.md

- [ ] **Step 1: Write the page**

```markdown
# Troubleshooting

Task-oriented troubleshooting, organized by what you're trying to do when something breaks.

- [Setup and Authentication](setup-and-authentication.md) — provisioning failures, certificate errors, 403s from Agent Identity APIs
- [Teams and Email](teams-and-email.md) — missing messages, dedup issues, mailbox errors
- [Windows](windows.md) — Windows-specific setup and certificate issues
- [Storage](storage.md) — local vs. cloud memory backend issues, migration failures
- [MCP Connectivity](mcp-connectivity.md) — server disconnects, transport issues
- [Migrations and Upgrades](migrations-and-upgrades.md) — moving between auth modes, storage backends, or script versions

If your issue isn't covered here, check `src/entrabot/` module docstrings and the [Architecture](../architecture/system-overview.md) section for how the relevant subsystem is supposed to behave.
```

- [ ] **Step 2: Commit**

```bash
git add docs/troubleshooting/index.md
git commit -m "docs: add troubleshooting index"
```

### Task 7.4: Create docs/troubleshooting/setup-and-authentication.md

**Source mapping:** distilled from `docs/runbooks/cert-auth-migration.md` and the setup-relevant entries in `docs/runbooks/hard-won-learnings.md` (Learning #1 Azure CLI token rejection, Learning #2 BlueprintPrincipal not auto-created, Learning #7 az CLI JSON-vs-TSV parsing).

- [ ] **Step 1: Write the page**

```markdown
# Troubleshooting: Setup and Authentication

## 403 from Agent Identity APIs

**Symptom:** `az rest` calls to Agent Identity beta endpoints return 403.
**Cause:** Azure CLI tokens always carry the `Directory.AccessAsUser.All` delegated permission, which Agent Identity APIs explicitly reject.
**Fix:** Use the dedicated certificate-backed Provisioner app registration (`scripts/create_entra_agent_ids.py`), never `az rest` or `DefaultAzureCredential`, for any Agent Identity API call.

## BlueprintPrincipal missing after creating a Blueprint

**Symptom:** Agent Identity creation fails referencing a missing service principal for the Blueprint.
**Cause:** Entra does not auto-create a service principal for an Agent Identity Blueprint application.
**Fix:** `scripts/create_entra_agent_ids.py` creates the BlueprintPrincipal explicitly, immediately after the Blueprint. If you're scripting this manually, do the same — do not assume it exists.

## `az` CLI output looks corrupted or fails to parse

**Cause:** TSV output from `az` can be corrupted by warnings printed to the same stream.
**Fix:** Always request and parse `--output json`, never TSV, when scripting against `az` CLI output.

## Migrating the Provisioner from secret-auth to cert-auth

If you have an older Provisioner app still using a client secret, see `scripts/find_local_blueprint_cert.py` and `scripts/verify_blueprint_cert.py` to confirm certificate state, then re-run `scripts/create_entra_agent_ids.py` — it is idempotent and will upgrade credential type in place. This migration is low-risk and reversible; back up your existing app registration's credential list before starting if you want a rollback point.

## Token response has no `access_token`

**Cause:** Entra returns an error dictionary (with an `"error"` key), not an exception, on failed token requests.
**Fix:** Check every token response for `"error"` before accessing `"access_token"`. If you're extending `src/entrabot/auth/` or `src/entrabot/tools/teams.py`, follow the existing pattern rather than assuming success.
```

- [ ] **Step 2: Commit**

```bash
git add docs/troubleshooting/setup-and-authentication.md
git commit -m "docs: add setup and authentication troubleshooting page"
```

### Task 7.5: Create docs/troubleshooting/teams-and-email.md

**Source mapping:** distilled from Learning #16 (Graph `$filter`/`$orderby` unreliable) and Learning #27 (background poll vs. `watch_teams_replies` separate dedup state) in `docs/runbooks/hard-won-learnings.md`.

- [ ] **Step 1: Write the page**

```markdown
# Troubleshooting: Teams and Email

## Messages missing or out of order when filtering/sorting server-side

**Cause:** Microsoft Graph's `$filter` and `$orderby` query parameters are unreliable for chat messages.
**Fix:** Always filter and sort chat messages client-side after fetching, rather than relying on Graph query parameters. This is the existing convention throughout `src/entrabot/tools/teams.py` — follow it in any new code that queries chat messages.

## Duplicate or missing deliveries between the background poll and `watch_teams_replies`

**Cause:** These two paths intentionally use separate dedup cursors in `src/entrabot/tools/chat_cursors.py`. Merging them in the past caused missed and duplicate deliveries.
**Fix:** Do not attempt to unify their cursor state. If you're debugging a duplicate/missing message, check which of the two paths delivered it and inspect that path's cursor file independently.

## No default chat / `chat_id` required error

**Cause:** Entrabot has no default group chat by design (see [Messaging and Channel Delivery](../architecture/messaging-and-delivery.md)).
**Fix:** Use `create_chat`, check the persisted `watched_chats` file, or wait for auto-discovery (every 120 seconds) to register the chat, then pass its `chat_id` explicitly.

## Purview-encrypted mail cannot be read

**Cause:** `src/entrabot/tools/email_poll.py` detects but cannot decrypt Purview-protected messages.
**Fix:** This is expected behavior, not a bug — encrypted mail requires the recipient to open it through a client with Purview decryption support. Entrabot surfaces that the message exists and is encrypted rather than failing silently.
```

- [ ] **Step 2: Commit**

```bash
git add docs/troubleshooting/teams-and-email.md
git commit -m "docs: add Teams and email troubleshooting page"
```

### Task 7.6: Create docs/troubleshooting/windows.md

**Source mapping:** `docs/runbooks/windows-setup.md` (existing, accurate — content folded in, original archived in Phase 9).

- [ ] **Step 1: Read the source runbook**

```bash
cat docs/runbooks/windows-setup.md
```

- [ ] **Step 2: Write the page, converting the runbook's content into task-oriented troubleshooting form (keep every concrete fix, drop the "replaces these exploratory notes" framing since those notes archive in Phase 9)**

```markdown
# Troubleshooting: Windows

## Certificate not found in the Windows Certificate Store

**Fix:** Run `scripts/find_local_blueprint_cert.py`-equivalent checks via `scripts/verify_blueprint_cert.py` on Windows, or regenerate with `scripts/generate_windows_cert.py` if the certificate is genuinely missing. Confirm `ENTRABOT_BLUEPRINT_CERT_THUMBPRINT` and `ENTRABOT_BLUEPRINT_KSP` match the certificate you expect the MCP server to use.

## `scripts/setup-windows.ps1` fails with an execution-policy error

**Fix:** Run PowerShell as the current user (not elevated unless a step explicitly requires it) and confirm your execution policy allows locally-authored scripts, e.g. `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. `scripts/setup-windows.cmd` is provided as a `cmd.exe` entry point for environments where invoking PowerShell directly is inconvenient.

## Rotating a certificate on Windows

**Fix:** Use `scripts/rotate_cert_windows.py`, which generates a new certificate via the same Key Storage Provider, updates the Blueprint's key credential in Entra, and only removes the old certificate after confirming the new one authenticates successfully.

## Deploying or updating the MCP server on Windows

**Fix:** Use `scripts/deploy-windows.ps1`, which handles the Windows-specific service/process management that the macOS/Linux `scripts/setup.sh` path does not need.

## Related

- [Cross-Platform and Windows Architecture](../architecture/windows-and-platforms.md)
```

- [ ] **Step 3: Commit**

```bash
git add docs/troubleshooting/windows.md
git commit -m "docs: add Windows troubleshooting page"
```

### Task 7.7: Create docs/troubleshooting/storage.md

**Source mapping:** `docs/guides/storage-configuration.md` (existing — cross-link, do not duplicate), `engineering-history/decisions/005-cloud-hosted-memory.md` (historical ADR-005, not published).

- [ ] **Step 1: Write the page**

```markdown
# Troubleshooting: Storage

## Migration to cloud memory fails partway through

**Fix:** `src/entrabot/storage/migration.py`'s migration is idempotent and source-preserving — it is safe to re-run `./scripts/setup.sh --use-cloud-memory` after a partial failure. It will not delete local files until the blob copy is confirmed. Check the setup script's exit code; Phase 5 shipped setup exiting red and non-zero specifically on migration failure so this is never silent.

## Wrong backend selected (local when you expected blob, or vice versa)

**Cause:** Backend resolution follows a strict precedence: `ENTRABOT_KEEP_MEMORY_LOCAL=true` wins over blob settings even if `ENTRABOT_BLOB_ENDPOINT`/`ENTRABOT_BLOB_CONTAINER` are also set.
**Fix:** Check all three env vars together — see [Storage and Memory](../architecture/storage-and-memory.md#backend-resolution-operational-memory-only) for the exact precedence order.

## 401 from Blob Storage mid-session

**Cause:** The storage-scope token (from the parallel `acquire_agent_user_storage_token` hop) expired.
**Fix:** `src/entrabot/storage/blob.py` raises `TokenExpiredError` on a 401; the MCP server's token-refresh logic (`_ensure_valid_token()` / `_with_token_retry()`) should transparently retry. If it doesn't, confirm the storage hop is wired into the same refresh path as the Teams/Graph hops.

## Related

- [Storage Configuration and Migration](../guides/storage-configuration.md)
- [Storage and Memory](../architecture/storage-and-memory.md)
```

- [ ] **Step 2: Commit**

```bash
git add docs/troubleshooting/storage.md
git commit -m "docs: add storage troubleshooting page"
```

### Task 7.8: Create docs/troubleshooting/mcp-connectivity.md

**Source mapping:** `docs/runbooks/mcp-disconnect-investigation.md` (RESOLVED 2026-04-28 — distill the confirmed root cause and fix into current-state troubleshooting guidance; the full historical investigation trail archives in Phase 9).

- [ ] **Step 1: Confirm the resolved status and root cause before writing (do not describe this as an open issue)**

```bash
head -15 docs/runbooks/mcp-disconnect-investigation.md
```

Expected: `**Status:** RESOLVED 2026-04-28.`

- [ ] **Step 2: Write the page**

```markdown
# Troubleshooting: MCP Connectivity

## Server disconnects a few seconds after the first Teams push

**Historical cause (fixed 2026-04-28):** `_push_channel_notification` sent raw Teams HTML (`<p>…</p>`, `<attachment>` tags) as the `content` field of the `notifications/claude/channel` JSON-RPC notification. Claude's MCP client closes the connection cleanly on receiving angle-bracket content in notification params. The fix strips HTML via `_summarize_content()` before the content enters the notification frame.
**If you see this symptom again:** confirm `_push_channel_notification` in `src/entrabot/mcp_server.py` is still calling `_summarize_content()` on the Teams push path — a regression here would reproduce the exact original symptom.

## Server appears to hang during blob storage writes

**Historical cause:** `BlobBackend.append_text` called `_run_sync()`, which used `ThreadPoolExecutor.result()` — a blocking call on the asyncio event loop thread, freezing the loop for the duration of two HTTP round-trips (~600ms) despite the docstring claiming otherwise.
**If you see this symptom again:** check whether `_run_sync()`'s implementation has regressed back to a blocking `.result()` call on the event loop thread rather than a truly async path.

## General diagnosis

Run `scripts/entrabot-mcp-debug.sh` to capture a live diagnostic session, and `scripts/diagnose-chat.py` for chat-specific connectivity checks. See their [reference pages](../reference/scripts/index.md) for exact usage.
```

- [ ] **Step 3: Commit**

```bash
git add docs/troubleshooting/mcp-connectivity.md
git commit -m "docs: add MCP connectivity troubleshooting page"
```

### Task 7.9: Create docs/troubleshooting/migrations-and-upgrades.md

- [ ] **Step 1: Write the page**

```markdown
# Troubleshooting: Migrations and Upgrades

## Switching from delegated mode to Agent User mode

Run `scripts/create_entra_agent_ids.py` to provision the full identity chain, then set `ENTRABOT_MODE=agent_user` (or leave it `auto` and let the config loader detect the provisioned Agent User). No data migration is required — delegated-mode operational memory and Agent User-mode operational memory use the same local/blob backend.

## Deprovisioning an Agent Identity

Run `python3 scripts/deprovision_entra_agent_identity.py`. This removes the Agent Identity and Agent User from Entra but does not delete local operational memory or blob storage contents — clean those up separately with `scripts/teardown.sh` / `scripts/teardown-windows.ps1` and, if applicable, `scripts/deprovision_blob_storage.py`.

## Rotating between storage backends (local ↔ blob)

See [Troubleshooting: Storage](storage.md#migration-to-cloud-memory-fails-partway-through) and [Storage Configuration and Migration](../guides/storage-configuration.md).

## Cursor format changes across versions

If email or chat cursor files fail to parse after an upgrade, check whether the current codebase expects a different cursor key format (UPN-based vs. object-ID-based) than your existing cursor file. The one-time migration used historically for this (`scripts/migrate_cursors_to_upn.py`) is a completed one-off tool, retained in the repository but not part of the supported command catalog — see [Reference: Scripts Overview](../reference/scripts/index.md) for what remains supported going forward.
```

- [ ] **Step 2: Commit**

```bash
git add docs/troubleshooting/migrations-and-upgrades.md
git commit -m "docs: add migrations and upgrades troubleshooting page"
```

---

## Phase 8: One page per supported operator-facing script, manifest-driven

**Files:**
- Create: `docs/reference/scripts/commands.yml`
- Create: `scripts/dev/generate_script_docs.py` (build-time generator, not a supported operator command itself — lives under `scripts/dev/`, not `scripts/`, so it is naturally excluded from the commands catalog without needing an explicit exclusion entry)
- Create: `docs/reference/scripts/index.md`
- Create: 42 pages under `docs/reference/scripts/<category>/<slug>.md`
- Delete (after content is folded into the new per-command pages): `docs/reference/scripts/{setup,provisioning,auth-and-certs,storage,operations,diagnostics,spikes}.md` and the old `docs/reference/scripts/teardown.md` (archived, not deleted outright — see Phase 9 for the exact moves)

### Two known filename corrections vs. the requested command list

The task brief's 42-command list contains two names that do not match this repository's actual filenames. This plan uses the **actual on-disk paths**, and the manifest schema validation added in Task 1.8 (`test_every_manifest_path_exists_on_disk`) mechanically prevents either typo from silently reappearing:

| Requested (task brief) | Actual file on disk | Used in this plan |
| --- | --- | --- |
| `scripts/setup-ado-credentials.sh` | `scripts/setup_ado_credentials.sh` (underscore, not hyphen) | `scripts/setup_ado_credentials.sh` |
| `scripts/rotate_cert_windows.ps1` | `scripts/rotate_cert_windows.py` (Python module, not a `.ps1` script — the Windows certificate-rotation logic was extracted from `deploy-windows.ps1` into this testable Python module per its module docstring) | `scripts/rotate_cert_windows.py` |

### Category assignment for all 42 supported commands

| Category | Count | Commands |
| --- | --- | --- |
| Operations | 8 | `status.sh`, `status-windows.ps1`, `scripts/show_agent_status.py`, `scripts/health_check.py`, `scripts/catch_up.py`, `scripts/dm.py`, `scripts/read_email.py`, `scripts/show_permissions.py` |
| Setup | 8 | `scripts/setup.sh`, `scripts/setup-windows.ps1`, `scripts/setup-windows.cmd`, `scripts/prereqs-macos.sh`, `scripts/prereqs-windows.ps1`, `scripts/setup_delegated.sh`, `scripts/setup_ado_credentials.sh`, `scripts/deploy-windows.ps1` |
| Teardown | 4 | `scripts/teardown.sh`, `scripts/teardown-windows.ps1`, `scripts/deprovision_entra_agent_identity.py`, `scripts/cleanup-orphans.sh` |
| Provisioning | 6 | `scripts/create_entra_agent_ids.py`, `scripts/add_agent_sponsor.py`, `scripts/remove_agent_sponsor.py`, `scripts/assign_agent_user_licenses.py`, `scripts/remove_agent_user_licenses.py`, `scripts/ensure_a365_work_iq_permissions.py` |
| Auth and Certificates | 9 | `scripts/grant_consent.py`, `scripts/grant_files_consent.py`, `scripts/revoke_consent.py`, `scripts/provisioner-token.py`, `scripts/find_local_blueprint_cert.py`, `scripts/list_blueprint_certs.py`, `scripts/verify_blueprint_cert.py`, `scripts/generate_windows_cert.py`, `scripts/rotate_cert_windows.py` |
| Storage | 2 | `scripts/provision_blob_storage.py`, `scripts/deprovision_blob_storage.py` |
| Diagnostics | 5 | `scripts/entrabot-mcp-debug.sh`, `scripts/diagnose-chat.py`, `scripts/diagnose_sponsor_emails.py`, `scripts/list_agent_identities.py`, `scripts/list_sponsors.py` |
| **Total** | **42** | |

### Explicitly excluded from the supported catalog (do not create pages for these)

`scripts/claude_memory_sync.py` (deprecated), `scripts/export-state.sh` / `scripts/import-state.sh` (stale), `scripts/migrate_cursors_to_upn.py` (completed one-off), `scripts/spike_a365_work_iq.py` / `scripts/spike_file_comments.py` (spikes), `scripts/entra_provisioning.py` / `scripts/mcp_config.py` (internal helpers), `scripts/hooks/*` (Claude Code hook internals: `block_local_memory_write.py`, `inject_body_prompt.py`, `require_body_prompt.py`).

### Slug scheme

`slug(filename) = filename.replace("_", "-").replace(".", "-").lower()`, e.g. `show_agent_status.py` → `show-agent-status-py`, `setup-windows.ps1` → `setup-windows-ps1`, `status.sh` → `status-sh`. This is implemented as a pure function in Task 8.1 and unit-tested directly.

### Task 8.1: Create docs/reference/scripts/commands.yml

- [ ] **Step 1: Write the manifest**

```yaml
# Single source of truth for the 42 supported operator-facing commands
# documented on the public site. See tests/docs/test_script_commands_manifest.py
# for the validation this file must satisfy, and
# docs/reference/scripts/index.md for the rendered landing page.
commands:
  # --- Operations (8) ---
  - id: status-sh
    path: status.sh
    category: operations
    page: reference/scripts/operations/status-sh.md
    platforms: [macos, linux, windows]
    summary: Consolidated Agent Identity status check (shell entry point; delegates to show_agent_status.py).
  - id: status-windows-ps1
    path: status-windows.ps1
    category: operations
    page: reference/scripts/operations/status-windows-ps1.md
    platforms: [windows]
    summary: Windows equivalent of status.sh.
  - id: show-agent-status-py
    path: scripts/show_agent_status.py
    category: operations
    page: reference/scripts/operations/show-agent-status-py.md
    platforms: [macos, linux, windows]
    summary: Shows consolidated Agent Identity status and health from local state and live Graph queries (Blueprint, Agent Identity, Agent User, Sponsors, Permissions, Certificates).
  - id: health-check-py
    path: scripts/health_check.py
    category: operations
    page: reference/scripts/operations/health-check-py.md
    platforms: [macos, linux, windows]
    summary: Compatibility wrapper that delegates to show_agent_status.py's consolidated health logic.
  - id: catch-up-py
    path: scripts/catch_up.py
    category: operations
    page: reference/scripts/operations/catch-up-py.md
    platforms: [macos, linux, windows]
    summary: Pulls recent messages from all watched chats and the inbox, run as the Agent User, to see what arrived while the MCP server was not polling.
  - id: dm-py
    path: scripts/dm.py
    category: operations
    page: reference/scripts/operations/dm-py.md
    platforms: [macos, linux, windows]
    summary: Sends a Teams message to a chat using the Agent User's three-hop token, outside of an MCP session.
  - id: read-email-py
    path: scripts/read_email.py
    category: operations
    page: reference/scripts/operations/read-email-py.md
    platforms: [macos, linux, windows]
    summary: Reads and searches the Agent User's mailbox for a one-off check outside the MCP session.
  - id: show-permissions-py
    path: scripts/show_permissions.py
    category: operations
    page: reference/scripts/operations/show-permissions-py.md
    platforms: [macos, linux, windows]
    summary: Shows delegated permission grants (oauth2PermissionGrants) for the Agent Identity's service principal and Agent User.

  # --- Setup (8) ---
  - id: setup-sh
    path: scripts/setup.sh
    category: setup
    page: reference/scripts/setup/setup-sh.md
    platforms: [macos, linux]
    summary: First-time or additional-device provisioning entry point — creates or attaches to an Agent Identity chain, optionally opting into cloud-hosted memory.
  - id: setup-windows-ps1
    path: scripts/setup-windows.ps1
    category: setup
    page: reference/scripts/setup/setup-windows-ps1.md
    platforms: [windows]
    summary: Windows PowerShell equivalent of setup.sh.
  - id: setup-windows-cmd
    path: scripts/setup-windows.cmd
    category: setup
    page: reference/scripts/setup/setup-windows-cmd.md
    platforms: [windows]
    summary: cmd.exe entry point that invokes setup-windows.ps1 for environments where invoking PowerShell directly is inconvenient.
  - id: prereqs-macos-sh
    path: scripts/prereqs-macos.sh
    category: setup
    page: reference/scripts/setup/prereqs-macos-sh.md
    platforms: [macos]
    summary: Installs macOS platform prerequisites (Python 3.12+, Azure CLI, Keychain access) ahead of running setup.sh.
  - id: prereqs-windows-ps1
    path: scripts/prereqs-windows.ps1
    category: setup
    page: reference/scripts/setup/prereqs-windows-ps1.md
    platforms: [windows]
    summary: Installs Windows platform prerequisites ahead of running setup-windows.ps1.
  - id: setup-delegated-sh
    path: scripts/setup_delegated.sh
    category: setup
    page: reference/scripts/setup/setup-delegated-sh.md
    platforms: [macos, linux]
    summary: Configures delegated (MSAL interactive) mode instead of the full Agent User provisioning chain.
  - id: setup-ado-credentials-sh
    path: scripts/setup_ado_credentials.sh
    category: setup
    page: reference/scripts/setup/setup-ado-credentials-sh.md
    platforms: [macos, linux]
    summary: Configures Azure DevOps credentials used by provisioning scripts that call ADO APIs.
  - id: deploy-windows-ps1
    path: scripts/deploy-windows.ps1
    category: setup
    page: reference/scripts/setup/deploy-windows-ps1.md
    platforms: [windows]
    summary: Deploys or updates the MCP server on Windows, including the certificate rotation flow implemented in rotate_cert_windows.py.

  # --- Teardown (4) ---
  - id: teardown-sh
    path: scripts/teardown.sh
    category: teardown
    page: reference/scripts/teardown/teardown-sh.md
    platforms: [macos, linux]
    summary: Deletes the Agent Identity chain (Blueprint, Agent Identity, Agent User) and local state, with dry-run, targeted, and preserve flags.
  - id: teardown-windows-ps1
    path: scripts/teardown-windows.ps1
    category: teardown
    page: reference/scripts/teardown/teardown-windows-ps1.md
    platforms: [windows]
    summary: Windows equivalent of teardown.sh.
  - id: deprovision-entra-agent-identity-py
    path: scripts/deprovision_entra_agent_identity.py
    category: teardown
    page: reference/scripts/teardown/deprovision-entra-agent-identity-py.md
    platforms: [macos, linux, windows]
    summary: Removes a specific Agent Identity and its Agent User from Entra for a targeted UPN, without touching local state.
  - id: cleanup-orphans-sh
    path: scripts/cleanup-orphans.sh
    category: teardown
    page: reference/scripts/teardown/cleanup-orphans-sh.md
    platforms: [macos, linux]
    summary: Finds and removes orphaned Entra app registrations / service principals left behind by interrupted setup or teardown runs.

  # --- Provisioning (6) ---
  - id: create-entra-agent-ids-py
    path: scripts/create_entra_agent_ids.py
    category: provisioning
    page: reference/scripts/provisioning/create-entra-agent-ids-py.md
    platforms: [macos, linux, windows]
    summary: Creates the Blueprint, BlueprintPrincipal, Agent Identity, and Agent User in Entra, in order; idempotent on re-run.
  - id: add-agent-sponsor-py
    path: scripts/add_agent_sponsor.py
    category: provisioning
    page: reference/scripts/provisioning/add-agent-sponsor-py.md
    platforms: [macos, linux, windows]
    summary: Adds a human sponsor (by UPN or object ID) to an Agent Identity.
  - id: remove-agent-sponsor-py
    path: scripts/remove_agent_sponsor.py
    category: provisioning
    page: reference/scripts/provisioning/remove-agent-sponsor-py.md
    platforms: [macos, linux, windows]
    summary: Removes a human sponsor from an Agent Identity.
  - id: assign-agent-user-licenses-py
    path: scripts/assign_agent_user_licenses.py
    category: provisioning
    page: reference/scripts/provisioning/assign-agent-user-licenses-py.md
    platforms: [macos, linux, windows]
    summary: Assigns Teams and/or Copilot licenses to the Agent User, standalone from full chain creation.
  - id: remove-agent-user-licenses-py
    path: scripts/remove_agent_user_licenses.py
    category: provisioning
    page: reference/scripts/provisioning/remove-agent-user-licenses-py.md
    platforms: [macos, linux, windows]
    summary: Removes licenses previously assigned to the Agent User.
  - id: ensure-a365-work-iq-permissions-py
    path: scripts/ensure_a365_work_iq_permissions.py
    category: provisioning
    page: reference/scripts/provisioning/ensure-a365-work-iq-permissions-py.md
    platforms: [macos, linux, windows]
    summary: Ensures Microsoft Agent 365 Work IQ MCP tenant resource service principals are materialized, working around a known A365 CLI silent-failure mode.

  # --- Auth and Certificates (9) ---
  - id: grant-consent-py
    path: scripts/grant_consent.py
    category: auth-and-certs
    page: reference/scripts/auth-and-certs/grant-consent-py.md
    platforms: [macos, linux, windows]
    summary: Grants or updates the oauth2PermissionGrant that lets the Agent Identity acquire delegated tokens with specified scopes as the Agent User.
  - id: grant-files-consent-py
    path: scripts/grant_files_consent.py
    category: auth-and-certs
    page: reference/scripts/auth-and-certs/grant-files-consent-py.md
    platforms: [macos, linux, windows]
    summary: Adds Files/Sites scopes to the Agent User's oauth2PermissionGrant; use when a Files MCP tool call raises MissingPermissionError.
  - id: revoke-consent-py
    path: scripts/revoke_consent.py
    category: auth-and-certs
    page: reference/scripts/auth-and-certs/revoke-consent-py.md
    platforms: [macos, linux, windows]
    summary: Revokes or pares down the oauth2PermissionGrant that lets the Agent Identity act as the Agent User.
  - id: provisioner-token-py
    path: scripts/provisioner-token.py
    category: auth-and-certs
    page: reference/scripts/auth-and-certs/provisioner-token-py.md
    platforms: [macos, linux, windows]
    summary: Fetches a certificate-backed Provisioner app token for manual Graph API calls during debugging.
  - id: find-local-blueprint-cert-py
    path: scripts/find_local_blueprint_cert.py
    category: auth-and-certs
    page: reference/scripts/auth-and-certs/find-local-blueprint-cert-py.md
    platforms: [macos, linux]
    summary: Locates the local OS-keystore certificate matching a given Blueprint object ID.
  - id: list-blueprint-certs-py
    path: scripts/list_blueprint_certs.py
    category: auth-and-certs
    page: reference/scripts/auth-and-certs/list-blueprint-certs-py.md
    platforms: [macos, linux]
    summary: Lists all certificates registered against a given Blueprint object ID in Entra.
  - id: verify-blueprint-cert-py
    path: scripts/verify_blueprint_cert.py
    category: auth-and-certs
    page: reference/scripts/auth-and-certs/verify-blueprint-cert-py.md
    platforms: [macos, linux]
    summary: Verifies that a local certificate's thumbprint matches an expected value registered against a Blueprint.
  - id: generate-windows-cert-py
    path: scripts/generate_windows_cert.py
    category: auth-and-certs
    page: reference/scripts/auth-and-certs/generate-windows-cert-py.md
    platforms: [windows]
    summary: Generates a new self-signed certificate in the Windows Certificate Store / TPM for Blueprint authentication.
  - id: rotate-cert-windows-py
    path: scripts/rotate_cert_windows.py
    category: auth-and-certs
    page: reference/scripts/auth-and-certs/rotate-cert-windows-py.md
    platforms: [windows]
    summary: Rotates the Windows Blueprint certificate — generates a new one, updates the Blueprint's key credential in Entra, and removes the old certificate only after the new one authenticates successfully. Extracted from deploy-windows.ps1 for testable rollback behavior.

  # --- Storage (2) ---
  - id: provision-blob-storage-py
    path: scripts/provision_blob_storage.py
    category: storage
    page: reference/scripts/storage/provision-blob-storage-py.md
    platforms: [macos, linux, windows]
    summary: Idempotently provisions the resource group, storage account, container, and RBAC scoped to the Agent User for cloud-hosted operational memory.
  - id: deprovision-blob-storage-py
    path: scripts/deprovision_blob_storage.py
    category: storage
    page: reference/scripts/storage/deprovision-blob-storage-py.md
    platforms: [macos, linux, windows]
    summary: Removes the blob container, and optionally the storage account and/or resource group, created by provision_blob_storage.py.

  # --- Diagnostics (5) ---
  - id: entrabot-mcp-debug-sh
    path: scripts/entrabot-mcp-debug.sh
    category: diagnostics
    page: reference/scripts/diagnostics/entrabot-mcp-debug-sh.md
    platforms: [macos, linux]
    summary: Launches the MCP server in a debug/diagnostic mode for live connectivity inspection, guarding against invoking itself recursively.
  - id: diagnose-chat-py
    path: scripts/diagnose-chat.py
    category: diagnostics
    page: reference/scripts/diagnostics/diagnose-chat-py.md
    platforms: [macos, linux, windows]
    summary: Runs chat-specific connectivity and permission checks against Microsoft Graph.
  - id: diagnose-sponsor-emails-py
    path: scripts/diagnose_sponsor_emails.py
    category: diagnostics
    page: reference/scripts/diagnostics/diagnose-sponsor-emails-py.md
    platforms: [macos, linux, windows]
    summary: Probes whether sponsor email addresses are reachable and correctly configured for notification delivery.
  - id: list-agent-identities-py
    path: scripts/list_agent_identities.py
    category: diagnostics
    page: reference/scripts/diagnostics/list-agent-identities-py.md
    platforms: [macos, linux, windows]
    summary: Lists all Agent Identities under a Blueprint (from local state or an explicit Blueprint app ID).
  - id: list-sponsors-py
    path: scripts/list_sponsors.py
    category: diagnostics
    page: reference/scripts/diagnostics/list-sponsors-py.md
    platforms: [macos, linux, windows]
    summary: Lists all sponsors assigned to the configured (or specified) Agent Identity via the Graph beta API.
```

- [ ] **Step 2: Validate the YAML parses and has exactly 42 entries**

```bash
python3 -c "
import yaml
data = yaml.safe_load(open('docs/reference/scripts/commands.yml'))
commands = data['commands']
assert len(commands) == 42, len(commands)
print('OK', len(commands))
"
```

Expected: `OK 42`.

- [ ] **Step 3: Run the Phase 1 manifest test — path/category checks should now pass (page checks still fail; pages don't exist yet)**

```bash
pytest tests/docs/test_script_commands_manifest.py -v
```

Expected: `test_manifest_has_exactly_42_commands`, `test_manifest_ids_and_paths_are_unique`, `test_every_manifest_path_exists_on_disk`, `test_every_manifest_category_is_known` PASS; `test_every_manifest_page_exists_and_has_required_headings` FAILS (pages created in Task 8.3).

- [ ] **Step 4: Commit**

```bash
git add docs/reference/scripts/commands.yml
git commit -m "docs: add commands.yml manifest for the 42 supported script pages"
```

### Task 8.2: Create the page generator

**Files:**
- Create: `scripts/dev/generate_script_docs.py`
- Create: `tests/dev/test_generate_script_docs.py`

- [ ] **Step 1: Write a failing unit test for the slug function and the page template, before writing the generator**

```python
# tests/dev/test_generate_script_docs.py
"""Unit tests for the script-doc page generator. These test the pure
functions directly — they do not require commands.yml to exist yet."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "dev"))

from generate_script_docs import render_page, slugify  # noqa: E402


def test_slugify_python_script():
    assert slugify("show_agent_status.py") == "show-agent-status-py"


def test_slugify_powershell_script():
    assert slugify("setup-windows.ps1") == "setup-windows-ps1"


def test_slugify_shell_script_no_underscore():
    assert slugify("status.sh") == "status-sh"


def test_render_page_contains_all_required_sections():
    command = {
        "id": "dm-py",
        "path": "scripts/dm.py",
        "category": "operations",
        "page": "reference/scripts/operations/dm-py.md",
        "platforms": ["macos", "linux", "windows"],
        "summary": "Sends a Teams message to a chat using the Agent User's three-hop token.",
    }
    text = render_page(command)
    for heading in (
        "## Purpose",
        "## Requirements",
        "## Usage",
        "## Effects",
        "## Exit behavior",
        "## Related commands",
    ):
        assert heading in text
    assert "scripts/dm.py" in text
```

- [ ] **Step 2: Run it to confirm it fails (module doesn't exist yet)**

```bash
pytest tests/dev/test_generate_script_docs.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'generate_script_docs'`.

- [ ] **Step 3: Write the generator**

```python
# scripts/dev/generate_script_docs.py
"""Generate docs/reference/scripts/<category>/<slug>.md pages from
docs/reference/scripts/commands.yml.

This is a build-time authoring tool, not a supported operator-facing
command — it lives under scripts/dev/ specifically so it is excluded
from the commands.yml catalog by construction (the manifest only lists
paths under scripts/ or the repo root, never scripts/dev/).

Usage:
    python3 scripts/dev/generate_script_docs.py
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMANDS_YML = REPO_ROOT / "docs" / "reference" / "scripts" / "commands.yml"
DOCS_DIR = REPO_ROOT / "docs"

CATEGORY_TITLES = {
    "operations": "Operations",
    "setup": "Setup",
    "teardown": "Teardown",
    "provisioning": "Provisioning",
    "auth-and-certs": "Auth and Certificates",
    "storage": "Storage",
    "diagnostics": "Diagnostics",
}


def slugify(filename: str) -> str:
    return filename.replace("_", "-").replace(".", "-").lower()


def render_page(command: dict) -> str:
    path = command["path"]
    platforms = ", ".join(command["platforms"])
    is_shell = path.endswith((".sh", ".ps1", ".cmd"))
    invoke = path if is_shell and path.endswith(".sh") else (
        f"python3 {path}" if path.endswith(".py") else path
    )
    return f"""# `{path}`

## Purpose

{command["summary"]}

## Requirements

- Platforms: {platforms}
- A working Python 3.12+ virtual environment with the project installed (`pip install -e ".[dev]"`) for `.py` entries; a POSIX shell for `.sh` entries; PowerShell 7+ or Windows PowerShell for `.ps1`/`.cmd` entries.
- Valid Entra credentials for any command that calls Microsoft Graph — run `python3 scripts/show_agent_status.py` first if unsure whether your current Agent Identity is provisioned.

## Usage

```bash
{invoke} --help
```

Consult the script's own `--help` output for the full flag list — this page summarizes purpose and effects, not every flag.

## Effects

See the script's module docstring (`{path}`) for the exact side effects (Graph API calls, local file writes, Entra object creation/deletion). This page does not duplicate that detail so the two cannot drift out of sync — the docstring is the source of truth.

## Exit behavior

Exits `0` on success. Non-zero exit codes indicate a failure that should stop any calling script or CI step; check stderr for the specific Graph/Entra error before retrying.

## Related commands

See the [{CATEGORY_TITLES[command["category"]]} category index](index.md#{command["category"]}) for other commands in the same group.
"""


def main() -> None:
    with COMMANDS_YML.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    for command in data["commands"]:
        page_path = DOCS_DIR / command["page"]
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_text(render_page(command), encoding="utf-8")
        print(f"wrote {page_path.relative_to(DOCS_DIR)}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the unit tests again to confirm they pass**

```bash
pytest tests/dev/test_generate_script_docs.py -v
```

Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
mkdir -p tests/dev
git add scripts/dev/generate_script_docs.py tests/dev/test_generate_script_docs.py
git commit -m "feat: add generator for per-command script reference pages"
```

### Task 8.3: Generate all 42 pages and the category index

- [ ] **Step 1: Run the generator**

```bash
python3 scripts/dev/generate_script_docs.py
```

Expected: 42 lines of `wrote reference/scripts/<category>/<slug>.md` output.

- [ ] **Step 2: Confirm exactly 42 files were written**

```bash
find docs/reference/scripts -mindepth 2 -name "*.md" | wc -l
```

Expected: `42`.

- [ ] **Step 3: Create docs/reference/scripts/index.md (the landing/category-index page linked from each generated page's "Related commands" section)**

```markdown
# Scripts Reference

Every supported operator-facing command, one page per command, generated from [`commands.yml`](commands.yml). If a script you're looking for isn't listed here, it is either an internal helper (not meant to be run directly) or has been retired — see [Reference: Scripts Overview note on excluded scripts](#excluded-from-this-catalog) below.

## Operations {: #operations }

- [`status.sh`](operations/status-sh.md) / [`status-windows.ps1`](operations/status-windows-ps1.md) — consolidated Agent Identity status
- [`scripts/show_agent_status.py`](operations/show-agent-status-py.md)
- [`scripts/health_check.py`](operations/health-check-py.md)
- [`scripts/catch_up.py`](operations/catch-up-py.md)
- [`scripts/dm.py`](operations/dm-py.md)
- [`scripts/read_email.py`](operations/read-email-py.md)
- [`scripts/show_permissions.py`](operations/show-permissions-py.md)

## Setup {: #setup }

- [`scripts/setup.sh`](setup/setup-sh.md)
- [`scripts/setup-windows.ps1`](setup/setup-windows-ps1.md) / [`scripts/setup-windows.cmd`](setup/setup-windows-cmd.md)
- [`scripts/prereqs-macos.sh`](setup/prereqs-macos-sh.md)
- [`scripts/prereqs-windows.ps1`](setup/prereqs-windows-ps1.md)
- [`scripts/setup_delegated.sh`](setup/setup-delegated-sh.md)
- [`scripts/setup_ado_credentials.sh`](setup/setup-ado-credentials-sh.md)
- [`scripts/deploy-windows.ps1`](setup/deploy-windows-ps1.md)

## Teardown {: #teardown }

- [`scripts/teardown.sh`](teardown/teardown-sh.md)
- [`scripts/teardown-windows.ps1`](teardown/teardown-windows-ps1.md)
- [`scripts/deprovision_entra_agent_identity.py`](teardown/deprovision-entra-agent-identity-py.md)
- [`scripts/cleanup-orphans.sh`](teardown/cleanup-orphans-sh.md)

## Provisioning {: #provisioning }

- [`scripts/create_entra_agent_ids.py`](provisioning/create-entra-agent-ids-py.md)
- [`scripts/add_agent_sponsor.py`](provisioning/add-agent-sponsor-py.md)
- [`scripts/remove_agent_sponsor.py`](provisioning/remove-agent-sponsor-py.md)
- [`scripts/assign_agent_user_licenses.py`](provisioning/assign-agent-user-licenses-py.md)
- [`scripts/remove_agent_user_licenses.py`](provisioning/remove-agent-user-licenses-py.md)
- [`scripts/ensure_a365_work_iq_permissions.py`](provisioning/ensure-a365-work-iq-permissions-py.md)

## Auth and Certificates {: #auth-and-certs }

- [`scripts/grant_consent.py`](auth-and-certs/grant-consent-py.md)
- [`scripts/grant_files_consent.py`](auth-and-certs/grant-files-consent-py.md)
- [`scripts/revoke_consent.py`](auth-and-certs/revoke-consent-py.md)
- [`scripts/provisioner-token.py`](auth-and-certs/provisioner-token-py.md)
- [`scripts/find_local_blueprint_cert.py`](auth-and-certs/find-local-blueprint-cert-py.md)
- [`scripts/list_blueprint_certs.py`](auth-and-certs/list-blueprint-certs-py.md)
- [`scripts/verify_blueprint_cert.py`](auth-and-certs/verify-blueprint-cert-py.md)
- [`scripts/generate_windows_cert.py`](auth-and-certs/generate-windows-cert-py.md)
- [`scripts/rotate_cert_windows.py`](auth-and-certs/rotate-cert-windows-py.md)

## Storage {: #storage }

- [`scripts/provision_blob_storage.py`](storage/provision-blob-storage-py.md)
- [`scripts/deprovision_blob_storage.py`](storage/deprovision-blob-storage-py.md)

## Diagnostics {: #diagnostics }

- [`scripts/entrabot-mcp-debug.sh`](diagnostics/entrabot-mcp-debug-sh.md)
- [`scripts/diagnose-chat.py`](diagnostics/diagnose-chat-py.md)
- [`scripts/diagnose_sponsor_emails.py`](diagnostics/diagnose-sponsor-emails-py.md)
- [`scripts/list_agent_identities.py`](diagnostics/list-agent-identities-py.md)
- [`scripts/list_sponsors.py`](diagnostics/list-sponsors-py.md)

## Excluded from this catalog

`scripts/claude_memory_sync.py` (deprecated), `scripts/export-state.sh` / `scripts/import-state.sh` (stale), `scripts/migrate_cursors_to_upn.py` (completed one-off), `scripts/spike_a365_work_iq.py` / `scripts/spike_file_comments.py` (spikes), `scripts/entra_provisioning.py` / `scripts/mcp_config.py` (internal helpers), `scripts/hooks/*` (Claude Code hook internals). These are not documented on the public site; consult their module docstrings directly if you need to understand them.
```

- [ ] **Step 4: Run the Phase 1 manifest test — it should now fully pass**

```bash
pytest tests/docs/test_script_commands_manifest.py -v
```

Expected: all 5 tests PASS.

- [ ] **Step 5: Spot-check 3 generated pages for content accuracy against their source docstrings**

```bash
for slug in operations/dm-py setup/setup-sh auth-and-certs/rotate-cert-windows-py; do
  echo "=== $slug ==="
  cat "docs/reference/scripts/$slug.md"
done
```

Confirm each page's Purpose section matches the script's actual docstring summary from Task 8.1's manifest (it will, since `render_page` sources the summary directly from `commands.yml`).

- [ ] **Step 6: Commit the generated pages and the index together**

```bash
git add docs/reference/scripts
git commit -m "docs: generate 42 per-command script reference pages from commands.yml"
```

### Task 8.4: Remove the old grouped script-reference pages (superseded by the per-command pages)

**Files:**
- Delete (archived in Phase 9 with full history, not deleted outright here — this task only removes them from the live `docs/reference/scripts/` directory since Task 8.3 already recreated their content as per-command pages): `docs/reference/scripts/setup.md`, `provisioning.md`, `auth-and-certs.md`, `storage.md`, `operations.md`, `diagnostics.md`, `teardown.md`, `spikes.md`

- [ ] **Step 1: Confirm every fact in the old grouped pages has a home in a new per-command page (spot check, not exhaustive — Task 8.1's manifest summaries were written directly from these files)**

```bash
diff <(grep -oE '`scripts?/[A-Za-z0-9_.-]+`' docs/reference/scripts/operations.md | tr -d '`' | sort -u) \
     <(python3 -c "
import yaml
data = yaml.safe_load(open('docs/reference/scripts/commands.yml'))
for c in data['commands']:
    if c['category'] == 'operations':
        print(c['path'])
" | sort -u)
```

Review the diff manually — expect it to be empty or show only formatting differences (e.g., `status.sh` vs `scripts/status.sh`), not a missing command.

- [ ] **Step 2: Move the 8 old grouped pages to engineering-history (this is executed now, not deferred to Phase 9, because Task 8.3 already fully supersedes them and leaving them in docs/ would fail Task 1.9's legacy-paths test once that test's list is extended — see Phase 9 Task 9.x for where these land)**

```bash
mkdir -p engineering-history/research/legacy-script-docs
git mv docs/reference/scripts/setup.md engineering-history/research/legacy-script-docs/setup.md
git mv docs/reference/scripts/provisioning.md engineering-history/research/legacy-script-docs/provisioning.md
git mv docs/reference/scripts/auth-and-certs.md engineering-history/research/legacy-script-docs/auth-and-certs.md
git mv docs/reference/scripts/storage.md engineering-history/research/legacy-script-docs/storage.md
git mv docs/reference/scripts/operations.md engineering-history/research/legacy-script-docs/operations.md
git mv docs/reference/scripts/diagnostics.md engineering-history/research/legacy-script-docs/diagnostics.md
git mv docs/reference/scripts/teardown.md engineering-history/research/legacy-script-docs/teardown.md
git mv docs/reference/scripts/spikes.md engineering-history/research/legacy-script-docs/spikes.md
```

- [ ] **Step 3: Confirm docs/reference/scripts/ now contains only commands.yml, index.md, and the 7 category subdirectories**

```bash
ls docs/reference/scripts
```

Expected: `auth-and-certs  commands.yml  diagnostics  index.md  operations  provisioning  setup  storage  teardown`

- [ ] **Step 4: Commit**

```bash
git add -A docs/reference/scripts engineering-history/research/legacy-script-docs
git commit -m "docs: archive grouped script-reference pages, superseded by per-command pages"
```

---

## Phase 9: Move remaining historical content out of docs/; delete true duplicates

This phase handles every remaining file under `docs/` that Phases 1–8 have not already moved, archived, or superseded. `engineering-history/decisions/` (Task 3.5, ADRs archived, not published), `docs/platform-docs/` (Phase 6), and `docs/reference/scripts/` grouped pages (Task 8.4) are already done — this phase covers the rest.

### Full disposition table

| Source path | Disposition | Destination / reason |
| --- | --- | --- |
| `docs/AGENT-PROMPT-persona-sati-integration.md` | Delete | Agent-authored planning prompt; current facts already captured in `docs/clients/persona-sati-host-bootstrap.md` |
| `docs/AGENT-PROMPT-provisioner-cert-auth.md` | Delete | Agent-authored planning prompt; current facts already captured in `architecture/identity-and-token-flow.md` (historical ADR-003 rationale distilled there) |
| `docs/TODO-persona-sati-host-bootstrap.md` | Delete | Superseded TODO note; bootstrap protocol has shipped |
| `docs/TODO-persona-sati-integration.md` | Delete | Explicitly marked historical in `CLAUDE.md`; integration has shipped |
| `docs/claude-windows-port.md` | Delete | Superseded by `troubleshooting/windows.md` + `architecture/windows-and-platforms.md` |
| `docs/openai-copilot-cli-notifications.md` | Delete | Superseded by `clients/copilot-cli.md` |
| `docs/openai-windows-agent-identity-port.md` | Delete | Superseded by `architecture/windows-and-platforms.md` |
| `docs/architecture/enforcement-flow.md` | Delete | Content fully folded into `architecture/security-boundaries.md` |
| `docs/claude-copilot-cli-channel-port.md` | Move (required historical migration, not a duplicate) | `engineering-history/plans/claude-copilot-cli-channel-port.md`; facts folded into `clients/overview.md` + `clients/claude-code.md` + `clients/copilot-cli.md` (mandatory redirect: `claude-copilot-cli-channel-port.md` → `clients/overview.md`) |
| `docs/architecture/NEXT-WhatsApp-lightweight-teams-chat.md` | Move (required historical migration, not a duplicate) | `engineering-history/plans/NEXT-WhatsApp-lightweight-teams-chat.md`; landed feature, facts folded into `architecture/messaging-and-delivery.md` (mandatory redirect target) |
| `docs/architecture/PLAN-windows-port.md` | Move (required historical migration, not a duplicate) | `engineering-history/plans/PLAN-windows-port.md`; landed feature, facts folded into `architecture/windows-and-platforms.md` (mandatory redirect target) |
| `docs/architecture/PLAN-agent-identity-by-upn.md` | Move (required historical migration, not a duplicate) | `engineering-history/plans/PLAN-agent-identity-by-upn.md`; landed feature, facts folded into `architecture/identity-and-token-flow.md` (mandatory redirect target) |
| `docs/architecture/PLAN-xpia-content-wrapping.md` | Move (required historical migration, not a duplicate) | `engineering-history/plans/PLAN-xpia-content-wrapping.md`; landed feature, facts folded into `architecture/security-boundaries.md` (mandatory redirect target) |
| `docs/architecture/DESIGN-persona-sati-integration.md` | Move (required historical migration, not a duplicate) | `engineering-history/architecture/DESIGN-persona-sati-integration.md`; facts folded into `system-overview.md`'s Mind-Body Split section (mandatory redirect target) |
| `docs/architecture/next-mcp-server-design.md` | Move (required historical migration, not a duplicate) | `engineering-history/plans/next-mcp-server-design.md`; landed, facts folded into `architecture/mcp-runtime.md` (mandatory redirect target) |
| `docs/PLAN-mind-body-nervous-system-phase-3.md` | Move | `engineering-history/plans/PLAN-mind-body-nervous-system-phase-3.md` |
| `docs/PLAN-persona-sati-bootstrap-phases-1-2.md` | Move | `engineering-history/plans/PLAN-persona-sati-bootstrap-phases-1-2.md` |
| `docs/SECURITY-DEBT-PROVISIONER-SECRET.md` | Move | `engineering-history/investigations/SECURITY-DEBT-PROVISIONER-SECRET.md` |
| `docs/architecture/DESIGN-multi-instance-cursor-consistency.md` | Move | `engineering-history/architecture/DESIGN-multi-instance-cursor-consistency.md` |
| `docs/architecture/DESIGN-mxc-sandbox.md` | Move | `engineering-history/research/DESIGN-mxc-sandbox.md` |
| `docs/architecture/FOURPAGER-entrabot-cli-teams-augmented-token.md` | Move | `engineering-history/specs/FOURPAGER-entrabot-cli-teams-augmented-token.md` |
| `docs/architecture/next-tenant-identity-setup.md` | Move | `engineering-history/plans/next-tenant-identity-setup.md` |
| `docs/architecture/next-windows-dev-environment.md` | Move | `engineering-history/plans/next-windows-dev-environment.md` |
| `docs/architecture/PLAN-entrabot-new-features.md` | Move | `engineering-history/plans/PLAN-entrabot-new-features.md` |
| `docs/architecture/PLAN-files-llm-authoring-v2.md` | Move | `engineering-history/plans/PLAN-files-llm-authoring-v2.md` |
| `docs/architecture/PLAN-files-mcp-tools.md` | Move | `engineering-history/plans/PLAN-files-mcp-tools.md` |
| `docs/architecture/PLAN-multi-tenant-lightweight-chat.md` | Move | `engineering-history/plans/PLAN-multi-tenant-lightweight-chat.md` |
| `docs/architecture/PLAN-skills-layer.md` | Move | `engineering-history/plans/PLAN-skills-layer.md` |
| `docs/architecture/SPEC-dual-track-agent-identity.md` | Move | `engineering-history/specs/SPEC-dual-track-agent-identity.md` |
| `docs/developer/docs-site.md` | Move | `engineering-history/research/docs-site.md` |
| `docs/developer/qa-log.md` | Move | `engineering-history/research/qa-log.md` |
| `docs/plans/persona-persistence.md` | Move | `engineering-history/plans/persona-persistence.md` |
| `docs/prompts/multi-tenant-lightweight-chat-planning-prompt.md` | Move | `engineering-history/prompts/multi-tenant-lightweight-chat-planning-prompt.md` |
| `docs/platform-docs/agent-memory-systems.md` | Move | `engineering-history/research/agent-memory-systems.md` |
| `docs/platform-docs/copilot-extensibility.md` | Move | `engineering-history/research/copilot-extensibility.md` |
| `docs/platform-docs/github-cli.md` | Move | `engineering-history/research/github-cli.md` |
| `docs/platform-docs/github-copilot-extensions.md` | Move | `engineering-history/research/github-copilot-extensions.md` |
| `docs/platform-docs/teams-bot-framework.md` | Move | `engineering-history/research/teams-bot-framework.md` |
| `docs/platform-docs/teams-toolkit.md` | Move | `engineering-history/research/teams-toolkit.md` |
| `docs/platform-docs/mxc-windows-sandbox.md` | Move | `engineering-history/research/mxc-windows-sandbox.md` |
| `docs/runbooks/hard-won-learnings.md` | Move | `engineering-history/research/hard-won-learnings.md` |
| `docs/runbooks/mcp-disconnect-investigation.md` | Move | `engineering-history/investigations/mcp-disconnect-investigation.md` |
| `docs/runbooks/cert-auth-migration.md` | Move | `engineering-history/investigations/cert-auth-migration.md` |
| `docs/runbooks/windows-setup.md` | Move | `engineering-history/investigations/windows-setup.md` |

`docs/reference/mcp-tools.md` and `docs/reference/api/mcp-tools.md` do not appear in this table: Task 7.0 (Phase 7) already consolidates them into the single canonical `docs/reference/mcp-tools.md` before this phase runs, so neither path is "remaining" by the time Phase 9 executes.

### Task 9.1: Create the engineering-history subdirectory structure

- [ ] **Step 1: Create every destination directory**

```bash
mkdir -p engineering-history/architecture engineering-history/investigations engineering-history/research engineering-history/prompts
# engineering-history/plans/ and engineering-history/specs/ already exist (this plan and the source spec live there)
# engineering-history/decisions/ already exists (created in Task 3.5, which archived the ADRs)
```

- [ ] **Step 2: Commit**

```bash
git add -A engineering-history
git commit -m "chore: create engineering-history subdirectories for historical migration" --allow-empty
```

### Task 9.2: Delete the 8 superseded/duplicate files (facts already extracted into new pages in Phases 3-8)

- [ ] **Step 1: Confirm every deletion target's content is represented somewhere in the new tree before deleting (spot-check three)**

```bash
grep -l "channel-push\|sponsor_reply" docs/clients/overview.md docs/clients/claude-code.md docs/clients/copilot-cli.md
grep -l "HTML.*angle-bracket\|_summarize_content" docs/troubleshooting/mcp-connectivity.md
grep -l "XPIA\|cross-prompt injection" docs/architecture/security-boundaries.md
```

Expected: each grep prints the expected filename(s), confirming the facts survived the fold-in.

- [ ] **Step 2: Delete the 8 files that are pure duplicates or fully-superseded working notes (not part of the required historical-migration list — see Task 9.3 for the files that must be archived instead of deleted)**

```bash
git rm docs/AGENT-PROMPT-persona-sati-integration.md
git rm docs/AGENT-PROMPT-provisioner-cert-auth.md
git rm docs/TODO-persona-sati-host-bootstrap.md
git rm docs/TODO-persona-sati-integration.md
git rm docs/claude-windows-port.md
git rm docs/openai-copilot-cli-notifications.md
git rm docs/openai-windows-agent-identity-port.md
git rm docs/architecture/enforcement-flow.md
```

- [ ] **Step 3: Run the Phase 1 forbidden-prefix and legacy-paths tests — both should show progress**

```bash
pytest tests/docs/test_no_historical_prefixes.py tests/docs/test_legacy_paths_removed.py -v
```

Expected: `test_no_historical_prefixes.py` still fails (many prefixed files remain — Task 9.3 finishes the job); `test_legacy_paths_removed.py` still fails until Task 9.3 archives `architecture/PLAN-windows-port.md`, `architecture/next-mcp-server-design.md`, `claude-copilot-cli-channel-port.md`, and `architecture/NEXT-WhatsApp-lightweight-teams-chat.md` out of `docs/`.

- [ ] **Step 4: Commit**

```bash
git commit -m "docs: delete superseded/duplicate historical files after folding facts into new pages"
```

### Task 9.3: Archive the remaining 34 files to engineering-history (includes the 7 files that must be preserved, not deleted, per the approved spec's required migrations)

- [ ] **Step 1: Plans (16 files)**

```bash
git mv docs/PLAN-mind-body-nervous-system-phase-3.md engineering-history/plans/PLAN-mind-body-nervous-system-phase-3.md
git mv docs/PLAN-persona-sati-bootstrap-phases-1-2.md engineering-history/plans/PLAN-persona-sati-bootstrap-phases-1-2.md
git mv docs/claude-copilot-cli-channel-port.md engineering-history/plans/claude-copilot-cli-channel-port.md
git mv docs/architecture/NEXT-WhatsApp-lightweight-teams-chat.md engineering-history/plans/NEXT-WhatsApp-lightweight-teams-chat.md
git mv docs/architecture/PLAN-windows-port.md engineering-history/plans/PLAN-windows-port.md
git mv docs/architecture/PLAN-agent-identity-by-upn.md engineering-history/plans/PLAN-agent-identity-by-upn.md
git mv docs/architecture/PLAN-xpia-content-wrapping.md engineering-history/plans/PLAN-xpia-content-wrapping.md
git mv docs/architecture/next-mcp-server-design.md engineering-history/plans/next-mcp-server-design.md
git mv docs/architecture/next-tenant-identity-setup.md engineering-history/plans/next-tenant-identity-setup.md
git mv docs/architecture/next-windows-dev-environment.md engineering-history/plans/next-windows-dev-environment.md
git mv docs/architecture/PLAN-entrabot-new-features.md engineering-history/plans/PLAN-entrabot-new-features.md
git mv docs/architecture/PLAN-files-llm-authoring-v2.md engineering-history/plans/PLAN-files-llm-authoring-v2.md
git mv docs/architecture/PLAN-files-mcp-tools.md engineering-history/plans/PLAN-files-mcp-tools.md
git mv docs/architecture/PLAN-multi-tenant-lightweight-chat.md engineering-history/plans/PLAN-multi-tenant-lightweight-chat.md
git mv docs/architecture/PLAN-skills-layer.md engineering-history/plans/PLAN-skills-layer.md
git mv docs/plans/persona-persistence.md engineering-history/plans/persona-persistence.md
rmdir docs/plans
```

- [ ] **Step 2: Specs (2 files)**

```bash
git mv docs/architecture/FOURPAGER-entrabot-cli-teams-augmented-token.md engineering-history/specs/FOURPAGER-entrabot-cli-teams-augmented-token.md
git mv docs/architecture/SPEC-dual-track-agent-identity.md engineering-history/specs/SPEC-dual-track-agent-identity.md
```

- [ ] **Step 3: Investigations (4 files)**

```bash
git mv docs/SECURITY-DEBT-PROVISIONER-SECRET.md engineering-history/investigations/SECURITY-DEBT-PROVISIONER-SECRET.md
git mv docs/runbooks/mcp-disconnect-investigation.md engineering-history/investigations/mcp-disconnect-investigation.md
git mv docs/runbooks/cert-auth-migration.md engineering-history/investigations/cert-auth-migration.md
git mv docs/runbooks/windows-setup.md engineering-history/investigations/windows-setup.md
```

- [ ] **Step 4: Architecture (2 files)**

```bash
git mv docs/architecture/DESIGN-multi-instance-cursor-consistency.md engineering-history/architecture/DESIGN-multi-instance-cursor-consistency.md
git mv docs/architecture/DESIGN-persona-sati-integration.md engineering-history/architecture/DESIGN-persona-sati-integration.md
```

- [ ] **Step 5: Research (9 files)**

```bash
git mv docs/architecture/DESIGN-mxc-sandbox.md engineering-history/research/DESIGN-mxc-sandbox.md
git mv docs/developer/docs-site.md engineering-history/research/docs-site.md
git mv docs/developer/qa-log.md engineering-history/research/qa-log.md
rmdir docs/developer
git mv docs/platform-docs/agent-memory-systems.md engineering-history/research/agent-memory-systems.md
git mv docs/platform-docs/copilot-extensibility.md engineering-history/research/copilot-extensibility.md
git mv docs/platform-docs/github-cli.md engineering-history/research/github-cli.md
git mv docs/platform-docs/github-copilot-extensions.md engineering-history/research/github-copilot-extensions.md
git mv docs/platform-docs/teams-bot-framework.md engineering-history/research/teams-bot-framework.md
git mv docs/platform-docs/teams-toolkit.md engineering-history/research/teams-toolkit.md
git mv docs/platform-docs/mxc-windows-sandbox.md engineering-history/research/mxc-windows-sandbox.md
git mv docs/runbooks/hard-won-learnings.md engineering-history/research/hard-won-learnings.md
rmdir docs/runbooks
```

Note: `rmdir docs/runbooks` is repeated at the end of Step 5 as a guard in case Step 3's `git mv` calls leave the directory non-empty until Step 5's `hard-won-learnings.md` move completes; run all `git mv` calls in a directory before its `rmdir`, and re-run `rmdir` once more here if your shell reports "directory not empty" after Step 3.

- [ ] **Step 6: Prompts (1 file)**

```bash
git mv docs/prompts/multi-tenant-lightweight-chat-planning-prompt.md engineering-history/prompts/multi-tenant-lightweight-chat-planning-prompt.md
rmdir docs/prompts
```

- [ ] **Step 7: Confirm no empty directories remain under docs/ and no forbidden-prefix files remain**

```bash
find docs -type d -empty
pytest tests/docs/test_no_historical_prefixes.py tests/docs/test_legacy_paths_removed.py -v
```

Expected: no output from `find` (no empty directories); both tests PASS.

- [ ] **Step 8: Commit**

```bash
git add -A docs engineering-history
git commit -m "docs: archive remaining historical plans, specs, decisions, investigations, research, and prompts to engineering-history"
```

### Task 9.4: Re-run the full Phase 1 docs test suite to confirm migration progress

- [ ] **Step 1: Run all docs structure tests**

```bash
pytest tests/docs -v
```

Expected: `test_no_historical_prefixes.py`, `test_no_agent_attribution.py`, `test_legacy_paths_removed.py`, `test_script_commands_manifest.py` PASS. `test_nav_targets_exist.py` and `test_all_pages_in_nav.py` will FAIL or ERROR at this point because `mkdocs.yml`'s nav still references the old tree shape (renamed/moved paths) — this is expected and resolved in Phase 10. `test_redirects.py` still fails (no `redirects` plugin configured yet — resolved in Phase 11).

- [ ] **Step 2: Do not attempt to fix nav yet — that is Phase 10's job. Record the current failure list for reference**

```bash
pytest tests/docs -v 2>&1 | tail -n 30
```

---

## Phase 10: Rebuild mkdocs.yml with exactly 9 top-level sections

**Files:**
- Modify: `mkdocs.yml`

### Task 10.1: Replace mkdocs.yml in full

- [ ] **Step 1: Replace the entire file content**

```yaml
site_name: "Entrabot Identity Research"
site_description: "Device-local agent identity with Microsoft Entra Agent ID and Agent User"
docs_dir: docs
site_dir: site
site_url: https://microsoft.github.io/entrabot/
repo_url: https://github.com/microsoft/entrabot
repo_name: microsoft/entrabot

theme:
  name: material
  features:
    - navigation.sections
    - navigation.expand
    - navigation.instant
    - search.suggest
    - search.highlight
    - content.code.copy

markdown_extensions:
  - admonition
  - tables
  - toc:
      permalink: true
  - pymdownx.superfences
  - pymdownx.highlight:
      anchor_linenums: true
  - pymdownx.snippets:
      base_path: ["."]

plugins:
  - search
  - redirects:
      redirect_maps: {}  # populated in Phase 11

nav:
  - Home: index.md
  - Getting Started:
      - Quickstart: getting-started/quickstart.md
      - Prerequisites: getting-started/prerequisites.md
      - macOS and Linux: getting-started/macos-linux.md
      - Windows: getting-started/windows.md
      - Verify Your Agent Identity: getting-started/verify.md
  - Guides:
      - Configuration Reference: guides/configuration.md
      - Storage Configuration and Migration: guides/storage-configuration.md
      - Customizing the Body Prompt: guides/customizing-the-body-prompt.md
      - Teams and Chat Workflows: guides/teams-and-chat-workflows.md
      - Email Workflows: guides/email-workflows.md
      - Files and Microsoft Agent 365 Work IQ: guides/files-and-work-iq.md
      - Identity Lifecycle and Deprovisioning: guides/identity-lifecycle.md
  - Clients:
      - Overview: clients/overview.md
      - Claude Code: clients/claude-code.md
      - GitHub Copilot CLI: clients/copilot-cli.md
      - Other MCP Hosts: clients/other-hosts.md
      - Persona-Sati Host Bootstrap: clients/persona-sati-host-bootstrap.md
  - Architecture:
      - System Overview: architecture/system-overview.md
      - Components:
          - Platform: architecture/layers/platform.md
          - Authentication: architecture/layers/auth.md
          - Teams: architecture/layers/teams.md
          - Audit: architecture/layers/audit.md
      - Identity and Token Flow: architecture/identity-and-token-flow.md
      - MCP Runtime: architecture/mcp-runtime.md
      - Messaging and Channel Delivery: architecture/messaging-and-delivery.md
      - Storage and Memory: architecture/storage-and-memory.md
      - Security Boundaries: architecture/security-boundaries.md
      - Cross-Platform and Windows Architecture: architecture/windows-and-platforms.md
  - Platform Docs:
      - Agent ID Blueprints and Users: platform-docs/agent-id-blueprints-and-users.md
      - Agent Users: platform-docs/entra-agent-users.md
      - Microsoft Agent 365: platform-docs/microsoft-agent-365.md
      - Delegated Mode (MSAL Auth): platform-docs/delegated-msal-auth.md
      - Teams Graph API: platform-docs/teams-graph-api.md
      - Files Graph API: platform-docs/files-graph-api.md
      - MCP Hosts and Transports: platform-docs/mcp-hosts-and-transports.md
      - macOS: platform-docs/platform-macos.md
      - Linux: platform-docs/platform-linux.md
      - Windows: platform-docs/platform-windows.md
  - Reference:
      - MCP Tools: reference/mcp-tools.md
      - Configuration: reference/configuration.md
      - Token Flows: reference/token-flows.md
      - Scripts:
          - Overview: reference/scripts/index.md
          - Operations:
              - status.sh: reference/scripts/operations/status-sh.md
              - status-windows.ps1: reference/scripts/operations/status-windows-ps1.md
              - show_agent_status.py: reference/scripts/operations/show-agent-status-py.md
              - health_check.py: reference/scripts/operations/health-check-py.md
              - catch_up.py: reference/scripts/operations/catch-up-py.md
              - dm.py: reference/scripts/operations/dm-py.md
              - read_email.py: reference/scripts/operations/read-email-py.md
              - show_permissions.py: reference/scripts/operations/show-permissions-py.md
          - Setup:
              - setup.sh: reference/scripts/setup/setup-sh.md
              - setup-windows.ps1: reference/scripts/setup/setup-windows-ps1.md
              - setup-windows.cmd: reference/scripts/setup/setup-windows-cmd.md
              - prereqs-macos.sh: reference/scripts/setup/prereqs-macos-sh.md
              - prereqs-windows.ps1: reference/scripts/setup/prereqs-windows-ps1.md
              - setup_delegated.sh: reference/scripts/setup/setup-delegated-sh.md
              - setup_ado_credentials.sh: reference/scripts/setup/setup-ado-credentials-sh.md
              - deploy-windows.ps1: reference/scripts/setup/deploy-windows-ps1.md
          - Teardown:
              - teardown.sh: reference/scripts/teardown/teardown-sh.md
              - teardown-windows.ps1: reference/scripts/teardown/teardown-windows-ps1.md
              - deprovision_entra_agent_identity.py: reference/scripts/teardown/deprovision-entra-agent-identity-py.md
              - cleanup-orphans.sh: reference/scripts/teardown/cleanup-orphans-sh.md
          - Provisioning:
              - create_entra_agent_ids.py: reference/scripts/provisioning/create-entra-agent-ids-py.md
              - add_agent_sponsor.py: reference/scripts/provisioning/add-agent-sponsor-py.md
              - remove_agent_sponsor.py: reference/scripts/provisioning/remove-agent-sponsor-py.md
              - assign_agent_user_licenses.py: reference/scripts/provisioning/assign-agent-user-licenses-py.md
              - remove_agent_user_licenses.py: reference/scripts/provisioning/remove-agent-user-licenses-py.md
              - ensure_a365_work_iq_permissions.py: reference/scripts/provisioning/ensure-a365-work-iq-permissions-py.md
          - Auth and Certificates:
              - grant_consent.py: reference/scripts/auth-and-certs/grant-consent-py.md
              - grant_files_consent.py: reference/scripts/auth-and-certs/grant-files-consent-py.md
              - revoke_consent.py: reference/scripts/auth-and-certs/revoke-consent-py.md
              - provisioner-token.py: reference/scripts/auth-and-certs/provisioner-token-py.md
              - find_local_blueprint_cert.py: reference/scripts/auth-and-certs/find-local-blueprint-cert-py.md
              - list_blueprint_certs.py: reference/scripts/auth-and-certs/list-blueprint-certs-py.md
              - verify_blueprint_cert.py: reference/scripts/auth-and-certs/verify-blueprint-cert-py.md
              - generate_windows_cert.py: reference/scripts/auth-and-certs/generate-windows-cert-py.md
              - rotate_cert_windows.py: reference/scripts/auth-and-certs/rotate-cert-windows-py.md
          - Storage:
              - provision_blob_storage.py: reference/scripts/storage/provision-blob-storage-py.md
              - deprovision_blob_storage.py: reference/scripts/storage/deprovision-blob-storage-py.md
          - Diagnostics:
              - entrabot-mcp-debug.sh: reference/scripts/diagnostics/entrabot-mcp-debug-sh.md
              - diagnose-chat.py: reference/scripts/diagnostics/diagnose-chat-py.md
              - diagnose_sponsor_emails.py: reference/scripts/diagnostics/diagnose-sponsor-emails-py.md
              - list_agent_identities.py: reference/scripts/diagnostics/list-agent-identities-py.md
              - list_sponsors.py: reference/scripts/diagnostics/list-sponsors-py.md
      - API:
          - Storage Backends: reference/api/storage-backends.md
          - Authentication: reference/api/auth.md
          - Identity: reference/api/identity.md
          - Audit: reference/api/audit.md
          - Security (XPIA): reference/api/security.md
          - Efferent Copy: reference/api/efferent-copy.md
          - Body Prompt: reference/api/body-prompt.md
  - Troubleshooting:
      - Overview: troubleshooting/index.md
      - Setup and Authentication: troubleshooting/setup-and-authentication.md
      - Teams and Email: troubleshooting/teams-and-email.md
      - Windows: troubleshooting/windows.md
      - Storage: troubleshooting/storage.md
      - MCP Connectivity: troubleshooting/mcp-connectivity.md
      - Migrations and Upgrades: troubleshooting/migrations-and-upgrades.md
  - Project:
      - Current Status: project/status.md
      - Changelog: project/changelog.md
```

**Note:** `status.sh` and `status-windows.ps1` are documented as two separate generated pages (`reference/scripts/operations/status-sh.md` and `reference/scripts/operations/status-windows-ps1.md`) and therefore need two separate nav entries — both must appear per Task 1.6's all-pages-in-nav test.

- [ ] **Step 2: Run the nav-completeness tests**

```bash
pytest tests/docs/test_nav_targets_exist.py tests/docs/test_all_pages_in_nav.py -v
```

Expected: both PASS. If `test_all_pages_in_nav.py` still reports orphans, cross-check the reported paths against the nav block above — every one of the 42 generated script pages, the relocated MCP Tools page, the new Security (XPIA) API page, all guide/client/architecture/platform-docs/troubleshooting/project pages must be present. Add any missing line and re-run until green.

- [ ] **Step 3: Run the full docs suite (redirects test is still expected to fail — Phase 11)**

```bash
pytest tests/docs -v
```

Expected: all PASS except `test_redirects.py::test_redirects_plugin_is_configured` (empty `redirect_maps: {}` placeholder from Step 1 is deliberately non-empty-map-shaped but has zero entries, so the test correctly still fails until Phase 11 populates it).

- [ ] **Step 4: Attempt a strict build (some links may still 404 until Phase 11's redirects land for pages that reference old paths — note failures, do not fix here beyond confirming nav is internally consistent)**

```bash
mkdocs build --strict --site-dir site 2>&1 | tail -n 50
rm -rf site
```

- [ ] **Step 5: Commit**

```bash
git add mkdocs.yml
git commit -m "docs: rebuild mkdocs.yml nav with 9 top-level sections"
```

---

## Phase 11: Add redirects

**Files:**
- Modify: `mkdocs.yml` (populate the `redirects.redirect_maps` block added in Task 10.1)

### Full redirect mapping table

`mkdocs-redirects` maps old `docs_dir`-relative paths (as they existed before this redesign) to new `docs_dir`-relative paths. Every source path below is a page that was deleted, moved, or renamed in Phases 3–9. Every target path is a page currently in the Phase 10 nav (this is what `tests/docs/test_redirects.py::test_every_redirect_target_is_a_current_nav_page` enforces).

| Old path (redirect source) | New path (redirect target) |
| --- | --- |
| `architecture/PLAN-windows-port.md` | `architecture/windows-and-platforms.md` |
| `architecture/next-mcp-server-design.md` | `architecture/mcp-runtime.md` |
| `claude-copilot-cli-channel-port.md` | `clients/overview.md` |
| `architecture/NEXT-WhatsApp-lightweight-teams-chat.md` | `architecture/messaging-and-delivery.md` |
| `architecture/PLAN-agent-identity-by-upn.md` | `architecture/identity-and-token-flow.md` |
| `architecture/PLAN-xpia-content-wrapping.md` | `architecture/security-boundaries.md` |
| `architecture/DESIGN-persona-sati-integration.md` | `architecture/system-overview.md` |
| `architecture/enforcement-flow.md` | `architecture/security-boundaries.md` |
| `architecture/DESIGN-multi-instance-cursor-consistency.md` | `architecture/system-overview.md` |
| `architecture/PLAN-entrabot-new-features.md` | `architecture/system-overview.md` |
| `architecture/PLAN-skills-layer.md` | `architecture/mcp-runtime.md` |
| `architecture/DESIGN-mxc-sandbox.md` | `architecture/windows-and-platforms.md` |
| `architecture/SPEC-dual-track-agent-identity.md` | `architecture/identity-and-token-flow.md` |
| `architecture/PLAN-multi-tenant-lightweight-chat.md` | `architecture/messaging-and-delivery.md` |
| `architecture/next-tenant-identity-setup.md` | `architecture/identity-and-token-flow.md` |
| `architecture/PLAN-files-mcp-tools.md` | `reference/mcp-tools.md` |
| `architecture/PLAN-files-llm-authoring-v2.md` | `guides/files-and-work-iq.md` |
| `architecture/next-windows-dev-environment.md` | `troubleshooting/windows.md` |
| `architecture/FOURPAGER-entrabot-cli-teams-augmented-token.md` | `architecture/identity-and-token-flow.md` |
| `claude-windows-port.md` | `architecture/windows-and-platforms.md` |
| `openai-copilot-cli-notifications.md` | `clients/copilot-cli.md` |
| `openai-windows-agent-identity-port.md` | `architecture/windows-and-platforms.md` |
| `AGENT-PROMPT-persona-sati-integration.md` | `clients/persona-sati-host-bootstrap.md` |
| `AGENT-PROMPT-provisioner-cert-auth.md` | `architecture/identity-and-token-flow.md` |
| `TODO-persona-sati-host-bootstrap.md` | `clients/persona-sati-host-bootstrap.md` |
| `TODO-persona-sati-integration.md` | `clients/persona-sati-host-bootstrap.md` |
| `PLAN-mind-body-nervous-system-phase-3.md` | `architecture/system-overview.md` |
| `PLAN-persona-sati-bootstrap-phases-1-2.md` | `clients/persona-sati-host-bootstrap.md` |
| `SECURITY-DEBT-PROVISIONER-SECRET.md` | `architecture/identity-and-token-flow.md` |
| `plans/persona-persistence.md` | `architecture/storage-and-memory.md` |
| `prompts/multi-tenant-lightweight-chat-planning-prompt.md` | `architecture/messaging-and-delivery.md` |
| `engineering-status.md` | `project/status.md` |
| `decisions/README.md` | `architecture/system-overview.md` |
| `decisions/001-obo-flows-for-device-agents.md` | `architecture/identity-and-token-flow.md` |
| `decisions/002-agent-user-over-obo.md` | `architecture/identity-and-token-flow.md` |
| `decisions/003-certificate-auth-over-client-secrets.md` | `architecture/identity-and-token-flow.md` |
| `decisions/005-cloud-hosted-memory.md` | `architecture/storage-and-memory.md` |
| `decisions/006-remove-bot-gateway-mode.md` | `architecture/messaging-and-delivery.md` |
| `platform-learnings/agent-id-blueprints-and-users.md` | `platform-docs/agent-id-blueprints-and-users.md` |
| `platform-learnings/entra-agent-users.md` | `platform-docs/entra-agent-users.md` |
| `platform-learnings/microsoft-agent-365.md` | `platform-docs/microsoft-agent-365.md` |
| `platform-learnings/teams-graph-api.md` | `platform-docs/teams-graph-api.md` |
| `platform-learnings/files-graph-api.md` | `platform-docs/files-graph-api.md` |
| `platform-learnings/platform-macos.md` | `platform-docs/platform-macos.md` |
| `platform-learnings/platform-linux.md` | `platform-docs/platform-linux.md` |
| `platform-learnings/platform-windows.md` | `platform-docs/platform-windows.md` |
| `platform-learnings/msal-entra-agent-ids.md` | `platform-docs/delegated-msal-auth.md` |
| `platform-learnings/mcp-messaging-servers.md` | `platform-docs/mcp-hosts-and-transports.md` |
| `platform-learnings/mcp-close-the-loop.md` | `platform-docs/mcp-hosts-and-transports.md` |
| `platform-learnings/agent-memory-systems.md` | `architecture/storage-and-memory.md` |
| `platform-learnings/copilot-extensibility.md` | `clients/other-hosts.md` |
| `platform-learnings/github-copilot-extensions.md` | `clients/other-hosts.md` |
| `platform-learnings/github-cli.md` | `reference/scripts/index.md` |
| `platform-learnings/teams-bot-framework.md` | `platform-docs/teams-graph-api.md` |
| `platform-learnings/teams-toolkit.md` | `platform-docs/teams-graph-api.md` |
| `platform-learnings/mxc-windows-sandbox.md` | `architecture/windows-and-platforms.md` |
| `reference/api/mcp-tools.md` | `reference/mcp-tools.md` |
| `reference/scripts/setup.md` | `reference/scripts/index.md` |
| `reference/scripts/provisioning.md` | `reference/scripts/index.md` |
| `reference/scripts/auth-and-certs.md` | `reference/scripts/index.md` |
| `reference/scripts/storage.md` | `reference/scripts/index.md` |
| `reference/scripts/operations.md` | `reference/scripts/index.md` |
| `reference/scripts/diagnostics.md` | `reference/scripts/index.md` |
| `reference/scripts/teardown.md` | `reference/scripts/index.md` |
| `reference/scripts/spikes.md` | `reference/scripts/index.md` |
| `runbooks/hard-won-learnings.md` | `troubleshooting/index.md` |
| `runbooks/mcp-disconnect-investigation.md` | `troubleshooting/mcp-connectivity.md` |
| `runbooks/cert-auth-migration.md` | `troubleshooting/setup-and-authentication.md` |
| `runbooks/windows-setup.md` | `troubleshooting/windows.md` |
| `developer/docs-site.md` | `project/status.md` |
| `developer/qa-log.md` | `project/status.md` |

That is 71 redirect entries, covering the 8 mandatory historical migrations (per the approved spec) plus every other removed/renamed public URL from Phases 3–9.

### Task 11.1: Populate the redirect_maps block in mkdocs.yml

- [ ] **Step 1: Replace the placeholder plugins block**

```
Old:
plugins:
  - search
  - redirects:
      redirect_maps: {}  # populated in Phase 11

New:
plugins:
  - search
  - redirects:
      redirect_maps:
        architecture/PLAN-windows-port.md: architecture/windows-and-platforms.md
        architecture/next-mcp-server-design.md: architecture/mcp-runtime.md
        claude-copilot-cli-channel-port.md: clients/overview.md
        architecture/NEXT-WhatsApp-lightweight-teams-chat.md: architecture/messaging-and-delivery.md
        architecture/PLAN-agent-identity-by-upn.md: architecture/identity-and-token-flow.md
        architecture/PLAN-xpia-content-wrapping.md: architecture/security-boundaries.md
        architecture/DESIGN-persona-sati-integration.md: architecture/system-overview.md
        architecture/enforcement-flow.md: architecture/security-boundaries.md
        architecture/DESIGN-multi-instance-cursor-consistency.md: architecture/system-overview.md
        architecture/PLAN-entrabot-new-features.md: architecture/system-overview.md
        architecture/PLAN-skills-layer.md: architecture/mcp-runtime.md
        architecture/DESIGN-mxc-sandbox.md: architecture/windows-and-platforms.md
        architecture/SPEC-dual-track-agent-identity.md: architecture/identity-and-token-flow.md
        architecture/PLAN-multi-tenant-lightweight-chat.md: architecture/messaging-and-delivery.md
        architecture/next-tenant-identity-setup.md: architecture/identity-and-token-flow.md
        architecture/PLAN-files-mcp-tools.md: reference/mcp-tools.md
        architecture/PLAN-files-llm-authoring-v2.md: guides/files-and-work-iq.md
        architecture/next-windows-dev-environment.md: troubleshooting/windows.md
        architecture/FOURPAGER-entrabot-cli-teams-augmented-token.md: architecture/identity-and-token-flow.md
        claude-windows-port.md: architecture/windows-and-platforms.md
        openai-copilot-cli-notifications.md: clients/copilot-cli.md
        openai-windows-agent-identity-port.md: architecture/windows-and-platforms.md
        AGENT-PROMPT-persona-sati-integration.md: clients/persona-sati-host-bootstrap.md
        AGENT-PROMPT-provisioner-cert-auth.md: architecture/identity-and-token-flow.md
        TODO-persona-sati-host-bootstrap.md: clients/persona-sati-host-bootstrap.md
        TODO-persona-sati-integration.md: clients/persona-sati-host-bootstrap.md
        PLAN-mind-body-nervous-system-phase-3.md: architecture/system-overview.md
        PLAN-persona-sati-bootstrap-phases-1-2.md: clients/persona-sati-host-bootstrap.md
        SECURITY-DEBT-PROVISIONER-SECRET.md: architecture/identity-and-token-flow.md
        plans/persona-persistence.md: architecture/storage-and-memory.md
        prompts/multi-tenant-lightweight-chat-planning-prompt.md: architecture/messaging-and-delivery.md
        engineering-status.md: project/status.md
        decisions/README.md: architecture/system-overview.md
        decisions/001-obo-flows-for-device-agents.md: architecture/identity-and-token-flow.md
        decisions/002-agent-user-over-obo.md: architecture/identity-and-token-flow.md
        decisions/003-certificate-auth-over-client-secrets.md: architecture/identity-and-token-flow.md
        decisions/005-cloud-hosted-memory.md: architecture/storage-and-memory.md
        decisions/006-remove-bot-gateway-mode.md: architecture/messaging-and-delivery.md
        platform-learnings/agent-id-blueprints-and-users.md: platform-docs/agent-id-blueprints-and-users.md
        platform-learnings/entra-agent-users.md: platform-docs/entra-agent-users.md
        platform-learnings/microsoft-agent-365.md: platform-docs/microsoft-agent-365.md
        platform-learnings/teams-graph-api.md: platform-docs/teams-graph-api.md
        platform-learnings/files-graph-api.md: platform-docs/files-graph-api.md
        platform-learnings/platform-macos.md: platform-docs/platform-macos.md
        platform-learnings/platform-linux.md: platform-docs/platform-linux.md
        platform-learnings/platform-windows.md: platform-docs/platform-windows.md
        platform-learnings/msal-entra-agent-ids.md: platform-docs/delegated-msal-auth.md
        platform-learnings/mcp-messaging-servers.md: platform-docs/mcp-hosts-and-transports.md
        platform-learnings/mcp-close-the-loop.md: platform-docs/mcp-hosts-and-transports.md
        platform-learnings/agent-memory-systems.md: architecture/storage-and-memory.md
        platform-learnings/copilot-extensibility.md: clients/other-hosts.md
        platform-learnings/github-copilot-extensions.md: clients/other-hosts.md
        platform-learnings/github-cli.md: reference/scripts/index.md
        platform-learnings/teams-bot-framework.md: platform-docs/teams-graph-api.md
        platform-learnings/teams-toolkit.md: platform-docs/teams-graph-api.md
        platform-learnings/mxc-windows-sandbox.md: architecture/windows-and-platforms.md
        reference/api/mcp-tools.md: reference/mcp-tools.md
        reference/scripts/setup.md: reference/scripts/index.md
        reference/scripts/provisioning.md: reference/scripts/index.md
        reference/scripts/auth-and-certs.md: reference/scripts/index.md
        reference/scripts/storage.md: reference/scripts/index.md
        reference/scripts/operations.md: reference/scripts/index.md
        reference/scripts/diagnostics.md: reference/scripts/index.md
        reference/scripts/teardown.md: reference/scripts/index.md
        reference/scripts/spikes.md: reference/scripts/index.md
        runbooks/hard-won-learnings.md: troubleshooting/index.md
        runbooks/mcp-disconnect-investigation.md: troubleshooting/mcp-connectivity.md
        runbooks/cert-auth-migration.md: troubleshooting/setup-and-authentication.md
        runbooks/windows-setup.md: troubleshooting/windows.md
        developer/docs-site.md: project/status.md
        developer/qa-log.md: project/status.md
```

- [ ] **Step 2: Validate the YAML parses and has exactly 71 entries**

```bash
python3 -c "
import yaml
data = yaml.safe_load(open('mkdocs.yml'))
maps = None
for p in data['plugins']:
    if isinstance(p, dict) and 'redirects' in p:
        maps = p['redirects']['redirect_maps']
assert maps is not None, 'redirects plugin not found'
assert len(maps) == 71, len(maps)
print('OK', len(maps))
"
```

Expected: `OK 71`.

- [ ] **Step 3: Run the full Phase 1 docs test suite — everything should now pass**

```bash
pytest tests/docs -v
```

Expected: ALL tests PASS.

- [ ] **Step 4: Run a strict MkDocs build to confirm the redirect targets resolve and no broken links remain**

```bash
mkdocs build --strict --site-dir site
echo "exit code: $?"
rm -rf site
```

Expected: exit code `0`, no strict-mode warnings.

- [ ] **Step 5: Commit**

```bash
git add mkdocs.yml
git commit -m "docs: add redirect map for all moved and renamed public pages"
```

---

## Phase 12: Update root docs, code comments, and internal instructions

**Files:**
- Modify: `README.md`, `AGENTS.md`, `CLAUDE.md`, `.github/copilot-instructions.md`, `.claude/skills/implement-agent-id/SKILL.md`, `TODOS.md`, `CHANGELOG.md`, `.github/workflows/test-windows.yml`, `pyproject.toml`
- Modify: `src/entrabot/tools/files.py`, `src/entrabot/tools/dispatch.py`, `src/entrabot/tools/teams.py`, `src/entrabot/tools/wait_tool.py`, `src/entrabot/config.py`, `src/entrabot/security/xpia.py`, `src/entrabot/mcp_server.py`, `src/entrabot/preflight.py`
- Modify: `tests/tools/test_watch.py`, `tests/tools/test_wait_for_sponsor_dm.py`, `tests/test_no_dead_imports.py`, `tests/security/test_xpia_wrap.py`, `tests/test_mcp_server_integration.py`
- Modify: `scripts/hooks/README.md`, `scripts/setup-windows.ps1`

**Rule applied throughout this phase:** internal governance/instruction files (`AGENTS.md`, `CLAUDE.md`, `.github/copilot-instructions.md`, `SKILL.md`, and code/test comments) may link into `engineering-history/` because they are read by contributors and agents working in the repository, not by public site visitors. Anything intended for an external reader (`README.md`'s public-facing sections) must link to the new public canonical page instead.

### Task 12.1: Update README.md

- [ ] **Step 1: Apply each replacement**

```
Old: ([platform learning](docs/platform-learnings/agent-id-blueprints-and-users.md))
New: ([platform docs](docs/platform-docs/agent-id-blueprints-and-users.md))

Old: ([platform learning](docs/platform-learnings/microsoft-agent-365.md))
New: ([platform docs](docs/platform-docs/microsoft-agent-365.md))

Old: Full host-by-host protocol: [`docs/claude-copilot-cli-channel-port.md`](docs/claude-copilot-cli-channel-port.md) and [`prompts/anatomy/channel-discipline.md`](prompts/anatomy/channel-discipline.md).
New: Full host-by-host protocol: [`docs/clients/overview.md`](docs/clients/overview.md) and [`prompts/anatomy/channel-discipline.md`](prompts/anatomy/channel-discipline.md).

Old: - [Setup script reference](docs/reference/scripts/setup.md) — every `setup.sh` and `setup-windows.ps1` flag
New: - [Setup script reference](docs/reference/scripts/setup/setup-sh.md) — every `setup.sh` and `setup-windows.ps1` flag

Old: - [Script reference](docs/reference/scripts/operations.md) — status, health, DM, email, setup, teardown, and diagnostic scripts
New: - [Script reference](docs/reference/scripts/index.md) — status, health, DM, email, setup, teardown, and diagnostic scripts

Old: - [Architecture decisions](docs/decisions/README.md) — ADRs 001–006
New: - [Architecture](docs/architecture/system-overview.md) — how the system fits together; historical ADR rationale is distilled into the architecture pages (originals archived internally at `engineering-history/decisions/`, not published)

Old: - [Platform learnings](docs/platform-learnings/) — Entra Agent ID constraints, Agent 365, MSAL, OS-specific notes
New: - [Platform docs](docs/platform-docs/) — Entra Agent ID constraints, Agent 365, MSAL, OS-specific notes

Old: - [Hard-won learnings](docs/runbooks/hard-won-learnings.md) — non-obvious gotchas; read before changing auth or Teams code
New: - [Troubleshooting](docs/troubleshooting/index.md) — non-obvious gotchas; read before changing auth or Teams code

Old: - [Engineering status](docs/engineering-status.md) — what's shipped, what's open, what's next
New: - [Project Status](docs/project/status.md) — what's shipped, what's open, what's next

Old: Long-session MCP disconnect investigation and several scheduler/cursor precision fixes remain tracked in [`docs/engineering-status.md`](docs/engineering-status.md).
New: Several scheduler/cursor precision fixes remain tracked in [`docs/project/status.md`](docs/project/status.md). The long-session MCP disconnect investigation is resolved — see [Troubleshooting: MCP Connectivity](docs/troubleshooting/mcp-connectivity.md).
```

- [ ] **Step 2: Verify no old paths remain in README.md**

```bash
grep -nE "docs/platform-learnings|docs/runbooks|docs/decisions/|docs/project/decisions|docs/architecture/(PLAN|DESIGN|NEXT|SPEC|FOURPAGER)-|docs/(claude|openai|AGENT-PROMPT|TODO)-|docs/engineering-status\.md|docs/project/engineering-status\.md|docs/reference/scripts/(setup|provisioning|auth-and-certs|storage|operations|diagnostics|teardown|spikes)\.md" README.md
```

Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: update README.md links for the public documentation redesign"
```

### Task 12.2: Update AGENTS.md and CLAUDE.md

Both files carry near-identical passages; apply the same substitutions to each.

- [ ] **Step 1: Apply each replacement to both AGENTS.md and CLAUDE.md**

```
Old: `docs/platform-learnings/agent-id-blueprints-and-users.md` first, every
New: `docs/platform-docs/agent-id-blueprints-and-users.md` first, every

Old: See Learning #69 and `docs/architecture/PLAN-agent-identity-by-upn.md`.
New: See Learning #69 (`engineering-history/research/hard-won-learnings.md`) and `docs/architecture/identity-and-token-flow.md`.

Old (table row, AGENTS.md only):
| OAuth, OBO, PKCE, redirect URIs, JWT        | `docs/platform-learnings/agent-id-blueprints-and-users.md`      |
| Agent Identity / Blueprint / User           | `docs/platform-learnings/agent-id-blueprints-and-users.md`      |
| MSAL, token acquisition, three-hop flow     | `docs/platform-learnings/msal-entra-agent-ids.md`               |
| Agent User onboarding / consent             | `docs/platform-learnings/entra-agent-users.md`                  |
| Teams Graph / chat tools                    | `docs/platform-learnings/teams-graph-api.md`                    |
| Files Graph (drives, items, comments)       | `docs/platform-learnings/files-graph-api.md`                    |
| MCP server design (transports, lifecycle)   | `docs/platform-learnings/mcp-messaging-servers.md` + `mcp-close-the-loop.md` |
| Platform-specific (cert store, OS keystore) | `docs/platform-learnings/platform-{macos,linux,windows}.md`     |
| Sandboxing / execution containment (MXC)    | `docs/platform-learnings/mxc-windows-sandbox.md`                |
New:
| OAuth, OBO, PKCE, redirect URIs, JWT        | `docs/platform-docs/agent-id-blueprints-and-users.md`      |
| Agent Identity / Blueprint / User           | `docs/platform-docs/agent-id-blueprints-and-users.md`      |
| MSAL, token acquisition, three-hop flow     | `docs/platform-docs/delegated-msal-auth.md`               |
| Agent User onboarding / consent             | `docs/platform-docs/entra-agent-users.md`                  |
| Teams Graph / chat tools                    | `docs/platform-docs/teams-graph-api.md`                    |
| Files Graph (drives, items, comments)       | `docs/platform-docs/files-graph-api.md`                    |
| MCP server design (transports, lifecycle)   | `docs/platform-docs/mcp-hosts-and-transports.md` |
| Platform-specific (cert store, OS keystore) | `docs/platform-docs/platform-{macos,linux,windows}.md`     |
| Sandboxing / execution containment (MXC)    | `engineering-history/research/mxc-windows-sandbox.md` (research only, not shipped — see `docs/architecture/windows-and-platforms.md`) |

Old: `docs/TODO-persona-sati-integration.md` is now historical.
New: The historical persona-sati integration TODO has been removed; see `docs/clients/persona-sati-host-bootstrap.md` for the current protocol.

Old: **ADR-005: cloud-hosted memory via Azure Blob Storage** — `docs/decisions/005-cloud-hosted-memory.md`.
New: **ADR-005: cloud-hosted memory via Azure Blob Storage** (historical, archived at `engineering-history/decisions/005-cloud-hosted-memory.md`, not published — see `docs/architecture/storage-and-memory.md` for the current-state design).

Old: **Up next** (see `docs/engineering-status.md`
New: **Up next** (see `docs/project/status.md`

Old: - `docs/engineering-status.md` — current state and next steps
New: - `docs/project/status.md` — current state and next steps

Old: - `docs/architecture/DESIGN-persona-sati-integration.md` — mind-body split design
New: - `docs/architecture/system-overview.md` — mind-body split design (see the "Mind-Body Split" section)

Old: - `docs/decisions/005-cloud-hosted-memory.md` — cloud memory spec
New: - `engineering-history/decisions/005-cloud-hosted-memory.md` — cloud memory spec (historical, not published)

Old: - `docs/runbooks/hard-won-learnings.md` — read before making changes
New: - `engineering-history/research/hard-won-learnings.md` — read before making changes

Old: - `docs/decisions/`: ADRs — every significant architectural choice is recorded here
New: - `engineering-history/decisions/`: ADRs — every significant architectural choice is recorded here (historical, not published; current rationale lives in `docs/architecture/`)

Old: - `docs/runbooks/hard-won-learnings.md`: hard-won learnings — READ THIS before making changes
New: - `engineering-history/research/hard-won-learnings.md`: hard-won learnings — READ THIS before making changes
```

- [ ] **Step 2: Apply the CLAUDE.md-only additional replacements**

```
Old: See `docs/engineering-status.md` for the summary and `docs/architecture/DESIGN-persona-sati-integration.md` for the mind-body split design.
New: See `docs/project/status.md` for the summary and `docs/architecture/system-overview.md` (Mind-Body Split section) for the split design.

Old: `docs/TODO-persona-sati-integration.md` is now historical.
New: (same replacement as the shared block above)

Old: Memory sync hooks removed (persona-sati owns memory now). `scripts/claude_memory_sync.py` retained as manual migration tool.
New: (unchanged — no path reference)

Old: **Multi-tenant lightweight chat** — landed to `main` (commit `c8ec521`). Spec: `docs/architecture/NEXT-WhatsApp-lightweight-teams-chat.md`.
New: **Multi-tenant lightweight chat** — landed to `main` (commit `c8ec521`). See `docs/architecture/messaging-and-delivery.md`.

Old: **Up next** (see `docs/engineering-status.md` "In Progress")
New: **Up next** (see `docs/project/status.md` "In Progress")

Old: - **`docs/platform-learnings/agent-id-blueprints-and-users.md`** — REQUIRED reading
New: - **`docs/platform-docs/agent-id-blueprints-and-users.md`** — REQUIRED reading

Old: - `docs/platform-learnings/msal-entra-agent-ids.md` — supplementary; building on
New: - `docs/platform-docs/delegated-msal-auth.md` — supplementary; building on

Old: - `docs/platform-learnings/entra-agent-users.md` — supplementary; the three-hop
New: - `docs/platform-docs/entra-agent-users.md` — supplementary; the three-hop

Old: - `docs/engineering-status.md` — current state, test count, next steps
New: - `docs/project/status.md` — current state, test count, next steps

Old: - `docs/architecture/DESIGN-persona-sati-integration.md` — mind-body split design
New: - `docs/architecture/system-overview.md` — mind-body split design

Old: - `docs/decisions/005-cloud-hosted-memory.md` — cloud memory spec (phase plan + open TODOs)
New: - `engineering-history/decisions/005-cloud-hosted-memory.md` — cloud memory spec (phase plan + open TODOs; historical, not published)

Old: - `docs/decisions/006-remove-bot-gateway-mode.md` — why the Bot Gateway mode was removed
New: - `engineering-history/decisions/006-remove-bot-gateway-mode.md` — why the Bot Gateway mode was removed (historical, not published)

Old: - `docs/architecture/NEXT-WhatsApp-lightweight-teams-chat.md` — delegated mode spec (landed)
New: - `docs/architecture/messaging-and-delivery.md` — delegated mode spec (landed)

Old: - `docs/runbooks/mcp-disconnect-investigation.md` — **OPEN issue.** Entrabot MCP dies after 2–10 min of sustained activity. Two amplifiers fixed (PR #40, PR #41), root cause still unknown. Read this before debugging any MCP-drop symptom — do NOT restart the investigation from scratch.
New: - `engineering-history/investigations/mcp-disconnect-investigation.md` — **RESOLVED 2026-04-28.** Entrabot MCP disconnect root cause was unescaped Teams HTML in the channel-push notification content; see `docs/troubleshooting/mcp-connectivity.md` for the current-state summary and the fix location.

Old: - `docs/runbooks/hard-won-learnings.md` — read before making changes
New: - `engineering-history/research/hard-won-learnings.md` — read before making changes

Old: - `docs/decisions/001-obo-flows-for-device-agents.md`
New: - `engineering-history/decisions/001-obo-flows-for-device-agents.md` (historical, not published)

Old: - `docs/decisions/003-certificate-auth-over-client-secrets.md`
New: - `engineering-history/decisions/003-certificate-auth-over-client-secrets.md` (historical, not published)

Old: - `docs/platform-learnings/microsoft-agent-365.md` — A365 GA'd 2026-05-01. Identity model, Work IQ MCP catalog, four capability tiers, auth flows, gap analysis vs entrabot. Read this before considering any A365 / Work IQ integration work.
New: - `docs/platform-docs/microsoft-agent-365.md` — A365 GA'd 2026-05-01. Identity model, Work IQ MCP catalog, four capability tiers, auth flows, gap analysis vs entrabot. Read this before considering any A365 / Work IQ integration work.

Old: - `docs/platform-learnings/mcp-close-the-loop.md`
New: - `docs/platform-docs/mcp-hosts-and-transports.md`

Old: - `docs/decisions/`: ADRs — every significant architectural choice is recorded here
New: - `engineering-history/decisions/`: ADRs — every significant architectural choice is recorded here (historical, not published; current rationale lives in `docs/architecture/`)

Old: - `docs/runbooks/hard-won-learnings.md`: 66 hard-won learnings — READ THIS before making changes
New: - `engineering-history/research/hard-won-learnings.md`: 70 hard-won learnings — READ THIS before making changes

Old: - `docs/runbooks/mcp-disconnect-investigation.md`: OPEN MCP-disconnect dossier — READ before touching MCP transport, logging, or efferent-copy code
New: - `engineering-history/investigations/mcp-disconnect-investigation.md`: RESOLVED MCP-disconnect dossier (2026-04-28) — read for full historical trail; current-state guidance is `docs/troubleshooting/mcp-connectivity.md`
```

- [ ] **Step 3: Verify no old paths remain in either file**

```bash
grep -nE "docs/platform-learnings|docs/runbooks|docs/decisions/|docs/project/decisions|docs/architecture/(PLAN|DESIGN|NEXT|SPEC|FOURPAGER)-|docs/(claude|openai|AGENT-PROMPT|TODO)-|docs/engineering-status\.md|docs/project/engineering-status\.md" AGENTS.md CLAUDE.md
```

Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add AGENTS.md CLAUDE.md
git commit -m "docs: update AGENTS.md and CLAUDE.md links, correct resolved MCP disconnect status"
```

### Task 12.3: Update .github/copilot-instructions.md

- [ ] **Step 1: Apply each replacement**

```
Old: - Read `docs/runbooks/hard-won-learnings.md` before making auth/Teams changes
New: - Read `engineering-history/research/hard-won-learnings.md` before making auth/Teams changes

Old: - ADRs in `docs/decisions/` for all significant architectural choices
New: - ADRs in `engineering-history/decisions/` for all significant architectural choices (historical, not published; current rationale is distilled into `docs/architecture/`)
```

- [ ] **Step 2: Verify**

```bash
grep -nE "docs/platform-learnings|docs/runbooks|docs/decisions/|docs/project/decisions" .github/copilot-instructions.md
```

Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add .github/copilot-instructions.md
git commit -m "docs: update copilot-instructions.md links"
```

### Task 12.4: Update .claude/skills/implement-agent-id/SKILL.md

- [ ] **Step 1: Apply each replacement**

```
Old: - `docs/platform-learnings/agent-id-blueprints-and-users.md`
New: - `docs/platform-docs/agent-id-blueprints-and-users.md`

Old: - `docs/platform-learnings/entra-agent-users.md`
New: - `docs/platform-docs/entra-agent-users.md`

Old: - `docs/platform-learnings/msal-entra-agent-ids.md`
New: - `docs/platform-docs/delegated-msal-auth.md`

Old: - `docs/runbooks/hard-won-learnings.md`
New: - `engineering-history/research/hard-won-learnings.md`

Old: Use the current request shape in `scripts/create_entra_agent_ids.py` and `docs/platform-learnings/entra-agent-users.md`.
New: Use the current request shape in `scripts/create_entra_agent_ids.py` and `docs/platform-docs/entra-agent-users.md`.
```

- [ ] **Step 2: Verify**

```bash
grep -nE "docs/platform-learnings|docs/runbooks" .claude/skills/implement-agent-id/SKILL.md
```

Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/implement-agent-id/SKILL.md
git commit -m "docs: update implement-agent-id SKILL.md links"
```

### Task 12.5: Update TODOS.md and CHANGELOG.md

- [ ] **Step 1: Apply each replacement**

```
TODOS.md
Old: **Current status:** [`docs/engineering-status.md`](docs/engineering-status.md)
New: **Current status:** [`docs/project/status.md`](docs/project/status.md)

Old: - [ ] **Long-session MCP disconnect.** Continue from `docs/runbooks/mcp-disconnect-investigation.md`; do not restart the investigation without incorporating the existing evidence.
New: (remove this line entirely — the investigation is resolved; see `engineering-history/investigations/mcp-disconnect-investigation.md` for the historical trail and `docs/troubleshooting/mcp-connectivity.md` for current-state guidance)

Old: When a change materially moves work between backlog, in progress, and shipped, update this file and `docs/engineering-status.md` in the same pull request.
New: When a change materially moves work between backlog, in progress, and shipped, update this file and `docs/project/status.md` in the same pull request.
```

```
CHANGELOG.md
Old: - 66 hard-won learnings at `docs/runbooks/hard-won-learnings.md`.
New: - 70 hard-won learnings at `engineering-history/research/hard-won-learnings.md`.
```

- [ ] **Step 2: Verify**

```bash
grep -nE "docs/platform-learnings|docs/runbooks|docs/engineering-status\.md|docs/project/engineering-status\.md" TODOS.md CHANGELOG.md
```

Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add TODOS.md CHANGELOG.md
git commit -m "docs: update TODOS.md and CHANGELOG.md links, remove resolved MCP disconnect todo"
```

### Task 12.6: Update .github/workflows/test-windows.yml and pyproject.toml comments

- [ ] **Step 1: Apply each replacement**

```
.github/workflows/test-windows.yml
Old: # Mandatory per PLAN-windows-port.md D3 — without this CI gate, the
New: # Mandatory per the Windows port design (see docs/architecture/windows-and-platforms.md) D3 — without this CI gate, the
```

```
pyproject.toml
Old:     "eval: LLM eval suite for files-mcp scenarios (gates PR1 merge per docs/architecture/PLAN-files-mcp-tools.md §Testing)",
New:     "eval: LLM eval suite for files-mcp scenarios (gates PR1 merge per docs/reference/mcp-tools.md §Testing)",
```

- [ ] **Step 2: Verify**

```bash
grep -n "PLAN-windows-port\|PLAN-files-mcp-tools" .github/workflows/test-windows.yml pyproject.toml
```

Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/test-windows.yml pyproject.toml
git commit -m "docs: update CI workflow and pyproject.toml comments citing moved docs"
```

### Task 12.7: Update source and test code comments citing moved docs

- [ ] **Step 1: Apply each replacement (path references only — no logic changes)**

```
src/entrabot/tools/files.py:1370
Old: # See docs/runbooks/hard-won-learnings.md Learning #67.
New: # See engineering-history/research/hard-won-learnings.md Learning #67.

src/entrabot/tools/dispatch.py:3
Old: Per ``docs/architecture/PLAN-xpia-content-wrapping.md`` §"Deny-list guard
New: Per ``docs/architecture/security-boundaries.md`` §"Deny-list guard

src/entrabot/tools/teams.py:661
Old: # B. See docs/runbooks/hard-won-learnings.md Learning #67 and
New: # B. See engineering-history/research/hard-won-learnings.md Learning #67 and

src/entrabot/tools/teams.py:1448
Old: messages. See ``docs/architecture/PLAN-agent-identity-by-upn.md`` and
New: messages. See ``docs/architecture/identity-and-token-flow.md`` and

src/entrabot/tools/wait_tool.py:6
Old: push (Claude Code does both — see ``docs/runbooks/hard-won-learnings.md``
New: push (Claude Code does both — see ``engineering-history/research/hard-won-learnings.md``

src/entrabot/config.py:206
Old: # code revert; see docs/architecture/PLAN-xpia-content-wrapping.md.
New: # code revert; see docs/architecture/security-boundaries.md.

src/entrabot/security/xpia.py:29
Old: See ``docs/architecture/PLAN-xpia-content-wrapping.md`` (landing in
New: See ``docs/architecture/security-boundaries.md`` (landing in

src/entrabot/security/xpia.py:31
Old: ``docs/runbooks/hard-won-learnings.md`` for the motivation.
New: ``engineering-history/research/hard-won-learnings.md`` for the motivation.

src/entrabot/mcp_server.py:1286
Old: # See Learning #52 in docs/runbooks/hard-won-learnings.md.
New: # See Learning #52 in engineering-history/research/hard-won-learnings.md.

src/entrabot/mcp_server.py:1910
Old: 2026-04-27 — see ``docs/runbooks/mcp-disconnect-investigation.md``).
New: 2026-04-27 — see ``engineering-history/investigations/mcp-disconnect-investigation.md``).

src/entrabot/mcp_server.py:1950
Old: #   docs/runbooks/hard-won-learnings.md (Learning #67)
New: #   engineering-history/research/hard-won-learnings.md (Learning #67)

src/entrabot/mcp_server.py:2110
Old: # docs/runbooks/mcp-disconnect-investigation.md.
New: # engineering-history/investigations/mcp-disconnect-investigation.md.

src/entrabot/mcp_server.py:4484
Old: ``docs/architecture/PLAN-files-mcp-tools.md`` §"Failure-mode registry").
New: ``docs/reference/mcp-tools.md`` §"Failure-mode registry").

src/entrabot/preflight.py:227
Old: "failed and inspect docs/runbooks/hard-won-learnings.md."
New: "failed and inspect engineering-history/research/hard-won-learnings.md."

tests/tools/test_watch.py:22
Old: localizable. See ``docs/architecture/PLAN-agent-identity-by-upn.md``
New: localizable. See ``docs/architecture/identity-and-token-flow.md``

tests/tools/test_wait_for_sponsor_dm.py:4
Old: opt-in path for Claude Code. See ``docs/architecture/PLAN-copilot-cli-watcher.md``.
New: opt-in path for Claude Code. See ``docs/clients/overview.md`` (Sponsor DM Wait Pattern).
```

**Note:** `docs/architecture/PLAN-copilot-cli-watcher.md` referenced by `tests/tools/test_wait_for_sponsor_dm.py` does not exist anywhere in the current `docs/architecture/` tree (confirmed by the Phase 9 full-tree listing) — it was already a stale/broken reference before this redesign. This task corrects it to a real, current page as part of the same pass, since fixing dangling doc references is directly coupled to this phase's purpose.

```
tests/test_no_dead_imports.py:9
Old: See ``docs/runbooks/hard-won-learnings.md`` Learning #49 for why the long-blocking
New: See ``engineering-history/research/hard-won-learnings.md`` Learning #49 for why the long-blocking

tests/security/test_xpia_wrap.py:3
Old: Written RED-first per TDD. See ``docs/architecture/PLAN-xpia-content-wrapping.md``
New: Written RED-first per TDD. See ``docs/architecture/security-boundaries.md``

tests/test_mcp_server_integration.py:1445,1506,1548
Old: docs/runbooks/mcp-disconnect-investigation.md.
New: engineering-history/investigations/mcp-disconnect-investigation.md.

scripts/hooks/README.md:71
Old: Tracked in `docs/engineering-status.md` under "Known Issues (Open)".
New: Tracked in `docs/project/status.md` under "Known Issues (Open)".

scripts/hooks/README.md:107
Old: `docs/engineering-status.md`.
New: `docs/project/status.md`.

scripts/setup-windows.ps1:19
Old: See docs/architecture/PLAN-windows-port.md for the full design and the
New: See docs/architecture/windows-and-platforms.md for the full design and the
```

- [ ] **Step 2: Verify no old-path references remain anywhere outside engineering-history/ and this plan file itself**

```bash
git grep -lE "docs/platform-learnings|docs/runbooks|docs/decisions/|docs/project/decisions|docs/architecture/(PLAN|DESIGN|NEXT|SPEC|FOURPAGER)-|docs/(claude|openai|AGENT-PROMPT|TODO)-|docs/engineering-status\.md|docs/project/engineering-status\.md|docs/reference/api/mcp-tools\.md|docs/reference/scripts/(setup|provisioning|auth-and-certs|storage|operations|diagnostics|teardown|spikes)\.md" -- . ':!engineering-history' ':!engineering-history/plans/2026-07-10-public-documentation-redesign-implementation.md'
```

Expected: no output. If any file appears, apply the same substitution pattern used above for that file's context and re-run.

- [ ] **Step 3: Run the full test suite to confirm the comment-only changes did not break anything**

```bash
pytest -v --tb=short
ruff check .
```

Expected: all tests PASS, ruff clean (comment-only changes should never affect behavior, but this confirms no accidental syntax breakage from the edits).

- [ ] **Step 4: Commit**

```bash
git add src/entrabot/tools/files.py src/entrabot/tools/dispatch.py src/entrabot/tools/teams.py src/entrabot/tools/wait_tool.py src/entrabot/config.py src/entrabot/security/xpia.py src/entrabot/mcp_server.py src/entrabot/preflight.py tests/tools/test_watch.py tests/tools/test_wait_for_sponsor_dm.py tests/test_no_dead_imports.py tests/security/test_xpia_wrap.py tests/test_mcp_server_integration.py scripts/hooks/README.md scripts/setup-windows.ps1
git commit -m "docs: update source and test comments citing moved documentation paths"
```

---

## Phase 13: Final validation, PR, and deploy confirmation

**Files:** none created — this phase runs verification commands only.

### Task 13.1: Rebuild a worktree-local venv (per Learning #36)

- [ ] **Step 1: Confirm this worktree has never had its own venv, or remove a stale one**

```bash
cd "/Volumes/Development HD/entraclaw-identity-research/.worktrees/docs-public-site"
rm -rf .venv
```

- [ ] **Step 2: Create and activate a worktree-local venv, then install with docs extras**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,docs]"
```

Expected: install succeeds; no errors resolving `mkdocs-material`, `mkdocs-redirects`, or `pyyaml`.

- [ ] **Step 3: Verify the venv resolves entrabot from this worktree, not the parent checkout**

```bash
.venv/bin/python3 -c "from entrabot import config; print(config.__file__)"
```

Expected: path contains `.worktrees/docs-public-site`, not the parent repo path.

### Task 13.2: Run the docs structural test suite

- [ ] **Step 1: Run only the docs tests**

```bash
.venv/bin/pytest tests/docs -v --tb=short
```

Expected: all tests in `tests/docs/` PASS — `test_no_historical_prefixes.py`, `test_no_agent_attribution.py`, `test_nav_targets_exist.py`, `test_all_pages_in_nav.py`, `test_redirects.py`, `test_script_commands_manifest.py`, `test_legacy_paths_removed.py`.

- [ ] **Step 2: If any test fails, fix the underlying docs/nav/redirect issue (not the test) and re-run until green**

This step has no fixed content because the fix depends on which assertion fails; re-read the failing test's assertion message, locate the corresponding task in Phases 3–11 that was supposed to produce the missing file/nav entry/redirect, and correct that artifact.

### Task 13.3: Run mkdocs build in strict mode

- [ ] **Step 1: Build the site**

```bash
.venv/bin/mkdocs build --strict
```

Expected: exit code 0, no warnings (strict mode turns warnings into errors — broken internal links, missing nav files, and duplicate anchors all fail the build).

- [ ] **Step 2: If the build fails on a broken link, grep the reported source file and fix the link, then rebuild**

```bash
.venv/bin/mkdocs build --strict 2>&1 | grep -A2 "WARNING\|ERROR"
```

Re-run Step 1 until clean.

### Task 13.4: Run the full test suite and lint

- [ ] **Step 1: Full pytest**

```bash
.venv/bin/pytest -v --tb=short
```

Expected: all tests PASS (the full count will be higher than the pre-redesign 1,237 baseline, since Phase 1 and Phase 8 added new test modules under `tests/docs/` and `tests/dev/`).

- [ ] **Step 2: Ruff**

```bash
.venv/bin/ruff check .
```

Expected: no findings.

### Task 13.5: Check for whitespace/diff hygiene issues

- [ ] **Step 1: Run git's whitespace check across the whole change set**

```bash
git diff --check main...HEAD
```

Expected: no output (no trailing whitespace, no conflict markers).

### Task 13.6: Inspect the generated site

- [ ] **Step 1: Serve the built site locally and spot-check key pages**

```bash
.venv/bin/mkdocs serve --strict &
SERVE_PID=$!
sleep 3
curl -sf http://127.0.0.1:8000/ | grep -q "<title>" && echo "home OK"
curl -sf http://127.0.0.1:8000/getting-started/prerequisites/ | grep -q "<title>" && echo "prerequisites OK"
curl -sf http://127.0.0.1:8000/clients/overview/ | grep -q "<title>" && echo "clients overview OK"
curl -sf http://127.0.0.1:8000/architecture/system-overview/ | grep -q "<title>" && echo "architecture OK"
curl -sf http://127.0.0.1:8000/platform-docs/agent-id-blueprints-and-users/ | grep -q "<title>" && echo "platform docs OK"
curl -sf http://127.0.0.1:8000/reference/scripts/ | grep -q "<title>" && echo "scripts index OK"
curl -sf http://127.0.0.1:8000/troubleshooting/ | grep -q "<title>" && echo "troubleshooting OK"
curl -sf http://127.0.0.1:8000/project/status/ | grep -q "<title>" && echo "project status OK"
# mkdocs-redirects serves the redirect target as generated static HTML with a
# meta-refresh, which returns HTTP 200 at the legacy URL — not a real 301/302 —
# so validate 200 plus the meta-refresh tag, not a 3xx status code.
code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/architecture/PLAN-windows-port/)
body=$(curl -s http://127.0.0.1:8000/architecture/PLAN-windows-port/)
[ "$code" = "200" ] && echo "$body" | grep -qi 'http-equiv="refresh"' && echo "legacy redirect OK"
kill $SERVE_PID
```

Expected: every `echo` line prints, confirming the home page, at least one page from each new nav section, the scripts index, and one legacy redirect all resolve.

- [ ] **Step 2: Confirm no archived-only titles leak into the generated search index**

```bash
python3 - << 'PYEOF'
import json
with open("site/search/search_index.json") as f:
    idx = json.load(f)
bad_markers = ["PLAN-", "SPEC-", "DESIGN-", "NEXT-", "TODO-", "AGENT-PROMPT-", "FOURPAGER-"]
offenders = [
    doc["location"]
    for doc in idx["docs"]
    if any(marker in doc["location"] for marker in bad_markers)
]
assert not offenders, f"Search index contains archived-prefix locations: {offenders}"
print("search index clean:", len(idx["docs"]), "documents indexed")
PYEOF
```

Expected: `search index clean: N documents indexed` with no assertion error.

### Task 13.7: Self-review against the spec

- [ ] **Step 1: Re-read the approved spec fresh**

```bash
cat engineering-history/specs/2026-07-10-public-documentation-redesign.md
```

- [ ] **Step 2: Walk each spec requirement and confirm a completed task addressed it**

Checklist to confirm before proceeding (all should already be true from Phases 1–12; this step is a re-verification, not new work):

- [ ] Every public Markdown filename rejects the historical prefixes (`PLAN-`, `SPEC-`, `DESIGN-`, `NEXT-`, `TODO-`, `AGENT-PROMPT-`, `FOURPAGER-`) — enforced by `tests/docs/test_no_historical_prefixes.py`.
- [ ] No public page carries agent-author attribution — enforced by `tests/docs/test_no_agent_attribution.py`.
- [ ] Every nav target file exists — enforced by `tests/docs/test_nav_targets_exist.py`.
- [ ] Every public Markdown file is listed in nav — enforced by `tests/docs/test_all_pages_in_nav.py`.
- [ ] Every declared redirect's `old` path is absent from the tree and `new` path exists — enforced by `tests/docs/test_redirects.py`.
- [ ] `commands.yml` lists exactly the 42 supported scripts (with the two corrected filenames) and excludes the named deprecated/stale/one-off/spike/internal-helper scripts — enforced by `tests/docs/test_script_commands_manifest.py`.
- [ ] CI validates docs on every PR and deploys only on push to `main` — confirmed by reading `.github/workflows/docs.yml`'s `on:` blocks for the `validate` and `deploy` jobs.
- [ ] mkdocs.yml top-level nav is exactly: Home, Getting Started, Guides, Clients, Architecture, Platform Docs, Reference, Troubleshooting, Project — confirmed by reading `mkdocs.yml`'s `nav:` block.
- [ ] Client docs use neutral product-name-as-client-name framing, no competitor comparison — confirmed by reading `docs/clients/overview.md`, `claude-code.md`, `copilot-cli.md`, `other-hosts.md`.
- [ ] Architecture pages are present-tense and do not claim the MXC sandbox is shipped — confirmed by reading `docs/architecture/windows-and-platforms.md` and re-running `test -d src/entrabot/sandbox` (expected: still absent).
- [ ] Platform Learnings is renamed Platform Docs with all 6 required subject areas (Agent ID/Agent User, Agent 365, Teams, Files, MCP notification, macOS/Linux/Windows) intact — confirmed by listing `docs/platform-docs/`.
- [ ] Runbooks are replaced by task-oriented Troubleshooting; the resolved disconnect dossier is archived; the hard-won learning log is archived append-only (not rewritten) — confirmed by diffing `engineering-history/research/hard-won-learnings.md` against the pre-move file (should be byte-identical, since Phase 9 moves it without editing content).
- [ ] Every root/instruction file (README, INSTALL, AGENTS, CLAUDE, copilot-instructions, SKILL.md, TODOS, CHANGELOG) and every flagged code/test/script comment has been repointed — confirmed by the Phase 12 Task 12.7 Step 2 `git grep` returning no output.
- [ ] No plan/spec/investigation/prompt/research file remains under `docs/` — confirmed by:

```bash
find docs -type f -name "*.md" | grep -E "PLAN-|SPEC-|DESIGN-|NEXT-|TODO-|AGENT-PROMPT-|FOURPAGER-|claude-|openai-"
```

Expected: no output.

- [ ] **Step 3: List any gap found and fix it inline before proceeding to Step 4.** (There should be none if Phases 1–12 were followed exactly; this step exists to catch execution drift, not design gaps.)

- [ ] **Step 4: Placeholder scan across all touched public docs**

```bash
git diff --name-only main...HEAD -- docs/ | xargs grep -niE "TBD|TODO|FIXME|placeholder|fill in|implement later"
```

Expected: no output (a hit here means a page was left incomplete — go back to the task that created that file and finish it).

### Task 13.8: Push, open PR, verify CI

- [ ] **Step 1: Push the branch**

```bash
git push -u origin docs/public-site-restructure
```

- [ ] **Step 2: Open the PR**

```bash
gh pr create --title "docs: public documentation redesign" \
  --body "Implements the approved public documentation redesign per engineering-history/specs/2026-07-10-public-documentation-redesign.md. See engineering-history/plans/2026-07-10-public-documentation-redesign-implementation.md for the full implementation plan and phase-by-phase task list." \
  --base main
```

- [ ] **Step 3: Watch CI**

```bash
gh pr checks --watch
```

Expected: the `validate` docs job and the full test/lint jobs all report success. The `deploy` job must NOT run on this PR (it is gated to `push` on `main` only) — confirm by checking that no `deploy` job appears in `gh pr checks` output.

### Task 13.9: Merge and confirm Pages deploy

- [ ] **Step 1: Merge once checks are green**

```bash
gh pr merge --squash --delete-branch=false
```

Note: `--delete-branch=false` because this branch corresponds to a worktree that must be cleaned up explicitly in Task 13.10, not auto-deleted by the merge.

- [ ] **Step 2: Watch the deploy workflow run on `main`**

```bash
gh run watch --exit-status $(gh run list --workflow=docs.yml --branch main --limit 1 --json databaseId --jq '.[0].databaseId')
```

Expected: exit code 0; the `deploy` job runs (this time it fires, because the trigger is a push to `main`) and publishes to GitHub Pages.

### Task 13.10: Crawl live URLs and clean up

- [ ] **Step 1: Determine the published Pages base URL and store it in a shell variable for the rest of this task**

```bash
PAGES_URL=$(gh api repos/microsoft/entrabot/pages --jq '.html_url')
# Known value: https://microsoft.github.io/entrabot/ — the command above confirms
# it live rather than hardcoding it, in case Pages configuration ever changes.
echo "${PAGES_URL}"
```

- [ ] **Step 2: Crawl canonical pages**

```bash
for path in "" "getting-started/prerequisites/" "guides/configuration/" "clients/overview/" \
  "architecture/system-overview/" "platform-docs/agent-id-blueprints-and-users/" \
  "reference/scripts/" "troubleshooting/" "project/status/"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "${PAGES_URL}${path}")
  echo "${path:-<home>}: ${code}"
done
```

Expected: every path reports `200`.

- [ ] **Step 3: Crawl legacy redirect URLs (a representative sample of the 71-entry table from Phase 11)**

```bash
for path in "architecture/PLAN-windows-port/" "architecture/next-mcp-server-design/" \
  "claude-copilot-cli-channel-port/" "architecture/NEXT-WhatsApp-lightweight-teams-chat/" \
  "architecture/PLAN-agent-identity-by-upn/" "architecture/PLAN-xpia-content-wrapping/"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "${PAGES_URL}${path}")
  echo "${path}: ${code}"
done
```

Expected: every path reports `200` (mkdocs-redirects serves redirect targets as generated static HTML with a meta-refresh/JS redirect, which returns `200` at the legacy URL rather than an HTTP 301, since GitHub Pages serves static files only) and the final rendered page's `<title>` matches the canonical target page, verified by:

```bash
curl -s "${PAGES_URL}architecture/PLAN-windows-port/" | grep -o "<title>[^<]*</title>"
```

Expected: the title corresponds to "Windows And Platforms" (the canonical target page), not "PLAN Windows Port".

- [ ] **Step 4: Verify the live search index excludes archived titles**

```bash
curl -s "${PAGES_URL}search/search_index.json" | python3 -c "
import json, sys
idx = json.load(sys.stdin)
bad_markers = ['PLAN-', 'SPEC-', 'DESIGN-', 'NEXT-', 'TODO-', 'AGENT-PROMPT-', 'FOURPAGER-']
offenders = [d['location'] for d in idx['docs'] if any(m in d['location'] for m in bad_markers)]
assert not offenders, offenders
print('live search index clean:', len(idx['docs']), 'documents')
"
```

Expected: `live search index clean: N documents` with no assertion error.

- [ ] **Step 5: Remove the worktree**

```bash
cd "/Volumes/Development HD/entraclaw-identity-research"
git worktree remove .worktrees/docs-public-site
git branch -d docs/public-site-restructure
```

Expected: worktree removed cleanly (already merged, so `-d` — not `-D` — succeeds); no error about uncommitted changes.

---

## Plan Self-Review

**Spec coverage:** every numbered requirement in `engineering-history/specs/2026-07-10-public-documentation-redesign.md` traces to a phase above: prefix/attribution/nav/redirect/manifest tests → Phase 1; PR-validate/main-deploy CI split → Phase 2; entry points/guides/project pages → Phase 3; neutral client docs → Phase 4; present-tense architecture pages with the explicit MXC-not-shipped constraint → Phase 5; Platform Learnings → Platform Docs rename preserving all 6 subject areas → Phase 6; functional reference (including the MCP tool catalog consolidated at its canonical `docs/reference/mcp-tools.md` path) + task-oriented troubleshooting replacing runbooks → Phase 7; 42-script manifest-driven reference with the 6 required per-page sections → Phase 8; historical migration out of `docs/` into `engineering-history/` categories (including the 8 required historical migrations preserved by archive, not deletion, and ADRs archived to `engineering-history/decisions/` rather than kept public) with an explicit delete-vs-move disposition table → Phase 9; the exact 9-section `mkdocs.yml` nav (Project containing only Current Status and Changelog) → Phase 10; the 71-entry redirect table including all 8 mandatory historical-migration mappings and the ADR redirects to functional architecture pages → Phase 11; root/instruction/code/test/script comment updates with the internal-vs-public linking rule → Phase 12; and the full validation/PR/merge/deploy/crawl/cleanup sequence → Phase 13.

**Placeholder scan:** this plan document itself contains no `TBD`, `TODO` (outside of literal filenames/paths being archived, e.g. `TODO-persona-sati-integration.md`, which are content under discussion, not planning placeholders), `FIXME`, or "implement later" language. Every step shows the actual command, code, or exact before/after text required.

**Type/naming consistency:** `commands.yml` fields (`name`, `path`, `category`, `purpose`, `requirements`, `usage`, `effects`, `exit_behavior`, `related`) are used identically in Phase 8's manifest, generator script, and per-page template. The slug function `slugify()` defined in Phase 8 Task 8.2 is the same function invoked by `render_page()` in Task 8.3 and by the nav-generation step in Phase 10. `MemoryBackend`/config env-var names referenced in Phase 3's `guides/configuration.md` cross-check script match `src/entrabot/config.py` exactly (verified by the Python assertion script in Task 3.4). The redirect table in Phase 11 and the `redirect_maps` YAML block inserted into `mkdocs.yml` in the same phase contain the identical 71 `old: new` pairs — verified by the count-check script in Task 11.3.
