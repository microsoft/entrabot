# Documentation Site

## Local preview

```bash
pip install mkdocs-material  # first time only
mkdocs serve
```

Open <http://localhost:8000>.

## Validation

Documentation builds are warning-free by contract:

```bash
mkdocs build --strict
```

Keep links between files under `docs/` relative so MkDocs can validate them. Links to repository-root source, scripts, prompts, or instruction files must use canonical `https://github.com/microsoft/entrabot/blob/main/...` URLs because those targets are outside the MkDocs content tree.

## Auto-deploy

Docs publish to GitHub Pages on every push to `main` that touches `docs/`, `mkdocs.yml`, or `.github/workflows/docs.yml`. The workflow:

1. Checks out the repository.
2. Installs Python 3.12 and `mkdocs-material`.
3. Runs `mkdocs build --strict`.
4. Uploads the generated `site/` artifact.
5. Deploys with `actions/deploy-pages@v4`.

Published site: <https://microsoft.github.io/entrabot/>.

GitHub Pages uses `build_type=workflow`. To enable it on a new fork:

```bash
gh api -X POST repos/<owner>/<repo>/pages -f 'build_type=workflow'
```

A `409 GitHub Pages is already enabled` response is safe to ignore.

## Adding pages

1. Create the Markdown file in the appropriate `docs/` subdirectory.
2. Add important current pages to `nav:` in `mkdocs.yml`; historical supporting material may remain unlisted.
3. Cross-link related pages.
4. Run `mkdocs build --strict` and fix every warning.

## Layout

- `docs/getting-started/` — onboarding.
- `docs/guides/` — operator how-to guides.
- `docs/clients/` — MCP host integration.
- `docs/architecture/` — system designs and implementation records.
- `docs/reference/scripts/` — script categories.
- `docs/reference/api/` — MCP and Python API surfaces.
- `docs/runbooks/` — operational runbooks and hard-won learnings.
- `docs/platform-docs/` — current vendor/platform reference (Entra Agent ID, Agent 365, Graph APIs, delegated auth, OS platform APIs).
- `docs/developer/` — contributor documentation.

Historical ADRs are archived at `engineering-history/decisions/` outside this tree and are not published on the docs site.
