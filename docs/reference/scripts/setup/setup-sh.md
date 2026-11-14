# `scripts/setup.sh`

Platforms: macOS, Linux.

## Purpose

`setup.sh` is the first-time and additional-device provisioning entry point for
macOS and Linux. It is a thin orchestrator around the Python provisioning
scripts: it verifies prerequisites, bootstraps the dedicated Provisioner app,
creates or attaches the full Agent Identity chain (Blueprint → Agent Identity →
Agent User), generates and registers the Blueprint certificate, writes `.env`,
optionally provisions cloud-hosted operational memory, registers the `entrabot`
MCP server, and runs a post-setup smoke check. State is persisted to
`.entrabot-state.json`, so re-runs are idempotent.

## Requirements

- macOS or Linux with Bash.
- Azure CLI (`az`) on `PATH`, already signed in (`az login`). Without an active
  login the script fails at Step 2.
- Python 3.12 or newer (`python3.12`, `python3.13`, or a `python3` that reports
  ≥ 3.12).
- `git`.
- An OS credential store reachable by `keyring` (Keychain on macOS, Secret
  Service / Keyring on Linux). The certificate step fails closed if the active
  keyring backend is not OS-native, rather than writing the private key to disk.
- A Teams-capable M365 license for the Agent User is required for end-to-end
  Teams operation; license replication can take 10–15 minutes after assignment.

Install the macOS prerequisites with [`prereqs-macos.sh`](prereqs-macos-sh.md).
On Linux, install `python3.12`, `git`, and the Azure CLI with your package
manager. See also [macOS / Linux installation](../../../getting-started/macos-linux.md).

## Usage

```bash
# First-time provisioning — create a brand-new identity chain
./scripts/setup.sh --new --with-upn-suffix=workstation

# Create an explicit Agent User UPN
./scripts/setup.sh --new --agent-user-upn=entrabot-agent@yourtenant.onmicrosoft.com

# Attach this machine to an existing Blueprint (multi-device / re-install).
# Adds a certificate for this machine; other machines' certificates are kept.
./scripts/setup.sh --use-blueprint=<BLUEPRINT_APP_ID> --agent-user-upn=<UPN>

# Replace EVERY certificate on the Blueprint with a new one for this machine.
# Other machines stop authenticating until they run setup again.
# This does not delete the Blueprint itself.
./scripts/setup.sh --use-blueprint=<BLUEPRINT_APP_ID> --remove-all-existing-certs

# Opt in to cloud-hosted operational memory (Azure Blob Storage)
./scripts/setup.sh --new --with-upn-suffix=workstation --use-cloud-memory

# Skip the post-setup smoke test (useful in CI with no live tenant)
./scripts/setup.sh --new --with-upn-suffix=ci --skip-smoke

# Re-run diagnostics or the consolidated status view instead of provisioning
./scripts/setup.sh --diagnose
./scripts/setup.sh --status --json
```

An identity mode is required: pass exactly one of `--new` or
`--use-blueprint=<APP_ID>`. `--new` and `--use-blueprint` are mutually
exclusive.

## Options

Identity mode (choose one):

- `--new` — create a completely fresh chain. Existing state is backed up and the
  identity keys are cleared (the Provisioner app is preserved). Requires a UPN
  suffix (`--with-upn-suffix`) or an explicit `--agent-user-upn`; if neither is
  given, the script prompts for a suffix.
- `--use-blueprint=ID` — attach to an existing Blueprint by App ID. `ID` must be
  the App ID, a GUID such as `1a2b3c4d-1234-5678-9abc-def012345678` (either
  letter case, nothing around it); anything else, an empty value included, is a
  usage error (exit `2`) before anything runs. A new cert is
  generated for this machine and added to the Blueprint; existing certificates are
  kept, so other machines keep working, and the existing Agent Identity and Agent
  User are reused. If local state points at a *different* Blueprint, the stale
  identity-derived state is wiped (after a timestamped backup) so discovery re-runs
  cleanly against the new Blueprint. The old machine's cert on the previous
  Blueprint is **not** revoked automatically. Unless `--agent-user-upn` or
  `--with-upn-suffix` names the Agent User, the one `.env` names
  (`ENTRABOT_AGENT_USER_UPN`) is looked for only when `.env` was written for
  this same Blueprint (`ENTRABOT_BLUEPRINT_APP_ID`). Otherwise setup runs the
  agent step without it, and says so. An Agent User found by its
  sign-in name is reused only when it belongs to this Blueprint; see
  [`create_entra_agent_ids.py`](../provisioning/create-entra-agent-ids-py.md#idempotency-and-resume).
- `--remove-all-existing-certs` — used with `--use-blueprint`. Removes **every**
  certificate on the Blueprint, including those of other machines, and replaces them
  with a new one for this machine, even if this machine's current certificate is
  still valid. **It does not delete the Blueprint**, its Agent Identities, or its
  Agent Users. Every other machine stops authenticating until it runs setup again.
  It lists what will be removed (name and expiry only) and asks `y/N` on a terminal;
  without a terminal the flag itself is the consent, and it says so loudly. Nothing on
  this machine changes until the replacement is confirmed, so declining, or Graph
  refusing the update, leaves the active key, the state file, and any pending key as
  they were. This is the only way setup removes a certificate. Mutually exclusive with
  `--new`, which creates a Blueprint with no certificates.

Agent User selection:

- `--with-upn-suffix=NAME` — UPN suffix for the Agent User (required with `--new`
  unless `--agent-user-upn` is given). With `--use-blueprint`, selects an
  existing suffixed Agent User of that Blueprint.
- `--agent-user-upn=UPN` — explicit Agent User UPN. With `--new`, creates exactly
  that UPN; with `--use-blueprint`, selects an existing Agent User to reuse. An
  Agent User of a different Blueprint stops setup at Step 5 with nothing
  created, granted or recorded.
- `--switch-user` — run `az login` first so a different signed-in user owns and
  sponsors the agent.
- `--teams-user=EMAIL` — set a different user (or comma-separated users) as the
  Teams recipient(s); the `az` CLI user remains the admin/provisioner. Guests
  (B2B) are detected and resolved to their home tenant. Without it, a re-run for
  the same Agent User keeps the sponsor already in `.env` (`ENTRABOT_HUMAN_*`);
  with it, the sponsor is changed to the user(s) you name.

Operational memory:

- `--use-cloud-memory` — opt in to Azure Blob Storage for operational data
  (interaction log, daily summaries, promises, per-chat delivery cursors, the
  watched-chat list, and the email-poll cursor). Provisions a resource group,
  storage account, and container scoped to the Agent User, and unsets
  `ENTRABOT_KEEP_MEMORY_LOCAL`.
  A re-run without this flag keeps cloud memory that is already set in `.env`
  for the same Agent User.
- `--keep-memory-local` — operational data stays on the local filesystem; sets
  `ENTRABOT_KEEP_MEMORY_LOCAL=true`. A first run with neither memory flag does
  this too. On a re-run for the same Agent User whose `.env` already has cloud
  memory, pass this flag to switch back to local; without it the cloud settings
  are kept.
- `--with-storage-account=NAME` — target a named storage account instead of the
  deterministic per-tenant default (used with `--use-cloud-memory`). Mutually
  exclusive with `--create-new-storage`.
- `--with-container=NAME` — target a named blob container instead of the
  `agent-<oid>` default.
- `--create-new-storage` — force a fresh randomly-suffixed storage account even
  when the deterministic-name one exists. Mutually exclusive with
  `--with-storage-account`.

Agent 365 Work IQ:

- `--with-a365-work-iq` — install or update the Microsoft Agent 365 DevTools CLI
  (`a365`) via `dotnet` global tools (requires a .NET SDK on `PATH`).
- `--configure-a365-work-iq` — run the interactive Work IQ Word developer setup
  against the existing Entrabot Blueprint, materialize the Work IQ OAuth grants
  and the Blueprint's inheritable permissions
  ([`ensure_a365_work_iq_permissions.py`](../provisioning/ensure-a365-work-iq-permissions-py.md),
  before `a365 setup permissions mcp`), and validate the tooling manifest.
  Requires `pwsh`.
- `--a365-agent-name=NAME` — deprecated compatibility flag; Work IQ setup now
  reuses the existing Entrabot Blueprint from state.

Diagnostics and control:

- `--status` — skip setup and delegate to `status.sh`; extra arguments are
  forwarded to `show_agent_status.py` (for example `--json`, `--health-only`,
  `--strict`).
- `--diagnose` — skip provisioning and run a full health check (state file,
  certificate, three-hop token, Graph identity, Teams scope, MCP wiring, and the
  optional `persona-sati` entry in `.mcp.json`). Exits non-zero if any check
  fails. The persona-sati check is advisory: it can report `SKIP` or `WARN` but
  never a failure, and it connects only to loopback addresses (see
  [MCP connectivity](../../../troubleshooting/mcp-connectivity.md#persona-sati-does-not-connect)).
- `--skip-smoke` — skip the post-setup smoke test (token + Graph identity +
  Teams scope).
- `--help`, `-h` — print the full option matrix.

## Effects

- Verifies `az`, Python ≥ 3.12 (each candidate interpreter checks its own
  version), and required CLI tools; verifies the Azure login.
- Hands values to its Python programs as arguments or in the environment, never
  as program text: the `--use-blueprint` App ID, paths, keys, and values read
  from `.env` or the state file. The license preflight's Graph token (the
  signed-in administrator's) goes to each check in that one command's
  environment, never on a command line, where any local user could read it from
  the process list, and is not exported to later commands.
- Bootstraps the Provisioner app via `entra_provisioning.py` (see
  [Which app registration is the Provisioner](#which-app-registration-is-the-provisioner))
  and creates or reuses the Blueprint, Agent Identity, and Agent User via
  [`create_entra_agent_ids.py`](../provisioning/create-entra-agent-ids-py.md)
  (using the Provisioner certificate token, never `az` CLI tokens).
- With `--use-blueprint`, checks after that step that the Blueprint recorded in
  `.entrabot-state.json` is the one asked for (in any letter case). If it is
  not, setup stops (exit `1`) before Work IQ is configured and before the
  certificate step: no certificate is registered on either Blueprint, and
  `.env` is not written.
- Reuses this machine's certificate only when
  [`find_local_blueprint_cert.py`](../auth-and-certs/find-local-blueprint-cert-py.md)
  shows that the local private key matches a certificate still registered on the
  Blueprint that hasn't expired. The thumbprint cached in the state file is never
  trusted on its own: setup uses the thumbprint that check returns, and saves it if it
  differs from the cached one. `verify_blueprint_cert.py` is an operator tool that setup
  does not call.
- Otherwise, including whenever a new private key is still waiting under the pending
  keystore entry from an earlier run,
  [`register_blueprint_cert.py`](../auth-and-certs/register-blueprint-cert-py.md)
  generates an RSA-2048 self-signed certificate (or reuses the pending key), keeps the
  private key under a temporary keystore entry, and uploads only the public certificate.
  Because a Graph `PATCH keyCredentials` replaces the whole list, it sends every existing
  certificate back along with the new one, then reads the list back to confirm before
  making the new key active and recording the thumbprint. A normal run never removes a
  certificate.
- Creates `.venv` if it is missing and installs the package with its provisioning dependencies there before any provisioning script runs (nothing is installed into the system Python), then later installs the dev extras, and writes `.env`
  (`chmod 600`) with the resulting tenant, Blueprint, Agent Identity, Agent User,
  and human-user identifiers plus the certificate thumbprint.
- Reads the existing `.env` before rewriting it. When this run is for the same
  Agent User (`ENTRABOT_AGENT_USER_ID` is unchanged), it keeps the sponsor
  (`ENTRABOT_HUMAN_*`) unless you pass `--teams-user` or `--switch-user`, and keeps the cloud-memory
  settings (`ENTRABOT_BLOB_ENDPOINT` / `ENTRABOT_BLOB_CONTAINER`) unless you pass
  `--keep-memory-local` or `--use-cloud-memory`. Cloud memory counts as set only
  when both blob settings are present and `ENTRABOT_KEEP_MEMORY_LOCAL` is not
  `true`, `1`, or `yes`. A run for a different Agent User (`--new`, or another
  Blueprint or Agent User) carries nothing over. The rest of `.env` is written
  fresh, so other lines you added by hand are dropped.
- With `--use-cloud-memory` and an Agent User present, provisions Azure Blob
  Storage via [`provision_blob_storage.py`](../storage/provision-blob-storage-py.md)
  and appends `ENTRABOT_BLOB_ENDPOINT` / `ENTRABOT_BLOB_CONTAINER` to `.env`;
  offers an idempotent, source-preserving migration prompt that copies existing
  local data (interaction log, daily summaries, promises) and Claude Code persona
  memory as a one-time artifact copy.
  If provisioning fails, setup shows the provisioner's output and warns. It keeps
  the cloud settings already in `.env` if there were any for this Agent User, and
  otherwise falls back to local-only memory. The migration doesn't
  copy local poll state: the watched-chat list, the email cursor, the legacy
  `chat_id` key, or the per-chat delivery cursors under `chat_cursors/`. A machine's
  copies may be stale or another agent's. The cloud cursor starts at "now", discovery
  rebuilds the list from Graph about two minutes after boot, and a chat with no
  cursor surfaces its newest message only if it is under 24 hours old.
- Registers the `entrabot` MCP server in the project-local `.mcp.json` (Claude
  Code) and `~/.copilot/mcp-config.json` (Copilot CLI) via `mcp_config.py`; both
  entries are byte-identical.
- Runs a post-setup smoke test (token + Graph identity + Teams scope) unless
  `--skip-smoke` is passed.
- Does **not** create or watch any Teams chat. There is no default chat; every
  Teams tool requires an explicit chat id supplied at runtime. See
  [Messaging and delivery](../../../architecture/messaging-and-delivery.md).

### Which app registration is the Provisioner

`entra_provisioning.py` uses the Provisioner app that an earlier run on this
machine recorded in `.entrabot-state.json` (`PROVISIONER_CLIENT_ID`). Windows
setup runs the same script with the same rule. When no app is recorded, or the
recorded one no longer exists in Microsoft Entra, it looks for app registrations
named exactly `EntraBot Agent ID Provisioner`:

- **None:** it creates one.
- **One:** it takes that app over only if doing so grants nobody anything. That
  is the case when either:
    - an administrator has already granted it admin consent for every Microsoft
      Graph permission setup needs, and anything else the app asks for is a
      Graph application permission that has been granted too. This is the usual
      case on a second machine, or for a second administrator. Setup then grants
      no consent in this run; or
    - nobody else can use it: you are the only owner of the app and of its
      service principal (or they have no owners), the app has no client secret
      and no federated credential, its service principal has no credential of
      its own, every certificate on the app has its private key on this machine,
      and the app asks for no permission besides setup's Graph application
      permissions.
- **More than one, or one that meets neither condition:** setup stops before it
  changes anything in Microsoft Entra. It lists each app (application ID,
  creation date, owners) and why it was not used, and exits `1`. Anything setup
  could not read counts against the app.

The reason: the Provisioner app is granted tenant-wide Microsoft Graph
application permissions with admin consent, and app names are not unique.
Anyone allowed to register applications can create an app with this name, and
taking it over would hand those permissions to whoever controls it.

To use a particular app, set `ENTRABOT_PROVISIONER_APP_ID_VERIFIED` to its
application (client) ID for one run (see
[Setup and authentication](../../../troubleshooting/setup-and-authentication.md#setup-will-not-take-over-an-existing-provisioner-app-registration)).
Setup only checks that an app with that ID exists, then records it and treats it
as its own: it adds the permissions setup needs and, when any are not yet
consented, grants admin consent, which covers everything the app asks for. As on
every run, it also deletes any client secret on the app. Check the app's owners,
credentials and permissions first. A value that is not an
application ID, an ID no app has, or an app other than the one this machine
already has recorded is refused, and nothing is changed. The variable is not
read from `.env`, and `ENTRABOT_PROVISIONER_APP_ID`, which setup writes to
`.env`, does not choose the app.

## Exit behavior

- `0` — setup (or `--status` / `--help`) completed successfully; the smoke test
  either passed or was skipped.
- `1` — a fatal setup error (for example a missing prerequisite, no Azure login,
  a failed provisioning step, an existing Provisioner app that setup would not take
  over, an agent step that recorded a different Blueprint from the one
  `--use-blueprint` named, a certificate check that couldn't complete, a declined
  `--remove-all-existing-certs` prompt, `--new` / `--use-blueprint` conflict, or a
  missing required identity mode).
- `2` — mutually exclusive storage flags, `--new` with
  `--remove-all-existing-certs`, a `--use-blueprint` value that is not an App
  ID, or setup completed provisioning but the
  cloud-memory **migration failed** (cloud memory is not in sync with local
  disk; re-run after fixing the underlying consent error).
- `3` — provisioning finished but the post-setup **smoke test failed** (the agent
  can't yet authenticate end-to-end, most often because a just-assigned Teams
  license has not replicated). Re-run `./scripts/setup.sh --diagnose` after
  replication completes.
- `--diagnose` exits with the diagnostics' own overall exit code (`0` unless a
  check fails; warnings and skips do not change it).

## Common failures

- **`Not logged in to Azure CLI`** — run `az login` (or `./scripts/setup.sh
  --switch-user`) and retry.
- **`No identity mode specified`** — pass `--new --with-upn-suffix=NAME` or
  `--use-blueprint=APP_ID`.
- **`ERROR: --use-blueprint takes the Blueprint's App ID, which looks like
  1a2b3c4d-1234-5678-9abc-def012345678.`** — followed by `Given: <value>`.
  Nothing ran. Pass the Blueprint's Application (client) ID from the Entra admin
  center, with nothing around it.
- **`Setup did not take over an existing 'EntraBot Agent ID Provisioner' app
  registration`** — see
  [Which app registration is the Provisioner](#which-app-registration-is-the-provisioner)
  and
  [Setup and authentication](../../../troubleshooting/setup-and-authentication.md#setup-will-not-take-over-an-existing-provisioner-app-registration).
- **`Not reusing the Agent User named in .env (...)`** — a warning, not a stop.
  With `--use-blueprint`, `.env` was written for another Blueprint (or records
  none), so the Agent User it names is not looked for under this one. To use a
  particular Agent User of this Blueprint, name it with `--agent-user-upn` or
  `--with-upn-suffix`. See
  [Setup and authentication](../../../troubleshooting/setup-and-authentication.md#setup-says-it-is-not-reusing-the-agent-user-named-in-env).
- **Step 5 stops: `The Agent User ... belongs to a different Blueprint`,
  `... does not exist in this tenant`, or `Could not check ...`** —
  `create_entra_agent_ids.py` stopped instead of guessing which agent is this
  one, before creating the object in question. The line above the message, such
  as `ENTRABOT_RESULT=blueprint-not-found`, names the outcome for the Windows
  wizard. See
  [Setup stops while creating the agent](../../../troubleshooting/setup-and-authentication.md#setup-stops-while-creating-the-agent).
- **`You asked to join Blueprint ..., but create_entra_agent_ids.py recorded
  Blueprint ...`** — setup stopped before the certificate step; see
  [Setup and authentication](../../../troubleshooting/setup-and-authentication.md#setup-stops-because-a-different-blueprint-was-recorded).
- **`Could not check ...` at the certificate step (Step 6)** — setup couldn't read the Blueprint's certificates or the
  OS credential store (the pending key entry included), so it doesn't know whether this
  machine is already registered. It stops rather than generating a certificate, and
  nothing was changed. Fix the error shown above the message and re-run.
- **`Could not confirm the certificate update`** — the update may have landed but
  couldn't be read back. The new private key is kept; re-run setup and it picks up
  where it left off. See
  [`register_blueprint_cert.py`](../auth-and-certs/register-blueprint-cert-py.md#what-happens-to-the-key-on-failure).
- **Old certificates building up** — a normal run only adds, including when a
  certificate has expired. Remove individual certificates in the Entra admin center,
  or replace them all with `--remove-all-existing-certs`.
- **Another machine stopped authenticating after `--remove-all-existing-certs`** — this
  is what the flag does. Run setup on that machine to add its certificate back.
- **Smoke test failed (exit 3)** — usually license replication latency; wait
  10–15 minutes and re-run with `--diagnose`.
- **Migration failed (exit 2)** — most often missing storage consent for the
  Agent Identity; re-run `setup.sh` (idempotent) or
  [`create_entra_agent_ids.py`](../provisioning/create-entra-agent-ids-py.md) to
  grant it, then retry.

## Related commands

- [Script reference — Setup](../index.md#setup)
- [`prereqs-macos.sh`](prereqs-macos-sh.md) — install the macOS prerequisites.
- [`setup_delegated.sh`](setup-delegated-sh.md) — delegated (MSAL) sign-in
  instead of the full Agent User chain.
- [`setup-windows.ps1`](setup-windows-ps1.md) — the Windows counterpart.
- [`create_entra_agent_ids.py`](../provisioning/create-entra-agent-ids-py.md) —
  the underlying identity-chain provisioner.
- [`register_blueprint_cert.py`](../auth-and-certs/register-blueprint-cert-py.md) —
  generates and registers this machine's certificate.
- [`list_blueprint_certs.py`](../auth-and-certs/list-blueprint-certs-py.md) — see which
  certificates are on the Blueprint.
- [`provision_blob_storage.py`](../storage/provision-blob-storage-py.md) —
  cloud-memory provisioning invoked by `--use-cloud-memory`.
- [`status.sh`](../operations/status-sh.md) /
  [`show_agent_status.py`](../operations/show-agent-status-py.md) — status and
  health checks (reached via `--status`).
- [`teardown.sh`](../teardown/teardown-sh.md) — remove everything `setup.sh`
  creates.
- [Identity and token flow](../../../architecture/identity-and-token-flow.md).
