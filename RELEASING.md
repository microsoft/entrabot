# Releasing Entrabot

People get Entrabot from tagged GitHub Releases. The owner runs the steps below by hand;
there is no release workflow.

- **The latest release is the production version.** It is the release GitHub marks "Latest"
  on the project's Releases page, and the install commands in the docs name its tag.
- **`main` is development.** It may be ahead of the latest release and is not what people
  are told to install. There is no stable branch.
- **A trial version is a pre-release** (for example `v0.3.0-rc.1`). GitHub marks it
  "Pre-release" and never makes it "Latest", so the install commands keep naming the latest
  release.

## Versions and tags

- Versions are `0.x.y`. While the major version is 0, `x` goes up for a release with new
  features or breaking changes, and `y` for a release with fixes only.
- Tags are `vX.Y.Z` (for example `v0.2.0`), annotated, on the merge commit on `main`. The
  older `v0.1` tag predates this scheme and stays as it is.
- A pre-release is tagged `vX.Y.Z-rc.N` (`N` from 1) on the commit being tried, which may be
  on its release branch rather than on `main`. Its version in the package is Python's form,
  `X.Y.ZrcN` (`0.3.0rc1` for `v0.3.0-rc.1`).
- The version is stated in `pyproject.toml` (`version`) and
  `src/entrabot/harness/__init__.py` (`__version__`). `tests/test_release_metadata.py`
  checks that the two agree and that `CHANGELOG.md` has a section for that version. The
  running agent reports its version from the installed package's metadata (`whoami`,
  `--diagnose`, the status report), and `--diagnose` warns when its checkout is not on a
  release tag.

## Between releases

Every pull request that changes behaviour adds a line to the `## Unreleased` section of
`CHANGELOG.md`, under Added, Changed, Fixed, Removed or Security. Mark breaking changes with
**Breaking:**.

## Cutting a release

1. **Branch.** Create `release/vX.Y.Z` from `main`, and merge into it the pull requests the
   release carries, if they are not on `main` yet.
2. **CHANGELOG.** Rename `## Unreleased` to `## X.Y.Z — YYYY-MM-DD` (the release day), check
   that it covers everything since the last tag (`git log vLAST..HEAD`), and add a new empty
   `## Unreleased` section above it.
3. **Version.** Set `X.Y.Z` in `pyproject.toml` and `src/entrabot/harness/__init__.py`.
4. **Install commands.** Change the tag in every `git clone --branch vX.Y.Z` command to the
   new one. They are in `README.md` and in `docs/getting-started/`: `quickstart.md`,
   `prerequisites.md`, `macos-linux.md` and `windows.md`. The upgrade steps in
   `docs/troubleshooting/migrations-and-upgrades.md` name the tag too.
   `tests/docs/test_install_commands.py` fails while any of them names a version other than
   the one in `pyproject.toml`.
5. **Gate.** On macOS or Linux, from the release branch:

   ```bash
   pytest -v --tb=short && ruff check .
   ```

   It must pass with no failures.
6. **Pull request.** Open a pull request from `release/vX.Y.Z` to `main`. Its `test-windows`
   job (the full test suite on Windows) and CodeQL must be green. Nothing else gets the
   Windows run, so do not skip it.
7. **Verification record.** Update `docs/project/verification.md`: what was run for real on
   this release's own code (which system, which checks), what is unit-tested only, and what has
   never been run. A run on a different branch, such as one that also holds unreleased work,
   is recorded as that, not as a run of the release. The page is public: no tenant, account or
   machine details.
8. **Status.** Update `docs/project/status.md` and close or open the GitHub issues the release
   moves (see `AGENTS.md`).
9. **Merge.** Merge the pull request into `main` with a merge commit.
10. **Tag.** On the merge commit:

    ```bash
    git fetch origin
    git tag -a vX.Y.Z -m "Entrabot X.Y.Z" origin/main
    git push origin vX.Y.Z
    ```

11. **GitHub Release.** Take the notes from the CHANGELOG section (everything under its
    heading, up to the next `## `), check them for tenant, account and machine details, and
    publish it as the latest release:

    ```bash
    awk '/^## X\.Y\.Z /{on=1; next} /^## /{on=0} on' CHANGELOG.md > /tmp/notes-vX.Y.Z.md
    gh release create vX.Y.Z --verify-tag --latest --title "Entrabot X.Y.Z" --notes-file /tmp/notes-vX.Y.Z.md
    ```

    Replace `X\.Y\.Z` in the `awk` pattern with the version, dots escaped.

## A pre-release

Use one to let people try a version before it becomes the production one, such as a feature
that needs running on systems the project has not run it on.

1. On the release branch, set the version to `X.Y.ZrcN` in `pyproject.toml` and
   `src/entrabot/harness/__init__.py`. The CHANGELOG section stays `## X.Y.Z (unreleased)`.
   The install commands are **not** changed: they keep naming the latest release.
2. Run the gate, and make sure the branch's pull request has a green `test-windows` run.
3. Tag the commit and push the tag:

   ```bash
   git tag -a vX.Y.Z-rc.N -m "Entrabot X.Y.Z release candidate N" <commit>
   git push origin vX.Y.Z-rc.N
   ```

4. Publish it as a pre-release, never as the latest:

   ```bash
   gh release create vX.Y.Z-rc.N --verify-tag --prerelease --latest=false \
     --title "Entrabot X.Y.Z release candidate N" --notes-file /tmp/notes-vX.Y.Z-rc.N.md
   ```

   The notes say what to try, what has not been run, and how to go back to the latest release
   (`git checkout vLATEST`, then run setup again).
5. Check on the Releases page that it says "Pre-release" and that "Latest" is still the
   production release.

## A fix to a released version

Branch `release/vX.Y.(Z+1)` from the tag `vX.Y.Z`, add the fix with its CHANGELOG line, and
follow the same steps from 2. Merge the fix into `main` as well.
