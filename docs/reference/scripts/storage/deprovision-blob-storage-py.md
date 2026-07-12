# `deprovision_blob_storage.py`

## Purpose

Removes the Azure resources created by `provision_blob_storage.py` — the container, and optionally the storage account and resource group — with staged, opt-in destructiveness. It is the inverse Azure resource-management script, not a data or migration tool: it never reads or copies the blob content it deletes, and it has no effect on local operational data, `.env`, or Claude Code / persona-sati memory. It also does not remove the Entra Agent User, Agent Identity, or Blueprint — that's `deprovision_entra_agent_identity.py`, which explicitly leaves blob storage untouched.

## Requirements

- The Azure CLI (`az`) installed and on `PATH`, with an active `az login` session that has delete permissions at the level you're escalating to: container deletion needs `Storage Blob Data Contributor` (or better) on the container, `--delete-account` needs `Contributor`/`Owner` on the storage account, and `--delete-resource-group` needs `Contributor`/`Owner` on the resource group.
- The exact `--storage-account` and `--container` names used when the resources were provisioned. Recover them from `.env` (`ENTRABOT_BLOB_ENDPOINT`, `ENTRABOT_BLOB_CONTAINER`) or `show_agent_status.py --json` if you don't have them handy — the endpoint's hostname before `.blob.core.windows.net` is the account name.
- No EntraBot Python environment or package install is required — the script only uses the standard library and shells out to `az`.

## Usage

```bash
# Default: delete the container only, with an interactive confirmation prompt
python3 scripts/deprovision_blob_storage.py \
  --storage-account entclaw0123456789abcdef \
  --container agent-66666666-7777-8888-9999-000000000000

# Preview what would be deleted without deleting anything or prompting
python3 scripts/deprovision_blob_storage.py \
  --storage-account entclaw0123456789abcdef \
  --container agent-66666666-7777-8888-9999-000000000000 \
  --dry-run

# Escalate to the storage account, skipping the confirmation prompt
python3 scripts/deprovision_blob_storage.py \
  --storage-account entclaw0123456789abcdef \
  --container agent-66666666-7777-8888-9999-000000000000 \
  --delete-account --yes

# Escalate to the whole resource group (implies --delete-account)
python3 scripts/deprovision_blob_storage.py \
  --storage-account entclaw0123456789abcdef \
  --container agent-66666666-7777-8888-9999-000000000000 \
  --delete-resource-group --yes
```

| Option | Default | Notes |
|---|---|---|
| `--storage-account` | *(required)* | Storage account name (no `.blob.core.windows.net` suffix). |
| `--container` | *(required)* | Container name. |
| `--resource-group` | `entrabot-rg` | Only relevant when `--delete-resource-group` is also passed. |
| `--delete-account` | `false` | Destructive escalation: also deletes the storage account, not just the container. |
| `--delete-resource-group` | `false` | Further destructive escalation: also deletes the resource group. Forces `--delete-account` on regardless of whether it was passed explicitly. |
| `--dry-run` | `false` | Prints the resources that would be deleted at the requested escalation level and exits, before any confirmation prompt or `az` call. |
| `--yes` / `-y` | `false` | Skips the interactive confirmation prompt (needed for non-interactive/CI use). |

## Effects

Deletion proceeds in safe order — container, then account, then resource group — matching increasing blast radius:

- **Default (no escalation flags)**: deletes only the named container (`az storage container delete --auth-mode login --yes`). The storage account and any other containers on it are untouched.
- **`--delete-account`**: also deletes the entire storage account. This destroys **every** container on that account, including any other Agent Users' data if they share the deterministic per-tenant account that `provision_blob_storage.py` creates by default.
- **`--delete-resource-group`**: also deletes `entrabot-rg` (or `--resource-group`) itself, asynchronously (`--no-wait`). This removes anything else provisioned into that resource group, not only EntraBot's storage account. Passing this flag forces account deletion too, even if `--delete-account` was omitted.

Confirmation and preview:

- Without `--yes`/`-y`, the script prints exactly what will be deleted at the requested level and asks `Proceed? [y/N]`; any answer other than `y`/`yes` aborts with no changes and exit 0.
- `--dry-run` short-circuits before the confirmation prompt and before any `az` call — it only prints what *would* be deleted and always exits 0, making it safe to inspect the blast radius first.

Unlike `provision_blob_storage.py`, this script does not detect and skip already-removed resources: deleting a container, account, or resource group that's already gone returns a non-zero `az` exit and is surfaced as an error rather than silently skipped.

This script never touches `~/.entrabot/data/`, `.env`, or persona-sati/Claude Code memory. If you deprovision storage but leave both `ENTRABOT_BLOB_ENDPOINT` and `ENTRABOT_BLOB_CONTAINER` set in `.env`, `get_backend()` still resolves to `BlobBackend` (both variables are present, so the config looks valid) and calls against the now-deleted container fail at request time. A half-configured environment (only one of the two variables set) does **not** fall back to `LocalBackend` either — it fails closed with `BackendMisconfiguredError` at boot, by design (see [Storage and Memory](../../../architecture/storage-and-memory.md)). `LocalBackend` is only selected when `ENTRABOT_KEEP_MEMORY_LOCAL=true` or when both Blob variables are absent. After deprovisioning, either unset both `ENTRABOT_BLOB_ENDPOINT` and `ENTRABOT_BLOB_CONTAINER` or set `ENTRABOT_KEEP_MEMORY_LOCAL=true`, then restart the MCP server to return to `LocalBackend`.

## Exit behavior

- **Exit 0** — `--dry-run` always exits 0 after printing the preview; declining the confirmation prompt exits 0 and prints `Aborted.`; a completed deletion exits 0 and prints `Done.` to stderr.
- **Exit 1** — any `az` command failed at the container, account, or resource-group deletion step, printed as `ERROR: az ... failed: <az stderr>`.
- **Exit 2** — argparse usage error: `--storage-account` or `--container` is missing.

Common failure causes and recovery:

- **Container/account already deleted, or a name typo** — `az` returns a not-found error, which this script raises as `ERROR: ...` rather than treating as success. Verify the exact name first with `az storage account show --name <name>` or `az storage container show --account-name <account> --name <container> --auth-mode login`, then retry with the corrected name or drop straight to the escalation level that still applies.
- **Insufficient delete permissions** — the container step can fail on `Contributor`-only access if data-plane RBAC was never propagated, or an account/resource-group deletion can fail without subscription-level `Contributor`/`Owner`. Have someone with the right role run it, or request the missing role assignment.
- **Resource group deletion appears to "hang"** — `--delete-resource-group` uses `--no-wait`, so the command returns immediately while Azure deletes asynchronously. Confirm completion with `az group show --name entrabot-rg` (expect a not-found error once it's fully gone) rather than assuming failure.
- **Partial failure mid-run** — because retries aren't idempotent here, re-running after a failure at, say, the account-deletion step should typically drop `--delete-resource-group`/re-check what already succeeded, to avoid a spurious container-not-found error on the next attempt.

## Related commands

- [`provision_blob_storage.py`](provision-blob-storage-py.md) — inverse: creates the resources this script removes.
- [`deprovision_entra_agent_identity.py`](../teardown/deprovision-entra-agent-identity-py.md) — removes the Agent User/Agent Identity/Blueprint chain but explicitly leaves blob storage untouched; run this script separately if you also want the storage resources gone.

See also [Storage scripts](../index.md#storage), the [Storage Configuration guide](../../../guides/storage-configuration.md) (including its [Troubleshooting](../../../guides/storage-configuration.md#troubleshooting) section), and the [Storage and Memory](../../../architecture/storage-and-memory.md) architecture page.
