#!/usr/bin/env python3
"""Explicitly authorize A365 observability for an existing Entrabot agent.

This command never creates a Blueprint, Agent Identity, or Agent User.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, replace
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
from pathlib import Path

import httpx

from entrabot.config import EntraBotConfig
from entrabot.errors import AuthError
from entrabot.harness.config import globalcfg, try_load
from entrabot.observability.tokens import refresh_observability_token

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ensure_a365_observability_permissions as permissions  # noqa: E402

SDK_PACKAGE = "microsoft-opentelemetry"
A365_SDK_MODULES = (
    "microsoft.opentelemetry.a365.core.invoke_agent_scope",
    "microsoft.opentelemetry.a365.core.exporters.agent365_exporter",
)
SETUP_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ObservabilityTarget:
    root: Path
    agent_name: str
    tenant_id: str
    agent_id: str
    agent_user_upn: str
    ids: permissions.ProvisioningIds
    config: EntraBotConfig


def load_target(root: Path) -> ObservabilityTarget:
    """Resolve file-based identity, not ambient credentials from a different agent."""
    harness = try_load(str(root))
    is_setup_root = root.resolve() == SETUP_ROOT.resolve()
    if (harness is None or not harness.name.strip()) and not is_setup_root:
        raise ValueError(
            f"No named harness configuration at {root}. Pass --agent-root/-AgentRoot "
            "with an existing agent directory containing .entrabot/harness.json. "
            "This command does not provision or recreate agents."
        )

    shared = globalcfg.read_env(globalcfg.global_env_path(), strict=True)
    setup_combined = globalcfg.read_env(str(SETUP_ROOT / ".env"), strict=True)
    combined = (
        setup_combined if is_setup_root else globalcfg.read_env(str(root / ".env"), strict=True)
    )
    agent_path = Path(globalcfg.agent_env_path(str(root)))
    agent = globalcfg.read_env(str(agent_path), strict=True) if agent_path.is_file() else combined
    env = globalcfg.resolve_agent_env(
        shared, setup_combined, combined, agent=agent,
    )
    required = (
        *globalcfg.TENANT_BLUEPRINT_KEYS,
        "ENTRABOT_AGENT_ID",
        "ENTRABOT_AGENT_OBJECT_ID",
        "ENTRABOT_AGENT_USER_ID",
        "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT",
    )
    missing = [key for key in required if not env.get(key, "").strip()]
    if missing:
        raise ValueError("Complete identity setup first; missing: " + ", ".join(missing))

    agent_name = (
        harness.name.strip()
        if harness is not None and harness.name.strip()
        else env.get("ENTRABOT_AGENT_USER_UPN", "").partition("@")[0] or root.name or "entrabot"
    )
    return ObservabilityTarget(
        root=root,
        agent_name=agent_name,
        tenant_id=env["ENTRABOT_TENANT_ID"],
        agent_id=env["ENTRABOT_AGENT_ID"],
        agent_user_upn=env.get("ENTRABOT_AGENT_USER_UPN", ""),
        ids=permissions.ProvisioningIds(
            blueprint_app_id=env["ENTRABOT_BLUEPRINT_APP_ID"],
            blueprint_object_id=env["ENTRABOT_BLUEPRINT_OBJECT_ID"],
            agent_identity_object_id=env["ENTRABOT_AGENT_OBJECT_ID"],
            agent_user_object_id=env["ENTRABOT_AGENT_USER_ID"],
        ),
        config=EntraBotConfig(
            tenant_id=env["ENTRABOT_TENANT_ID"],
            blueprint_app_id=env["ENTRABOT_BLUEPRINT_APP_ID"],
            blueprint_object_id=env["ENTRABOT_BLUEPRINT_OBJECT_ID"],
            blueprint_cert_thumbprint=env["ENTRABOT_BLUEPRINT_CERT_THUMBPRINT"],
            blueprint_cert_sha1=env.get("ENTRABOT_BLUEPRINT_CERT_SHA1"),
            blueprint_ksp=env.get("ENTRABOT_BLUEPRINT_KSP"),
            agent_id=env["ENTRABOT_AGENT_ID"],
            agent_object_id=env["ENTRABOT_AGENT_OBJECT_ID"],
            agent_user_id=env["ENTRABOT_AGENT_USER_ID"],
            agent_user_upn=env.get("ENTRABOT_AGENT_USER_UPN"),
        ),
    )


def authorize(target: ObservabilityTarget) -> None:
    """Apply approved grants, acquire a resource token, then enable this agent only."""
    config = replace(target.config, a365_observability_enabled=True, a365_export_enabled=True)
    previous_tenant = os.environ.get("ENTRABOT_TENANT_ID")
    os.environ["ENTRABOT_TENANT_ID"] = target.tenant_id
    try:
        token = permissions.get_existing_graph_token()
    finally:
        if previous_tenant is None:
            os.environ.pop("ENTRABOT_TENANT_ID", None)
        else:
            os.environ["ENTRABOT_TENANT_ID"] = previous_tenant
    permissions.ensure_a365_observability_permissions(
        token=token,
        blueprint_app_id=target.ids.blueprint_app_id,
        blueprint_object_id=target.ids.blueprint_object_id,
        agent_identity_object_id=target.ids.agent_identity_object_id,
        agent_user_object_id=target.ids.agent_user_object_id,
    )
    asyncio.run(refresh_observability_token(config, force=True))

    # The explicit per-root overlay wins over legacy combined/global flags at runtime.
    # Do not enable every other agent sharing this Blueprint by changing global.env.
    path = globalcfg.agent_env_path(str(target.root))
    env = globalcfg.read_env(path, strict=True)
    env.update(
        {
            "ENTRABOT_AGENT_ID": target.agent_id,
            "ENTRABOT_AGENT_OBJECT_ID": target.ids.agent_identity_object_id,
            "ENTRABOT_AGENT_USER_ID": target.ids.agent_user_object_id,
            "ENTRABOT_A365_OBSERVABILITY_ENABLED": "true",
            "ENTRABOT_A365_EXPORT_ENABLED": "true",
        }
    )
    if target.agent_user_upn:
        env["ENTRABOT_AGENT_USER_UPN"] = target.agent_user_upn
    globalcfg.write_env(path, env)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--authorize",
        action="store_true",
        required=True,
        help="Apply approved observability grants to existing identities and enable export",
    )
    parser.add_argument(
        "--agent-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Agent directory containing .entrabot/harness.json (default: this clone)",
    )
    args = parser.parse_args(argv)
    try:
        target = load_target(args.agent_root.expanduser().resolve())
        version(SDK_PACKAGE)
        if any(find_spec(module) is None for module in A365_SDK_MODULES):
            raise ValueError(
                "The installed Microsoft OpenTelemetry distribution is missing A365 components. "
                "Install this checkout's dependencies first."
            )
        print(
            f"Authorizing A365 observability for '{target.agent_name}' "
            f"in tenant {target.tenant_id}."
        )
        authorize(target)
        print("Observability permissions configured; export enabled for this agent.")
        print("Restart the harness to load the configuration.")
    except (PackageNotFoundError, ModuleNotFoundError) as exc:
        print(
            f"ERROR: Observability SDK dependency missing ({exc.name}). "
            "Install this checkout's dependencies first.",
            file=sys.stderr,
        )
        return 2
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except (
        permissions.A365ObservabilityPermissionError,
        permissions.ProvisionerBootstrapError,
        AuthError,
        httpx.HTTPError,
    ) as exc:
        print(
            f"ERROR: A365 onboarding failed ({type(exc).__name__}). "
            "Confirm provisioner access, approved permissions, and certificate configuration. "
            "Runtime flags were not changed; rerun --authorize after resolving the failure.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
