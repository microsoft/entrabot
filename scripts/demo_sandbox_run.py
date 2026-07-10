#!/usr/bin/env python3
"""demo_sandbox_run.py — run ONE sandbox scenario through the real run_code chain.

This is the enforcement engine behind ``scripts/demo_sandbox.ps1`` (Windows) and
is intentionally narration-free: it takes a single command + the paths the agent
*requests*, pushes them through the exact operator-ceiling -> clamp ->
canonicalize -> MXC chain the MCP server's ``run_code`` tool uses, runs the real
SHA256-pinned MXC binary, and prints a JSON result on stdout.

The operator ceiling is read from the environment (``ENTRABOT_SANDBOX_*`` vars)
via :func:`entrabot.sandbox.local_files.ceiling_from_env`, so the demo proves the
*real* configured ceiling — including the ``os.pathsep`` parsing that lets
Windows drive-letter paths (``C:\\Users\\...``) survive.

Usage:
    python demo_sandbox_run.py --cmd "<commandLine>" \
        [--ro <path> ...] [--rw <path> ...]

Output (stdout): a single JSON object. Exit code 0 if the JSON was produced
(regardless of whether the sandboxed command was allowed or blocked); non-zero
only on harness/setup errors (e.g. binary unavailable).
"""

from __future__ import annotations

# ruff: noqa: I001 — import order is deliberate (sys.path insert + .env
# side-effect load must precede the entrabot.sandbox imports).

import argparse
import json
import os
import sys
from pathlib import Path

# Make the entrabot package importable and load .env (ceiling + MXC_BIN_DIR).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import entrabot.config  # noqa: E402, F401  (import side-effect: loads .env)

from entrabot.sandbox import get_sandbox_runner  # noqa: E402
from entrabot.sandbox.base import (  # noqa: E402
    SandboxBackendUnsupportedError,
    SandboxPolicy,
    SandboxPolicyError,
    SandboxTimeoutError,
    SandboxUnavailableError,
    SandboxUntrustedBinaryError,
)
from entrabot.sandbox.local_files import ceiling_from_env  # noqa: E402
from entrabot.sandbox.policy import canonicalize_paths, clamp_to_ceiling  # noqa: E402


def _real(p: str) -> str:
    return os.path.realpath(os.path.expanduser(p))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cmd", required=True, help="commandLine to run in the sandbox")
    parser.add_argument("--ro", action="append", default=[], help="path requested READ access")
    parser.add_argument("--rw", action="append", default=[], help="path requested WRITE access")
    args = parser.parse_args(argv)

    result: dict = {
        "requested_ro": args.ro,
        "requested_rw": args.rw,
    }

    try:
        ceiling = ceiling_from_env()
        result["ceiling_ro"] = ceiling.readonly_paths
        result["ceiling_rw"] = ceiling.readwrite_paths

        runner = get_sandbox_runner()
        caps = runner.get_capabilities()
        result["backend"] = caps["backend"]

        requested = SandboxPolicy(
            backend="process",
            command_line=args.cmd,
            readonly_paths=args.ro,
            readwrite_paths=args.rw,
            timeout_ms=ceiling.timeout_ms,
            network_default_policy="block",
            keychain_access=False,
        )

        clamped = clamp_to_ceiling(requested, ceiling, caps)
        # The clamp money-shot: which requested paths were dropped because they
        # were NOT within the operator ceiling (the agent tried to widen).
        kept_rw = {_real(p) for p in clamped.readwrite_paths}
        kept_ro = {_real(p) for p in clamped.readonly_paths}
        result["dropped_rw"] = [p for p in args.rw if _real(p) not in kept_rw]
        result["dropped_ro"] = [p for p in args.ro if _real(p) not in kept_ro]

        if clamped.readonly_paths:
            clamped.readonly_paths = canonicalize_paths(clamped.readonly_paths)
        if clamped.readwrite_paths:
            clamped.readwrite_paths = canonicalize_paths(clamped.readwrite_paths)
        result["clamped_ro"] = clamped.readonly_paths
        result["clamped_rw"] = clamped.readwrite_paths

        run = runner.run(clamped)
        result["exit_code"] = run.exit_code
        result["allowed"] = run.exit_code == 0
        result["stdout"] = run.stdout.strip()
        result["stderr"] = run.stderr.strip()
        result["timed_out"] = run.timed_out

    except (
        SandboxUnavailableError,
        SandboxUntrustedBinaryError,
        SandboxBackendUnsupportedError,
        SandboxPolicyError,
        SandboxTimeoutError,
    ) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["allowed"] = False
    except Exception as exc:  # noqa: BLE001 — surface anything else as a harness error
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["allowed"] = False

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
