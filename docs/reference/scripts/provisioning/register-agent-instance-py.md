# register_agent_instance.py

## Purpose

Registers the agent in the Microsoft Agent 365 registry through the Agent Registry API (Microsoft Graph beta `POST /copilot/agentRegistrations`), so Microsoft lists it in the Microsoft 365 admin center under Agents > All agents > Registry. Microsoft Learn notes that creating an Entra identity alone doesn't register the instance, and a Blueprint created through the Graph API, as Entrabot's is, registers with an explicit call.

Setup runs it when you answer yes to Agent 365 integration (`--configure-a365`, `-ConfigureA365`), after the telemetry step. You can also run it yourself: to look at the request (the default dry run), to check or delete the recorded registration, or to record a registration made from another computer of the same agent.

## Requirements

- An agent already set up on this computer (`AGENT_ID` and `BLUEPRINT_APP_ID` in `.entrabot-state.json`).
- With `--apply`, `--check` or `--delete --apply`: the setup (Provisioner) app must hold the Microsoft Graph application permission `AgentRegistration.ReadWrite.All`, with admin consent. Setup adds it, through the signed-in administrator's Azure CLI, when you answer yes to Agent 365 integration. Run on its own, the script never adds a permission: it checks its token and stops if the permission is missing.
- At least one owner, a user's object ID: `--owner-id`, or the sponsors in `.env` (`ENTRABOT_HUMAN_USER_IDS`, members of the tenant before guests). Setup passes the sponsors, or the signed-in account when `.env` names none.

## Usage

```bash
python scripts/register_agent_instance.py                    # dry run: print the registration
python scripts/register_agent_instance.py --apply            # create it, or update the recorded one
python scripts/register_agent_instance.py --apply --owner-id <object-id>
python scripts/register_agent_instance.py --apply --registration-id <id>   # use one made elsewhere
python scripts/register_agent_instance.py --check            # is the recorded one still there?
python scripts/register_agent_instance.py --delete           # dry run of the delete
python scripts/register_agent_instance.py --delete --apply   # delete the recorded one
```

`--display-name NAME` sets the name the registry shows; the default is `ENTRABOT_AGENT_DISPLAY_NAME` from `.env`, else the part of the agent's address before the `@`. `--print-result` ends the output with one `ENTRABOT_RESULT=<json>` line (`outcome`, `id`, `reason`), which is how setup, `--diagnose` and teardown read it.

## Effects

- Without `--apply`, prints the registration body and sends nothing.
- With `--apply`, reads `AGENT_REGISTRATION_ID` from the state file (or uses `--registration-id`). If one is recorded, it is read and updated; a registration that names another agent is left alone. If Graph answers 404, a new one is created. Any other answer stops the run without creating anything, so a lookup that failed never leads to a second registration. A new ID is saved as `AGENT_REGISTRATION_ID` as soon as Graph returns it.
- The Agent Registry API has no list call, so a registration this computer has not recorded cannot be found. Setup does not run `--apply` on a computer that joined an agent that already existed and has no registration recorded; use `--registration-id` there to record the one another computer made (its `--diagnose` shows the ID).
- `--check` only reads. `--delete --apply` deletes the recorded registration and removes `AGENT_REGISTRATION_ID`; teardown with `--delete-agent` runs it.
- The request body is built in one function, `entrabot.a365.registry.build_registration`. It sends application IDs for `agentIdentityId` and `agentIdentityBlueprintId`; Learn does not say whether they take application IDs or object IDs, and Graph accepted them in a real run.
- Run for real on Linux through setup: `--apply` registered the agent and `--check` (through `--diagnose`) found it. For an Entra agent identity the registration ID Graph returned was the agent identity's ID. `--delete --apply` deleted the registration and cleared `AGENT_REGISTRATION_ID`, after which `--diagnose` said "not registered". The admin center can keep listing a deleted agent for a few minutes.

## Exit behavior

- `0`: dry run printed, the registration was written or deleted (or was already gone), or `--check` had an answer (present, gone, or nothing recorded).
- `1`: no setup app token, the permission is missing, Graph refused, or the recorded registration could not be read.
- `2`: no owner, or no agent recorded on this computer.

## Related commands

- [`setup.sh`](../setup/setup-sh.md) (`--configure-a365`) and [`setup-windows.ps1`](../setup/setup-windows-ps1.md) (`-ConfigureA365`): Agent 365 integration, which runs this script after the telemetry step.
- [`ensure_a365_observability_permissions.py`](ensure-a365-observability-permissions-py.md): the telemetry half of Agent 365 integration.
- [Microsoft Agent 365](../../../platform-docs/microsoft-agent-365.md): telemetry and the registry.
