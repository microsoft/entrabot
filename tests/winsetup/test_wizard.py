"""The wizard end to end, with the outside world faked.

Every test builds a throwaway project folder and a scripted world: ``az`` answers from JSON,
the helper scripts are stand-ins that record how they were called, the certificate store is
in memory. Nothing reaches the network, a tenant, the real ``.env`` or the real keystore.
"""

from __future__ import annotations

import io
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from entrabot.winsetup.azcli import GLOBAL_ADMIN
from entrabot.winsetup.console import Console, RunRequest
from entrabot.winsetup.envfile import read_env
from entrabot.winsetup.helpers import CHECKS_MARKER, RESULT_MARKER, Paths
from entrabot.winsetup.wizard import Options, Wizard, World, licence_outcome
from tests.winsetup.fakes import (
    FakeDirectory,
    FakeSpawn,
    FakeWindowsAcl,
    Reply,
    script,
    snippet,
    words,
)
from tests.winsetup.test_certstep import NEW_SHA1, NEW_X5T, FakeWinCert

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 11, 9, 30, 15, tzinfo=UTC)
TENANT = "aaaaaaaa-0000-0000-0000-000000000001"
OTHER_TENANT = "bbbbbbbb-0000-0000-0000-000000000002"
BP_APP = "11111111-2222-3333-4444-555555555555"
OTHER_BP = "99999999-2222-3333-4444-555555555555"
CREATED_STATE = {
    "TENANT_ID": TENANT,
    "PROVISIONER_CLIENT_ID": "prov-app",
    "BLUEPRINT_APP_ID": BP_APP,
    "BLUEPRINT_OBJECT_ID": "bp-object",
    "AGENT_ID": "agent-app",
    "AGENT_OBJECT_ID": "agent-object",
    "AGENT_USER_ID": "agent-user",
    "AGENT_USER_UPN": "entrabot-agent-sati@werner.ac",
    "AGENT_USER_LICENSE_SKU": "TEAMS_ESSENTIALS",
}
PASSING_SMOKE = [
    {"name": "Token mint (three-hop)", "status": "pass", "detail": "ok", "remediation": None},
    {"name": "Graph /me identity", "status": "pass", "detail": "ok", "remediation": None},
    {"name": "Teams scope (/me/chats)", "status": "pass", "detail": "ok", "remediation": None},
]
# What .env holds when this computer was set up for some other agent.
OTHER_AGENT_ENV = [
    f"ENTRABOT_TENANT_ID={TENANT}",
    "ENTRABOT_AGENT_USER_ID=the-previous-agent-user",
    "ENTRABOT_AGENT_UPN=entrabot-agent-old@werner.ac",
    "ENTRABOT_HUMAN_USER_ID=the-sponsor",
    "ENTRABOT_BLOB_ENDPOINT=https://old.blob.core.windows.net",
    "ENTRABOT_BLOB_CONTAINER=old",
]
ADMIN_USER = {
    "id": "admin-object",
    "displayName": "Tenant Admin",
    "userPrincipalName": "admin@werner.ac",
    "mail": "admin@werner.ac",
    "userType": "Member",
    "otherMails": [],
}
ADMIN_SPONSOR = {
    "id": "admin-object",
    "displayName": "Tenant Admin",
    "userPrincipalName": "admin@werner.ac",
    "mail": "admin@werner.ac",
}
PAT = {
    "id": "pat-object",
    "displayName": "Pat Member",
    "userPrincipalName": "pat@werner.ac",
    "mail": "pat@werner.ac",
    "userType": "Member",
    "otherMails": [],
}
# The tenant's owner signs in with a personal Microsoft account (measured on a real tenant).
OWNER = {
    "id": "owner-object",
    "displayName": "Brandon Werner",
    "userPrincipalName": "brandwe_outlook.com#EXT#@werner.onmicrosoft.com",
    "mail": None,
    "userType": "Member",
    "otherMails": ["brandwe@outlook.com"],
}
# The kind of thing the provisioning helpers print: useful in a log, noise on screen.
PROVISION_CHATTER = (
    "=" * 60
    + "\n  EntraBot Provisioner bootstrap\n"
    + "=" * 60
    + "\n"
    + "".join(f"  Permission {n}: Graph.Scope{n}.ReadWrite.All\n" for n in range(1, 27))
    + "  Using existing Provisioner cert\n"
)
CREATE_CHATTER = (
    "  Blueprint object id: bp-object\n  Agent Identity: agent-app\n  Created Agent User\n"
)
HOOK_OUTPUT = json.dumps({"hookSpecificOutput": {"additionalContext": "body"}})


def _checks(checks: list[dict]) -> Reply:
    return Reply(stdout=CHECKS_MARKER + json.dumps(checks))


@dataclass
class Scenario:
    tmp_path: Path
    spawn: FakeSpawn = field(default_factory=FakeSpawn)
    wincert: FakeWinCert = field(default_factory=FakeWinCert)
    env: dict[str, str] = field(default_factory=dict)
    signed_in: bool = True
    roles: list[str] = field(default_factory=lambda: [GLOBAL_ADMIN])
    subscriptions: list[dict] = field(default_factory=list)
    account_tenant: str = TENANT
    az_on_path: bool = True
    licence: str = "assigned"  # what create_entra_agent_ids.py does: assigned | existing | none
    directory: FakeDirectory = field(
        default_factory=lambda: FakeDirectory(users=[dict(PAT), dict(ADMIN_USER)])
    )
    # The sponsors on the Agent Identity in the directory: who the agent really obeys.
    # A new agent's only sponsor is whoever created it.
    agent_sponsors: list[dict] = field(default_factory=lambda: [dict(ADMIN_SPONSOR)])
    sponsor_add_fails: dict[str, str] = field(default_factory=dict)
    sponsors_unreadable: str = ""
    sponsor_adds_stay_invisible: bool = False
    launched: list[tuple[list[str], Path]] = field(default_factory=list)  # new console windows
    launch_override: Callable[[list[str], Path], None] | None = None
    claude_version: str = "2.1.300 (Claude Code)"  # what "claude --version" prints
    acl: FakeWindowsAcl = field(default_factory=FakeWindowsAcl)  # .env's permissions
    out: str = ""
    log: str = ""

    def __post_init__(self) -> None:
        root = self.root
        (root / "scripts").mkdir(parents=True)
        shutil.copy(REPO_ROOT / "scripts" / "mcp_config.py", root / "scripts" / "mcp_config.py")
        (root / "scripts" / "entra_provisioning.py").write_text("", encoding="utf-8")
        (root / "scripts" / "prereqs-windows.ps1").write_text("# helper\n", encoding="utf-8")
        (root / "start-entrabot.cmd").write_text("@echo off\n", encoding="utf-8")
        venv = root / ".venv" / "Scripts"
        venv.mkdir(parents=True)
        for name in ("python.exe", "entrabot-mcp.exe"):
            (venv / name).write_bytes(b"")
        (root / ".claude").mkdir()
        hook = {"type": "command", "command": "run-the-hook", "shell": "bash"}
        (root / ".claude" / "settings.json").write_text(
            json.dumps({"hooks": {"SessionStart": [{"hooks": [hook]}]}}), encoding="utf-8"
        )
        tools = self.tmp_path / "tools"
        tools.mkdir()
        for name in ("bash.exe", "claude.exe"):
            (tools / name).write_bytes(b"")
        self.env.update(
            {
                "USERNAME": "Brandon",
                "USERPROFILE": str(self.tmp_path / "profile"),
                "LOCALAPPDATA": str(self.tmp_path / "localappdata"),
                "SESSIONNAME": "Console",
                "CLAUDE_CODE_GIT_BASH_PATH": str(tools / "bash.exe"),
                "COPILOT_HOME": str(self.tmp_path / "copilot"),
                "PATH": "C:/Windows",
            }
        )
        self._rules()

    @property
    def root(self) -> Path:
        return self.tmp_path / "proj"

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    @property
    def state_file(self) -> Path:
        return self.root / ".entrabot-state.json"

    def _account(self) -> dict:
        return {
            "id": TENANT,
            "name": "N/A(tenant level account)",
            "tenantId": self.account_tenant,
            "tenantDisplayName": "Werner",
            "tenantDefaultDomain": "werner.onmicrosoft.com",
            "user": {"name": "admin@werner.ac", "type": "user"},
        }

    def _rules(self) -> None:
        on = self.spawn.on

        def account(_request: RunRequest) -> Reply:
            if not self.signed_in:
                return Reply(returncode=1, stderr="Please run 'az login' to setup account.")
            return Reply(stdout=json.dumps(self._account()))

        def login(_request: RunRequest) -> Reply:
            self.signed_in = True
            return Reply()

        def roles(_request: RunRequest) -> Reply:
            value = [
                {"@odata.type": "#microsoft.graph.directoryRole", "roleTemplateId": role}
                for role in self.roles
            ]
            return Reply(stdout=json.dumps({"value": value}))

        def create(_request: RunRequest) -> Reply:
            current = (
                json.loads(self.state_file.read_text("utf-8")) if self.state_file.exists() else {}
            )
            created = dict(CREATED_STATE)
            chatter = CREATE_CHATTER
            if self.licence == "assigned":
                chatter += "  Teams/mailbox provisioning can take 10-15 minutes\n"
            else:
                created.pop("AGENT_USER_LICENSE_SKU")
                current.pop("AGENT_USER_LICENSE_SKU", None)
                chatter += {
                    "existing": "  [skip] Agent User already has Teams-capable license: SPE_E5\n",
                    "none": "  No Teams-capable licenses found with available seats.\n",
                }[self.licence]
            self.state_file.write_text(json.dumps({**current, **created}), encoding="utf-8")
            return Reply(stdout=chatter)

        on(words("account", "show"), account)
        on(words("account", "list"), lambda _r: Reply(stdout=json.dumps(self.subscriptions)))
        on(words("login"), login)
        on(
            words("ad", "signed-in-user", "show"),
            Reply(
                stdout=json.dumps(
                    {
                        "id": "admin-object",
                        "userPrincipalName": "admin@werner.ac",
                        "mail": "admin@werner.ac",
                        "userType": "Member",
                    }
                )
            ),
        )
        on(words("rest"), roles)
        self.directory.attach(self.spawn)  # after the roles rule, so /users lookups land here
        on(words("account", "get-access-token"), Reply(stdout=json.dumps({"accessToken": "tok"})))
        on(script("entra_provisioning.py"), Reply(stdout=PROVISION_CHATTER))
        on(snippet("_add_sponsor"), self.sponsor_helper)
        on(lambda r: r.argv[0].endswith("powershell.exe"), self.install_helper)
        on(script("create_entra_agent_ids.py"), create)
        on(script("verify_blueprint_cert.py"), Reply())
        on(script("register_windows_blueprint_cert.py"), Reply())
        on(
            script("provision_blob_storage.py"),
            Reply(
                stdout="BLOB_ENDPOINT=https://new.blob.core.windows.net\nBLOB_CONTAINER=agent-x\n"
            ),
        )
        on(snippet("migrate_legacy_data_dir"), Reply(stdout=RESULT_MARKER + "nothing"))
        on(
            snippet("check_teams_license_availability"),
            _checks(
                [
                    {
                        "name": "Teams license",
                        "status": "pass",
                        "detail": "3 seats",
                        "remediation": None,
                    }
                ]
            ),
        )
        on(snippet("run_smoke_checks"), _checks(PASSING_SMOKE))
        on(
            snippet("claude_code_memory_dir"),
            Reply(
                stdout=RESULT_MARKER
                + json.dumps(
                    {"data_dir": "d", "data_bytes": 0, "persona_dir": "p", "persona_bytes": 0}
                )
            ),
        )
        on(
            lambda r: r.argv[0].endswith("whoami.exe") and "/user" in r.argv,
            Reply(stdout='"ebwintest\\\\brandon","S-1-5-21-1-2-3-1001"\n'),
        )
        on(
            lambda r: r.argv[0].endswith("whoami.exe") and "/groups" in r.argv,
            Reply(stdout='"BUILTIN\\\\Users","Alias","S-1-5-32-545"\n'),
        )
        on(lambda r: r.argv[0].endswith("icacls.exe"), Reply())
        on(lambda r: r.argv[0].endswith("bash.exe"), Reply(stdout=HOOK_OUTPUT))
        on(
            lambda r: r.argv[0].endswith("claude.exe") and r.argv[1:] == ["--version"],
            lambda _r: Reply(stdout=self.claude_version + "\n"),
        )

    def sponsor_helper(self, request: RunRequest) -> Reply:
        """Stands in for the child that reads and adds sponsors with the provisioner token."""
        wanted = request.argv[5:]
        if self.sponsors_unreadable:
            payload = {
                "before": None,
                "added": [],
                "failed": {},
                "after": None,
                "error": self.sponsors_unreadable,
            }
            return Reply(stdout=RESULT_MARKER + json.dumps(payload))
        before = [dict(item) for item in self.agent_sponsors]
        present = {str(item["id"]).lower() for item in before}
        added: list[str] = []
        failed: dict[str, str] = {}
        visible = [dict(item) for item in before]
        for user_id in wanted:
            if user_id.lower() in present:
                continue
            if user_id in self.sponsor_add_fails:
                failed[user_id] = self.sponsor_add_fails[user_id]
                continue
            added.append(user_id)
            self.agent_sponsors.append({"id": user_id})  # Graph returns little more than the id
            if not self.sponsor_adds_stay_invisible:
                visible.append({"id": user_id})
        payload = {
            "before": before,
            "added": added,
            "failed": failed,
            "after": visible,
            "error": "",
        }
        return Reply(stdout=RESULT_MARKER + json.dumps(payload))

    def sponsor_calls(self) -> list[RunRequest]:
        return [call for call in self.spawn.calls if snippet("_add_sponsor")(call)]

    def install_helper(self, request: RunRequest) -> Reply:
        """Stands in for scripts\\prereqs-windows.ps1: puts Claude Code where its installer does."""
        if "-WithClaudeCode" in request.argv:
            exe = self.tmp_path / "profile" / ".local" / "bin" / "claude.exe"
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_bytes(b"")
        return Reply()

    def which(self, name: str) -> str | None:
        if name == "az" and self.az_on_path:
            return "C:/tools/az"
        claude = self.tmp_path / "tools" / "claude.exe"
        if name == "claude" and claude.exists():
            return str(claude)
        return None

    def run(
        self,
        *,
        typed: str | None = None,
        answers: dict[str, str] | None = None,
        platform: str = "win32",
        **options: object,
    ) -> int:
        yes = bool(options.pop("yes", typed is None and answers is None))
        out = io.StringIO()
        log_file = self.tmp_path / "setup.log"
        console = Console(
            log_file=log_file,
            verbose=bool(options.get("verbose", False)),
            stdin=io.StringIO(typed or ""),
            stdout=out,
            interactive=typed is not None,
            assume_yes=yes,
            answers=answers,
            spawn=self.spawn,
            color=False,
            unicode=False,
        )
        world = World(
            env=self.env,
            platform=platform,
            now=lambda: NOW,
            which=self.which,
            wincert=self.wincert,
            http_get=lambda url: None,
            os_description=lambda: "Windows 11 (10.0.26200), AMD64",
            launch=self.launch_override
            or (lambda argv, cwd: self.launched.append((list(argv), cwd))),
        )
        paths = Paths(
            self.root,
            self.root / ".venv" / "Scripts" / "python.exe",
            self.tmp_path / "localappdata" / "entrabot",
        )
        opts = Options(project_root=self.root, yes=yes, **options)  # type: ignore[arg-type]
        try:
            return Wizard(opts, console, paths, world).run()
        finally:
            console.close()
            self.out = out.getvalue()
            self.log = log_file.read_text(encoding="utf-8") if log_file.exists() else ""

    # ── What happened ────────────────────────────────────────────────────────

    def ran(self, helper: str) -> list[RunRequest]:
        return [call for call in self.spawn.calls if script(helper)(call)]

    def az(self, *expected: str) -> list[RunRequest]:
        return [call for call in self.spawn.calls if words(*expected)(call)]

    def mutated(self) -> bool:
        """Did anything touch the tenant or this computer's Entrabot files?"""
        return bool(
            self.ran("entra_provisioning.py")
            or self.ran("create_entra_agent_ids.py")
            or self.wincert.created
            or self.env_file.exists()
        )

    @property
    def dotenv(self) -> dict[str, str]:
        return read_env(self.env_file)

    def existing(self, *, env: list[str] | None = None, state: dict | None = None) -> None:
        self.state_file.write_text(json.dumps(state or CREATED_STATE), encoding="utf-8")
        lines = (
            env
            if env is not None
            else [
                f"ENTRABOT_TENANT_ID={TENANT}",
                "ENTRABOT_AGENT_USER_ID=agent-user",
                "ENTRABOT_AGENT_UPN=entrabot-agent-sati@werner.ac",
                "ENTRABOT_HUMAN_USER_ID=the-sponsor",
                "ENTRABOT_HUMAN_UPN=boss@werner.ac",
            ]
        )
        self.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scenario:
    scenario = Scenario(tmp_path)
    # Windows file permissions, in memory: the same behaviour on every OS the tests run on,
    # and no real access list is touched even on Windows.
    scenario.acl.install(monkeypatch)
    return scenario


class TestScriptedNewAgent:
    def test_a_fresh_machine_is_taken_all_the_way(self, world: Scenario) -> None:
        code = world.run(new_chain=True, upn_suffix="sati")
        assert code == 0, world.out
        env = world.dotenv
        assert env["ENTRABOT_TENANT_ID"] == TENANT
        assert env["ENTRABOT_BLUEPRINT_APP_ID"] == BP_APP
        assert env["ENTRABOT_AGENT_UPN"] == "entrabot-agent-sati@werner.ac"
        assert env["ENTRABOT_BLUEPRINT_CERT_SHA1"] == NEW_SHA1
        assert env["ENTRABOT_BLUEPRINT_CERT_THUMBPRINT"] == NEW_X5T
        assert env["ENTRABOT_BLUEPRINT_KSP"] == "tpm"
        assert env["ENTRABOT_HUMAN_USER_ID"] == "admin-object"
        assert env["ENTRABOT_HUMAN_UPN"] == "admin@werner.ac"
        assert env["ENTRABOT_KEEP_MEMORY_LOCAL"] == "true"
        servers = json.loads((world.root / ".mcp.json").read_text("utf-8"))["mcpServers"]
        assert servers["entrabot"]["command"].endswith("entrabot-mcp.exe")
        assert (world.tmp_path / "copilot" / "mcp-config.json").is_file()

    def test_the_helpers_run_in_order(self, world: Scenario) -> None:
        world.run(new_chain=True, upn_suffix="sati")
        helpers = [
            Path(call.argv[1]).name
            for call in world.spawn.calls
            if len(call.argv) > 1 and call.argv[1].endswith(".py")
        ]
        assert helpers == [
            "entra_provisioning.py",
            "create_entra_agent_ids.py",
            "register_windows_blueprint_cert.py",
        ]

    def test_the_new_agent_choice_reaches_the_provisioning_helper(self, world: Scenario) -> None:
        # The bug this replaces: the PowerShell script passed --new and --with-upn-suffix as
        # arguments, which the helper never reads, so "-NewChain -UpnSuffix bob" silently
        # attached to whatever agent the tenant already had.
        world.run(new_chain=True, upn_suffix="sati")
        request = world.ran("create_entra_agent_ids.py")[0]
        assert request.argv[2:] == []  # the helper takes no arguments
        assert request.env is not None
        assert request.env["ENTRABOT_NEW_CHAIN"] == "1"
        assert request.env["_ENTRABOT_UPN_SUFFIX"] == "sati"
        assert request.env["ENTRABOT_AGENT_USER_UPN"] == ""
        assert request.env["ENTRABOT_TENANT_ID"] == TENANT
        assert request.env["PYTHONUTF8"] == "1" and request.env["PYTHONIOENCODING"] == "utf-8"
        # Helpers find the same az: its folder comes first, then whatever PATH held.
        az_folder = str(Path("C:/tools/az").parent)
        assert request.env["PATH"] == f"{az_folder}{os.pathsep}C:/Windows"
        assert "ENTRABOT_ASSIGN_WORK_IQ_LICENSE" not in request.env  # Work IQ was not asked for

    def test_the_finish_screen_says_how_to_start_and_what_protects_the_key(
        self, world: Scenario
    ) -> None:
        world.run(new_chain=True, upn_suffix="sati")
        assert "claude --dangerously-load-development-channels server:entrabot" in world.out
        assert "entrabot-agent-sati@werner.ac" in world.out
        assert "hardware TPM" in world.out
        assert "valid until" in world.out
        assert "-Diagnose" in world.out

    def test_a_software_key_says_why_the_tpm_was_not_used(self, world: Scenario) -> None:
        world.wincert.tpm_reason = "This computer has no TPM."
        world.run(new_chain=True, upn_suffix="sati")
        assert "Windows software key store" in world.out
        assert "This computer has no TPM." in world.out

    def test_no_full_thumbprint_or_token_is_ever_printed(self, world: Scenario) -> None:
        world.run(new_chain=True, upn_suffix="sati")
        assert NEW_SHA1 not in world.out and "tok" not in world.out.split()

    def test_the_output_is_plain_ascii_on_a_plain_console(self, world: Scenario) -> None:
        world.run(new_chain=True, upn_suffix="sati")
        assert world.out.isascii()

    def test_the_smoke_test_reads_the_new_env_not_values_inherited_from_this_process(
        self, world: Scenario
    ) -> None:
        # entrabot.config never overwrites a variable that is already set.
        world.env["ENTRABOT_AGENT_ID"] = "stale-from-the-shell"
        world.run(new_chain=True, upn_suffix="sati")
        smoke = next(c for c in world.spawn.calls if snippet("run_smoke_checks")(c))
        assert smoke.env is not None and "ENTRABOT_AGENT_ID" not in smoke.env

    def test_skip_smoke(self, world: Scenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati", skip_smoke=True) == 0
        assert not any(snippet("run_smoke_checks")(c) for c in world.spawn.calls)

    def test_a_sponsor_given_on_the_command_line_wins(self, world: Scenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati", sponsors=["pat@werner.ac"]) == 0
        assert world.dotenv["ENTRABOT_HUMAN_USER_ID"] == "pat-object"

    def test_a_sponsor_who_signs_in_with_a_personal_account_is_found(self, world: Scenario) -> None:
        # The defect, on a real tenant: joining an agent with this sponsor stopped with
        # "Could not find 'brandwe@outlook.com' in this tenant". The person is the tenant's
        # owner; their address is not their sign-in name and they have no "mail".
        world.directory.users.append(dict(OWNER))
        code = world.run(use_blueprint=BP_APP, sponsors=["brandwe@outlook.com"])
        assert code == 0, world.out
        env = world.dotenv
        assert env["ENTRABOT_HUMAN_USER_ID"] == "owner-object"
        assert env["ENTRABOT_HUMAN_UPN"] == "brandwe@outlook.com"  # as the other machine has it
        assert env["ENTRABOT_HUMAN_USER_TYPES"] == "Member"  # what the directory says
        assert env["ENTRABOT_HUMAN_USER_TENANT_IDS"] == ""

    def test_an_ambiguous_sponsor_lists_the_candidates_and_changes_nothing(
        self, world: Scenario
    ) -> None:
        world.directory.users += [
            dict(OWNER),
            {
                **OWNER,
                "id": "twin",
                "displayName": "Brandon (old)",
                "userPrincipalName": "brandon.old@werner.ac",
            },
        ]
        assert world.run(new_chain=True, upn_suffix="sati", sponsors=["brandwe@outlook.com"]) == 1
        assert "Brandon Werner" in world.out and "Brandon (old)" in world.out
        assert "object ID" in world.out and not world.mutated()

    def test_only_the_chosen_assistant_is_registered(self, world: Scenario) -> None:
        world.run(new_chain=True, upn_suffix="sati", hosts="claude")
        assert (world.root / ".mcp.json").is_file()
        assert not (world.tmp_path / "copilot").exists()


class TestOtherIdentityModes:
    def test_join_records_the_blueprint_and_tells_the_helper_nothing_about_a_new_chain(
        self, world: Scenario
    ) -> None:
        assert world.run(use_blueprint=BP_APP, agent_user_upn="bot@werner.ac") == 0
        request = world.ran("create_entra_agent_ids.py")[0]
        assert request.env is not None
        assert "ENTRABOT_NEW_CHAIN" not in request.env
        assert request.env["ENTRABOT_AGENT_USER_UPN"] == "bot@werner.ac"
        assert "_ENTRABOT_UPN_SUFFIX" not in request.env

    def test_joining_a_different_blueprint_clears_the_old_agents_state_first(
        self, world: Scenario
    ) -> None:
        world.existing(state={**CREATED_STATE, "BLUEPRINT_APP_ID": OTHER_BP}, env=OTHER_AGENT_ENV)
        seen: dict[str, str] = {}

        def provision(_request: RunRequest) -> Reply:
            seen.update(json.loads(world.state_file.read_text("utf-8")))
            return Reply()

        world.spawn.on(script("entra_provisioning.py"), provision)
        assert world.run(use_blueprint=BP_APP) == 0
        assert seen == {
            "TENANT_ID": TENANT,
            "PROVISIONER_CLIENT_ID": "prov-app",
            "BLUEPRINT_APP_ID": BP_APP,
        }
        assert list(world.root.glob(".entrabot-state.json.bak.*"))
        # The previous agent's sponsor and blob store do not carry over to this one.
        assert world.dotenv["ENTRABOT_HUMAN_USER_ID"] == "admin-object"
        assert "ENTRABOT_BLOB_ENDPOINT" not in world.dotenv
        assert world.dotenv["ENTRABOT_KEEP_MEMORY_LOCAL"] == "true"

    def test_stale_identity_variables_in_the_shell_never_reach_the_helper(
        self, world: Scenario
    ) -> None:
        world.env.update({"ENTRABOT_NEW_CHAIN": "1", "_ENTRABOT_UPN_SUFFIX": "old"})
        world.existing()
        assert world.run() == 0
        request = world.ran("create_entra_agent_ids.py")[0]
        assert request.env is not None
        assert "ENTRABOT_NEW_CHAIN" not in request.env
        assert "_ENTRABOT_UPN_SUFFIX" not in request.env


class TestRepair:
    def test_re_running_with_no_flags_continues_with_the_existing_agent(
        self, world: Scenario
    ) -> None:
        world.existing()
        assert world.run() == 0, world.out
        assert "already set up for entrabot-agent-sati@werner.ac" in world.out
        assert world.dotenv["ENTRABOT_HUMAN_USER_ID"] == "the-sponsor"  # sponsor unchanged
        assert world.directory.asked == []  # nobody is looked up when nothing will change

    def test_an_agent_recorded_only_in_env_can_still_be_continued(self, world: Scenario) -> None:
        # The state file was lost (or the machine was set up by a version that kept less in
        # it). .env still names the agent, so "continue" means: attach to that Blueprint.
        world.existing(
            env=[
                f"ENTRABOT_TENANT_ID={TENANT}",
                f"ENTRABOT_BLUEPRINT_APP_ID={BP_APP}",
                "ENTRABOT_AGENT_USER_ID=agent-user",
                "ENTRABOT_HUMAN_USER_ID=the-sponsor",
            ]
        )
        world.state_file.unlink()
        assert world.run() == 0, world.out
        request = world.ran("create_entra_agent_ids.py")[0]
        assert request.env is not None and "ENTRABOT_NEW_CHAIN" not in request.env
        assert world.dotenv["ENTRABOT_HUMAN_USER_ID"] == "the-sponsor"

    def test_a_machine_on_cloud_memory_stays_there(self, world: Scenario) -> None:
        world.existing(
            env=[
                f"ENTRABOT_TENANT_ID={TENANT}",
                "ENTRABOT_AGENT_USER_ID=agent-user",
                "ENTRABOT_BLOB_ENDPOINT=https://old.blob.core.windows.net",
                "ENTRABOT_BLOB_CONTAINER=old",
            ]
        )
        assert world.run() == 0
        assert "ENTRABOT_KEEP_MEMORY_LOCAL" not in world.dotenv
        assert world.dotenv["ENTRABOT_BLOB_ENDPOINT"] == "https://old.blob.core.windows.net"
        assert world.ran("provision_blob_storage.py") == []

    def test_asking_for_local_is_the_way_back_from_cloud(self, world: Scenario) -> None:
        world.existing(
            env=[
                "ENTRABOT_AGENT_USER_ID=agent-user",
                "ENTRABOT_BLOB_ENDPOINT=https://old.blob.core.windows.net",
                "ENTRABOT_BLOB_CONTAINER=old",
            ]
        )
        assert world.run(memory="local") == 0
        assert world.dotenv["ENTRABOT_KEEP_MEMORY_LOCAL"] == "true"

    def test_a_working_registered_certificate_is_reused(self, world: Scenario) -> None:
        from tests.winsetup.test_certstep import OLD_SHA1, OLD_X5T, StoreCert

        world.existing(
            env=[
                "ENTRABOT_AGENT_USER_ID=agent-user",
                f"ENTRABOT_BLUEPRINT_CERT_SHA1={OLD_SHA1}",
                f"ENTRABOT_BLUEPRINT_CERT_THUMBPRINT={OLD_X5T}",
            ]
        )
        world.wincert.store[OLD_SHA1] = StoreCert(OLD_SHA1, OLD_X5T, b"\x01")
        assert world.run() == 0
        assert not world.wincert.created
        assert world.ran("register_windows_blueprint_cert.py") == []
        assert world.dotenv["ENTRABOT_BLUEPRINT_CERT_SHA1"] == OLD_SHA1
        # A machine set up by an older version has a certificate to reuse and no identity in
        # .env, so reusing a certificate must not skip writing it.
        assert world.dotenv["ENTRABOT_AGENT_ID"] == "agent-app"
        assert world.dotenv["ENTRABOT_TENANT_ID"] == TENANT

    def test_a_certificate_check_that_fails_leaves_env_exactly_as_it_was(
        self, world: Scenario
    ) -> None:
        from tests.winsetup.test_certstep import OLD_SHA1, OLD_X5T, StoreCert

        world.existing(
            env=[
                " ENTRABOT_BLUEPRINT_CERT_THUMBPRINT = " + OLD_X5T + " ",  # padded, as typed
                f"ENTRABOT_BLUEPRINT_CERT_SHA1 = {OLD_SHA1}",
                "ENTRABOT_AGENT_USER_ID=agent-user",
            ]
        )
        before = world.env_file.read_bytes()
        world.wincert.store[OLD_SHA1] = StoreCert(OLD_SHA1, OLD_X5T, b"\x01")
        world.spawn.on(script("verify_blueprint_cert.py"), Reply(returncode=3))
        assert world.run() == 1
        assert "Nothing was generated or changed" in world.out
        assert world.env_file.read_bytes() == before
        assert not world.wincert.created

    def test_signed_in_to_the_wrong_tenant_stops_before_anything_changes(
        self, world: Scenario
    ) -> None:
        world.existing()
        before = world.env_file.read_bytes()
        world.account_tenant = OTHER_TENANT
        assert world.run() == 1
        assert "different tenant" in world.out and "Nothing was changed" in world.out
        assert world.ran("entra_provisioning.py") == [] and not world.wincert.created
        assert world.env_file.read_bytes() == before


class TestUnattended:
    def test_no_identity_and_nothing_to_continue_is_exit_2_naming_the_flags(
        self, world: Scenario
    ) -> None:
        assert world.run() == 2
        assert "-NewChain -UpnSuffix NAME" in world.out and "-UseBlueprint APP_ID" in world.out
        assert world.az("login") == [] and not world.mutated()

    def test_a_new_agent_without_a_name_is_exit_2(self, world: Scenario) -> None:
        # A name is part of the agent's identity; an unattended run never invents one.
        assert world.run(new_chain=True) == 2
        assert "-UpnSuffix NAME" in world.out and not world.mutated()

    def test_both_identity_flags_are_a_usage_error(self, world: Scenario) -> None:
        assert world.run(new_chain=True, use_blueprint=BP_APP) == 2
        assert not world.mutated()

    @pytest.mark.parametrize(
        "bad",
        [
            {"use_blueprint": "not-a-guid"},
            {"new_chain": True, "upn_suffix": "Has Space"},
            {"new_chain": True, "upn_suffix": "a", "tenant": "x&calc"},
            {"new_chain": True, "upn_suffix": "a", "sponsors": ["nope"]},
            {"new_chain": True, "upn_suffix": "a", "hosts": "cursor"},
            {"new_chain": True, "upn_suffix": "a", "storage_account": "acct"},
            {
                "new_chain": True,
                "upn_suffix": "a",
                "memory": "cloud",
                "storage_account": "acct",
                "create_new_storage": True,
            },
        ],
    )
    def test_bad_values_are_refused_before_anything_runs(self, world: Scenario, bad: dict) -> None:
        assert world.run(**bad) == 2
        assert world.spawn.calls == []


class TestInteractive:
    def test_pressing_enter_at_every_prompt_sets_up_a_new_agent(self, world: Scenario) -> None:
        code = world.run(typed="\n" * 30)
        assert code == 0, world.out
        request = world.ran("create_entra_agent_ids.py")[0]
        assert request.env is not None
        assert request.env["ENTRABOT_NEW_CHAIN"] == "1"
        assert request.env["_ENTRABOT_UPN_SUFFIX"] == "brandon"  # from the Windows username
        assert world.dotenv["ENTRABOT_KEEP_MEMORY_LOCAL"] == "true"
        assert world.dotenv["ENTRABOT_HUMAN_UPN"] == "admin@werner.ac"

    def test_nothing_changes_before_the_review_screen_is_confirmed(self, world: Scenario) -> None:
        assert world.run(answers={"identity": "new", "upn_suffix": "sati", "start": "no"}) == 130
        assert "Nothing has been changed yet." in world.out
        assert not world.mutated()

    def test_the_review_screen_shows_every_decision(self, world: Scenario) -> None:
        world.run(answers={"identity": "new", "upn_suffix": "sati", "start": "no"})
        for expected in (
            "admin@werner.ac",
            "entrabot-agent-sati",
            "you (admin@werner.ac)",
            "on this computer",
            "Claude Code",
        ):
            assert expected in world.out

    def test_joining_asks_for_the_app_id(self, world: Scenario) -> None:
        answers = {"identity": "join", "blueprint": BP_APP}
        assert world.run(answers=answers, yes=False) == 0
        assert json.loads(world.state_file.read_text("utf-8"))["BLUEPRINT_APP_ID"] == BP_APP

    def test_an_existing_setup_offers_a_health_check_that_changes_nothing(
        self, world: Scenario
    ) -> None:
        world.existing()
        world.spawn.on(snippet("run_diagnostics"), Reply(stdout="[ PASS ] everything"))
        before = world.env_file.read_bytes()
        assert world.run(answers={"existing": "diagnose"}) == 0
        assert "everything" in world.out
        assert world.env_file.read_bytes() == before
        assert world.ran("entra_provisioning.py") == []

    def test_choosing_a_different_agent_from_the_menu(self, world: Scenario) -> None:
        world.existing(env=OTHER_AGENT_ENV)
        answers = {"existing": "different", "identity": "new", "upn_suffix": "second"}
        assert world.run(answers=answers) == 0
        request = world.ran("create_entra_agent_ids.py")[0]
        assert request.env is not None and request.env["_ENTRABOT_UPN_SUFFIX"] == "second"
        assert world.dotenv["ENTRABOT_HUMAN_USER_ID"] == "admin-object"  # not the old sponsor

    def test_naming_someone_else_as_the_sponsor(self, world: Scenario) -> None:
        answers = {
            "identity": "new",
            "upn_suffix": "sati",
            "sponsor": "other",
            "sponsor_emails": "pat@werner.ac",
        }
        assert world.run(answers=answers) == 0
        assert world.dotenv["ENTRABOT_HUMAN_UPNS"] == "pat@werner.ac"

    def test_cloud_memory_is_only_offered_with_a_subscription(self, world: Scenario) -> None:
        world.run(typed="\n" * 30)
        assert "Cloud memory is not offered" in world.out
        assert "In your Azure subscription" not in world.out


class TestSignIn:
    def test_nobody_signed_in_means_a_sign_in_that_needs_no_subscription(
        self, world: Scenario
    ) -> None:
        world.signed_in = False
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        login = world.az("login")[0]
        assert "--allow-no-subscriptions" in login.argv and login.interactive
        assert "--use-device-code" not in login.argv  # a desktop session gets the browser

    def test_an_ssh_session_gets_a_device_code_automatically(self, world: Scenario) -> None:
        world.signed_in = False
        world.env.update({"SESSIONNAME": "", "SSH_CONNECTION": "10.0.0.4 51000 10.0.0.5 22"})
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        assert "--use-device-code" in world.az("login")[0].argv
        assert "short code" in world.out

    def test_the_device_code_flag_forces_it(self, world: Scenario) -> None:
        world.signed_in = False
        world.run(new_chain=True, upn_suffix="sati", device_code=True, tenant="werner.ac")
        argv = world.az("login")[0].argv
        assert "--use-device-code" in argv and argv[argv.index("--tenant") + 1] == "werner.ac"

    def test_a_failed_browser_sign_in_is_retried_with_a_code(self, world: Scenario) -> None:
        world.signed_in = False
        attempts: list[list[str]] = []

        def login(request: RunRequest) -> Reply:
            attempts.append(request.argv)
            if "--use-device-code" not in request.argv:
                return Reply(returncode=1)
            world.signed_in = True
            return Reply()

        world.spawn.on(words("login"), login)
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        assert len(attempts) == 2 and "--use-device-code" in attempts[1]

    def test_a_sign_in_that_never_completes_stops_setup(self, world: Scenario) -> None:
        world.signed_in = False
        world.spawn.on(words("login"), Reply(returncode=1))
        assert world.run(new_chain=True, upn_suffix="sati") == 1
        assert "Sign-in did not complete" in world.out and not world.mutated()

    def test_an_existing_session_in_another_tenant_is_not_reused_when_one_is_asked_for(
        self, world: Scenario
    ) -> None:
        world.run(new_chain=True, upn_suffix="sati", tenant=OTHER_TENANT)
        assert world.az("login")  # signed in again, to the tenant that was asked for

    def test_an_existing_session_in_the_right_tenant_is_reused(self, world: Scenario) -> None:
        world.run(new_chain=True, upn_suffix="sati", tenant="werner.onmicrosoft.com")
        assert world.az("login") == []

    def test_a_missing_azure_cli_is_installed_without_admin_rights(self, world: Scenario) -> None:
        world.az_on_path = False
        tools = world.tmp_path / "localappdata" / "entrabot" / "tools" / "azure-cli" / "Scripts"

        def make_venv(_request: RunRequest) -> Reply:
            tools.mkdir(parents=True, exist_ok=True)
            (tools / "python.exe").write_bytes(b"")
            return Reply()

        def pip(_request: RunRequest) -> Reply:
            (tools / "az.bat").write_text("", encoding="utf-8")
            return Reply()

        world.spawn.on(lambda r: r.argv[1:3] == ["-m", "venv"], make_venv)
        world.spawn.on(lambda r: r.argv[1:4] == ["-m", "pip", "install"], pip)
        world.spawn.on(
            lambda r: r.argv[1:4] == ["-m", "azure.cli", "version"],
            Reply(stdout=json.dumps({"azure-cli": "2.91.0"})),
        )
        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out
        request = world.ran("create_entra_agent_ids.py")[0]
        assert request.env is not None and request.env["PATH"].startswith(str(tools))


class TestPrivileges:
    def test_no_admin_role_stops_at_enter_before_anything_changes(self, world: Scenario) -> None:
        world.roles = []
        assert world.run(typed="\n" * 30) == 130
        assert "Global Administrator" in world.out and not world.mutated()

    def test_an_unattended_run_warns_and_goes_on(self, world: Scenario) -> None:
        # Roles activated on demand and app ownership can't be seen from here.
        world.roles = []
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        assert "Global Administrator" in world.out

    def test_the_person_can_choose_to_continue(self, world: Scenario) -> None:
        world.roles = []
        answers = {"identity": "new", "upn_suffix": "sati", "continue_without_admin": "yes"}
        assert world.run(answers=answers, typed="\n" * 30) == 0


class TestFailures:
    def test_a_failed_provisioner_step_says_what_to_do(self, world: Scenario) -> None:
        world.spawn.on(
            script("entra_provisioning.py"),
            Reply(returncode=1, stderr="ERROR: Insufficient privileges"),
        )
        assert world.run(new_chain=True, upn_suffix="sati") == 1
        assert "Insufficient privileges" in world.out  # the helper's own error is not hidden
        assert "administrator" in world.out and "Run setup again" in world.out
        assert world.ran("create_entra_agent_ids.py") == []

    def test_a_failed_agent_creation_stops_before_the_certificate(self, world: Scenario) -> None:
        world.spawn.on(script("create_entra_agent_ids.py"), Reply(returncode=1))
        assert world.run(new_chain=True, upn_suffix="sati") == 1
        assert not world.wincert.created and not world.env_file.exists()

    def test_an_unconfirmed_certificate_is_saved_and_then_setup_stops(
        self, world: Scenario
    ) -> None:
        world.spawn.on(script("register_windows_blueprint_cert.py"), Reply(returncode=3))
        assert world.run(new_chain=True, upn_suffix="sati") == 1
        assert world.dotenv["ENTRABOT_BLUEPRINT_CERT_SHA1"] == NEW_SHA1
        assert "ENTRABOT_KEEP_MEMORY_LOCAL" not in world.dotenv  # stopped before the memory step
        assert not (world.root / ".mcp.json").exists()
        assert "run setup again" in world.out.lower()

    def test_a_refused_certificate_leaves_no_settings_behind(self, world: Scenario) -> None:
        world.spawn.on(script("register_windows_blueprint_cert.py"), Reply(returncode=1))
        assert world.run(new_chain=True, upn_suffix="sati") == 1
        assert not world.env_file.exists()
        assert world.wincert.deleted == [(NEW_SHA1, True)]

    def test_settings_that_cannot_be_locked_are_a_warning(self, world: Scenario) -> None:
        world.spawn.on(
            lambda r: r.argv[0].endswith("icacls.exe"),
            Reply(returncode=5, stderr="Access is denied."),
        )
        world.acl.fail_set = "SetNamedSecurityInfoW failed (Windows error 5: Access is denied.)"
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        assert "Access is denied" in world.out
        assert "only your account" not in world.out
        assert "NOT restricted" in world.out and "SYSTEM" in world.out

    def test_ctrl_c_exits_130_and_says_nothing_is_half_written(self, world: Scenario) -> None:
        def interrupt(_request: RunRequest) -> Reply:
            raise KeyboardInterrupt

        world.spawn.on(script("create_entra_agent_ids.py"), interrupt)
        assert world.run(new_chain=True, upn_suffix="sati") == 130
        assert "Nothing is left half-written" in world.out
        assert not world.env_file.exists()

    def test_not_windows_is_refused(self, world: Scenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati", platform="darwin") == 1
        assert "setup.sh" in world.out and world.spawn.calls == []

    def test_work_iq_without_its_tools_stops_before_anything_changes(self, world: Scenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati", configure_work_iq=True) == 1
        assert "dotnet tool install" in world.out and not world.mutated()


class TestSmokeOutcomes:
    def _fail(self, world: Scenario, index: int, detail: str) -> int:
        checks = [dict(check) for check in PASSING_SMOKE]
        checks[index].update(status="fail", detail=detail)
        for later in checks[index + 1 :]:
            later.update(status="skip", detail="Skipped")
        world.spawn.on(snippet("run_smoke_checks"), _checks(checks))
        return world.run(new_chain=True, upn_suffix="sati")

    def test_teams_not_ready_is_exit_3_with_the_licence_wait_explained(
        self, world: Scenario
    ) -> None:
        assert self._fail(world, 2, "403 Forbidden") == 3
        assert "10-15 minutes" in world.out and "not ready" in world.out
        assert "The agent can reach Teams: no" in world.out and "403 Forbidden" in world.out
        assert world.dotenv["ENTRABOT_AGENT_ID"] == "agent-app"  # setup itself is complete

    def test_a_failed_sign_in_is_not_blamed_on_the_licence(self, world: Scenario) -> None:
        assert self._fail(world, 0, "Hop 1 failed - invalid_client: AADSTS700027") == 3
        assert "This is not a licence delay" in world.out

    def test_a_session_without_credential_manager_is_reported_as_exactly_that(
        self, world: Scenario
    ) -> None:
        # Measured over SSH on Windows 11: Credential Manager refuses with this message.
        detail = (
            "Unexpected error: (1312, 'CredWrite', 'A specified logon session does not exist.')"
        )
        assert self._fail(world, 0, detail) == 3
        assert "Credential Manager" in world.out and "limit of the session" in world.out
        assert "This is not a licence delay" not in world.out

    def test_a_test_that_cannot_run_is_exit_3_with_the_reason(self, world: Scenario) -> None:
        world.spawn.on(
            snippet("run_smoke_checks"),
            Reply(returncode=1, stderr="ModuleNotFoundError: No module named 'x'"),
        )
        assert world.run(new_chain=True, upn_suffix="sati") == 3
        assert "ModuleNotFoundError" in world.out


class TestCloudMemory:
    SUBS = [
        {
            "id": "sub-1",
            "name": "Visual Studio Enterprise",
            "tenantId": TENANT,
            "state": "Enabled",
            "isDefault": True,
        },
        {
            "id": "sub-2",
            "name": "Dev & Test (R&D)",
            "tenantId": TENANT,
            "state": "Enabled",
            "isDefault": False,
        },
    ]

    def test_cloud_memory_provisions_storage_in_the_chosen_subscription(
        self, world: Scenario
    ) -> None:
        world.subscriptions = self.SUBS
        code = world.run(
            new_chain=True,
            upn_suffix="sati",
            memory="cloud",
            subscription="Dev & Test (R&D)",
            location="westus2",
        )
        assert code == 0, world.out
        argv = world.ran("provision_blob_storage.py")[0].argv
        # The ID, never the name: a name can hold characters cmd.exe would reinterpret.
        assert argv[argv.index("--subscription") + 1] == "sub-2"
        assert argv[argv.index("--location") + 1] == "westus2"
        assert world.dotenv["ENTRABOT_BLOB_ENDPOINT"] == "https://new.blob.core.windows.net"
        assert "ENTRABOT_KEEP_MEMORY_LOCAL" not in world.dotenv

    def test_the_default_subscription_and_region_are_used_unattended(self, world: Scenario) -> None:
        world.subscriptions = self.SUBS
        world.run(new_chain=True, upn_suffix="sati", memory="cloud")
        argv = world.ran("provision_blob_storage.py")[0].argv
        assert argv[argv.index("--subscription") + 1] == "sub-1"
        assert argv[argv.index("--location") + 1] == "eastus"

    def test_no_subscription_stops_before_anything_changes(self, world: Scenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati", memory="cloud") == 1
        assert "Azure subscription" in world.out and not world.mutated()

    def test_an_unknown_subscription_lists_the_real_ones(self, world: Scenario) -> None:
        world.subscriptions = self.SUBS
        assert (
            world.run(new_chain=True, upn_suffix="sati", memory="cloud", subscription="nope") == 1
        )
        assert "Visual Studio Enterprise" in world.out and not world.mutated()

    def test_failed_storage_falls_back_to_local_loudly(self, world: Scenario) -> None:
        world.subscriptions = self.SUBS
        world.spawn.on(
            script("provision_blob_storage.py"),
            Reply(returncode=1, stderr="ERROR: AuthorizationFailed"),
        )
        assert world.run(new_chain=True, upn_suffix="sati", memory="cloud") == 0
        assert world.dotenv["ENTRABOT_KEEP_MEMORY_LOCAL"] == "true"
        assert "AuthorizationFailed" in world.out
        assert "Cloud memory was asked for but not set up" in world.out

    def test_interactive_cloud_choice_asks_which_subscription_and_region(
        self, world: Scenario
    ) -> None:
        world.subscriptions = self.SUBS
        answers = {
            "identity": "new",
            "upn_suffix": "sati",
            "memory": "cloud",
            "subscription": "sub-2",
            "location": "westeurope",
        }
        assert world.run(answers=answers) == 0
        argv = world.ran("provision_blob_storage.py")[0].argv
        assert argv[argv.index("--subscription") + 1] == "sub-2"
        assert argv[argv.index("--location") + 1] == "westeurope"


class TestHostAdvice:
    def test_missing_claude_code_is_the_first_thing_to_do(self, world: Scenario) -> None:
        (world.tmp_path / "tools" / "claude.exe").unlink()
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        todo = world.out[world.out.index("To start your agent:") :]
        assert todo.index("scripts\\prereqs-windows.ps1 -WithClaudeCode") < todo.index(
            "claude --dangerously-load-development-channels"
        )
        assert "winget" not in world.out  # winget does not exist for every user
        # Unattended: nothing was installed on the computer without asking.
        assert not any("-File" in call.argv for call in world.spawn.calls)

    def test_interactively_missing_claude_code_is_installed_on_enter(self, world: Scenario) -> None:
        (world.tmp_path / "tools" / "claude.exe").unlink()
        assert world.run(typed="\n" * 40) == 0, world.out
        helper = next(call for call in world.spawn.calls if "-File" in call.argv)
        assert helper.argv[-1] == "-WithClaudeCode" and helper.interactive
        todo = world.out[world.out.index("To start your agent:") :]
        assert "prereqs-windows.ps1" not in todo  # nothing left to install
        assert todo.index("1. Double-click start-entrabot.cmd") < todo.index("2. Then say")

    def test_a_licence_just_assigned_is_explained(self, world: Scenario) -> None:
        world.run(new_chain=True, upn_suffix="sati")
        assert "TEAMS_ESSENTIALS" in world.out and "10-15 minutes" in world.out


class TestThisComputer:
    def test_a_onedrive_folder_is_flagged_but_not_refused(self, world: Scenario) -> None:
        world.env["OneDrive"] = str(world.tmp_path)
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        assert "synced by OneDrive" in world.out

    def test_an_incomplete_project_folder_is_refused(self, world: Scenario) -> None:
        (world.root / "scripts" / "entra_provisioning.py").unlink()
        assert world.run(new_chain=True, upn_suffix="sati") == 1
        assert "incomplete" in world.out and world.az("account", "show") == []

    def test_data_from_an_older_version_is_moved(self, world: Scenario) -> None:
        world.spawn.on(snippet("migrate_legacy_data_dir"), Reply(stdout=RESULT_MARKER + "moved"))
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        assert "Moved data from an older Entrabot version" in world.out

    def test_two_data_folders_are_explained_and_setup_goes_on(self, world: Scenario) -> None:
        world.spawn.on(
            snippet("migrate_legacy_data_dir"),
            Reply(returncode=1, stderr="RuntimeError: two entrabot dirs detected: legacy ..."),
        )
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        assert "in two places" in world.out

    def test_a_corrupt_state_file_is_not_mistaken_for_a_new_machine(self, world: Scenario) -> None:
        world.state_file.write_text("{not json", encoding="utf-8")
        assert world.run(new_chain=True, upn_suffix="sati") == 1
        assert ".entrabot-state.json" in world.out and not world.wincert.created


class TestMoreChoices:
    CLOUD_ENV = [
        f"ENTRABOT_TENANT_ID={TENANT}",
        "ENTRABOT_AGENT_USER_ID=agent-user",
        "ENTRABOT_HUMAN_UPN=boss@werner.ac",
        "ENTRABOT_BLOB_ENDPOINT=https://old.blob.core.windows.net",
        "ENTRABOT_BLOB_CONTAINER=old",
    ]

    def test_a_machine_on_cloud_memory_is_offered_keep_or_move(self, world: Scenario) -> None:
        world.existing(env=self.CLOUD_ENV)
        assert world.run(answers={"start": "no"}, typed="\n" * 20) == 130
        assert "Keep it in Azure (current)" in world.out
        assert "in Azure (unchanged)" in world.out

    def test_moving_from_cloud_to_this_computer(self, world: Scenario) -> None:
        world.existing(env=self.CLOUD_ENV)
        assert world.run(answers={"memory": "local"}) == 0
        assert world.dotenv["ENTRABOT_KEEP_MEMORY_LOCAL"] == "true"

    def test_taking_over_as_sponsor_on_an_existing_agent(self, world: Scenario) -> None:
        world.existing(env=self.CLOUD_ENV)
        assert world.run(answers={"sponsor": "me"}) == 0
        assert world.dotenv["ENTRABOT_HUMAN_UPN"] == "admin@werner.ac"
        assert world.dotenv["ENTRABOT_HUMAN_USER_ID"] == "admin-object"

    def test_keeping_the_sponsor_is_the_default_on_an_existing_agent(self, world: Scenario) -> None:
        world.existing(env=self.CLOUD_ENV)
        assert world.run(typed="\n" * 20) == 0
        assert world.dotenv["ENTRABOT_HUMAN_UPN"] == "boss@werner.ac"
        assert "Contact in this computer's settings unchanged: boss@werner.ac" in world.out

    def test_an_unknown_sponsor_stops_before_anything_changes(self, world: Scenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati", sponsors=["nobody@werner.ac"]) == 1
        assert "nobody@werner.ac" in world.out and not world.mutated()
        assert "object ID" in world.out  # what to do about it

    def test_work_iq_chosen_interactively_without_its_tools_is_skipped_not_fatal(
        self, world: Scenario
    ) -> None:
        answers = {"identity": "new", "upn_suffix": "sati", "work_iq": "yes"}
        assert world.run(answers=answers) == 0
        assert "skipped for now" in world.out
        request = world.ran("create_entra_agent_ids.py")[0]
        assert request.env is not None and "ENTRABOT_ASSIGN_WORK_IQ_LICENSE" not in request.env

    def test_a_work_iq_licence_request_left_in_the_shell_is_not_inherited(
        self, world: Scenario
    ) -> None:
        # The old PowerShell setup set this variable in the calling session and left it there.
        # A later run that does not ask for Work IQ must not assign a Copilot licence.
        world.env["ENTRABOT_ASSIGN_WORK_IQ_LICENSE"] = "1"
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        for helper in ("entra_provisioning.py", "create_entra_agent_ids.py"):
            request = world.ran(helper)[0]
            assert request.env is not None
            assert "ENTRABOT_ASSIGN_WORK_IQ_LICENSE" not in request.env

    def test_work_iq_with_its_tools_runs_after_the_agent_exists(self, world: Scenario) -> None:
        a365 = world.tmp_path / "tools" / "a365.exe"
        a365.write_bytes(b"")
        base = world.which
        world.which = lambda name: str(a365) if name == "a365" else base(name)  # type: ignore[method-assign]
        world.spawn.on(words("ad", "app", "list"), Reply(stdout=json.dumps([{"appId": "cli"}])))
        world.spawn.on(words("ad", "sp", "show"), Reply(stdout=json.dumps({"displayName": "A"})))
        assert world.run(new_chain=True, upn_suffix="sati", configure_work_iq=True) == 0, world.out
        request = world.ran("create_entra_agent_ids.py")[0]
        assert request.env is not None and request.env["ENTRABOT_ASSIGN_WORK_IQ_LICENSE"] == "1"
        order = [call.argv for call in world.spawn.calls]
        created = next(
            i
            for i, argv in enumerate(order)
            if argv[1:2] and argv[1].endswith("create_entra_agent_ids.py")
        )
        requirements = next(
            i for i, argv in enumerate(order) if argv[:3] == [str(a365), "setup", "requirements"]
        )
        assert created < requirements
        assert (
            json.loads((world.root / "a365.config.json").read_text("utf-8"))["agentBlueprintId"]
            == BP_APP
        )

    def test_a_failed_work_iq_step_stops_setup(self, world: Scenario) -> None:
        a365 = world.tmp_path / "tools" / "a365.exe"
        a365.write_bytes(b"")
        base = world.which
        world.which = lambda name: str(a365) if name == "a365" else base(name)  # type: ignore[method-assign]
        world.spawn.on(words("ad", "app", "list"), Reply(stdout="[]"))
        assert world.run(new_chain=True, upn_suffix="sati", configure_work_iq=True) == 1
        assert "Agent 365 CLI" in world.out and not world.wincert.created


class TestCloudMemoryEdges:
    def test_refusing_the_local_fallback_stops_with_the_agent_saved(self, world: Scenario) -> None:
        world.subscriptions = TestCloudMemory.SUBS
        world.spawn.on(script("provision_blob_storage.py"), Reply(returncode=1, stderr="boom"))
        answers = {
            "identity": "new",
            "upn_suffix": "sati",
            "memory": "cloud",
            "memory_fallback": "no",
        }
        assert world.run(answers=answers) == 1
        assert world.dotenv["ENTRABOT_AGENT_ID"] == "agent-app"
        assert "ENTRABOT_KEEP_MEMORY_LOCAL" not in world.dotenv
        assert "chose not to continue" in world.out

    def test_a_failed_memory_upload_is_exit_1_and_says_so(self, world: Scenario) -> None:
        world.subscriptions = TestCloudMemory.SUBS
        world.spawn.on(
            snippet("claude_code_memory_dir"),
            Reply(
                stdout=RESULT_MARKER
                + json.dumps(
                    {"data_dir": "d", "data_bytes": 4096, "persona_dir": "p", "persona_bytes": 0}
                )
            ),
        )
        world.spawn.on(
            snippet("migrate_local_to_backend"),
            Reply(
                returncode=2,
                stdout=RESULT_MARKER
                + json.dumps(
                    {
                        "copied": 0,
                        "skipped": 0,
                        "bytes": 0,
                        "errors": [["a.jsonl", "403"]],
                        "error_count": 1,
                    }
                ),
            ),
        )
        answers = {
            "identity": "new",
            "upn_suffix": "sati",
            "memory": "cloud",
            "memory_upload": "yes",
        }
        assert world.run(answers=answers) == 1
        assert "NOT copied to the cloud" in world.out
        assert world.dotenv["ENTRABOT_BLOB_CONTAINER"] == "agent-x"  # cloud memory itself is set


class TestEnvStaysLocked:
    """Two defects measured on Windows 11. First: setup locked .env, a later step rewrote it,
    and the new file was not locked, while setup had printed "only your account can read it".
    Second: the lock itself left SYSTEM and Administrators with full control, because on a
    new file those are explicit entries and removing inheritance does not touch them."""

    def test_every_write_of_env_is_locked_before_it_lands(self, world: Scenario) -> None:
        world.subscriptions = TestCloudMemory.SUBS
        assert world.run(new_chain=True, upn_suffix="sati", memory="cloud") == 0, world.out
        events = world.acl.events
        landings = [index for index, event in enumerate(events) if event == "replace .env"]
        # Identity, then memory: the second write is the one that used to drop the lock.
        assert len(landings) >= 2
        for index in landings:
            assert events[index - 1].startswith("lock .env."), (
                "a new .env was moved into place without being locked first"
            )
        # And what is there at the end is exactly one entry: this user.
        assert world.acl.acls[str(world.env_file)] == f"D:P(A;;0x1301bf;;;{world.acl.sid})"

    def test_the_claim_is_made_only_after_the_permissions_are_read_back_from_the_last_write(
        self, world: Scenario
    ) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        about_env = [event for event in world.acl.events if event.endswith(" .env")]
        assert about_env[-1] == "read .env" and "replace .env" in about_env
        assert "only your account can read or change it" in world.out

    def test_a_lock_that_leaves_other_accounts_in_is_reported_precisely_and_never_claimed(
        self, world: Scenario
    ) -> None:
        # What the old lock produced: it "succeeded", and SYSTEM and Administrators stayed.
        world.acl.leaves_others = True
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        assert "only your account" not in world.out
        assert "SYSTEM" in world.out and "Administrators" in world.out
        assert "NOT restricted" in world.out

    def test_permissions_that_cannot_be_read_are_not_claimed_either(self, world: Scenario) -> None:
        world.acl.fail_read = "GetNamedSecurityInfoW failed (Windows error 5)"
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        assert "only your account" not in world.out and "could not be read" in world.out

    def test_locking_and_checking_start_no_other_program(self, world: Scenario) -> None:
        world.run(new_chain=True, upn_suffix="sati")
        for tool in ("icacls.exe", "whoami.exe", "powershell.exe"):
            assert not [
                call
                for call in world.spawn.calls
                if call.argv[0].endswith(tool) and "/groups" not in call.argv
            ], tool


class TestLicenceAdvice:
    def _teams_fails(self, world: Scenario, **options: object) -> int:
        checks = [dict(check) for check in PASSING_SMOKE]
        checks[2].update(status="fail", detail="403 Forbidden")
        world.spawn.on(snippet("run_smoke_checks"), _checks(checks))
        return world.run(new_chain=True, upn_suffix="sati", **options)

    def test_no_licence_is_said_plainly_with_who_fixes_it_and_where(self, world: Scenario) -> None:
        world.licence = "none"
        assert self._teams_fails(world) == 3
        finish = world.out[world.out.index("Setup finished") :]
        assert "can sign in" in finish and "no Teams licence" in finish
        assert "https://admin.microsoft.com" in finish and "administrator" in finish
        assert "Run setup again" in finish  # it assigns the licence by itself
        assert "-Diagnose" in finish
        # Waiting does not help when there is no licence to wait for.
        assert "10-15 minutes" not in world.out

    def test_without_a_licence_the_first_thing_to_say_does_not_ask_for_a_teams_message(
        self, world: Scenario
    ) -> None:
        world.licence = "none"
        self._teams_fails(world)
        assert "Then say:  Who are you?" in world.out
        assert "Send me a Teams message" not in world.out

    def test_no_licence_is_flagged_when_the_agent_is_created_too(self, world: Scenario) -> None:
        world.licence = "none"
        self._teams_fails(world)
        created = world.out[world.out.index("Creating your agent") : world.out.index("[7/11]")]
        assert "no Teams licence" in created

    def test_no_licence_is_explained_even_when_the_test_was_skipped(self, world: Scenario) -> None:
        world.licence = "none"
        assert world.run(new_chain=True, upn_suffix="sati", skip_smoke=True) == 0
        assert "no Teams licence" in world.out[world.out.index("To start your agent") - 900 :]

    def test_a_licence_assigned_in_this_run_keeps_the_waiting_advice(self, world: Scenario) -> None:
        world.licence = "assigned"
        assert self._teams_fails(world) == 3
        finish = world.out[world.out.index("Setup finished") :]
        assert "10-15 minutes" in finish and "no Teams licence" not in finish

    def test_a_licence_the_agent_already_had_is_not_called_new(self, world: Scenario) -> None:
        world.licence = "existing"
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        assert "already has a Teams licence" in world.out
        assert "no Teams licence" not in world.out
        assert "a new licence takes" not in world.out  # nothing was just assigned

    @pytest.mark.parametrize(
        ("output", "before", "after", "expected"),
        [
            ("  No Teams-capable licenses found with available seats.", {}, {}, "none"),
            ("  ERROR: No subscribed SKUs found in this tenant", {}, {}, "none"),
            ("  [skip] Agent User already has Teams-capable license: SPE_E5", {}, {}, "existing"),
            ("  [skip] Agent User already has Teams and Work IQ licenses", {}, {}, "existing"),
            ("  Teams/mailbox provisioning can take 10-15 minutes", {}, {}, "assigned"),
            ("", {}, {"AGENT_USER_LICENSE_SKU": "X"}, "assigned"),
            ("", {"AGENT_USER_LICENSE_SKU": "X"}, {"AGENT_USER_LICENSE_SKU": "X"}, "existing"),
            ("", {}, {}, "unknown"),
        ],
    )
    def test_the_outcome_is_read_from_what_the_helper_said(
        self, output: str, before: dict, after: dict, expected: str
    ) -> None:
        assert licence_outcome(output, before, after) == expected


class TestQuietByDefault:
    """The helpers print banners, every ID and permission tables. That belongs in the log."""

    def test_helper_output_stays_off_the_screen_and_goes_to_the_log(self, world: Scenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        for noise in (
            "Permission 12",
            "EntraBot Provisioner bootstrap",
            "Blueprint object id",
            "Created Agent User",
        ):
            assert noise not in world.out
            assert noise in world.log

    def test_each_step_says_in_a_line_what_happened(self, world: Scenario) -> None:
        world.run(new_chain=True, upn_suffix="sati")
        assert "Setup has the access it needs" in world.out
        assert "Created your agent: entrabot-agent-sati@werner.ac" in world.out
        assert "Teams licence assigned (TEAMS_ESSENTIALS)" in world.out

    def test_a_repair_says_the_agent_was_already_there(self, world: Scenario) -> None:
        world.existing()
        world.licence = "existing"
        world.run()
        assert "Your agent: entrabot-agent-sati@werner.ac (already set up)" in world.out
        assert "Created your agent" not in world.out

    def test_joining_says_this_computer_was_connected(self, world: Scenario) -> None:
        world.run(use_blueprint=BP_APP)
        assert "Connected this computer to the agent: entrabot-agent-sati@werner.ac" in world.out

    def test_the_quiet_helpers_still_get_a_heartbeat(self, world: Scenario) -> None:
        world.run(new_chain=True, upn_suffix="sati")
        for helper in ("entra_provisioning.py", "create_entra_agent_ids.py"):
            request = world.ran(helper)[0]
            assert request.silent and request.heartbeat_seconds

    def test_verbose_shows_the_full_stream(self, world: Scenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati", verbose=True) == 0
        assert "Permission 12" in world.out and "Created Agent User" in world.out
        assert not world.ran("create_entra_agent_ids.py")[0].silent

    def test_a_failure_shows_the_last_lines_and_where_the_rest_is(self, world: Scenario) -> None:
        world.spawn.on(
            script("create_entra_agent_ids.py"),
            Reply(
                returncode=1,
                stdout="".join(f"  step {n}\n" for n in range(1, 61)),
                stderr="ERROR: Authorization_RequestDenied\n",
            ),
        )
        assert world.run(new_chain=True, upn_suffix="sati") == 1
        assert "Authorization_RequestDenied" in world.out and "step 60" in world.out
        assert "step 20" not in world.out  # the end of it, not all of it
        assert "setup.log" in world.out  # where the rest is


class TestTheChoicesStep:
    def test_an_unattended_run_says_where_its_answers_came_from(self, world: Scenario) -> None:
        world.run(new_chain=True, upn_suffix="sati")
        choices = world.out[world.out.index("[3/11]") : world.out.index("[4/11]")]
        assert "Using the answers given on the command line" in choices

    def test_an_interactive_run_asks_instead(self, world: Scenario) -> None:
        world.run(typed="\n" * 40)
        assert "Using the answers given on the command line" not in world.out


class TestDirectorySponsors:
    """The sponsor setup records is the sponsor the agent obeys.

    The gap this closes: at runtime the agent takes instructions in Teams only from the
    sponsors on its Agent Identity in the directory. A new agent's only directory sponsor is
    whoever created it, so setup could write "pat" to .env, print "Works for: pat", and
    produce an agent that ignored pat.
    """

    def _works_for(self, world: Scenario) -> str:
        line = next(
            item for item in world.out.splitlines() if item.strip().startswith("Works for:")
        )
        return line.split("Works for:", 1)[1].strip()

    def _review_line(self, world: Scenario) -> str:
        return next(
            item.strip()
            for item in world.out.splitlines()
            if "Will take instructions in Teams from:" in item
        )

    # ── A new agent ──────────────────────────────────────────────────────────

    def test_a_new_agent_whose_sponsor_is_its_creator_needs_nothing_added(
        self, world: Scenario
    ) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out
        sync = world.sponsor_calls()[-1]
        assert sync.argv[4] == "agent-object" and sync.argv[5:] == []  # a read, nothing to add
        assert [item["id"] for item in world.agent_sponsors] == ["admin-object"]
        assert self._works_for(world) == "admin@werner.ac"
        assert self._review_line(world).endswith("from: you (admin@werner.ac)")

    def test_a_different_sponsor_chosen_for_a_new_agent_is_added_in_the_directory(
        self, world: Scenario
    ) -> None:
        code = world.run(new_chain=True, upn_suffix="sati", sponsors=["pat@werner.ac"])
        assert code == 0, world.out
        assert world.sponsor_calls()[-1].argv[5:] == ["pat-object"]
        assert [item["id"] for item in world.agent_sponsors] == ["admin-object", "pat-object"]
        assert "pat@werner.ac (added)" in self._review_line(world)
        assert "admin@werner.ac (you, because you create the agent)" in self._review_line(world)
        assert self._works_for(world) == "pat@werner.ac, admin@werner.ac"
        assert world.dotenv["ENTRABOT_HUMAN_UPN"] == "pat@werner.ac"  # .env as before

    def test_the_creator_is_told_they_are_a_sponsor_too_and_how_to_step_back(
        self, world: Scenario
    ) -> None:
        world.run(new_chain=True, upn_suffix="sati", sponsors=["pat@werner.ac"])
        assert "You (admin@werner.ac) are a sponsor too" in world.out
        assert "scripts\\remove_agent_sponsor.py admin@werner.ac" in world.out
        # Said, never done: setup only ever adds.
        assert {"id": "admin-object"}.items() <= world.agent_sponsors[0].items()

    def test_the_sponsors_are_synced_after_the_agent_exists_and_before_the_certificate(
        self, world: Scenario
    ) -> None:
        world.run(new_chain=True, upn_suffix="sati", sponsors=["pat@werner.ac"])
        calls = world.spawn.calls
        created = next(
            i for i, call in enumerate(calls) if script("create_entra_agent_ids.py")(call)
        )
        synced = next(i for i, call in enumerate(calls) if snippet("_add_sponsor")(call))
        registered = next(
            i for i, call in enumerate(calls) if script("register_windows_blueprint_cert.py")(call)
        )
        assert created < synced < registered
        request = calls[synced]
        assert request.env is not None and request.env["ENTRABOT_TENANT_ID"] == TENANT

    def test_nothing_is_added_before_the_review_screen_is_confirmed(self, world: Scenario) -> None:
        answers = {
            "identity": "new",
            "upn_suffix": "sati",
            "sponsor": "other",
            "sponsor_emails": "pat@werner.ac",
            "start": "no",
        }
        assert world.run(answers=answers) == 130
        assert "pat@werner.ac (added)" in self._review_line(world)
        assert [item["id"] for item in world.agent_sponsors] == ["admin-object"]

    # ── Joining or continuing an existing agent ──────────────────────────────

    def test_continuing_without_naming_a_sponsor_adds_nobody_and_shows_who_is_there(
        self, world: Scenario
    ) -> None:
        world.existing()
        world.agent_sponsors = [dict(ADMIN_SPONSOR), {"id": "pat-object"}]
        assert world.run() == 0, world.out
        assert all(call.argv[5:] == [] for call in world.sponsor_calls())
        assert len(world.agent_sponsors) == 2
        assert self._review_line(world).endswith(
            "from: admin@werner.ac (already), pat@werner.ac (already)"
        )
        assert self._works_for(world) == "admin@werner.ac, pat@werner.ac"

    def test_joining_on_a_new_computer_lists_the_sponsors_once_it_is_connected(
        self, world: Scenario
    ) -> None:
        world.agent_sponsors = [{"id": "pat-object"}]
        assert world.run(use_blueprint=BP_APP) == 0, world.out
        assert "its current sponsors (listed once this computer is connected)" in (
            self._review_line(world)
        )
        assert "Takes instructions in Teams from: pat@werner.ac" in world.out
        assert [item["id"] for item in world.agent_sponsors] == ["pat-object"]  # nobody added
        assert self._works_for(world) == "pat@werner.ac"

    def test_someone_who_runs_setup_but_is_not_a_sponsor_is_told_so(self, world: Scenario) -> None:
        # .env still names whoever ran setup (other code writes to them), but the agent will
        # not take their instructions in Teams. Say it, and say how to change it.
        world.agent_sponsors = [{"id": "pat-object"}]
        world.run(use_blueprint=BP_APP)
        assert "will not take instructions from you" in world.out
        assert "-Sponsor admin@werner.ac" in world.out

    def test_a_named_sponsor_who_is_already_one_is_not_added_again(self, world: Scenario) -> None:
        world.existing()
        world.agent_sponsors = [dict(ADMIN_SPONSOR), {"id": "pat-object"}]
        assert world.run(sponsors=["pat@werner.ac"]) == 0, world.out
        assert "pat@werner.ac (already)" in self._review_line(world)
        assert len(world.agent_sponsors) == 2
        assert "pat@werner.ac is already a sponsor" in world.out

    def test_a_named_sponsor_who_is_missing_is_added_when_continuing(self, world: Scenario) -> None:
        world.existing()
        assert world.run(sponsors=["pat@werner.ac"]) == 0, world.out
        assert "pat@werner.ac (added), admin@werner.ac (already)" in self._review_line(world)
        assert [item["id"] for item in world.agent_sponsors] == ["admin-object", "pat-object"]
        assert self._works_for(world) == "pat@werner.ac, admin@werner.ac"

    def test_the_interactive_question_defaults_to_keeping_the_current_sponsors(
        self, world: Scenario
    ) -> None:
        world.existing()
        world.agent_sponsors = [{"id": "pat-object"}]
        assert world.run(typed="\n" * 40) == 0, world.out
        assert "Keep the current sponsors: pat@werner.ac" in world.out
        assert "Add me (admin@werner.ac)" in world.out
        assert [item["id"] for item in world.agent_sponsors] == ["pat-object"]

    def test_adding_yourself_interactively(self, world: Scenario) -> None:
        world.existing()
        world.agent_sponsors = [{"id": "pat-object"}]
        assert world.run(answers={"sponsor": "me"}) == 0, world.out
        assert [item["id"] for item in world.agent_sponsors] == ["pat-object", "admin-object"]
        assert "admin@werner.ac (added), pat@werner.ac (already)" in self._review_line(world)

    def test_someone_already_a_sponsor_is_not_offered_to_add_themselves(
        self, world: Scenario
    ) -> None:
        world.existing()
        world.run(typed="\n" * 40)
        assert "Add me (admin@werner.ac)" not in world.out

    # ── When it does not work ────────────────────────────────────────────────

    def test_a_sponsor_who_could_not_be_added_is_a_warning_with_the_exact_command(
        self, world: Scenario
    ) -> None:
        world.sponsor_add_fails = {"pat-object": "Failed to add sponsor: 403 Forbidden"}
        code = world.run(new_chain=True, upn_suffix="sati", sponsors=["pat@werner.ac"])
        assert code == 3  # finished, with something to check: never a silent pass
        assert "Setup finished, with something to check" in world.out
        assert "403 Forbidden" in world.out
        assert ".venv\\Scripts\\python.exe scripts\\add_agent_sponsor.py pat@werner.ac" in world.out
        assert self._works_for(world) == "admin@werner.ac"  # the truth, not the wish
        assert "will not take instructions from pat@werner.ac" in world.out

    def test_the_command_for_an_address_a_shell_would_expand_names_the_object_id(
        self, world: Scenario
    ) -> None:
        # "$" is legal in an address. Typed into PowerShell inside the suggested command it
        # would be expanded, and the command would add somebody else.
        dollar = {
            "id": "dollar-object",
            "displayName": "Pat Dollar",
            "userType": "Member",
            "mail": "pat$ops@werner.ac",
            "userPrincipalName": "pat$ops@werner.ac",
            "otherMails": [],
        }
        world.directory.users.append(dollar)
        world.sponsor_add_fails = {"dollar-object": "Failed to add sponsor: 403 Forbidden"}
        code = world.run(new_chain=True, upn_suffix="sati", sponsors=["pat$ops@werner.ac"])
        assert code == 3, world.out
        assert "scripts\\add_agent_sponsor.py dollar-object" in world.out
        assert "add_agent_sponsor.py pat$ops" not in world.out
        assert 'add_agent_sponsor.py "pat$ops' not in world.out

    def test_an_add_the_directory_does_not_confirm_is_something_to_check(
        self, world: Scenario
    ) -> None:
        world.sponsor_adds_stay_invisible = True
        code = world.run(new_chain=True, upn_suffix="sati", sponsors=["pat@werner.ac"])
        assert code == 3
        assert "does not list" in world.out and "list_sponsors.py" in world.out

    def test_a_directory_that_cannot_be_read_never_claims_who_the_agent_works_for(
        self, world: Scenario
    ) -> None:
        world.sponsors_unreadable = "Failed to read sponsors: 403"
        code = world.run(new_chain=True, upn_suffix="sati", sponsors=["pat@werner.ac"])
        assert code == 3
        assert "Failed to read sponsors: 403" in world.out
        assert "add_agent_sponsor.py pat@werner.ac" in world.out
        assert "could not be read from the directory" in self._works_for(world)

    def test_an_unreadable_directory_with_nothing_to_add_is_a_warning_only(
        self, world: Scenario
    ) -> None:
        world.sponsors_unreadable = "Failed to read sponsors: 503"
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        assert "Failed to read sponsors: 503" in world.out

    def test_setup_itself_is_complete_when_only_the_sponsor_needs_attention(
        self, world: Scenario
    ) -> None:
        world.sponsor_add_fails = {"pat-object": "403"}
        world.run(new_chain=True, upn_suffix="sati", sponsors=["pat@werner.ac"])
        assert world.dotenv["ENTRABOT_AGENT_ID"] == "agent-app"
        assert (world.root / ".mcp.json").is_file()


class TestStartingTheAgent:
    """Measured on Windows 11: the official installer puts claude.exe in
    %USERPROFILE%\\.local\\bin and warns that folder is not on PATH, so "run: claude ..." in a
    new terminal fails. The project's start-entrabot.cmd finds Claude Code wherever it is."""

    def _finish(self, world: Scenario) -> str:
        return world.out[world.out.index("To start your agent:") :]

    def test_the_finish_screen_points_at_the_launcher(self, world: Scenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        finish = self._finish(world)
        assert f"Double-click start-entrabot.cmd in {world.root}" in finish
        # The raw command stays, as the second way.
        assert "claude --dangerously-load-development-channels server:entrabot" in finish
        assert finish.index("Double-click") < finish.index("--dangerously-load-development")

    def test_the_raw_command_uses_the_full_path_when_claude_is_not_on_path(
        self, world: Scenario
    ) -> None:
        (world.tmp_path / "tools" / "claude.exe").unlink()  # not on PATH...
        installed = world.tmp_path / "profile" / ".local" / "bin" / "claude.exe"
        installed.parent.mkdir(parents=True)
        installed.write_bytes(b"")  # ...but where the official installer puts it
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        finish = self._finish(world)
        assert f"{installed} --dangerously-load-development-channels server:entrabot" in finish
        assert "terminal there:  claude --dangerously" not in finish

    def test_a_path_with_a_space_is_quoted_in_the_raw_command(self, world: Scenario) -> None:
        (world.tmp_path / "tools" / "claude.exe").unlink()
        world.env["USERPROFILE"] = str(world.tmp_path / "Brandon Werner")
        installed = world.tmp_path / "Brandon Werner" / ".local" / "bin" / "claude.exe"
        installed.parent.mkdir(parents=True)
        installed.write_bytes(b"")
        world.run(new_chain=True, upn_suffix="sati")
        assert f'"{installed}" --dangerously-load-development-channels' in self._finish(world)

    def test_without_the_launcher_the_old_instructions_are_shown(self, world: Scenario) -> None:
        (world.root / "start-entrabot.cmd").unlink()
        assert world.run(typed="\n" * 40) == 0
        finish = self._finish(world)
        assert f"Open a terminal in {world.root} and run:" in finish
        assert "start-entrabot.cmd" not in finish
        assert world.launched == []  # nothing to start it with

    def test_interactively_enter_starts_the_agent_in_its_own_window(self, world: Scenario) -> None:
        assert world.run(typed="\n" * 40) == 0, world.out
        assert "Start your agent now?" in world.out
        ((argv, cwd),) = world.launched
        assert Path(argv[0]).name.lower() == "cmd.exe" and argv[1] == "/c"
        assert argv[2] == str(world.root / "start-entrabot.cmd")
        assert cwd == world.root

    def test_saying_no_starts_nothing(self, world: Scenario) -> None:
        answers = {"identity": "new", "upn_suffix": "sati", "start_agent": "no"}
        assert world.run(answers=answers, typed="\n" * 40) == 0
        assert world.launched == []

    def test_an_unattended_run_never_starts_the_agent(self, world: Scenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        assert world.launched == [] and "Start your agent now?" not in world.out

    def test_a_session_without_a_desktop_is_not_offered_a_window(self, world: Scenario) -> None:
        world.env.update({"SESSIONNAME": "", "SSH_CONNECTION": "10.0.0.4 51000 10.0.0.5 22"})
        assert world.run(typed="\n" * 40) == 0
        assert world.launched == [] and "Start your agent now?" not in world.out

    def test_it_is_not_offered_while_claude_code_is_still_missing(self, world: Scenario) -> None:
        (world.tmp_path / "tools" / "claude.exe").unlink()
        answers = {"identity": "new", "upn_suffix": "sati", "install_hosts": "no"}
        assert world.run(answers=answers, typed="\n" * 40) == 0
        assert world.launched == [] and "Start your agent now?" not in world.out

    def test_it_is_not_offered_when_the_agent_cannot_sign_in(self, world: Scenario) -> None:
        checks = [dict(check) for check in PASSING_SMOKE]
        checks[0].update(status="fail", detail="Hop 1 failed - invalid_client")
        world.spawn.on(snippet("run_smoke_checks"), _checks(checks))
        assert world.run(typed="\n" * 40) == 3
        assert world.launched == [] and "Start your agent now?" not in world.out

    def test_a_window_that_cannot_be_opened_is_a_note_not_a_failure(self, world: Scenario) -> None:
        def broken(argv: list[str], cwd: Path) -> None:
            raise OSError("no window station")

        world.launch_override = broken
        assert world.run(typed="\n" * 40) == 0
        assert "Could not open it" in world.out and "double-click" in world.out.lower()

    def test_a_folder_name_cmd_would_misread_is_not_started_automatically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        odd = tmp_path / "R&D"
        odd.mkdir()
        world = Scenario(odd)
        world.acl.install(monkeypatch)
        assert world.run(typed="\n" * 40) == 0
        assert world.launched == [] and "Start your agent now?" not in world.out
        assert "Double-click start-entrabot.cmd" in world.out

    def test_copilot_only_setups_keep_their_own_instructions(self, world: Scenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati", hosts="copilot") == 0
        assert "Open GitHub Copilot CLI" in self._finish(world)
        assert "start-entrabot.cmd" not in self._finish(world)


class TestClaudeCodeVersion:
    def test_an_old_claude_code_makes_setup_finish_with_something_to_check(
        self, world: Scenario
    ) -> None:
        world.claude_version = "2.1.120 (Claude Code)"
        assert world.run(new_chain=True, upn_suffix="sati") == 3
        assert "Setup finished, with something to check" in world.out
        assert "2.1.120" in world.out and "2.1.295" in world.out
        assert "safety checks are not guaranteed" in world.out
        finish = world.out[world.out.index("To start your agent:") :]
        assert finish.index("claude update") < finish.index("Double-click start-entrabot.cmd")
        assert world.dotenv["ENTRABOT_AGENT_ID"] == "agent-app"  # setup itself is complete

    def test_an_old_claude_code_is_not_offered_a_start(self, world: Scenario) -> None:
        world.claude_version = "2.1.120 (Claude Code)"
        assert world.run(typed="\n" * 40) == 3
        assert world.launched == [] and "Start your agent now?" not in world.out

    def test_a_current_claude_code_is_fine(self, world: Scenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        assert "Claude Code 2.1.300 is installed" in world.out

    def test_a_version_that_cannot_be_read_does_not_fail_setup(self, world: Scenario) -> None:
        world.claude_version = "development build"
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        assert "2.1.295" in world.out
