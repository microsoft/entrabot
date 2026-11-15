"""`entrabot init` setup wizard, split by concern (platform leaf · re-runnable steps ·
provisioning · top-level flow). Public surface re-exported so callers/tests import from
``entrabot.harness.setup``."""

from .platform import _clone_root
from .steps import _apply_existing_env
from .wizard import run_init

__all__ = ["run_init", "_apply_existing_env", "_clone_root"]
