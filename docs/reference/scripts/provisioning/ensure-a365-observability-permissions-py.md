# ensure_a365_observability_permissions.py

## Purpose

Gives the agent the delegated permission Agent 365 telemetry needs, `Agent365.Observability.OtelWrite` on the Agent 365 Observability API (`9b975845-388f-4429-889e-eab1ef63949c`), using the Provisioner token. The setup wizard runs it when telemetry is switched on (`--configure-a365-observability`, `-ConfigureA365Observability`, or the telemetry question); it can also be run by hand.

## Requirements

- The setup (Provisioner) certificate and state from a completed setup.
- The Blueprint, Agent Identity and Agent User IDs, from `.entrabot-state.json` or the arguments below.

## Usage

```bash
python scripts/ensure_a365_observability_permissions.py
python scripts/ensure_a365_observability_permissions.py --blueprint-app-id <id> \
    --blueprint-object-id <id> --agent-object-id <id> --agent-user-id <id>
```

## Effects

- Grants the Blueprint and its Agent User path the delegated Observability write scope, idempotently: a second run changes nothing.
- Never calls the Agent 365 service itself and never assigns a licence. Agent 365 drops the agent's telemetry unless someone in the tenant has a Microsoft 365 E7 or Agent 365 licence.
- Setup then checks, as the agent, that its token carries the scope before switching telemetry on in `.env`.

## Exit behavior

- `0`: the permissions are in place.
- `1`: no Provisioner token, or Graph refused a grant.
- `2`: an ID is missing from the arguments and the state file.

## Related commands

- [`setup.sh`](../setup/setup-sh.md) (`--configure-a365-observability`) and [`setup-windows.ps1`](../setup/setup-windows-ps1.md) (`-ConfigureA365Observability`).
- [`register_agent_instance.py`](register-agent-instance-py.md): the separate, opt-in registry registration.
