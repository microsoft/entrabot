# Changelog

## Unreleased

### Added

- Rename-safe agent matching through canonical `ENTRABOT_AGENT_UPN` with object-ID fallback.
- Boundary-owned XPIA envelopes for model-facing Teams, email, Files, and Work IQ content.
- `read_email`, `read_interactions`, and `bootstrap_body_state`, bringing the MCP surface to 37 tools.
- `list_workbook_worksheets` and `read_workbook_range` for bounded, read-only Excel access through Microsoft Graph, bringing the MCP surface to 39 tools.
- Work IQ Excel adapter with `read_excel_workbook`, `create_excel_workbook`, `add_excel_comment`, and `reply_to_excel_comment`, bringing the MCP surface to 43 tools. Work IQ setup now adds `mcp_ExcelServer` and requests `McpServers.Excel.All`.
- Windows status tooling and expanded Windows setup/teardown guidance.
- Windows setup wizard. `scripts\setup-windows.cmd` is now the single entry point and can be double-clicked. It runs on the Windows PowerShell 5.1 that ships with Windows and needs no administrator rights, PowerShell 7, `winget`, or pre-installed Python or Azure CLI: it finds Python or downloads a private copy, checked against a pinned SHA-512 checksum and the Python Software Foundation's signature, builds `.venv`, and starts `python -m entrabot.winsetup`, which signs in (browser, or device code in a remote session), creates or joins the agent, sets sponsors, offers local or cloud memory, shows a review screen before changing anything, and tests the result. `-Yes` plus parameters runs it unattended; `-Diagnose` checks an existing setup and changes nothing. Exit codes are `0`, `1`, `2` (usage), `3` (set up, something to check), `130` (cancelled).
- `start-entrabot.cmd` starts the agent in Claude Code on Windows, bringing the supported operator commands to 46.
- `scripts\prereqs-windows.ps1 -WithGit` and `-WithClaudeCode` install Git for Windows and Claude Code for the current user without administrator rights or `winget`, verifying the Git download's signature before running it.
- `entrabot.platform.wincert`, the Windows certificate and key lifecycle: create with a non-exportable key (TPM first), find, test-sign, and delete together with the key.
- `blueprint_certs.remove_key_credential` removes one machine's certificate from a Blueprint, keeps the others, and confirms by read-back. `register_windows_blueprint_cert.py --sha1` registers a certificate straight from the certificate store.

### Changed

- **Breaking:** `scripts\prereqs-windows.ps1` is no longer a required step and installs nothing by default. It installs extras only on request (`-WithGit`, `-WithClaudeCode`, `-WithWorkIq`, `-WithBuildTools`). Visual Studio Build Tools, the .NET SDK and the `a365` CLI are no longer needed unless Work IQ is requested or a package has to be compiled.
- **Breaking:** `scripts\teardown-windows.ps1` now disconnects this PC by default: it removes this PC's certificate from the Blueprint and deletes it locally with its key, selected by thumbprint rather than by subject, and retires this PC's setup (Provisioner) certificate, removing it from the Provisioner app and deleting it locally with its key, unless another Entrabot agent on the PC still needs it or `-KeepSetupCredential` is given. It no longer deletes `%LOCALAPPDATA%\entrabot` unless `-PurgeData` is given, and `-DeleteAgent` deletes the agent itself.
- `scripts\deploy-windows.ps1` rotation keeps the current certificate in use until the new one has signed the agent in, and reports `0` rotated, `1` failed, or `3` rotated with cleanup pending. `scripts\setup-windows.ps1`, `deploy-windows.ps1`, `teardown-windows.ps1` and `status-windows.ps1` all run on Windows PowerShell 5.1; `status-windows.ps1` no longer installs anything.
- On Windows, the TPM is chosen by creating a key and signing with it instead of asking `Get-Tpm`, the key is non-exportable in the software key store as well, and the certificate carries no TLS usages. The Provisioner key is stored the same way; an existing Provisioner PEM file under `%LOCALAPPDATA%\entrabot` is replaced and deleted the next time setup runs.
- The Windows setup wizard adds each chosen sponsor on the agent's directory object, which is the list the agent obeys at runtime, and never removes one. When joining or continuing an agent with no sponsor named, it adds nobody and lists the current sponsors.
- `add_agent_sponsor.py` and `remove_agent_sponsor.py` accept an Entra object ID as well as an address.
- Status output colours its `PASS`/`WARN`/`FAIL` labels only on a terminal that can draw colour, and honours `NO_COLOR`. `status-windows.ps1` in a folder that is not connected to an agent says so in one line (a small JSON object with `-Json`) and exits `1`.
- Claude Code hooks run through one cross-platform launcher (`scripts/hooks/run-hook.sh`), pinned to `bash`, with a 20-second timeout. The two gates now block when the hook cannot run. Claude Code on Windows needs Git for Windows for these hooks.
- A second Entrabot server on one Windows PC now exits, as on macOS and Linux; before, the single-instance lock did nothing on Windows.
- `.gitattributes` pins `.cmd`, `.bat` and `.ps1` files to CRLF and `.sh` files to LF.
- Blueprint, BlueprintPrincipal, and Agent Identity creation now use the dedicated Microsoft Graph v1.0 subtype endpoints; Agent User creation remains on beta.
- The public docs site was reorganized into nine task-oriented sections; plans, specs, and investigations were moved to non-published `engineering-history/`; source-audited MCP and script references were added; and legacy URLs redirect to current pages.
- GitHub Pages builds run in strict mode.
- **Breaking:** Work IQ Word and ODSP tools no longer return `raw`. `read_word_document` now returns `comments` as a wrapped string, plus `drive_id`, `document_id`, and `sensitivity_label`; `create_word_document` adds `drive_id` and `item_id`.
- **Breaking:** `get_a365_file_metadata_by_url` now returns `name` inside the XPIA envelope. Work IQ ODSP tools raise a typed error when Work IQ reports `isError`, instead of returning empty fields.
- `add_word_comment` returns a `warning` when Work IQ reports success without a comment ID.
- `ENTRABOT_FILES_MAX_TEXT_BYTES` falls back to the default, with a logged warning, for a zero, negative, or unparseable value. A negative value used to disable the cap.
- `ENTRABOT_FILES_DENIED_SITES` is now checked against the site Graph reports for the item in `list_workbook_worksheets`, `read_workbook_range`, `read_excel_workbook`, `add_excel_comment`, and `reply_to_excel_comment`, and these refuse the request if the site can't be confirmed. Those checks only run when a denylist is set. The denylist is **not** enforced by the Work IQ Word and ODSP tools, and `read_file` and `add_file_comment` still check the `site_id` the caller supplies.
- `scripts/spike_a365_work_iq.py` now validates both Word and Excel, including their scope and audience.
- With cloud memory, the watched-chat list and email cursor now live in the storage container instead of local files, so instances on one container share them and a machine moved to another container can't carry over stale values. **Upgrading a cloud deployment:** on first boot the email cursor starts at "now", so mail received before that first boot isn't pushed but stays readable through `read_email`; after that, a restart reuses the cursor and mail received during downtime is pushed as normal. The watched list comes from the cloud copy plus auto-discovery, and old local `watched_chats` and `email_cursor.txt` files are ignored and can be removed by hand. Upgrade every instance on a container together. See "Upgrading to shared poll state" in Migrations and upgrades.
- Chat auto-discovery reads every page of `/me/chats`, up to 20 pages. A chat first seen through discovery or the shared list surfaces its newest message only if it is under 24 hours old.
- Setup's cloud-memory migration no longer copies local poll state: the watched-chat list, both email cursor files, the legacy `chat_id` key, and the per-chat cursors under `chat_cursors/`. A machine's copies may be stale or belong to another agent, and a copied cursor would skip the 24-hour first-sight check.

### Fixed

- `setup-windows.ps1 -NewChain`, `-UseBlueprint` and `-UpnSuffix` were passed to a helper that ignores command-line arguments, so they had no effect: in a tenant that already had an Entrabot agent, setup could attach to that agent instead of creating a new one. The wizard now passes them the way the helper reads them.
- On Windows a standard user always got a software key, because `Get-Tpm` returns blank values without elevation. Keys are now created in the TPM when it accepts them, with no elevation.
- The Windows Provisioner private key is no longer written to disk as a PEM file.
- Windows certificate rotation was rebuilt. The previous flow removed the old certificate in the same write that added the new one, leaving no working certificate while Microsoft Entra took up the new one, and its smoke test signed with the old certificate rather than the new one.
- Deleting a Windows certificate now deletes its private key as well, instead of leaving it in the key store.
- `.env` on Windows is restricted by replacing its access list with a single entry, and setup reports the file as private only after reading the permissions back. `icacls /inheritance:r` left explicit entries for other accounts in place.
- The Claude Code hooks called `python3`, which a standard Windows Python install does not provide, so the gates could not run on Windows.
- The daily summary could never be archived, and so never sent, on Windows with local memory: storage wrote text in the system code page and translated line endings. Storage now writes UTF-8 with LF on every platform.
- A byte-order mark at the start of `.env` no longer hides its first key.
- The persona-sati token command can be a `.ps1`, `.py` or `.cmd` file, or a command with arguments, on Windows.
- Windows health checks no longer fail for the wrong reason: the certificate check looks in the Windows certificate store, and remediation text names `scripts\setup-windows.cmd`.
- The Windows provisioning helper no longer routes Graph URLs containing parentheses through `az.cmd`.
- The Windows setup wizard finds a sponsor whose address is not their sign-in name, such as a personal Microsoft account that administers the tenant (its directory sign-in name is the `#EXT#` form and its `mail` field is empty). When more than one person matches it lists them instead of picking one.
- Two instances on one container can no longer push the same email: each poll claims its window of mail with a compare-and-swap before pushing.
- A failed Graph read during a chat's bootstrap no longer leaves it with no watermark, which could push up to 10 old messages on the next cycle.
- `bootstrap_body_state` reports the real watched-chat count on a cloud backend, and `null` rather than `0` if the list can't be read ([#64](https://github.com/microsoft/entrabot/issues/64)).
- `scripts/catch_up.py` reads the configured backend instead of a hardcoded home-directory path.
- Channel pushes now mark outside content as data. A Teams push lost the XPIA envelope to the push sanitizer and an email push never had one, so `wrap_for_channel` writes a bracket form a push can carry. A verified sponsor's Teams message stays outside the envelope, and all email is wrapped. Sender names and subjects on an email push also lose their angle brackets, as a Teams push already did.
- Attaching a machine with `setup.sh --use-blueprint` no longer removes other machines' Blueprint certificates: it adds to the list, and `--remove-all-existing-certs` is the only way to replace it. That flag removes certificates only and does not delete the Blueprint. `deploy-windows.ps1` rotation also keeps other machines' certificates ([#124](https://github.com/microsoft/entrabot/issues/124)).
- `setup-windows.ps1` now registers the Blueprint certificate it generates; before, a fresh Windows machine held a certificate Entra had never seen and failed at Hop 1. Re-running setup registers an existing local certificate the Blueprint lacks, other machines' certificates are kept, and the `.env` thumbprint lines are replaced instead of appended.
- `setup-windows.ps1` now writes the Blueprint, agent and Agent User IDs and a default sponsor to `.env`. The runtime reads identity only from the environment and `.env`, so a machine set up with it before had no identity to sign in with.
- Re-running `setup-windows.ps1` no longer moves a machine from cloud memory to local memory. Without `-UseCloudMemory` it added `ENTRABOT_KEEP_MEMORY_LOCAL=true`, which outranks the blob settings; the memory lines are now replaced instead of appended, and a machine already on cloud memory stays there. A blob provisioner that prints no endpoint now falls back as intended instead of stopping setup.
- A failed Graph read can no longer look like an empty certificate list. `verify_blueprint_cert.py` and `find_local_blueprint_cert.py` exit `3` for "could not check", `list_blueprint_certs.py` exits non-zero and prints no count, and setup stops instead of regenerating.
- A new Blueprint private key stays under a temporary keystore entry until its certificate is confirmed on the Blueprint, so a failed upload no longer strands the machine.
- `setup.sh` no longer trusts the thumbprint cached in the state file. It reuses a certificate only when this machine's private key matches one registered on the Blueprint, and it always runs `register_blueprint_cert.py` while a new key is still pending, so a re-run after an interrupted or unconfirmed certificate update recovers instead of carrying on with a credential that is about to be revoked. `--remove-all-existing-certs` changes nothing on the machine until the replacement is confirmed. `verify_blueprint_cert.py` is now an operator tool that setup does not call.
- An expired Blueprint certificate is no longer reused: `find_local_blueprint_cert.py` and pending-key recovery treat it as no match, so setup registers a new one (the expired entry stays on the list). Two `register_blueprint_cert.py` runs by the same OS user can no longer overlap and clobber the shared pending key, and the state file is written atomically so a full disk can't leave it truncated.
- Re-running `setup.sh` for the same Agent User no longer changes the sponsor or moves an agent on cloud memory to an empty local store. Setup reads the existing `.env` first and keeps the sponsor (`ENTRABOT_HUMAN_*`) unless `--teams-user` or `--switch-user` is passed, and keeps the blob settings unless `--keep-memory-local` or `--use-cloud-memory` is passed; the repair command `setup.sh --use-blueprint=<id>` passes neither. A failed blob provisioning now shows the provisioner's output and keeps the cloud settings (or falls back to local) instead of ending setup silently after `.env` was already rewritten. A run for a different Agent User carries nothing over, and other lines added to `.env` by hand are still dropped.
- Self-authored Teams messages remain filtered after an Agent User display-name change.
- Forged `<external_content>` text can no longer bypass the authoritative XPIA boundary.
- A UTC-midnight-dependent test no longer fails intermittently on Windows CI.
- Work IQ Word and ODSP tool output now passes model-facing content only through the XPIA boundary, and Word comments are no longer dropped from the parsed result.
- `teardown-windows.ps1` no longer takes a certificate off the Blueprint just because `.env` names it. A certificate that is not in this PC's certificate store may be another PC's (a copied or shared `.env`), so it is left on the Blueprint: with `-Yes` it is left and reported, and interactively you are asked, default No. A certificate left behind by an interrupted rotation (`ENTRABOT_BLUEPRINT_CERT_RETIRING_SHA1`) is cleaned up too. A private key that could not be deleted is reported as such, never as deleted, and teardown exits `1`.
- `deploy-windows.ps1` refuses to rotate unattended without `-Yes` (exit `2`), and removes the old certificate from the Blueprint only from a read of the list that shows the new one. If Microsoft Entra has not listed the new certificate yet, the old one is left and rotation exits `3`; running it again finishes the cleanup.
- The Windows setup wizard shows an older setup key it could not switch off in Microsoft Entra on screen in every mode, at the provisioning step and on the finish screen, with the last four characters of its thumbprint, its expiry date and the admin-center path, and finishes with something to check (exit `3`). Before, the warning was only in the setup log unless `-Verbose` was given, and it did not change the exit code.
- `teardown-windows.ps1` removes the certificates that must come off the Blueprint (this PC's, and one an interrupted rotation left) in one write, through the new `blueprint_certs.remove_key_credentials`. Two removals in a row were two whole-list writes, and the second one's read could still list the first certificate and put it back.
- The Windows setup wizard finds a sponsor whose sign-in name contains an apostrophe (`o'brien@contoso.com`): a search for the sign-in name now runs right after the direct lookup, which may not accept the apostrophe.
- `create_entra_agent_ids.py` no longer reads a failed Agent Identity or Agent User lookup as "there is none". An HTTP error, a failed request, an answer it cannot read, or a partial list now stops the run before that object is created, instead of creating a second Agent Identity for the machine or an Agent User that then collided on its name. Each stop prints one `ENTRABOT_RESULT=` line naming its outcome just before its message.
- The Windows setup wizard says why the agent step stopped (the Blueprint App ID does not exist in this tenant, Microsoft Entra could not be read, several applications have the agent's name, or the chosen name belongs to a different agent) instead of "The agent could not be created.". At the step that connects it to the tenant it says when the value in `ENTRABOT_PROVISIONER_APP_ID_VERIFIED` was not accepted, or when Microsoft Entra could not be read, instead of "Setup could not get the access it needs in your tenant.". The `ENTRABOT_RESULT=` lines it reads are kept off the screen; the setup log has them.
- The health checks report an older setup key that is still waiting to be switched off, as an "Older setup keys" warning that does not change the exit code: `scripts\setup-windows.cmd -Diagnose`, which then ends "No check failed, but an older setup key still has to be switched off in Microsoft Entra (see above).", and `show_agent_status.py` on every platform (`status-windows.ps1`, `status.sh`). In a folder that holds only setup's note about such a key, `status-windows.ps1` says the folder is not connected to an agent and which key is waiting, and exits `1`.

### Removed

- Bot Gateway mode and its M365 Agents SDK dependency (ADR-006).
- The unused Claude pull-request review workflow.

### Security

- Windows setup checks the Claude Code version. The two hook gates block a tool call when they fail to start only on Claude Code 2.1.295 or later; on an older version setup warns, prints the update command, and finishes with something to check (exit `3`).
- The Claude Code hooks read their input as UTF-8 on every platform, and the two gates block on any unexpected error, including input they cannot decode, instead of exiting `1`. On Windows a non-ASCII character in the input crashed the hooks, and a non-ASCII profile path let local memory writes through.
- On Windows the persona-sati token command must be an absolute path or a program on `PATH`. A relative path, or a program found only in the current folder, is refused, and a `.ps1` script runs under the in-box Windows PowerShell, never a `pwsh` or `powershell` found by name.
- When Windows setup moves off an older install's Provisioner key file, it now also removes that file's certificate from the Provisioner app, so a copy of the file stops working. If the removal fails, setup still finishes, says which certificate to remove (last four characters of its thumbprint, and its expiry) and where, and tries again on the next run.
- Setup no longer takes over an existing app registration just because it is named 'EntraBot Agent ID Provisioner' (macOS, Linux and Windows). That app is granted tenant-wide Microsoft Graph permissions with admin consent, and app names are not unique. A machine with no Provisioner app recorded takes an existing one over only when an administrator has already approved it for every permission setup needs, or when nobody else can use it (you are its only owner or it has none, every credential on it is this machine's, and it asks for no other permissions). Otherwise setup stops before changing anything in Microsoft Entra, lists the apps it found and why it did not use them, and exits `1`: delete or rename the app you don't recognise, or set `ENTRABOT_PROVISIONER_APP_ID_VERIFIED` to the application ID of the one you have checked, for that run. Microsoft Graph is identified by its well-known application ID, never by its display name. Earlier versions took over the first app whose name began with it; in a tenant set up with one, check that app's owners and credentials once (see "Setup will not take over an existing Provisioner app registration" in Setup and authentication troubleshooting).
- `teardown-windows.ps1` tries to remove an older setup key that setup could not take off the Provisioner app (`PROVISIONER_CERT_REVOKE_PENDING`) before it deletes the state file that records it. If that fails it lists the certificate and where to remove it by hand, and exits `1`; when it keeps the setup certificate (`-KeepSetupCredential`) it leaves `.entrabot-state.json` holding only that note, so that setup's next run tries again.
- `create_entra_agent_ids.py` reuses an existing Agent User found by its sign-in name only when its parent Agent Identity belongs to the Blueprint the run is for. Before, it took the Agent User and its Agent Identity whatever Blueprint they belonged to, granted consents, an app role and a licence on them, and recorded them beside the run's Blueprint; `setup.sh --use-blueprint` reached this on a machine switching agents, because the script read the previous agent's `ENTRABOT_AGENT_USER_UPN` from `.env`. An Agent User of another Blueprint now stops the run before anything is created, granted or recorded, and the message says how to join that Blueprint or choose another name; one whose Blueprint can't be read stops as "could not check".
- `setup.sh --use-blueprint` no longer hands the agent step an `ENTRABOT_AGENT_USER_UPN` that `.env` holds for another Blueprint, and says so. After that step it checks that the Blueprint recorded in `.entrabot-state.json` is the one asked for, and otherwise stops before Work IQ and the certificate step, as the Windows wizard does.
- `teardown.sh` deletes only the Provisioner app recorded for this machine in `.entrabot-state.json`, by its application ID. It used to delete the first app registration whose name began with 'EntraBot Provisioner' and the first whose name began with 'EntraBot Agent ID Provisioner', which could be somebody else's. With no application ID recorded it deletes no app and prints how to delete this machine's by hand.

## v0.1 — 2026-05-21

First public release. Reference implementation for Microsoft Entra Agent ID and Microsoft Agent 365 (GA 2026-05-01). MIT licensed. **Research repo, not production-ready** — see Known Limitations below.

### Added

**Identity & auth**
- Three-hop certificate chain: Blueprint → Agent Identity → Agent User. Private key in OS keystore (Keychain / Windows TPM via CNG / Linux Secret Service). No client secret in flight, no `.env` secrets on disk.
- Three auth modes: `agent_user` (full three-hop), `delegated` (MSAL interactive), `bot` (Bot Framework + M365 Agents SDK).
- Progressive identity state machine (UNAUTHENTICATED → DELEGATED → PROVISIONING → AGENT_USER).
- Cross-tenant federated B2B chat — guest UPN auto-decoded, home tenant resolved via OIDC discovery.
- Sponsor gate enforcement on every mutating tool (`share_file`, `add_teams_member`); requester must be an authorized sponsor of the chat.

**Agent capabilities (34 MCP tools)**
- Teams: send, read, create chat, list/add members, watch replies, placeholder + resolve, delete own message, image view.
- Outlook: `send_email` with thread-preserving replies, daily summary at 5pm PDT.
- Files: `resolve_file_url`, `list_recent_files`, `read_file`, `add_file_comment`.
- Agent 365 Work IQ Word: create/read documents, add/reply to comments.
- Promises (durable across restart): add, list, resolve.
- Identity: `whoami`, `audit_log`, on-demand daily summary.

**Channel push & polling fallback**
- `notifications/claude/channel` push extension — Claude Code gets inbound Teams DMs and emails as next-turn system reminders. The Teams conversation IS the conversation with the agent.
- `send_teams_message` auto-blocks on non-Claude-Code hosts (Copilot CLI, Codex, Cursor) for the sponsor's reply. Deterministic, host-detected, no parameter the model can disable.

**Body prompt architecture**
- Non-overridable body prompt at `prompts/agent_system.md` with `@include` expansion of `prompts/anatomy/*.md`. Security, channel discipline, identity/tools rules load below the persona line.
- Body-first instruction boundaries and channel discipline designed to reduce instruction-injection and operator-impersonation risk.

**Mind / persona (optional)**
- Persona-sati MCP integration. Body composes `body + persona` at boot when `PERSONA_SATI_MCP_URL` is set. Clean fallback to body-only mode when persona-sati is unreachable.

**Storage**
- ADR-005 Phases 1, 2, 5, 6a: opt-in cloud-hosted memory via Azure Blob. Per-Agent-User container, RBAC scoped to the agent's object ID. Local filesystem fallback.

**Agent 365 Work IQ provider**
- Reusable `entrabot.a365` provider boundary for Work IQ MCP servers (Word, Mail, Copilot, Dataverse). Manifest loading, audience-specific token acquisition, MCP-client calls. Teams intentionally remains Graph-native.

**Discipline**
- 1,237 tests, ruff clean, 80% coverage threshold enforced.
- 66 hard-won learnings (now archived at `engineering-history/research/hard-won-learnings.md`).
- Full docs site at <https://microsoft.github.io/entrabot/> with API reference, script reference, ADRs, platform learnings, and runbooks (the original structure; later reorganized — see Unreleased → Changed).

### Known limitations

- **Windows port is acceptance-tested on one ARM64 VM only.** (At that release; see Unreleased for the current Windows verification scope.) Verified end-to-end (cert provisioning, three-hop CNG signing, MCP registration, Teams DM round-trip) but not exercised on broad hardware, intel x64, or in long-running sessions.
- **Bot Gateway needs live Azure Bot Service registration.** The local server + Dev Tunnel path works; the productized "bot has its own Teams app identity" path needs an Azure Bot resource and Teams app manifest signed in a tenant.
- **Persona-sati 12h MCP bearer refresh** is open (draft PR persona-sati#47 paused at Agent Blueprint OAuth client constraints). Affects only sessions that connect to persona-sati after the 12h boundary; restart the MCP host to recover.
- **Agent Identity provisioning omits `Application.Read.All`**, which `wait_for_sponsor_dm` needs to call the sponsors API. Manual grant required as a workaround until `scripts/create_entra_agent_ids.py` is updated.
- **`add_file_comment` on `.docx` files returns 404** on Graph beta `/comments` — Microsoft does not expose Word document comments there. Use the Work IQ Word path instead (`add_word_comment` / `reply_to_word_comment`).
- **CLI commitment-language detection is not enforced.** Server-side detection fires on outbound Teams text; commitments made to the operator in the host terminal silently drift. Mitigation is body-prompt strengthening, not mechanical enforcement.

APIs may change in 0.x releases. Pin to a commit SHA or a release tag for stability.

### Provenance

History was rewritten with `git filter-repo` before this release to remove pre-sanitization PII commits (real Microsoft employee names, emails, production tenant GUIDs, internal codenames). The repo went from private to public at this release. Commits before `f2a3c18` no longer exist.
