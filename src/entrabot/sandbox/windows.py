"""
Windows process-container runner for MXC sandbox.

Uses MXC's ``processcontainer`` backend (Windows AppContainer / BaseContainer)
via the ``wxc-exec.exe`` binary shipped in ``@microsoft/mxc-sdk``. Unlike the
macOS Seatbelt path, ``processcontainer`` is a **default, non-experimental**
backend on Windows 11 24H2+ (build 26100+), so no ``--experimental`` flag is
required.

Config delivery differs from macOS too: ``wxc-exec.exe`` does not read config
from stdin. It accepts a positional config-file path, ``--config <path>``, or
``--config-base64 <b64>``. We use ``--config-base64`` so there is no temp file
to create, secure, or clean up — the policy JSON is passed inline.

Containment notes (see docs/platform-learnings/mxc-windows-sandbox-preview.md):
- ``network.allowedHosts`` / ``blockedHosts`` are NOT enforced on Windows. Only
  ``network.defaultPolicy`` (allow/block) is honoured, so ``get_capabilities``
  reports ``network_host_filtering=False`` and policy building must fail closed
  if an allow-list is requested (handled in ``clamp_to_ceiling``).
- ``wxc-exec.exe`` invokes ``process.commandLine`` with ``CreateProcessW``
  directly — there is no implicit shell. Callers that need shell builtins,
  redirection, or PATH resolution must invoke ``cmd /c ...`` explicitly.
"""

import base64
import contextlib
import subprocess
import time

from entrabot.sandbox.base import (
    SandboxPolicy,
    SandboxResult,
    SandboxTimeoutError,
)
from entrabot.sandbox.policy import build_policy

# Bound on the post-kill pipe drain. After a tree-kill every pipe writer is
# dead, so the drain normally returns in milliseconds; the bound only matters
# if taskkill could not reach a descendant.
_POST_KILL_DRAIN_TIMEOUT_S = 5.0


def _kill_process_tree(pid: int) -> None:
    """Force-kill ``pid`` and every descendant via ``taskkill /T /F``.

    ``Popen.kill()`` (TerminateProcess) reaches only the direct child.
    ``wxc-exec.exe`` spawns the container host as a grandchild that inherits
    the stdout/stderr pipe handles; if only the direct child dies, those
    handles stay open and any pipe drain blocks until the orphan exits —
    observed live 2026-07-02 as a 26-minute ``write_local_file`` hang on a
    30-second timeout. ``taskkill /T`` walks the tree so every handle-holder
    dies. Failures are ignored: the tree may already be gone, and the caller's
    bounded drain is the backstop either way.
    """
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True,
            timeout=15,
            check=False,
        )


class ProcessContainerRunner:
    """Windows MXC process-container sandbox runner.

    Implements the SandboxRunner protocol for Windows.
    Uses the ``wxc-exec.exe`` binary with the ``processcontainer`` backend.
    """

    def __init__(self, binary_path: str):
        """Initialize with path to the verified ``wxc-exec.exe`` binary.

        Args:
            binary_path: Absolute path to verified wxc-exec.exe binary
        """
        self.binary_path = binary_path

    def run(self, policy: SandboxPolicy) -> SandboxResult:
        """Execute command in the Windows process-container sandbox.

        Args:
            policy: Sandbox policy configuration

        Returns:
            SandboxResult with stdout, stderr, exit code, duration

        Raises:
            SandboxTimeoutError: Execution exceeded timeout
        """
        # Build MXC JSON config and pass it inline as base64 (no stdin, no
        # temp file). The binary resolves the abstract "process" intent to the
        # concrete "processcontainer" backend for us.
        mxc_config = build_policy(policy)
        config_b64 = base64.b64encode(mxc_config.encode("utf-8")).decode("ascii")

        # processcontainer is a default (non-experimental) backend on Windows,
        # so --experimental is intentionally NOT passed.
        cmd = [
            self.binary_path,
            "--config-base64",
            config_b64,
        ]

        start_time = time.time()
        timeout_seconds = policy.timeout_ms / 1000.0

        # NOT subprocess.run(timeout=...): on Windows its timeout path kills
        # only the direct child and then drains the pipes with NO timeout —
        # the container grandchild inherits the pipe handles, so that drain
        # blocks for as long as the orphan lives (26 minutes observed on a
        # 30s timeout). Popen + explicit tree-kill + bounded drain instead.
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as e:
            _kill_process_tree(process.pid)
            # Abandon the pipes rather than block the tool call if the drain
            # still cannot complete after the tree-kill.
            with contextlib.suppress(subprocess.TimeoutExpired, OSError, ValueError):
                process.communicate(timeout=_POST_KILL_DRAIN_TIMEOUT_S)
            raise SandboxTimeoutError(f"Execution exceeded {policy.timeout_ms}ms timeout") from e

        end_time = time.time()
        duration_ms = int((end_time - start_time) * 1000)

        return SandboxResult(
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=False,
        )

    def get_capabilities(self) -> dict:
        """Return processcontainer backend capabilities.

        Returns:
            Dict with backend capabilities:
            - backend: 'processcontainer'
            - network_host_filtering: False (allowedHosts not enforced on Windows)
            - deny_paths_supported: False (positive-allowlist only)
        """
        return {
            "backend": "processcontainer",
            # allowedHosts/blockedHosts have no enforcement on Windows — only
            # network.defaultPolicy is honoured. Report False so fail-closed
            # logic refuses any policy that depends on host filtering.
            "network_host_filtering": False,
            "deny_paths_supported": False,  # Using positive-allowlist only
        }

    def identity_binding(self, agent_identity: str) -> None:
        """No-op in Phase 1 (process isolation).

        Phase 2: the ``isolation_session`` backend is the only MXC backend with
        a state-aware lifecycle and the announced Entra-identity binding. It is
        experimental and not wired here yet (see session.py).

        Args:
            agent_identity: Entra Agent ID (unused in Phase 1)
        """
        pass  # No-op in Phase 1
