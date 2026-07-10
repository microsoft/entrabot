#!/usr/bin/env python3
"""
demo_sandbox.py — Presentation demo for the EntraBot MXC sandbox.

Runs the REAL ``mxc-exec-mac`` (Seatbelt) binary through the exact same
``run_code`` enforcement chain the MCP server uses (operator ceiling →
clamp → canonicalize → MXC), and narrates each step so an audience can
see least-privilege containment enforced by the OS kernel — not by Python.

This is the "proof harness" you run alongside the live Teams chat: it
demonstrates that when the agent says "write to your Documents," the
kernel says no.

Usage:
    ./scripts/demo_sandbox.py            # interactive (pauses between beats)
    ./scripts/demo_sandbox.py --no-pause # run straight through (CI/recording)

Requires:
    - ENTRABOT_ENABLE_RUN_CODE=1 and the MXC sandbox env vars in .env
    - The real mxc-exec-mac binary resolvable via MXC_BIN_DIR
"""

from __future__ import annotations

# ruff: noqa: I001 — import order is deliberate (venv re-exec + sys.path insert +
# .env side-effect load must precede the entrabot.sandbox imports).

import os
import sys
from pathlib import Path

# Re-exec under the repo's venv interpreter if we're not already running it.
# The entrabot package needs Python 3.12+; running ``./scripts/demo_sandbox.py``
# directly would otherwise pick up the system python3 (often 3.9) and crash on
# modern type syntax. Uses only stdlib so it's safe on any Python 3.x.
_VENV_PY = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python3"
if _VENV_PY.exists() and os.path.realpath(sys.executable) != os.path.realpath(_VENV_PY):
    os.execv(str(_VENV_PY), [str(_VENV_PY), *sys.argv])

import contextlib  # noqa: E402

# Make the entrabot package importable and load .env (handles spaces in paths).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import entrabot.config  # noqa: E402, F401  (import side-effect: loads .env)

from entrabot.sandbox import get_sandbox_runner  # noqa: E402
from entrabot.sandbox.base import SandboxPolicy  # noqa: E402
from entrabot.sandbox.policy import (  # noqa: E402
    canonicalize_paths,
    clamp_to_ceiling,
)

# ── ANSI styling ────────────────────────────────────────────────────────────
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"
NC = "\033[0m"

PAUSE = "--no-pause" not in sys.argv
CONFIG_ONLY = "--config-only" in sys.argv
HOME = os.path.expanduser("~")


def banner(text: str) -> None:
    line = "═" * 62
    print(f"\n{BOLD}{CYAN}╔{line}╗{NC}")
    print(f"{BOLD}{CYAN}║{NC}  {BOLD}{text}{NC}")
    print(f"{BOLD}{CYAN}╚{line}╝{NC}")


def beat(text: str) -> None:
    if PAUSE:
        try:
            input(f"\n{DIM}  ↵ {text}{NC}")
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
    else:
        print(f"\n{DIM}  → {text}{NC}")


def load_ceiling() -> tuple[list[str], list[str]]:
    ro = [p for p in os.environ.get("ENTRABOT_SANDBOX_READONLY_PATHS", "").split(":") if p]
    rw = [p for p in os.environ.get("ENTRABOT_SANDBOX_READWRITE_PATHS", "").split(":") if p]
    return ro, rw


def run_scenario(
    runner,
    caps,
    ceiling_ro: list[str],
    ceiling_rw: list[str],
    *,
    title: str,
    cmd: str,
    req_ro: list[str],
    req_rw: list[str],
    expect_allow: bool,
) -> bool:
    """Run one scenario through the real run_code chain and narrate it."""
    print(f"\n{BOLD}{BLUE}▎{title}{NC}")
    print(f"  {DIM}agent runs:{NC} {cmd}")
    print(f"  {DIM}agent requests:{NC} readonly={req_ro or '[]'}  readwrite={req_rw or '[]'}")

    ceiling = SandboxPolicy(
        backend="process",
        command_line="",
        readonly_paths=ceiling_ro,
        readwrite_paths=ceiling_rw,
        timeout_ms=30000,
        network_default_policy="block",
        keychain_access=False,
    )
    requested = SandboxPolicy(
        backend="process",
        command_line=cmd,
        readonly_paths=req_ro,
        readwrite_paths=req_rw,
        timeout_ms=30000,
        network_default_policy="block",
        keychain_access=False,
    )

    clamped = clamp_to_ceiling(requested, ceiling, caps)
    if clamped.readonly_paths:
        clamped.readonly_paths = canonicalize_paths(clamped.readonly_paths)
    if clamped.readwrite_paths:
        clamped.readwrite_paths = canonicalize_paths(clamped.readwrite_paths)

    # Show the clamp decision — the security money-shot.
    dropped_rw = [p for p in req_rw if not _kept(p, clamped.readwrite_paths)]
    if dropped_rw:
        print(
            f"  {YELLOW}clamp:{NC} dropped write paths "
            f"{dropped_rw} {DIM}(not within operator ceiling){NC}"
        )
    print(
        f"  {DIM}policy sent to MXC:{NC} "
        f"readonlyPaths={clamped.readonly_paths or '[]'} "
        f"readwritePaths={clamped.readwrite_paths or '[]'}"
    )

    result = runner.run(clamped)
    allowed = result.exit_code == 0
    correct = allowed == expect_allow

    if allowed:
        verdict = f"{GREEN}✅ ALLOWED{NC}"
        detail = result.stdout.strip() or "(no output)"
        print(f"  {verdict}  exit={result.exit_code}  output: {detail[:80]!r}")
    else:
        verdict = f"{RED}⛔ BLOCKED by the OS kernel{NC}"
        detail = (result.stderr.strip() or result.stdout.strip() or "").splitlines()
        msg = detail[-1] if detail else "(no message)"
        print(f"  {verdict}  exit={result.exit_code}  reason: {msg[:80]!r}")

    expectation = "ALLOW" if expect_allow else "BLOCK"
    mark = f"{GREEN}as designed{NC}" if correct else f"{RED}UNEXPECTED{NC}"
    print(f"  {DIM}expected {expectation} →{NC} {mark}")
    return correct


def _kept(requested_path: str, kept_canonical: list[str]) -> bool:
    real = os.path.realpath(os.path.expanduser(requested_path))
    return real in kept_canonical


def main() -> int:
    banner("EntraBot × MXC — Least-Privilege Local Execution Demo")
    print(
        f"\n  An AI agent with its own Entra identity wants to run code on this Mac.\n"
        f"  {BOLD}The operator{NC} decides what it may touch. {BOLD}The agent can only\n"
        f"  narrow that — never widen it.{NC} Containment is enforced by Apple's\n"
        f"  Seatbelt kernel sandbox via Microsoft Execution Containers (MXC)."
    )

    # Preconditions
    if os.environ.get("ENTRABOT_ENABLE_RUN_CODE") != "1":
        print(f"\n{RED}run_code is disabled. Set ENTRABOT_ENABLE_RUN_CODE=1 in .env.{NC}")
        return 1

    ceiling_ro, ceiling_rw = load_ceiling()
    print(f"\n{BOLD}Operator ceiling (the human-set maximum):{NC}")
    print(f"  {GREEN}read-only :{NC} {ceiling_ro}")
    print(f"  {GREEN}read-write:{NC} {ceiling_rw}")
    print(f"  {DIM}keychain access: hard-disabled (not overridable by the agent){NC}")

    try:
        runner = get_sandbox_runner()
    except Exception as exc:  # noqa: BLE001
        print(f"\n{RED}MXC binary unavailable: {exc}{NC}")
        print(f"{DIM}Build it or set MXC_BIN_DIR. See scripts/setup_sandbox.sh.{NC}")
        return 1
    caps = runner.get_capabilities()
    print(f"\n{BOLD}Backend:{NC} {caps['backend']} {DIM}(real binary, SHA256-verified){NC}")

    # Agent identity (who is constrained, and on whose behalf).
    agent_upn = os.environ.get("ENTRABOT_AGENT_USER_UPN", "(unset)")
    run_code_on = os.environ.get("ENTRABOT_ENABLE_RUN_CODE") == "1"
    net = os.environ.get("ENTRABOT_SANDBOX_NETWORK", "block")
    print(f"\n{BOLD}Agent identity:{NC} {agent_upn} {DIM}(its own Entra Agent User){NC}")
    print(f"{BOLD}run_code tool:{NC} {'enabled' if run_code_on else 'DISABLED'}  "
          f"{DIM}· network: {net} · keychain: disabled{NC}")

    if CONFIG_ONLY:
        print(
            f"\n  {DIM}This is the operator-set configuration. The agent can only "
            f"narrow it.\n  Run without --config-only to see it enforced.{NC}\n"
        )
        return 0

    # Fixture: an informational file in Documents the agent may READ but not WRITE.
    info = Path(HOME) / "Documents" / "entrabot-info.txt"
    info.parent.mkdir(parents=True, exist_ok=True)
    if not info.exists():
        info.write_text("EntraBot demo file - figures the agent may read but must not alter\n")
    print(f"\n{DIM}Fixture ready: {info}{NC}")

    # ── Act 1: the threat ────────────────────────────────────────────────
    banner("Act 1 — Why containment matters")
    print(
        f"\n  EntraBot ships a deliberately-unsafe tool, {BOLD}write_local_file{NC},\n"
        f"  to show the baseline: an unsandboxed agent can write {BOLD}anywhere{NC}.\n"
        f"  That's the risk a compromised or over-eager agent poses to your machine."
    )
    print(f"  {DIM}(We don't run it here — the point of the rest of the demo is the cure.){NC}")
    beat("Press enter to see the sandbox in action…")

    # ── Act 2: the protection ────────────────────────────────────────────
    banner("Act 2 — run_code: the sandboxed path")
    results: list[bool] = []

    beat("Scenario 1 — the agent reads your Documents (legitimate analysis)")
    results.append(run_scenario(
        runner, caps, ceiling_ro, ceiling_rw,
        title="“Read my file in Documents.”",
        cmd=f"cat {HOME}/Documents/entrabot-info.txt",
        req_ro=[f"{HOME}/Documents"], req_rw=[],
        expect_allow=True,
    ))

    beat("Scenario 2 — the agent tries to WRITE to your Documents (tampering)")
    results.append(run_scenario(
        runner, caps, ceiling_ro, ceiling_rw,
        title="“Overwrite that file in Documents.”",
        cmd=f"echo TAMPERED > {HOME}/Documents/entrabot-hack.txt",
        req_ro=[], req_rw=[f"{HOME}/Documents"],
        expect_allow=False,
    ))
    print(
        f"  {DIM}Documents is in the read-only ceiling, not read-write. The agent's\n"
        f"  attempt to widen is clamped to nothing, and the kernel blocks the write.{NC}"
    )

    beat("Scenario 3 — the agent writes a report to /tmp (allowed output)")
    results.append(run_scenario(
        runner, caps, ceiling_ro, ceiling_rw,
        title="“Save a scratch report to /tmp.”",
        cmd="echo 'report' > /tmp/entrabot-report.txt && cat /tmp/entrabot-report.txt",
        req_ro=[], req_rw=["/tmp"],
        expect_allow=True,
    ))

    beat("Scenario 4 — the agent writes to ~/Downloads (allowed output)")
    results.append(run_scenario(
        runner, caps, ceiling_ro, ceiling_rw,
        title="“Drop the export in my Downloads folder.”",
        cmd=(
            f"echo 'export' > {HOME}/Downloads/entrabot-export.txt "
            f"&& cat {HOME}/Downloads/entrabot-export.txt"
        ),
        req_ro=[], req_rw=[f"{HOME}/Downloads"],
        expect_allow=True,
    ))

    # ── Act 3: the hardening ─────────────────────────────────────────────
    banner("Act 3 — The agent can't cheat the boundary")
    beat("Scenario 5 — a symlink inside an allowed dir pointing OUT is rejected")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        granted = os.path.join(d, "granted")
        secret_dir = os.path.join(d, "secret")
        os.mkdir(granted)
        os.mkdir(secret_dir)
        Path(secret_dir, "creds.txt").write_text("top secret\n")
        evil = os.path.join(granted, "escape")
        os.symlink(secret_dir, evil)  # granted/escape -> ../secret
        results.append(run_scenario(
            runner, caps, [granted], [granted],
            title="agent grants itself the 'granted' dir, then follows a symlink out",
            cmd=f"cat {evil}/creds.txt",
            req_ro=[evil], req_rw=[],
            expect_allow=False,
        ))
    print(
        f"  {DIM}Paths are canonicalized (realpath) BEFORE the containment check, so a\n"
        f"  symlink can't smuggle access to a target outside the ceiling.{NC}"
    )

    # Cleanup
    for p in (
        Path(HOME, "Documents", "entrabot-hack.txt"),
        Path("/tmp/entrabot-report.txt"),
        Path(HOME, "Downloads", "entrabot-export.txt"),
    ):
        with contextlib.suppress(FileNotFoundError):
            p.unlink()

    # ── Curtain ──────────────────────────────────────────────────────────
    banner("Recap")
    passed = sum(results)
    total = len(results)
    print(
        f"\n  {GREEN}READ Documents{NC} allowed · {RED}WRITE Documents{NC} blocked · "
        f"{GREEN}WRITE /tmp + Downloads{NC} allowed · {RED}symlink escape{NC} blocked"
    )
    print(
        f"\n  Every action is {BOLD}audit-first{NC} (logged before it runs; if audit\n"
        f"  fails, the action doesn't), {BOLD}fail-closed{NC}, and attributed to the\n"
        f"  agent's own Entra identity — not yours."
    )
    color = GREEN if passed == total else RED
    print(f"\n  {BOLD}{color}{passed}/{total} scenarios behaved exactly as designed.{NC}")

    print_teams_talktrack()
    return 0 if passed == total else 1


def print_teams_talktrack() -> None:
    banner("Now do it live — Teams talk-track")
    print(
        f"""
  Chat with the agent ({BOLD}entrabot-mxc-test@werner.ac{NC}) in Teams and ask,
  in plain language. The agent will call run_code under the hood.

  {GREEN}1){NC} "Read my file at ~/Documents/entrabot-info.txt and tell me what it says."
       {DIM}→ Agent reads it. Point out: Documents is read-only in the ceiling.{NC}

  {RED}2){NC} "Now save the text 'hello' to ~/Documents/note.txt."
       {DIM}→ Blocked. The agent reports it can't write there. Show the audit log.{NC}

  {GREEN}3){NC} "Write a short summary to ~/Downloads/summary.txt instead."
       {DIM}→ Works. Downloads is in the read-write ceiling.{NC}

  {DIM}The agent never sees the ceiling as something it can change — it's set by
  you, the operator, in .env, and enforced by the OS. The model can only narrow.{NC}
"""
    )


if __name__ == "__main__":
    raise SystemExit(main())
