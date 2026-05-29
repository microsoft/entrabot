# Documentation Site

## Local Preview

```bash
pip install mkdocs-material  # first time only
mkdocs serve
```

Open <http://localhost:8000>.

## Auto-deploy

Docs auto-publish to GitHub Pages on every push to `main` that touches `docs/`, `mkdocs.yml`, or the workflow itself. Workflow file: `.github/workflows/docs.yml`.

The workflow:

1. Checks out the repo.
2. Installs Python 3.12 and `mkdocs-material` (+ `mkdocstrings[python]` for future API extraction).
3. Runs `mkdocs build` (non-strict — see warnings below).
4. Uploads the `site/` artifact to GitHub Pages.
5. Deploys via `actions/deploy-pages@v4`.

Published at <https://microsoft.github.io/entrabot/>. GitHub Pages is configured with `build_type=workflow`; re-enable via:

```bash
gh api -X POST repos/<owner>/<repo>/pages -f 'build_type=workflow'
```

`gh` returns `409 GitHub Pages is already enabled` when Pages is already on — that's expected and safe to ignore.

## Build warnings

`mkdocs build --strict` currently fails because some in-repo docs reference source files via `../../src/...` and `../../scripts/...`. MkDocs can't validate those because they live outside `docs/`. The workflow runs without `--strict` to keep the build green. The links work at GitHub-rendered Markdown level (they point at real files in the repo tree).

If you add new docs, keep cross-tree links pointing at real source paths so reading on GitHub still works. Inside `docs/`, prefer relative links so MkDocs can validate them.

## Adding new pages

1. Create the markdown file in the appropriate `docs/` subdirectory.
2. Add it to the `nav:` section in `mkdocs.yml`.
3. Cross-link from related pages.
4. Run `mkdocs build` locally and check for new warnings.

## Layout

- `docs/getting-started/` — onboarding pages (quickstart).
- `docs/guides/` — how-to guides for operators.
- `docs/architecture/` — system design docs, four-pagers, plan documents.
- `docs/reference/scripts/` — one page per script category.
- `docs/reference/api/` — Python API surface (MCP tools, storage, auth, identity, audit, efferent-copy, body prompt).
- `docs/decisions/` — ADRs.
- `docs/runbooks/` — operational runbooks and hard-won learnings.
- `docs/platform-learnings/` — vendor-platform notes (Entra, Teams, MCP, A365, etc.).
- `docs/developer/` — contributor-facing docs (this file, QA log, engineering status).
- `docs/plans/`, `docs/prompts/`, `docs/clients/` — supporting material.
