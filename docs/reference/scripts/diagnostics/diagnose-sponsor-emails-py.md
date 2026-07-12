# `diagnose_sponsor_emails.py`

## Purpose

Read-only diagnostic that pinpoints why an Agent Identity's sponsor email fields (`mail`, `userPrincipalName`, `otherMails`, `proxyAddresses`, `identities`) come back null — the root cause behind `SponsorGate` and other email-matching allowlist logic silently rejecting a sponsor it should recognize. It runs the same sequence of Graph projections and token checks used to originally diagnose this class of bug, so a fresh run reproduces the full picture (which token, which endpoint, which fields) in one pass instead of re-deriving it by hand.

## Requirements

- Python 3.12+ with the project's virtualenv active and `entrabot` importable (run via the venv's own interpreter, not a bare system `python`).
- `ENTRABOT_AGENT_OBJECT_ID` (or the equivalent state written by `setup.sh`) must resolve via `get_config()` — the script exits immediately if it doesn't.
- Both the Agent Identity FIC token (Hop 2) and the Agent User token (Hop 3) must be acquirable, which in turn requires the full Blueprint certificate + tenant + Agent Identity + Agent User configuration already in place from provisioning.
- At least one sponsor must already be assigned to the Agent Identity (via `add_agent_sponsor.py`) — the script stops after probe 2 if the sponsors collection is empty.

## Usage

```bash
./.venv/bin/python scripts/diagnose_sponsor_emails.py
```

```powershell
.\.venv\Scripts\python.exe scripts\diagnose_sponsor_emails.py
```

No command-line arguments or flags are read; the script operates on the single Agent Identity resolved from configuration.

## Effects

None on any Entra or Graph resource — this script only issues `GET` requests. It prints, in order:

1. `GET /servicePrincipals/{agentObjectId}/microsoft.graph.agentIdentity/sponsors` without `$select`.
2. The same endpoint with `$select=id,userPrincipalName,mail,otherMails,proxyAddresses,identities` — this is where the first sponsor's object ID is captured for the remaining probes.
3. `GET /users/{sponsorId}` (with `$select`) using the **Agent Identity FIC token**.
4. The same `GET /users/{sponsorId}` call (with `$select`) using the **Agent User token**.
5. The same call again **without** `$select`, still on the Agent User token, to rule out `$select` itself as the cause of empty fields.
6. `GET /users?$filter=id eq '{sponsorId}'` (with `$select`) on the Agent User token — filter-based search sometimes has different permission semantics than a direct `GET /users/{id}`.
7. `GET /me` (with `$select`) on the Agent User token, to show what the token believes its own identity projection looks like.
8. A local (no-signature-verification) JWT decode of the Agent User token, printing only `aud`, `iss`, `appid`, `tid`, `oid`, `upn`, `scp`, `roles`, `idtyp`, `amr` — enough to see whether the token has the delegated scope the enrichment call needs, without printing the token itself.
9. The same decode for the Agent Identity FIC token, printing `aud`, `iss`, `appid`, `tid`, `oid`, `upn`, `scp`, `roles`, `idtyp`.

**This script diagnoses allowlist/permission gaps, not email deliverability.** It never sends a message or an email; it only tells you why a sponsor's address fields are empty in Graph's response, which is a directory-read problem, not a mail-transport problem.

The most common pattern to look for in the output: the `/sponsors` projection at step 2 returns only a populated `id` with every other field null regardless of `$select` (this is expected Graph behavior for that nav-property collection, not a bug), and the enrichment calls at steps 3–5 return `403 Forbidden` because the token in use lacks `User.ReadBasic.All` (a delegated, tenant-wide read is required — `User.Read` alone only covers `/me`). Step 8's `scp` claim will show whether that scope is present.

## Exit behavior

- `2` — `agent_object_id` is not configured; nothing else runs.
- `1` — the sponsors collection at step 2 came back empty (no sponsor IDs to probe further).
- `0` — all nine probes ran to completion. This does **not** mean every probe succeeded — a `0` exit with several `403`/`404` lines printed is the expected result when reproducing the allowlist gap; read the printed status codes, not the exit code, to judge the outcome.

## Related commands

- [`list_sponsors.py`](list-sponsors-py.md) — lists the same sponsors collection this script probes, without the extra enrichment/token diagnostics.
- [`add_agent_sponsor.py`](../provisioning/add-agent-sponsor-py.md) / [`remove_agent_sponsor.py`](../provisioning/remove-agent-sponsor-py.md) — sponsor management; run these before this diagnostic has anything to probe.
- [`grant_consent.py`](../auth-and-certs/grant-consent-py.md) / [`grant_files_consent.py`](../auth-and-certs/grant-files-consent-py.md) — use these if a probe shows the Agent User token is missing a required scope.
- [Microsoft Entra Agent ID — Sponsors](../../../platform-docs/agent-id-blueprints-and-users.md#sponsors) and [Agent Users — Consent for an Agent User](../../../platform-docs/entra-agent-users.md#consent-for-an-agent-user) — the underlying object model and consent semantics this script's probes are built against.
- [Scripts reference: Diagnostics](../index.md#diagnostics) — the other diagnostics in this set.
