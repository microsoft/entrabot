# `scripts/setup_ado_credentials.sh`

Platforms: macOS.

## Purpose

`setup_ado_credentials.sh` prompts for an Azure DevOps Personal Access Token
(PAT) and stores it with `git credential approve` for `https://dev.azure.com`,
so subsequent `git push`/`pull` against an Azure DevOps remote authenticate
without further prompts. It assumes a git remote named `ado` already points
at the target `dev.azure.com` repository.

## Requirements

- `git` on `PATH`.
- An Azure DevOps organization and a PAT with at least **Code (Read &
  Write)** scope, generated at
  `https://dev.azure.com/<YourOrg>/_usersSettings/tokens`.
- **Keychain storage only actually happens if Git's credential helper is
  configured as `osxkeychain`.** The script pipes the credential to
  `git credential approve`, which hands it to whatever `credential.helper`
  is configured — `osxkeychain` is common on macOS but not guaranteed to be
  the active helper. If a different (or no) helper is configured, the PAT is
  stored wherever that helper stores it, not necessarily the macOS Keychain.
- A local `ado` git remote for the verification step
  (`git ls-remote ado HEAD`) to succeed.

## Usage

```bash
./scripts/setup_ado_credentials.sh
```

The script takes no arguments; it prompts interactively for the PAT
(`read -rsp`, not echoed to the terminal).

## Effects

1. Prompts for the PAT without echoing it to the terminal.
2. Exits `1` immediately if the input is empty.
3. Pipes a credential record (`protocol=https`, `host=dev.azure.com`, a
   placeholder `username`, and the PAT as `password`) to
   `git credential approve`, which stores it via the configured
   `credential.helper`.
4. Runs `git ls-remote ado HEAD` to verify the stored credential actually
   authenticates against the `ado` remote.

## Exit behavior

- `0` — the PAT was stored via `git credential approve`, **regardless of
  whether the verification step in effect 4 succeeded.** A failed
  verification only prints a warning; it does not change the exit code.
- `1` — no PAT was entered at the prompt.
- Any other non-zero exit comes from `set -euo pipefail` surfacing a failure
  in `git credential approve` itself (for example, no credential helper
  configured at all).

## Common failures

- **`Could not verify access` warning after a seemingly successful store** —
  expected when the PAT lacks `Code (Read & Write)` scope, or when
  `credential.helper` isn't set to a helper this shell session can read back
  from; try `git push ado <branch>` directly to see the underlying error.
- **PAT stored but git still prompts on push** — check
  `git config --get credential.helper`; if it's unset or not
  `osxkeychain`, the credential was stored by a different mechanism (or not
  persisted at all) and the Keychain won't have it.
- **`ado` remote not found** — add it first with
  `git remote add ado https://dev.azure.com/<org>/<project>/_git/<repo>`.

## Related commands

- [Script reference — Setup](../index.md#setup)
- [`setup.sh`](setup-sh.md) — the main setup entry point this is unrelated
  to functionally; both live under Setup because they are one-shot
  machine-bootstrap scripts.
