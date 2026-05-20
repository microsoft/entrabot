#!/usr/bin/env python3
"""End-to-end destructive smoke harness for EntraClaw scripts.

This is intentionally not a pytest test. It provisions real Entra/Azure/M365
resources, sends a real Teams message, tears the test resources down, and writes
a self-contained log bundle for humans or LLM agents to inspect.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESOURCE_GROUP = "entraclaw-rg"
TAIL_LINES = 80


class SmokeFailure(RuntimeError):
    """Raised when a smoke step fails."""


@dataclass
class CommandRecord:
    name: str
    argv: list[str]
    log_file: str
    returncode: int
    duration_seconds: float


@dataclass
class SmokeContext:
    run_id: str
    log_dir: Path
    agent_user_upn: str
    storage_account: str
    container: str
    resource_group: str
    sponsor_upn: str | None
    chat_id: str | None
    test_teams: bool
    test_a365: bool
    wait_for_teams_reply: bool
    teams_reply_timeout: int
    teams_reply_interval: int
    keep_resources_on_failure: bool
    records: list[CommandRecord] = field(default_factory=list)
    setup_started: bool = False
    teardown_attempted: bool = False
    storage_delete_attempted: bool = False
    local_state_restored: bool = False
    blueprint_key_was_present: bool = False
    blueprint_key_backup: str | None = None
    failure: str | None = None


def utc_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d%H%M%S")


def default_storage_account(run_id: str) -> str:
    # Storage account names: 3-24 chars, lowercase letters/numbers only.
    return f"entsmoke{run_id[-14:]}"[:24].lower()


def default_container(run_id: str) -> str:
    return f"smoke-{run_id.lower()}"


def mask_command(argv: list[str]) -> str:
    masked: list[str] = []
    for item in argv:
        if re.search(r"token|secret|password|assertion", item, re.IGNORECASE):
            masked.append("<redacted>")
        else:
            masked.append(item)
    return " ".join(shlex_quote(part) for part in masked)


def shlex_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def tail(path: Path, lines: int = TAIL_LINES) -> str:
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_command(
    ctx: SmokeContext,
    name: str,
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    check: bool = True,
) -> CommandRecord:
    log_file = ctx.log_dir / f"{len(ctx.records) + 1:02d}-{name}.log"
    started = time.monotonic()
    command_line = mask_command(argv)
    print(f"\n==> {name}: {command_line}")
    with log_file.open("w", encoding="utf-8") as log:
        log.write(f"# step: {name}\n")
        log.write(f"# cwd: {REPO_ROOT}\n")
        log.write(f"# command: {command_line}\n\n")
        log.flush()
        proc = subprocess.Popen(
            argv,
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if input_text is not None and proc.stdin:
            proc.stdin.write(input_text)
            proc.stdin.close()
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="")
            log.write(line)
        returncode = proc.wait()

    duration = time.monotonic() - started
    record = CommandRecord(
        name=name,
        argv=argv,
        log_file=str(log_file),
        returncode=returncode,
        duration_seconds=round(duration, 3),
    )
    ctx.records.append(record)
    if check and returncode != 0:
        raise SmokeFailure(
            f"Step '{name}' failed with exit {returncode}. Log: {log_file}\n"
            f"Last {TAIL_LINES} log lines:\n{tail(log_file)}"
        )
    return record


def smoke_env(ctx: SmokeContext) -> dict[str, str]:
    env = os.environ.copy()
    # Avoid setup.sh prompting to migrate unrelated local operational/persona data.
    env["ENTRACLAW_DATA_DIR"] = str(ctx.log_dir / "isolated-data")
    env["ENTRACLAW_LOG_DIR"] = str(ctx.log_dir / "entraclaw-logs")
    env["ENTRACLAW_AUDIT_DIR"] = str(ctx.log_dir / "entraclaw-audit")
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def is_windows() -> bool:
    return os.name == "nt"


def setup_command(ctx: SmokeContext) -> list[str]:
    if is_windows():
        cmd = [
            "pwsh",
            "-ExecutionPolicy",
            "Bypass",
            "-NoProfile",
            "-File",
            "scripts/setup-windows.ps1",
            "-NewChain",
            "-AgentUserUpn",
            ctx.agent_user_upn,
            "-UseCloudMemory",
            "-WithStorageAccount",
            ctx.storage_account,
            "-WithContainer",
            ctx.container,
        ]
    else:
        cmd = [
            "./scripts/setup.sh",
            "--new",
            f"--agent-user-upn={ctx.agent_user_upn}",
            "--use-cloud-memory",
            f"--with-storage-account={ctx.storage_account}",
            f"--with-container={ctx.container}",
        ]
        if ctx.sponsor_upn:
            cmd.append(f"--teams-user={ctx.sponsor_upn}")
        if ctx.test_a365:
            cmd.append("--with-a365-work-iq")
    return cmd


def status_health_command() -> list[str]:
    if is_windows():
        return [
            "pwsh",
            "-NoProfile",
            "-File",
            "scripts/setup-windows.ps1",
            "-Status",
            "-HealthOnly",
        ]
    return ["./scripts/setup.sh", "--status", "--health-only"]


def status_json_command() -> list[str]:
    if is_windows():
        return ["pwsh", "-NoProfile", "-File", "scripts/setup-windows.ps1", "-Status", "-Json"]
    return ["./scripts/setup.sh", "--status", "--json"]


def teardown_command(ctx: SmokeContext) -> list[str]:
    if is_windows():
        return [
            sys.executable,
            "scripts/deprovision_entra_agent_identity.py",
            "--agent-user-upn",
            ctx.agent_user_upn,
        ]
    return [
        "./scripts/teardown.sh",
        f"--agent-user-upn={ctx.agent_user_upn}",
        "--yes",
        "--preserve-provisioner",
        "--preserve-local-state",
    ]


def snapshot_local_state(ctx: SmokeContext) -> None:
    backup_dir = ctx.log_dir / "local-state-backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for name in (".env", ".entraclaw-state.json"):
        source = REPO_ROOT / name
        if source.exists():
            shutil.copy2(source, backup_dir / name)
    try:
        import keyring

        value = keyring.get_password("entraclaw", "blueprint-private-key")
        ctx.blueprint_key_was_present = value is not None
        ctx.blueprint_key_backup = value
        (backup_dir / "keyring-blueprint-private-key.present").write_text(
            "yes\n" if value is not None else "no\n", encoding="utf-8"
        )
    except Exception as exc:  # pragma: no cover - depends on host keyring
        (backup_dir / "keyring-backup-error.txt").write_text(str(exc) + "\n", encoding="utf-8")


def restore_local_state(ctx: SmokeContext) -> None:
    backup_dir = ctx.log_dir / "local-state-backup"
    for name in (".env", ".entraclaw-state.json"):
        target = REPO_ROOT / name
        backup = backup_dir / name
        if backup.exists():
            shutil.copy2(backup, target)
        elif target.exists():
            target.unlink()
    try:
        import keyring

        if ctx.blueprint_key_was_present and ctx.blueprint_key_backup is not None:
            keyring.set_password("entraclaw", "blueprint-private-key", ctx.blueprint_key_backup)
        else:
            with contextlib_suppress_keyring_delete(keyring):
                keyring.delete_password("entraclaw", "blueprint-private-key")
    except Exception as exc:  # pragma: no cover - depends on host keyring
        (ctx.log_dir / "restore-keyring-error.txt").write_text(str(exc) + "\n", encoding="utf-8")
    ctx.local_state_restored = True


class contextlib_suppress_keyring_delete:
    def __init__(self, keyring_module: object) -> None:
        self.keyring_module = keyring_module

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        errors = getattr(self.keyring_module, "errors", None)
        delete_error = getattr(errors, "PasswordDeleteError", None)
        return delete_error is not None and isinstance(exc, delete_error)


def load_state() -> dict[str, str]:
    state_path = REPO_ROOT / ".entraclaw-state.json"
    if not state_path.exists():
        return {}
    return json.loads(state_path.read_text(encoding="utf-8"))


def write_send_teams_driver(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            import argparse
            import asyncio
            import json
            import time

            from entraclaw.config import get_config
            from entraclaw.tools.teams import (
                acquire_agent_user_token,
                create_one_on_one_chat,
                filter_human_messages,
                read,
                send,
            )

            def _is_after(candidate: str | None, floor: str | None) -> bool:
                if not candidate:
                    return False
                if not floor:
                    return True
                return candidate > floor

            async def wait_for_reply(
                *,
                chat_id: str,
                token: str,
                after_iso: str | None,
                sent_message_id: str,
                timeout: int,
                interval: int,
            ) -> dict:
                started = time.monotonic()
                poll_count = 0
                while True:
                    poll_count += 1
                    raw_messages = await read(chat_id=chat_id, token=token, count=20)
                    human_messages = filter_human_messages(
                        raw_messages,
                        "EntraClaw Agent",
                        sent_message_ids={sent_message_id},
                    )
                    replies = [
                        message
                        for message in human_messages
                        if _is_after(message.get("sent_at"), after_iso)
                    ]
                    if replies:
                        replies.sort(key=lambda message: message.get("sent_at", ""))
                        return {
                            "messages": replies,
                            "timed_out": False,
                            "poll_count": poll_count,
                            "sent_message_id": sent_message_id,
                        }
                    elapsed = time.monotonic() - started
                    if elapsed >= timeout:
                        return {
                            "messages": [],
                            "timed_out": True,
                            "poll_count": poll_count,
                            "sent_message_id": sent_message_id,
                        }
                    await asyncio.sleep(interval)

            async def main() -> int:
                parser = argparse.ArgumentParser()
                parser.add_argument("--chat-id", default="")
                parser.add_argument("--sponsor-upn", default="")
                parser.add_argument("--message", required=True)
                parser.add_argument("--out", required=True)
                parser.add_argument("--wait-for-reply", action="store_true")
                parser.add_argument("--reply-timeout", type=int, default=300)
                parser.add_argument("--reply-interval", type=int, default=5)
                parser.add_argument("--reply-out", default="")
                args = parser.parse_args()

                cfg = get_config()
                token = acquire_agent_user_token(cfg)
                chat_id = args.chat_id
                created_chat = None
                if not chat_id:
                    if not args.sponsor_upn:
                        raise SystemExit("--chat-id or --sponsor-upn is required")
                    created_chat = await create_one_on_one_chat(
                        token=token,
                        target_email=args.sponsor_upn,
                        agent_user_id=cfg.agent_user_id,
                    )
                    chat_id = created_chat["chat_id"]

                sent = await send(
                    chat_id=chat_id,
                    message=args.message,
                    token=token,
                    content_type="html",
                )
                with open(args.out, "w", encoding="utf-8") as handle:
                    json.dump(
                        {"chat_id": chat_id, "created_chat": created_chat, "message": sent},
                        handle,
                        indent=2,
                    )
                    handle.write("\\n")
                print(f"sent Teams smoke message {sent['message_id']} to chat {chat_id}")
                if args.wait_for_reply:
                    if not args.reply_out:
                        raise SystemExit("--reply-out is required with --wait-for-reply")
                    print(
                        "waiting for Teams reply "
                        f"for up to {args.reply_timeout}s "
                        f"(interval={args.reply_interval}s)"
                    )
                    reply = await wait_for_reply(
                        chat_id=chat_id,
                        token=token,
                        after_iso=sent.get("sent_at"),
                        sent_message_id=sent["message_id"],
                        timeout=args.reply_timeout,
                        interval=args.reply_interval,
                    )
                    with open(args.reply_out, "w", encoding="utf-8") as handle:
                        json.dump(reply, handle, indent=2)
                        handle.write("\\n")
                    if reply["timed_out"]:
                        print("timed out waiting for Teams reply")
                        return 2
                    print(f"received {len(reply['messages'])} Teams reply message(s)")
                return 0

            raise SystemExit(asyncio.run(main()))
            """
        ).lstrip(),
        encoding="utf-8",
    )


def write_verify_deleted_driver(path: Path, state: dict[str, str], out_path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            f"""
            from __future__ import annotations

            import json
            import sys
            import time
            import requests

            sys.path.insert(0, {str(REPO_ROOT / 'scripts')!r})
            from entra_provisioning import get_existing_graph_token

            token = get_existing_graph_token()
            targets = [
                ("agent_user", "/users/{state.get('AGENT_USER_ID', '')}"),
                ("agent_identity", "/servicePrincipals/{state.get('AGENT_OBJECT_ID', '')}"),
                ("blueprint", "/applications/{state.get('BLUEPRINT_OBJECT_ID', '')}"),
            ]
            max_attempts = 12
            delay_seconds = 10
            result = {{}}
            ok = False
            for attempt in range(1, max_attempts + 1):
                result = {{"attempt": attempt, "targets": {{}}}}
                ok = True
                for name, path in targets:
                    if path.endswith("/"):
                        result["targets"][name] = {{"status": "missing-id"}}
                        ok = False
                        continue
                    resp = requests.get(
                        "https://graph.microsoft.com/beta" + path,
                        headers={{"Authorization": "Bearer " + token}},
                        timeout=30,
                    )
                    result["targets"][name] = {{"status_code": resp.status_code}}
                    if resp.status_code != 404:
                        ok = False
                        result["targets"][name]["body"] = resp.text[:1000]
                if ok:
                    break
                if attempt < max_attempts:
                    print(
                        "Graph resources still visible after "
                        f"attempt {{attempt}}/{{max_attempts}}; "
                        f"retrying in {{delay_seconds}}s"
                    )
                    time.sleep(delay_seconds)
            with open({str(out_path)!r}, "w", encoding="utf-8") as handle:
                json.dump(result, handle, indent=2)
                handle.write("\\n")
            if not ok:
                print(json.dumps(result, indent=2))
                raise SystemExit(1)
            print("verified Graph resources are deleted")
            """
        ).lstrip(),
        encoding="utf-8",
    )


def validate_args(args: argparse.Namespace) -> None:
    if not args.confirm_destroy_test_resources:
        raise SystemExit("Refusing to run without --confirm-destroy-test-resources")
    if not args.agent_user_upn:
        raise SystemExit(
            "--agent-user-upn is required, e.g. smoketest-agent@tenant.onmicrosoft.com"
        )
    if args.skip_teams and args.test_teams:
        raise SystemExit("Use either --test-teams or --skip-teams, not both")
    if args.skip_teams:
        args.test_teams = False
    if args.test_teams and not (args.chat_id or args.sponsor_upn):
        raise SystemExit(
            "Provide --chat-id or --sponsor-upn when --test-teams is enabled"
        )
    if not args.test_teams and (args.chat_id or args.sponsor_upn):
        raise SystemExit("Pass --test-teams when providing --chat-id or --sponsor-upn")
    if args.wait_for_teams_reply and not args.test_teams:
        raise SystemExit("Pass --test-teams when using --wait-for-teams-reply")
    if args.wait_for_teams_reply and args.teams_reply_timeout <= 0:
        raise SystemExit("--teams-reply-timeout must be greater than zero")
    if args.wait_for_teams_reply and args.teams_reply_interval <= 0:
        raise SystemExit("--teams-reply-interval must be greater than zero")
    if args.storage_account and not re.fullmatch(r"[a-z0-9]{3,24}", args.storage_account):
        raise SystemExit("--storage-account must be 3-24 lowercase letters/numbers")


def parse_args() -> argparse.Namespace:
    run_id = utc_run_id()
    parser = argparse.ArgumentParser(
        description="Provision, exercise, tear down, and verify an isolated EntraClaw smoke chain.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            Examples:
              tests/smoke/smokeit.sh --confirm-destroy-test-resources \\
                --agent-user-upn smoketest-agent@contoso.onmicrosoft.com \\
                                --test-teams \
                --sponsor-upn human@contoso.com

              tests/smoke/smokeit.sh --confirm-destroy-test-resources \\
                --agent-user-upn smoketest-agent@contoso.onmicrosoft.com \\
                                --test-teams \
                --chat-id 19:...
            """
        ),
    )
    parser.add_argument("--confirm-destroy-test-resources", action="store_true")
    parser.add_argument("--agent-user-upn", required=True)
    parser.add_argument("--sponsor-upn", default="")
    parser.add_argument("--chat-id", default="")
    parser.add_argument("--test-teams", action="store_true")
    parser.add_argument("--test-a365", action="store_true")
    parser.add_argument(
        "--wait-for-teams-reply",
        action="store_true",
        help="After sending the smoke Teams message, poll until a human reply arrives.",
    )
    parser.add_argument("--teams-reply-timeout", type=int, default=300)
    parser.add_argument("--teams-reply-interval", type=int, default=5)
    parser.add_argument("--skip-teams", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--storage-account", default=default_storage_account(run_id))
    parser.add_argument("--container", default=default_container(run_id))
    parser.add_argument("--resource-group", default=DEFAULT_RESOURCE_GROUP)
    parser.add_argument("--message", default=f"<b>EntraClaw smoke test</b> {run_id}")
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=REPO_ROOT / "tests" / "smoke" / "logs" / run_id,
    )
    parser.add_argument("--keep-resources-on-failure", action="store_true")
    parser.set_defaults(run_id=run_id)
    return parser.parse_args()


def delete_storage_account(ctx: SmokeContext, env: dict[str, str], *, check: bool) -> None:
    ctx.storage_delete_attempted = True
    run_command(
        ctx,
        "delete-storage-account",
        [
            "az",
            "storage",
            "account",
            "delete",
            "--name",
            ctx.storage_account,
            "--resource-group",
            ctx.resource_group,
            "--yes",
        ],
        env=env,
        check=check,
    )


def verify_storage_deleted(ctx: SmokeContext, env: dict[str, str]) -> None:
    record = run_command(
        ctx,
        "verify-storage-deleted",
        [
            "az",
            "storage",
            "account",
            "show",
            "--name",
            ctx.storage_account,
            "--resource-group",
            ctx.resource_group,
            "-o",
            "json",
        ],
        env=env,
        check=False,
    )
    if record.returncode == 0:
        raise SmokeFailure(
            f"Storage account still exists: {ctx.storage_account}. Log: {record.log_file}"
        )


def write_summary(ctx: SmokeContext) -> None:
    data = asdict(ctx)
    data.pop("blueprint_key_backup", None)
    data["log_dir"] = str(ctx.log_dir)
    data["records"] = [asdict(record) for record in ctx.records]
    write_json(ctx.log_dir / "smoke-summary.json", data)


def main() -> int:
    args = parse_args()
    validate_args(args)
    args.log_dir.mkdir(parents=True, exist_ok=True)

    ctx = SmokeContext(
        run_id=args.run_id,
        log_dir=args.log_dir,
        agent_user_upn=args.agent_user_upn,
        storage_account=args.storage_account,
        container=args.container,
        resource_group=args.resource_group,
        sponsor_upn=args.sponsor_upn or None,
        chat_id=args.chat_id or None,
        test_teams=args.test_teams,
        test_a365=args.test_a365,
        wait_for_teams_reply=args.wait_for_teams_reply,
        teams_reply_timeout=args.teams_reply_timeout,
        teams_reply_interval=args.teams_reply_interval,
        keep_resources_on_failure=args.keep_resources_on_failure,
    )
    env = smoke_env(ctx)
    env["ENTRACLAW_ASSIGN_TEAMS_LICENSE"] = "1" if args.test_teams else "0"
    env["ENTRACLAW_ASSIGN_WORK_IQ_LICENSE"] = "1" if args.test_a365 else "0"
    inputs = dict(vars(args))
    inputs["log_dir"] = str(args.log_dir)
    write_json(ctx.log_dir / "smoke-inputs.json", inputs)
    print(f"Smoke log bundle: {ctx.log_dir}")

    state_after_setup: dict[str, str] = {}
    try:
        snapshot_local_state(ctx)
        run_command(ctx, "git-status", ["git", "status", "--short", "--branch"], env=env)
        run_command(ctx, "az-account", ["az", "account", "show", "-o", "json"], env=env)
        ctx.setup_started = True
        run_command(ctx, "setup-new-chain", setup_command(ctx), env=env, input_text="n\n")
        state_after_setup = load_state()
        write_json(ctx.log_dir / "state-after-setup.json", state_after_setup)
        run_command(ctx, "status-health", status_health_command(), env=env)
        run_command(ctx, "status-json", status_json_command(), env=env)
        if args.test_teams:
            driver = ctx.log_dir / "send_teams_smoke.py"
            out = ctx.log_dir / "teams-message.json"
            reply_out = ctx.log_dir / "teams-reply.json"
            write_send_teams_driver(driver)
            cmd = [
                sys.executable,
                str(driver),
                "--message",
                args.message,
                "--out",
                str(out),
            ]
            if ctx.chat_id:
                cmd.extend(["--chat-id", ctx.chat_id])
            if ctx.sponsor_upn:
                cmd.extend(["--sponsor-upn", ctx.sponsor_upn])
            if ctx.wait_for_teams_reply:
                cmd.extend(
                    [
                        "--wait-for-reply",
                        "--reply-timeout",
                        str(ctx.teams_reply_timeout),
                        "--reply-interval",
                        str(ctx.teams_reply_interval),
                        "--reply-out",
                        str(reply_out),
                    ]
                )
            run_command(ctx, "teams-message", cmd, env=env)

        ctx.teardown_attempted = True
        run_command(
            ctx,
            "teardown-target-chain",
            teardown_command(ctx),
            env=env,
        )
        verify_driver = ctx.log_dir / "verify_graph_deleted.py"
        verify_out = ctx.log_dir / "graph-deletion-verification.json"
        write_verify_deleted_driver(verify_driver, state_after_setup, verify_out)
        run_command(
            ctx,
            "verify-graph-deleted",
            [sys.executable, str(verify_driver)],
            env=env,
        )
        delete_storage_account(ctx, env, check=True)
        verify_storage_deleted(ctx, env)
        print("\nSMOKE PASSED")
        print(f"Log bundle: {ctx.log_dir}")
        return 0
    except BaseException as exc:
        ctx.failure = str(exc)
        print("\nSMOKE FAILED")
        print(str(exc))
        print(f"Log bundle: {ctx.log_dir}")
        if not ctx.keep_resources_on_failure:
            print("Attempting best-effort cleanup after failure...")
            try:
                if ctx.setup_started and not ctx.teardown_attempted:
                    run_command(
                        ctx,
                        "cleanup-teardown-target-chain",
                        teardown_command(ctx),
                        env=env,
                        check=False,
                    )
                if not ctx.storage_delete_attempted:
                    delete_storage_account(ctx, env, check=False)
            except Exception as cleanup_exc:  # pragma: no cover - defensive logging
                (ctx.log_dir / "cleanup-error.txt").write_text(str(cleanup_exc) + "\n")
        return 1
    finally:
        restore_local_state(ctx)
        write_summary(ctx)


if __name__ == "__main__":
    raise SystemExit(main())
