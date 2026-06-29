"""`entrabot init` — an interactive, cross-platform setup walkthrough.

Sets up an agent **in a chosen directory**. The identity chain has a shared root and a per-agent
leaf, so the wizard does only as much as needed:

* **First time** (no global config yet): confirm a tenant → ``az login`` → prereqs → provision a
  *new chain* (Blueprint + cert + Agent) → split the result into a shared ``~/.entrabot/global.env``
  (tenant + blueprint) and this directory's per-agent ``.env``.
* **Adding an agent** (global config exists): skip tenant/az/prereqs entirely and provision just a
  new Agent User **under the existing Blueprint** (reusing tenant + cert), writing only this
  directory's per-agent ``.env``.

So a second agent that "goes by a different name" is seconds, not the full walkthrough. Provisioning
runs the platform scripts under ``scripts/`` (a clone / unpacked sdist); a lean wheel degrades to
"clone to provision" with links, while the runtime stays repo-independent.
"""

from __future__ import annotations

import os

from ..config import globalcfg
from ..ui import ansi
from .platform import _ask, _derive_suffix, _platform, _say
from .provisioning import _provision_identity
from .steps import (
    _apply_existing_env,
    _choose_directory,
    _connection_test,
    _existing_name,
    _scaffold_config,
)


class _Stepper:
    """Numbers steps as we go, since the reuse path has fewer of them."""

    def __init__(self, total: int) -> None:
        self.step_number = 0
        self.total = total

    def __call__(self, title: str) -> None:
        self.step_number += 1
        print()
        print(ansi.cyan(ansi.bold(f"═══ Step {self.step_number}/{self.total} — {title}")))


def _resume_existing_agent(root: str) -> tuple[str, _Stepper]:
    """Idempotent re-run: this dir already has an agent. Load its identity and continue with the
    remaining (re-runnable) setup — connection test + config — skipping provisioning."""
    existing_env = globalcfg.read_env(globalcfg.agent_env_path(root))
    name = _existing_name(root) or _derive_suffix(os.path.basename(root.rstrip("/\\")))
    _say(ansi.green(f"\n  Found an existing agent here: {existing_env['ENTRABOT_AGENT_USER_UPN']}"))
    _say(ansi.dim("  Re-running to continue setup — identity already provisioned, skipping it."))
    _apply_existing_env(root)
    return name, _Stepper(total=2)


def run_init(root: str) -> bool:
    """Run the walkthrough for an agent rooted at ``root``. Returns True if set up + verified."""
    platform_name = _platform()
    print(ansi.bold(ansi.cyan("\nENTRABOT setup")) + ansi.dim(f"  ({platform_name})"))

    # Directory (always asked). An already-provisioned dir resumes instead of re-minting.
    root = _choose_directory(root)
    if globalcfg.agent_exists(root):
        name, step = _resume_existing_agent(root)
    else:
        default_name = os.path.basename(root.rstrip("/\\")) or "entrabot"
        name = _ask("Name this agent (its Teams display name)", default=default_name)
        step = _Stepper(total=3 if globalcfg.global_exists() else 6)
        if not _provision_identity(platform_name, root, name, step):
            return False

    step("Connection test")
    verified = _connection_test(os.environ.get("ENTRABOT_AGENT_USER_UPN", name))
    if not verified:
        _say(ansi.yellow("  A new agent's Teams/mailbox can take 10-15 min to provision. The"))
        _say(ansi.yellow("  identity is created and saved — re-check later with `entrabot doctor`."))

    step("Harness config")
    _scaffold_config(root, name)

    print()
    if verified:
        _say(ansi.green(ansi.bold(f"✓ ENTRABOT agent '{name}' is set up and verified.")))
    else:
        _say(ansi.green(ansi.bold(f"✓ ENTRABOT agent '{name}' is set up (token not live yet).")))
    _say(ansi.dim(f"  Launch it with:  cd {root} && entrabot"))
    return True
