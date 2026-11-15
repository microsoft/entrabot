# register_agent_instance.py

## Purpose

Registers the agent in the Microsoft Agent 365 registry through the Agent Registry API (Microsoft Graph beta `POST /copilot/agentRegistrations`). Microsoft Learn notes that creating an Entra identity alone doesn't register the instance, and a Blueprint created through the Graph API, as Entrabot's is, registers with an explicit call. Opt-in and separate from setup.

## Requirements

- An agent already set up on this computer (`AGENT_ID` and `BLUEPRINT_APP_ID` in `.entrabot-state.json`).
- With `--apply`: the setup (Provisioner) app must hold the Microsoft Graph application permission `AgentRegistration.ReadWrite.All`, added and admin-consented by an administrator. The script checks its token and stops if the permission is missing; it never adds one.
- At least one owner: a user's object ID (`--owner-id`, or `ENTRABOT_HUMAN_USER_ID` from `.env`).

## Usage

```bash
python scripts/register_agent_instance.py                 # dry run: print the registration
python scripts/register_agent_instance.py --apply         # write it
python scripts/register_agent_instance.py --apply --owner-id <object-id>
```

## Effects

- Without `--apply`, prints the registration body and sends nothing.
- With `--apply`, reads `AGENT_REGISTRATION_ID` from the state file. If it is set, the registration is read and updated; if Graph answers 404, a new one is created; any other answer stops the run. The new ID is saved as `AGENT_REGISTRATION_ID`, so a later run never creates a second registration because a lookup failed.
- Never run against a real tenant yet: dry run and unit tests only.

## Exit behavior

- `0`: dry run printed, or the registration was written.
- `1`: no Provisioner token, the permission is missing, or Graph refused the write.
- `2`: no owner, or no agent recorded on this computer.

## Related commands

- [`ensure_a365_observability_permissions.py`](ensure-a365-observability-permissions-py.md): the telemetry permission, which setup grants when telemetry is switched on.
- [Microsoft Agent 365](../../../platform-docs/microsoft-agent-365.md): telemetry and the registry.
