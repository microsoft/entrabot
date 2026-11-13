"""Write the agent's identity, certificate and sponsor to ``.env``.

``entrabot.config`` reads identity only from the environment and ``.env``, never from the
state file, so everything the runtime needs is written on every run.

Two things in ``.env`` belong to ONE Agent User and are never carried to another: the sponsor
and the blob store. A run for the same agent keeps the sponsor it finds (the account running
setup is not always the sponsor); a run for a different agent drops both.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from entrabot.winsetup.certstep import CertOutcome
from entrabot.winsetup.envfile import EnvGuard, read_env, update_env
from entrabot.winsetup.sponsors import SPONSOR_KEYS, SPONSOR_NAMING_KEYS, Sponsor, sponsor_env

BLOB_KEYS = ("ENTRABOT_BLOB_ENDPOINT", "ENTRABOT_BLOB_CONTAINER")
LOCAL_MEMORY_KEY = "ENTRABOT_KEEP_MEMORY_LOCAL"


@dataclass(frozen=True)
class Identity:
    tenant_id: str
    blueprint_app_id: str
    blueprint_object_id: str
    agent_id: str
    agent_object_id: str
    agent_user_id: str
    agent_user_upn: str
    provisioner_app_id: str


@dataclass(frozen=True)
class EnvResult:
    same_agent: bool
    sponsor: Literal["kept", "set", "none"]
    sponsor_label: str


def identity_from_state(state: Mapping[str, str], *, tenant_id: str) -> Identity:
    """The IDs ``create_entra_agent_ids.py`` recorded in ``.entrabot-state.json``."""
    return Identity(
        tenant_id=tenant_id,
        blueprint_app_id=state.get("BLUEPRINT_APP_ID", ""),
        blueprint_object_id=state.get("BLUEPRINT_OBJECT_ID", ""),
        agent_id=state.get("AGENT_ID", ""),
        agent_object_id=state.get("AGENT_OBJECT_ID", ""),
        agent_user_id=state.get("AGENT_USER_ID", ""),
        agent_user_upn=state.get("AGENT_USER_UPN", ""),
        provisioner_app_id=state.get("PROVISIONER_CLIENT_ID", ""),
    )


def is_same_agent(previous_agent_user_id: str, agent_user_id: str) -> bool:
    """With no ID on one side there's no telling, which counts as the same agent: a machine
    set up before setup wrote IDs has none in ``.env``."""
    return (
        not previous_agent_user_id or not agent_user_id or (previous_agent_user_id == agent_user_id)
    )


def named_sponsor(env_values: Mapping[str, str]) -> str:
    """The sponsor ``.env`` already names, for display, or '' when it names none."""
    for key in (
        "ENTRABOT_HUMAN_UPNS",
        "ENTRABOT_HUMAN_UPN",
        "ENTRABOT_HUMAN_USER_IDS",
        "ENTRABOT_HUMAN_USER_ID",
    ):
        if env_values.get(key):
            return env_values[key]
    return ""


def write_identity_env(
    env_path: Path,
    *,
    identity: Identity,
    cert: CertOutcome,
    explicit_sponsors: Sequence[Sponsor] | None,
    default_sponsor: Callable[[], Sponsor | None],
    guard: EnvGuard | None = None,
) -> EnvResult:
    """Replace the identity, certificate and (when it should change) sponsor lines in ``.env``.

    Lines are replaced, never appended. A value this run doesn't have keeps what the file
    already says. *explicit_sponsors* is what the person chose in this run and always wins;
    without it the signed-in account (*default_sponsor*) becomes the sponsor only when the
    file names none for this agent.
    """
    existing = read_env(env_path)
    same_agent = is_same_agent(existing.get("ENTRABOT_AGENT_USER_ID", ""), identity.agent_user_id)

    values: dict[str, str] = {
        "ENTRABOT_TENANT_ID": identity.tenant_id,
        "ENTRABOT_BLUEPRINT_APP_ID": identity.blueprint_app_id,
        "ENTRABOT_BLUEPRINT_OBJECT_ID": identity.blueprint_object_id,
        "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT": cert.x5t_s256,
        "ENTRABOT_BLUEPRINT_CERT_SHA1": cert.sha1_hex,
        "ENTRABOT_BLUEPRINT_KSP": cert.ksp,
        "ENTRABOT_AGENT_ID": identity.agent_id,
        "ENTRABOT_AGENT_OBJECT_ID": identity.agent_object_id,
        "ENTRABOT_AGENT_USER_ID": identity.agent_user_id,
        "ENTRABOT_AGENT_UPN": identity.agent_user_upn,
        "ENTRABOT_AGENT_USER_UPN": identity.agent_user_upn,
        "ENTRABOT_PROVISIONER_APP_ID": identity.provisioner_app_id,
    }
    to_set = {key: value for key, value in values.items() if value}

    kept_label = named_sponsor(existing) if same_agent else ""
    sponsor: Literal["kept", "set", "none"]
    if explicit_sponsors:
        to_set.update(sponsor_env(explicit_sponsors))
        sponsor, label = "set", ",".join(item.upn for item in explicit_sponsors)
    elif same_agent and any(existing.get(key) for key in SPONSOR_NAMING_KEYS):
        sponsor, label = "kept", kept_label
    else:
        found = default_sponsor()
        if found is not None:
            to_set.update(sponsor_env([found]))
            sponsor, label = "set", found.upn
        else:
            sponsor, label = "none", ""

    # A different agent's sponsor and blob settings go now, whatever happens next: a failed
    # lookup or an unconfirmed certificate must not leave them looking like this agent's.
    inherited = () if same_agent else (*SPONSOR_KEYS, LOCAL_MEMORY_KEY, *BLOB_KEYS)
    update_env(env_path, to_set, remove=inherited, guard=guard)
    return EnvResult(same_agent=same_agent, sponsor=sponsor, sponsor_label=label)
