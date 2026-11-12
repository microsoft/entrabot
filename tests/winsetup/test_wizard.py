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
from entrabot.winsetup.oldkeys import OldSetupKey, old_setup_key_lines, old_setup_keys
from entrabot.winsetup.wizard import (
    NEW_CERT_ATTEMPTS,
    NEW_CERT_RETRY_SECONDS,
    Options,
    Wizard,
    World,
    licence_outcome,
)
from tests.winsetup.fakes import (
    BUSINESS_PLANS,
    COPILOT_PLANS,
    E3_PLANS,
    FakeDirectory,
    FakeLicences,
    FakeSpawn,
    FakeWindowsAcl,
    Reply,
    script,
    snippet,
    tenant_sku,
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
    # ...and with a Microsoft 365 Copilot licence, when Work IQ asks for one.
    copilot: str = "assigned"  # assigned | existing | none
    # The tenant's licences, for the licence walkthrough (create_entra_agent_ids.py's
    # --licence-report and --assign-licence).
    licences: FakeLicences = field(default_factory=FakeLicences)
    work_iq_tools: bool = False  # the a365 CLI and PowerShell 7 are installed
    helper_installs_work_iq: bool = True  # prereqs-windows.ps1 -WithWorkIq installs them
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
    slept: list[float] = field(default_factory=list)  # every wait, in seconds; none is real
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

        def create(request: RunRequest) -> Reply:
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
            if (request.env or {}).get("ENTRABOT_ASSIGN_WORK_IQ_LICENSE") == "1":
                if self.copilot == "assigned":
                    created["AGENT_USER_WORK_IQ_LICENSE_SKU"] = "Microsoft_365_Copilot"
                    chatter += "  [done] Work IQ license assigned: Microsoft_365_Copilot\n"
                else:
                    chatter += {
                        "existing": "  [skip] Agent User already has Work IQ license: x\n",
                        "none": "  No Microsoft 365 Copilot licenses found with available seats.\n",
                    }[self.copilot]
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
        self.licences.state_file = self.state_file
        self.licences.attach(self.spawn)  # after the agent step: the licence-only calls
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

    @property
    def installed_a365(self) -> Path:
        """Where the helper puts the Agent 365 CLI: Entrabot's own folder, no admin rights."""
        return self.tmp_path / "localappdata" / "entrabot" / "tools" / "a365" / "a365.exe"

    @property
    def installed_dotnet(self) -> Path:
        """...and the .NET SDK it runs on, when the PC has none."""
        return self.tmp_path / "localappdata" / "entrabot" / "tools" / "dotnet" / "dotnet.exe"

    @property
    def installed_pwsh(self) -> Path:
        return self.tmp_path / "localappdata" / "Microsoft" / "WindowsApps" / "pwsh.exe"

    def install_helper(self, request: RunRequest) -> Reply:
        """Stands in for scripts\\prereqs-windows.ps1: puts each tool where its installer does."""
        if "-WithClaudeCode" in request.argv:
            exe = self.tmp_path / "profile" / ".local" / "bin" / "claude.exe"
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_bytes(b"")
        if "-WithWorkIq" in request.argv:
            if not self.helper_installs_work_iq:
                return Reply(returncode=1)
            for exe in (self.installed_a365, self.installed_dotnet, self.installed_pwsh):
                exe.parent.mkdir(parents=True, exist_ok=True)
                exe.write_bytes(b"")
        return Reply()

    def which(self, name: str) -> str | None:
        if name == "az" and self.az_on_path:
            return "C:/tools/az"
        claude = self.tmp_path / "tools" / "claude.exe"
        if name == "claude" and claude.exists():
            return str(claude)
        if name in ("a365", "pwsh") and self.work_iq_tools:
            return str(self.tmp_path / "tools" / f"{name}.exe")
        return None

    def with_work_iq_tools(self) -> None:
        """The a365 CLI and PowerShell 7 on PATH, and a tenant where a365's lookups succeed."""
        self.work_iq_tools = True
        for name in ("a365.exe", "pwsh.exe"):
            (self.tmp_path / "tools" / name).write_bytes(b"")
        self.a365_lookups()

    def a365_lookups(self) -> None:
        """The tenant answers the Work IQ step's own lookups (its CLI app, the agent's name)."""
        self.spawn.on(words("ad", "app", "list"), Reply(stdout=json.dumps([{"appId": "cli"}])))
        self.spawn.on(words("ad", "sp", "show"), Reply(stdout=json.dumps({"displayName": "A"})))

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
            sleep=self.slept.append,
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


class TestTheAgentStepKeepsTheBlueprint:
    """Defence in depth. A run that joins a Blueprint by App ID, or continues the one recorded
    on this computer, must come back from the agent step with that same Blueprint. The helper
    once fell back to the first Blueprint with the right display name; had it come back with
    another one, the next step would have registered this computer's certificate on it."""

    LETTERED_BP = "abcdef01-2345-4678-9abc-def012345678"

    @staticmethod
    def _agent_step_records(world: Scenario, blueprint_app_id: str) -> None:
        """create_entra_agent_ids.py succeeds and records *blueprint_app_id* as the agent's."""

        def create(_request: RunRequest) -> Reply:
            current = (
                json.loads(world.state_file.read_text("utf-8")) if world.state_file.exists() else {}
            )
            created = {**CREATED_STATE, "BLUEPRINT_APP_ID": blueprint_app_id}
            world.state_file.write_text(json.dumps({**current, **created}), encoding="utf-8")
            return Reply(stdout=CREATE_CHATTER)

        world.spawn.on(script("create_entra_agent_ids.py"), create)

    @staticmethod
    def _stopped_after_the_agent_step(world: Scenario) -> bool:
        """Was the agent step the last thing setup ran (no certificate, sponsors or Work IQ)?"""
        return world.spawn.calls[-1] is world.ran("create_entra_agent_ids.py")[0]

    def test_joining_one_blueprint_and_getting_another_stops_before_the_certificate(
        self, world: Scenario
    ) -> None:
        self._agent_step_records(world, OTHER_BP)

        assert world.run(use_blueprint=BP_APP) == 1

        assert f"You asked to join the agent with Blueprint {BP_APP}" in world.out
        assert f"returned Blueprint {OTHER_BP}" in world.out
        assert "Nothing further was changed" in world.out
        assert f"-UseBlueprint {BP_APP}" in world.out
        assert self._stopped_after_the_agent_step(world)
        assert not world.wincert.created
        assert not world.env_file.exists()

    def test_continuing_and_getting_another_blueprint_stops_too(self, world: Scenario) -> None:
        world.existing()
        before = world.env_file.read_bytes()
        self._agent_step_records(world, OTHER_BP)

        assert world.run() == 1

        assert f"This computer is set up for the agent with Blueprint {BP_APP}" in world.out
        assert f"returned Blueprint {OTHER_BP}" in world.out
        assert "Nothing further was changed" in world.out
        assert self._stopped_after_the_agent_step(world)
        assert not world.wincert.created
        assert world.env_file.read_bytes() == before

    @pytest.mark.parametrize(
        ("asked", "recorded"),
        [(LETTERED_BP.upper(), LETTERED_BP), (LETTERED_BP, LETTERED_BP.upper())],
        ids=["typed-in-capitals", "recorded-in-capitals"],
    )
    def test_the_same_blueprint_in_other_letter_case_is_the_same_blueprint(
        self, world: Scenario, asked: str, recorded: str
    ) -> None:
        self._agent_step_records(world, recorded)

        assert world.run(use_blueprint=asked) == 0, world.out

        assert world.ran("register_windows_blueprint_cert.py")
        assert world.dotenv["ENTRABOT_BLUEPRINT_APP_ID"] == recorded

    def test_a_new_agent_has_no_blueprint_to_keep(self, world: Scenario) -> None:
        world.existing(state={**CREATED_STATE, "BLUEPRINT_APP_ID": OTHER_BP}, env=OTHER_AGENT_ENV)
        self._agent_step_records(world, BP_APP)

        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out


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

    def test_at_a_desktop_the_person_is_told_their_browser_opens(self, world: Scenario) -> None:
        world.signed_in = False
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        assert "Your browser is opening" in world.out
        assert "sign-in window is opening" not in world.out

    def test_a_sign_in_that_can_no_longer_get_a_token_is_done_again(self, world: Scenario) -> None:
        # Seen on the test machine: az still listed the account, but every token request
        # failed (the session had been revoked). A sign-in made through the Windows account
        # broker looks the same once setup uses the browser.
        def token(_request: RunRequest) -> Reply:
            if not world.az("login"):
                return Reply(returncode=1, stderr="ERROR: InteractionRequired: sign in again")
            return Reply(stdout=json.dumps({"accessToken": "tok"}))

        world.spawn.on(words("account", "get-access-token"), token)
        assert world.run(typed="\n" * 40) == 0, world.out
        assert len(world.az("login")) == 1
        assert "can no longer be used" in world.out
        assert "Use this account?" not in world.out  # there is nothing to choose

    def test_a_sign_in_that_still_works_is_offered_as_before(self, world: Scenario) -> None:
        assert world.run(typed="\n" * 40) == 0, world.out
        assert "Use this account?" in world.out and world.az("login") == []

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

    # How az login ends when nobody used the device code before it ran out (seen verbatim on a
    # real machine). Setup used to answer it with advice about managed devices.
    UNUSED_CODE = (
        "ERROR: AADSTS70016: OAuth 2.0 device flow error. Authorization is pending. Continue "
        "polling. Trace ID: 0b1c Correlation ID: 2d3e Timestamp: 2026-11-12 09:15:00Z"
    )
    CODE_NOT_USED = "The sign-in code was not used in time. Codes last about 15 minutes."
    GET_A_NEW_ONE = "Run setup again to get a new code."

    @pytest.mark.parametrize("code", ["AADSTS70016", "AADSTS70019", "AADSTS70020"])
    def test_a_device_code_nobody_used_in_time_is_said_as_exactly_that(
        self, world: Scenario, code: str
    ) -> None:
        world.signed_in = False
        world.spawn.on(
            words("login"),
            Reply(returncode=1, stderr=self.UNUSED_CODE.replace("AADSTS70016", code)),
        )

        assert world.run(new_chain=True, upn_suffix="sati", device_code=True) == 1

        out = world.out
        assert f"[xx] {self.CODE_NOT_USED}" in out and self.GET_A_NEW_ONE in out
        assert "managed devices" not in out
        assert len(world.az("login")) == 1  # unattended: nobody is there to use another code
        assert not world.mutated()

    def test_interactively_a_new_code_is_offered_at_once(self, world: Scenario) -> None:
        world.signed_in = False
        attempts: list[list[str]] = []

        def login(request: RunRequest) -> Reply:
            attempts.append(request.argv)
            if len(attempts) == 1:
                return Reply(returncode=1, stderr=self.UNUSED_CODE)
            world.signed_in = True
            return Reply()

        world.spawn.on(words("login"), login)

        assert world.run(typed="\n" * 40, device_code=True) == 0, world.out

        assert len(attempts) == 2 and all("--use-device-code" in argv for argv in attempts)
        assert f"[!!] {self.CODE_NOT_USED}" in world.out
        assert "Get a new code and try again? [Y/n]" in world.out
        assert "managed devices" not in world.out

    def test_declining_a_new_code_ends_with_how_to_get_one(self, world: Scenario) -> None:
        world.signed_in = False
        world.spawn.on(words("login"), Reply(returncode=1, stderr=self.UNUSED_CODE))

        # Enter for a new agent and for its name, then "n" when a new code is offered.
        assert world.run(typed="\n\nn\n", device_code=True) == 1

        assert "Get a new code and try again? [Y/n]" in world.out
        assert self.GET_A_NEW_ONE in world.out and "managed devices" not in world.out
        assert len(world.az("login")) == 1 and not world.mutated()

    def test_a_code_that_runs_out_after_the_browser_failed_is_said_the_same_way(
        self, world: Scenario
    ) -> None:
        world.signed_in = False
        world.spawn.on(words("login"), Reply(returncode=1, stderr=self.UNUSED_CODE))

        assert world.run(new_chain=True, upn_suffix="sati") == 1

        logins = world.az("login")
        assert len(logins) == 2 and "--use-device-code" in logins[1].argv
        assert self.CODE_NOT_USED in world.out and "managed devices" not in world.out

    def test_any_other_failed_sign_in_keeps_the_managed_device_advice(
        self, world: Scenario
    ) -> None:
        world.signed_in = False
        world.spawn.on(
            words("login"),
            Reply(returncode=1, stderr="ERROR: AADSTS50097: Device authentication is required."),
        )

        assert world.run(new_chain=True, upn_suffix="sati", device_code=True) == 1

        assert "Sign-in did not complete." in world.out
        assert "only allows sign-in from managed devices" in world.out
        assert "not used in time" not in world.out

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

    # scripts/entra_provisioning.py prints this, on a line of its own, when it would not take
    # over an existing app registration with the Provisioner's name
    # (tests/scripts/test_entra_provisioning.py pins the same text on that side).
    TAKEOVER_REFUSED = "ENTRABOT_RESULT=provisioner-takeover-refused"

    def test_a_refused_takeover_says_what_exists_and_how_to_name_the_right_app(
        self, world: Scenario
    ) -> None:
        candidate = "11111111-2222-3333-4444-555555555555"
        world.spawn.on(
            script("entra_provisioning.py"),
            Reply(
                returncode=1,
                stdout=(
                    f"{self.TAKEOVER_REFUSED}\n"
                    "  An app registration named 'EntraBot Agent ID Provisioner' already "
                    "exists, and this\n"
                    f"    - 'EntraBot Agent ID Provisioner' (application ID {candidate})\n"
                    "      created 2026-03-04, owners: mallory@contoso.com\n"
                    "ERROR: Setup did not take over an existing 'EntraBot Agent ID "
                    "Provisioner' app registration (see above)\n"
                ),
            ),
        )

        assert world.run(new_chain=True, upn_suffix="sati") == 1

        out = world.out
        assert "already exists in this tenant" in out and "would not take it over" in out
        assert "could not get the access it needs" not in out
        assert candidate in out, "what the helper found is on screen"
        assert 'set "ENTRABOT_PROVISIONER_APP_ID_VERIFIED=<application ID>"' in out  # cmd.exe
        assert "$env:ENTRABOT_PROVISIONER_APP_ID_VERIFIED = '<application ID>'" in out
        assert "delete or rename" in out
        assert world.ran("create_entra_agent_ids.py") == []

    def test_only_the_marker_line_means_a_refused_takeover(self, world: Scenario) -> None:
        # The words alone, or the marker anywhere but at the start of a line, are some
        # other failure and keep the usual message.
        world.spawn.on(
            script("entra_provisioning.py"),
            Reply(
                returncode=1,
                stdout="  Setup did not take over an existing app registration.\n"
                f"  echo {self.TAKEOVER_REFUSED}\n",
            ),
        )

        assert world.run(new_chain=True, upn_suffix="sati") == 1

        assert "could not get the access it needs" in world.out
        assert "ENTRABOT_PROVISIONER_APP_ID_VERIFIED" not in world.out

    # The same script prints these the same way, just before its error, when the app named in
    # ENTRABOT_PROVISIONER_APP_ID_VERIFIED is not accepted, and when it could not read what it
    # needed from Microsoft Entra (tests/scripts/test_entra_provisioning.py pins them there).
    APP_CHOICE_REJECTED = "ENTRABOT_RESULT=provisioner-app-choice-rejected"
    COULD_NOT_CHECK = "ENTRABOT_RESULT=provisioner-could-not-check"

    def test_a_rejected_app_choice_says_so_and_how_to_correct_or_clear_it(
        self, world: Scenario
    ) -> None:
        world.spawn.on(
            script("entra_provisioning.py"),
            Reply(
                returncode=1,
                stdout=(
                    f"{PROVISION_CHATTER}{self.APP_CHOICE_REJECTED}\n"
                    "ERROR: ENTRABOT_PROVISIONER_APP_ID_VERIFIED is set to "
                    f"{OTHER_BP}, but no app registration in this tenant has that application "
                    "ID. Nothing was changed in Microsoft Entra.\n"
                ),
            ),
        )

        assert world.run(new_chain=True, upn_suffix="sati") == 1

        out = world.out
        assert (
            "The value given in ENTRABOT_PROVISIONER_APP_ID_VERIFIED was not accepted. Nothing "
            "was changed in Microsoft Entra." in out
        )
        assert "no app registration in this tenant has that application ID" in out  # why
        assert "The reason is in the lines above" in out
        assert 'cmd.exe:     set "ENTRABOT_PROVISIONER_APP_ID_VERIFIED=<application ID>"' in out
        assert "PowerShell:  $env:ENTRABOT_PROVISIONER_APP_ID_VERIFIED = '<application ID>'" in out
        assert 'cmd.exe:     set "ENTRABOT_PROVISIONER_APP_ID_VERIFIED="' in out  # cleared
        assert "PowerShell:  Remove-Item Env:ENTRABOT_PROVISIONER_APP_ID_VERIFIED" in out
        assert "could not get the access it needs" not in out
        assert "would not take it over" not in out
        assert "ENTRABOT_RESULT" not in out  # the marker is for setup, not for people
        assert world.ran("create_entra_agent_ids.py") == []

    def test_a_directory_that_could_not_be_read_is_not_blamed_on_admin_rights(
        self, world: Scenario
    ) -> None:
        world.spawn.on(
            script("entra_provisioning.py"),
            Reply(
                returncode=1,
                stdout=(
                    f"{self.COULD_NOT_CHECK}\n"
                    "ERROR: Could not look for an existing 'EntraBot Agent ID Provisioner' app "
                    "registration (ERROR: Request_Throttled). Nothing was changed in Microsoft "
                    "Entra.\n"
                ),
            ),
        )

        assert world.run(new_chain=True, upn_suffix="sati") == 1

        out = world.out
        assert (
            "Setup could not read what it needed from Microsoft Entra. Nothing was changed in "
            "Microsoft Entra." in out
        )
        assert "Request_Throttled" in out  # what Entra said, from the lines above
        assert "usually temporary, or a problem with the sign-in or an admin role" in out
        assert "Run setup again" in out
        assert "could not get the access it needs" not in out
        assert "needs an administrator" not in out
        assert "ENTRABOT_RESULT" not in out
        assert world.ran("create_entra_agent_ids.py") == []

    def test_a_result_setup_does_not_know_keeps_the_administrator_message(
        self, world: Scenario
    ) -> None:
        world.spawn.on(
            script("entra_provisioning.py"),
            Reply(returncode=1, stdout="ENTRABOT_RESULT=something-new\nERROR: boom\n"),
        )

        assert world.run(new_chain=True, upn_suffix="sati") == 1

        assert "could not get the access it needs" in world.out
        assert "ENTRABOT_RESULT" not in world.out

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
        assert not world.mutated()
        # The fix is the repo's helper, which installs what is missing; never a download page.
        assert "scripts\\prereqs-windows.ps1 -WithWorkIq" in world.out
        assert "dotnet.microsoft.com" not in world.out
        assert not any("-WithWorkIq" in call.argv for call in world.spawn.calls)  # unattended


class TestTheAgentStepSaysWhyItStopped:
    """scripts/create_entra_agent_ids.py prints one of these, alone on a line, just before it
    stops. Pinned here as the wizard reads them; that script's own tests pin them on its
    side. The exit code stays what it was for any failed agent step: 1."""

    NOT_FOUND = "ENTRABOT_RESULT=blueprint-not-found"
    COULD_NOT_CHECK = "ENTRABOT_RESULT=blueprint-could-not-check"
    AMBIGUOUS = "ENTRABOT_RESULT=blueprint-ambiguous"
    WRONG_BLUEPRINT = "ENTRABOT_RESULT=agent-user-wrong-blueprint"
    AGENT_COULD_NOT_CHECK = "ENTRABOT_RESULT=agent-lookup-could-not-check"

    @staticmethod
    def _stops(world: Scenario, marker: str, explanation: str = "  ERROR: why it stopped") -> None:
        world.spawn.on(
            script("create_entra_agent_ids.py"),
            Reply(returncode=1, stdout=f"{CREATE_CHATTER}{marker}\n{explanation}\n"),
        )

    @staticmethod
    def _stopped_there(world: Scenario) -> None:
        """What every one of these stops has in common with the generic one."""
        out = world.out
        assert "The details are in the lines above and in the setup log." in out
        assert "setup.log" in out
        assert "The agent could not be created." not in out  # replaced, not added to
        assert "ENTRABOT_RESULT" not in out
        assert not world.ran("register_windows_blueprint_cert.py")
        assert not world.wincert.created

    def test_a_blueprint_app_id_that_does_not_exist_here(self, world: Scenario) -> None:
        self._stops(world, self.NOT_FOUND, f"  ERROR: Blueprint {BP_APP} does not exist")

        assert world.run(use_blueprint=BP_APP) == 1

        out = world.out
        assert (
            f"The Blueprint App ID you gave ({BP_APP}) does not exist in this tenant (Werner)."
            in out
        )
        assert "Check the Blueprint App ID and that you signed in to the agent's tenant" in out
        assert "-UseBlueprint" in out and "-NewChain" in out  # join the right one, or start over
        self._stopped_there(world)

    def test_a_recorded_blueprint_that_does_not_exist_here(self, world: Scenario) -> None:
        world.existing()
        self._stops(world, self.NOT_FOUND)

        assert world.run() == 1

        assert (
            f"The Blueprint App ID recorded on this computer ({BP_APP}) does not exist in this "
            "tenant (Werner)." in world.out
        )
        self._stopped_there(world)

    def test_a_blueprint_lookup_that_could_not_be_read(self, world: Scenario) -> None:
        # The helper asks about the Blueprint before it records or creates anything.
        self._stops(world, self.COULD_NOT_CHECK, "  ERROR: Microsoft Graph answered HTTP 503")

        assert world.run(new_chain=True, upn_suffix="sati") == 1

        out = world.out
        assert (
            "Setup could not read what it needed from Microsoft Entra. Nothing was changed." in out
        )
        assert "HTTP 503" in out  # what Entra said, from the lines above
        assert "This is usually temporary: run setup again in a few minutes." in out
        self._stopped_there(world)

    def test_an_agent_lookup_that_could_not_be_read_does_not_claim_nothing_changed(
        self, world: Scenario
    ) -> None:
        # The Agent Identity and Agent User lookups run after the Blueprint is recorded (or,
        # for a new agent, created), and the Agent User's after a new Agent Identity may have
        # been created. The helper's own lines say which.
        self._stops(
            world,
            self.AGENT_COULD_NOT_CHECK,
            "  ERROR: Could not check whether the Agent User exists: Microsoft Graph answered "
            "HTTP 503.\n  No Agent User was created, and no permission or licence was granted.",
        )

        assert world.run(new_chain=True, upn_suffix="sati") == 1

        out = world.out
        assert "[xx] Setup could not read what it needed from Microsoft Entra.\n" in out
        assert "Nothing was changed" not in out
        assert "No Agent User was created" in out  # the helper's own account, from above
        assert "The lines above say what was and was not created." in out
        assert "This is usually temporary: run setup again in a few minutes." in out
        assert "It continues from where it stopped." in out
        self._stopped_there(world)

    def test_several_applications_with_the_agents_name(self, world: Scenario) -> None:
        self._stops(
            world,
            self.AMBIGUOUS,
            f"    App ID {BP_APP}   created 2026-01-02\n    App ID {OTHER_BP}   created 2026-03-04",
        )

        assert world.run(new_chain=True, upn_suffix="sati") == 1

        out = world.out
        assert "Several applications in this tenant have the agent's name" in out
        assert "none is recorded on this computer" in out
        assert BP_APP in out and OTHER_BP in out  # the candidates, from the lines above
        assert "scripts\\setup-windows.cmd -UseBlueprint <App ID>" in out
        self._stopped_there(world)

    def test_an_agent_name_that_belongs_to_another_agent(self, world: Scenario) -> None:
        self._stops(world, self.WRONG_BLUEPRINT)

        assert world.run(new_chain=True, upn_suffix="sati") == 1

        out = world.out
        assert "The name chosen for the agent already belongs to a different agent." in out
        assert "-UseBlueprint" in out and "-UpnSuffix" in out  # join that one, or another name
        self._stopped_there(world)

    def test_a_stop_without_a_marker_keeps_the_general_message(self, world: Scenario) -> None:
        world.spawn.on(
            script("create_entra_agent_ids.py"),
            Reply(returncode=1, stdout="ENTRABOT_RESULT=something-new\nERROR: boom\n"),
        )

        assert world.run(new_chain=True, upn_suffix="sati") == 1

        assert "The agent could not be created." in world.out
        assert "boom" in world.out and "ENTRABOT_RESULT" not in world.out


class TestABlueprintThatDoesNotExistIsNotKept:
    """Seen on a real machine: a run asked to join a Blueprint App ID that does not exist and
    stopped at the agent step, as it should, but the App ID stayed recorded in the state file.
    The next run, with the right App ID, was then a "switch", and warned that this machine's
    certificate on the previous Blueprint was not revoked. There never was one."""

    NOT_REVOKED = "This machine's certificate on the previous Blueprint is NOT revoked."

    @staticmethod
    def _only_exists(world: Scenario, blueprint: str) -> None:
        """The agent step finds *blueprint* and no other. For any other it stops the way
        create_entra_agent_ids.py does: before it writes anything."""

        def create(_request: RunRequest) -> Reply:
            current = json.loads(world.state_file.read_text("utf-8"))
            recorded = current.get("BLUEPRINT_APP_ID", "")
            if recorded.lower() != blueprint.lower():
                return Reply(
                    returncode=1,
                    stdout=f"ENTRABOT_RESULT=blueprint-not-found\n"
                    f"  ERROR: Blueprint {recorded} does not exist in this tenant.\n",
                )
            created = {**CREATED_STATE, "BLUEPRINT_APP_ID": blueprint}
            world.state_file.write_text(json.dumps({**current, **created}), encoding="utf-8")
            return Reply(stdout=CREATE_CHATTER)

        world.spawn.on(script("create_entra_agent_ids.py"), create)

    @staticmethod
    def _provisioning_records(world: Scenario, **values: str) -> None:
        """entra_provisioning.py records what it set up in the state file, as it does."""

        def provision(_request: RunRequest) -> Reply:
            current = (
                json.loads(world.state_file.read_text("utf-8")) if world.state_file.exists() else {}
            )
            world.state_file.write_text(json.dumps({**current, **values}), encoding="utf-8")
            return Reply(stdout=PROVISION_CHATTER)

        world.spawn.on(script("entra_provisioning.py"), provision)

    def test_the_next_run_with_the_right_app_id_is_not_a_switch(self, world: Scenario) -> None:
        self._only_exists(world, BP_APP)
        self._provisioning_records(world, PROVISIONER_CLIENT_ID="prov-app", TENANT_ID=TENANT)

        assert world.run(use_blueprint=OTHER_BP) == 1
        assert f"({OTHER_BP}) does not exist" in world.out
        assert "this computer's record of its agent is as it was before this run" in world.out
        # What the provisioning step recorded stays; the App ID that was not found does not.
        recorded = json.loads(world.state_file.read_text("utf-8"))
        assert recorded == {"PROVISIONER_CLIENT_ID": "prov-app", "TENANT_ID": TENANT}

        assert world.run(use_blueprint=BP_APP) == 0, world.out
        assert "Switching agents" not in world.out and self.NOT_REVOKED not in world.out
        assert not list(world.root.glob(".entrabot-state.json.bak.*"))

    def test_a_switch_to_one_that_does_not_exist_leaves_this_computer_on_its_agent(
        self, world: Scenario
    ) -> None:
        world.existing(state={**CREATED_STATE, "PROVISIONER_CERT_THUMBPRINT": "old-setup-key"})
        env_before = world.env_file.read_bytes()
        self._only_exists(world, BP_APP)
        # In the same run the provisioning step moved this computer to a new setup key.
        self._provisioning_records(world, PROVISIONER_CERT_THUMBPRINT="new-setup-key")

        assert world.run(use_blueprint=OTHER_BP) == 1

        assert "Switching agents" in world.out  # said before the agent step looked it up
        assert json.loads(world.state_file.read_text("utf-8")) == {
            **CREATED_STATE,
            "PROVISIONER_CERT_THUMBPRINT": "new-setup-key",
        }
        assert world.env_file.read_bytes() == env_before
        assert not world.wincert.created

    def test_a_blueprint_that_could_not_be_checked_stays_recorded_for_the_next_try(
        self, world: Scenario
    ) -> None:
        # "Could not check" is not "does not exist": the next run asks about the same one.
        world.spawn.on(
            script("create_entra_agent_ids.py"),
            Reply(returncode=1, stdout="ENTRABOT_RESULT=blueprint-could-not-check\n"),
        )

        assert world.run(use_blueprint=OTHER_BP) == 1

        assert json.loads(world.state_file.read_text("utf-8"))["BLUEPRINT_APP_ID"] == OTHER_BP

    def test_a_switch_away_from_this_computers_certificate_still_warns_about_it(
        self, world: Scenario
    ) -> None:
        world.existing(
            state={**CREATED_STATE, "BLUEPRINT_APP_ID": OTHER_BP},
            env=[
                *OTHER_AGENT_ENV,
                f"ENTRABOT_BLUEPRINT_APP_ID={OTHER_BP}",
                f"ENTRABOT_BLUEPRINT_CERT_SHA1={'D' * 40}",
            ],
        )

        assert world.run(use_blueprint=BP_APP) == 0, world.out

        assert "Switching agents. Backed up the previous record to .entrabot-state.json.bak." in (
            world.out
        )
        assert (
            f"{self.NOT_REVOKED} Ask an administrator to remove it if this machine should lose "
            "access to that agent." in world.out
        )

    def test_a_switch_away_from_an_agent_without_one_says_only_that_it_was_backed_up(
        self, world: Scenario
    ) -> None:
        world.existing(state={**CREATED_STATE, "BLUEPRINT_APP_ID": OTHER_BP}, env=OTHER_AGENT_ENV)

        assert world.run(use_blueprint=BP_APP) == 0, world.out

        assert "Switching agents. Backed up the previous record to" in world.out
        assert "NOT revoked" not in world.out


# What preflight's token check says when Microsoft Entra does not know the certificate that
# signed the sign-in, as seen on a real machine moments after setup registered it (the
# identifiers here are made up). Entra echoes the full thumbprint back.
NOT_ACCEPTED_YET = (
    "hop1:blueprint failed — invalid_client: AADSTS700027: The certificate with identifier "
    "used to sign the client assertion is not registered on application. [Reason - The key was "
    f"not found., Thumbprint of key used by client: '{'C' * 40}', Please visit the Azure Portal, "
    "Graph Explorer or directly use MS Graph to see configured keys for app Id "
    f"'{BP_APP}'.] Trace ID: 0b1c Correlation ID: 2d3e Timestamp: 2026-11-12 09:00:00Z"
)


class TestANewCertificateIsGivenTimeToBeAccepted:
    """Seen on a real machine: the certificate step registered this computer's new certificate
    (its helper read it back from Microsoft Graph), and the end-to-end test straight after
    failed at hop 1 with AADSTS700027. Microsoft Entra's sign-in can lag behind Graph by tens of
    seconds; setup run again a minute later passed. Certificate rotation waits for exactly
    this. Setup now does too, but only when this run registered a certificate, and only for
    that answer at hop 1."""

    @staticmethod
    def _answers(world: Scenario, *details: str | None) -> None:
        """The end-to-end test fails at sign-in with each detail in turn (None: everything
        passes). The last answer repeats."""
        queue = list(details)

        def smoke(_request: RunRequest) -> Reply:
            detail = queue.pop(0) if len(queue) > 1 else queue[0]
            if detail is None:
                return _checks(PASSING_SMOKE)
            checks = [dict(check) for check in PASSING_SMOKE]
            checks[0].update(status="fail", detail=detail)
            for later in checks[1:]:
                later.update(status="skip", detail="Skipped - token mint failed.")
            return _checks(checks)

        world.spawn.on(snippet("run_smoke_checks"), smoke)

    @staticmethod
    def _tests_run(world: Scenario) -> int:
        return len([call for call in world.spawn.calls if snippet("run_smoke_checks")(call)])

    @staticmethod
    def _with_this_computers_certificate(world: Scenario, *, on_the_blueprint: bool) -> None:
        from tests.winsetup.test_certstep import OLD_SHA1, OLD_X5T, StoreCert

        world.existing(
            env=[
                "ENTRABOT_AGENT_USER_ID=agent-user",
                f"ENTRABOT_BLUEPRINT_CERT_SHA1={OLD_SHA1}",
                f"ENTRABOT_BLUEPRINT_CERT_THUMBPRINT={OLD_X5T}",
            ]
        )
        world.wincert.store[OLD_SHA1] = StoreCert(OLD_SHA1, OLD_X5T, b"\x01")
        world.spawn.on(
            script("verify_blueprint_cert.py"), Reply(returncode=0 if on_the_blueprint else 1)
        )

    def test_two_answers_of_not_yet_then_a_sign_in_is_a_normal_finish(
        self, world: Scenario
    ) -> None:
        self._answers(world, NOT_ACCEPTED_YET, NOT_ACCEPTED_YET, None)

        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out

        assert self._tests_run(world) == 3
        assert world.slept == [NEW_CERT_RETRY_SECONDS] * 2
        step = world.out[world.out.index("Testing the agent end to end") :]
        assert "Microsoft Entra has not started accepting the certificate just registered" in step
        assert f"Trying again in 10s (attempt 2 of {NEW_CERT_ATTEMPTS})" in step
        assert f"Trying again in 10s (attempt 3 of {NEW_CERT_ATTEMPTS})" in step
        assert "[ok] The agent can sign in" in step
        assert "The agent can sign in: no" not in world.out  # only the real result is reported
        assert "Entrabot is set up" in world.out

    def test_one_never_accepted_is_reported_after_about_two_minutes(self, world: Scenario) -> None:
        self._answers(world, NOT_ACCEPTED_YET)

        assert world.run(new_chain=True, upn_suffix="sati") == 3

        assert self._tests_run(world) == NEW_CERT_ATTEMPTS
        assert world.slept == [NEW_CERT_RETRY_SECONDS] * (NEW_CERT_ATTEMPTS - 1)
        assert 90 <= sum(world.slept) <= 120  # the order of what rotation allows
        finish = world.out[world.out.index("Setup finished, with something to check") :]
        assert (
            "The agent could not sign in yet: this computer's certificate was registered on the "
            "agent's Blueprint moments ago, and Microsoft Entra has not started accepting it "
            "yet." in finish
        )
        assert (
            "Run setup again in a few minutes. It continues from here and keeps what already "
            "works." in finish
        )
        assert "This is not a licence delay" not in world.out
        assert "C" * 40 not in world.out  # only the end of the echoed thumbprint is shown

    def test_this_computers_own_certificate_just_added_to_the_blueprint_gets_the_wait_too(
        self, world: Scenario
    ) -> None:
        self._with_this_computers_certificate(world, on_the_blueprint=False)
        self._answers(world, NOT_ACCEPTED_YET, None)

        assert world.run() == 0, world.out

        assert world.ran("register_windows_blueprint_cert.py") and not world.wincert.created
        assert self._tests_run(world) == 2 and world.slept == [NEW_CERT_RETRY_SECONDS]

    def test_without_a_registration_in_this_run_the_same_answer_is_reported_at_once(
        self, world: Scenario
    ) -> None:
        self._with_this_computers_certificate(world, on_the_blueprint=True)
        self._answers(world, NOT_ACCEPTED_YET)

        assert world.run() == 3

        assert world.ran("register_windows_blueprint_cert.py") == []
        assert self._tests_run(world) == 1 and world.slept == []
        assert "This is not a licence delay" in world.out
        assert "has not started accepting" not in world.out

    @pytest.mark.parametrize(
        "detail",
        [
            "hop1:blueprint failed — invalid_client: AADSTS7000215: Invalid client secret.",
            "hop1:blueprint failed — invalid_client: AADSTS700024: Client assertion is not "
            "within its valid time range.",
            "hop2:agent_identity failed — invalid_client: AADSTS700027: Client assertion "
            "failed signature validation.",
            "hop1:blueprint failed — invalid_client: The certificate with identifier used to "
            "sign the client assertion is not registered on application.",
        ],
        ids=["another-code", "clock-skew", "the-code-at-hop-2", "the-words-without-the-code"],
    )
    def test_any_other_sign_in_failure_is_reported_at_once(
        self, world: Scenario, detail: str
    ) -> None:
        self._answers(world, detail)

        assert world.run(new_chain=True, upn_suffix="sati") == 3

        assert self._tests_run(world) == 1 and world.slept == []
        assert "This is not a licence delay" in world.out

    def test_an_agent_whose_certificate_is_not_accepted_yet_is_not_offered_a_start(
        self, world: Scenario
    ) -> None:
        self._answers(world, NOT_ACCEPTED_YET)

        assert world.run(typed="\n" * 40) == 3

        assert world.launched == [] and "Start your agent now?" not in world.out


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
        assert todo.index("1. Double-click start-entrabot.cmd") < todo.index("3. Then say")

    def test_a_licence_just_assigned_is_explained(self, world: Scenario) -> None:
        world.run(new_chain=True, upn_suffix="sati")
        assert "Teams licence assigned (Teams Essentials)" in world.out
        assert "10-15 minutes" in world.out
        # The product by a name, never only its part number; the log keeps the part number.
        assert "TEAMS_ESSENTIALS" not in world.out and "TEAMS_ESSENTIALS" in world.log

    def test_a_copilot_licence_just_assigned_is_said_without_its_part_number(
        self, world: Scenario
    ) -> None:
        world.with_work_iq_tools()
        assert world.run(new_chain=True, upn_suffix="sati", configure_work_iq=True) == 0
        assert "[ok] Microsoft 365 Copilot licence assigned" in world.out
        assert "Microsoft_365_Copilot" not in world.out and "Microsoft_365_Copilot" in world.log


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

    def test_work_iq_chosen_without_its_tools_and_nobody_to_ask_is_skipped_not_fatal(
        self, world: Scenario
    ) -> None:
        # Answers but no keyboard: nothing is installed without being asked, setup carries on,
        # and the last screen says it was asked for and is off.
        answers = {"identity": "new", "upn_suffix": "sati", "work_iq": "yes"}
        assert world.run(answers=answers) == 3
        assert "stay off for now" in world.out
        assert not any("-WithWorkIq" in call.argv for call in world.spawn.calls)
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
        world.with_work_iq_tools()
        a365 = world.tmp_path / "tools" / "a365.exe"
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

    def test_a_failed_work_iq_step_never_stops_the_rest_of_setup(self, world: Scenario) -> None:
        # From a real run over SSH: 'a365 setup requirements' could not sign in and setup
        # stopped at step 6, with no certificate, no settings and nothing registered; running
        # it again hit the same wall.
        world.with_work_iq_tools()
        world.spawn.on(words("ad", "app", "list"), Reply(stdout="[]"))  # no Agent 365 CLI app
        assert world.run(new_chain=True, upn_suffix="sati", configure_work_iq=True) == 3
        assert world.wincert.created and world.env_file.exists()
        assert (world.root / ".mcp.json").exists()
        last = _flat(_last_screen(world))
        assert last.startswith("Setup finished, with something to check")
        assert "Word, Excel and OneDrive files were asked for, but they are not switched on" in last
        assert "Agent 365 CLI" in last  # what was missing, in setup's own words
        things = last[last.index("Things to try next") : last.index("Later:")]
        assert "Create a Word document" not in things
        assert "were asked for but are not switched on yet" in things
        # The failed attempt left nothing behind that says Work IQ is on.
        assert not (world.root / "ToolingManifest.json").exists()
        assert not (world.root / "a365.config.json").exists()

    def test_a_sign_in_failure_is_said_in_plain_words_with_the_log(self, world: Scenario) -> None:
        world.with_work_iq_tools()
        world.spawn.on(words("setup", "permissions"), Reply(returncode=1, stdout=MSAL_OUTPUT))
        assert world.run(new_chain=True, upn_suffix="sati", configure_work_iq=True) == 3
        last = _last_screen(world)
        assert "The Agent 365 tool could not sign in to Microsoft." in _flat(last)
        assert "at this PC's own desktop" in _flat(last)
        assert "The details are in the setup log:" in last and "setup.log" in last
        assert "MsalAuthenticationFailedException" not in last
        assert "MsalAuthenticationFailedException" in world.log

    def test_the_work_iq_steps_wait_until_the_agent_is_set_up_and_tested(
        self, world: Scenario
    ) -> None:
        # Nothing optional sits in front of the steps the agent needs.
        world.with_work_iq_tools()
        a365 = str(world.tmp_path / "tools" / "a365.exe")
        assert world.run(new_chain=True, upn_suffix="sati", configure_work_iq=True) == 0
        calls = world.spawn.calls
        smoke = next(i for i, call in enumerate(calls) if snippet("run_smoke_checks")(call))
        first = next(i for i, call in enumerate(calls) if call.argv[0] == a365)
        assert smoke < first
        assert world.out.index("[11/11]") < world.out.index("Configuring Microsoft Agent 365")


MSAL_OUTPUT = (
    "ERROR: MSAL authentication failed: Unknown Status: Unexpected Error: 0xffffffff800703f0\n"
    "ERROR: Interactive authentication failed: Failed to acquire token\n"
    "ERROR: Exception type: Microsoft.Agents.A365.DevTools.Cli.Services."
    "MsalAuthenticationFailedException\n"
)


class TestWorkIqNeedsTheDesktop:
    """From a real run over SSH: the a365 sign-in window cannot open in a session without a
    desktop. Setup no longer tries there, and says so before offering the install."""

    def _over_ssh(self, world: Scenario) -> None:
        world.env["SESSIONNAME"] = ""
        world.env["SSH_CONNECTION"] = "10.0.0.2 50000 10.0.0.1 22"

    def test_the_tools_are_installed_but_nothing_is_configured(self, world: Scenario) -> None:
        self._over_ssh(world)
        answers = {"identity": "new", "upn_suffix": "sati", "work_iq": "yes"}
        assert world.run(answers=answers, typed="\n" * 40) == 3, world.out
        out = world.out
        said = out.index("can only be switched on from this PC's own desktop")
        assert said < out.index("Install them now?")  # known before agreeing to the install
        assert "so that switching them on later at the desktop is quick" in _flat(out)
        assert any("-WithWorkIq" in call.argv for call in world.spawn.calls)
        assert not [call for call in world.spawn.calls if "a365" in Path(call.argv[0]).name]
        assert "Work IQ:     no: only from this PC's own desktop" in out
        last = _flat(_last_screen(world))
        assert "they can only be switched on from this PC's own desktop" in last
        assert "Remote Desktop" in last
        assert "answering yes to the Word, Excel and OneDrive question" in last
        assert "Their tools are installed, so that run is quick." in last
        things = last[last.index("Things to try next") : last.index("Later:")]
        assert "Create a Word document" not in things
        # Its Copilot licence waits for the run that switches them on.
        request = world.ran("create_entra_agent_ids.py")[0]
        assert request.env is not None and "ENTRABOT_ASSIGN_WORK_IQ_LICENSE" not in request.env
        assert WALKTHROUGH not in out

    def test_declined_the_last_screen_has_the_install_command_too(self, world: Scenario) -> None:
        self._over_ssh(world)
        answers = {
            "identity": "new",
            "upn_suffix": "sati",
            "work_iq": "yes",
            "install_work_iq": "no",
        }
        assert world.run(answers=answers, typed="\n" * 40) == 3
        last = _flat(_last_screen(world))
        assert "can only be switched on from this PC's own desktop" in last
        assert workiq_command() in last and "so that run is quick" not in last

    def test_unattended_with_the_tools_there_it_is_said_at_the_end(self, world: Scenario) -> None:
        self._over_ssh(world)
        world.with_work_iq_tools()
        assert world.run(new_chain=True, upn_suffix="sati", configure_work_iq=True) == 3
        assert not [call for call in world.spawn.calls if "a365" in Path(call.argv[0]).name]
        assert "can only be switched on from this PC's own desktop" in _flat(_last_screen(world))


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
        out = world.out
        assert "no Teams licence" in out[out.index("Entrabot is set up") : out.index("To start")]

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


class TestNoLicenceNeverClaimsASignIn:
    """Seen on a real machine, a line apart on the finish screen: "The agent could not sign in.
    This is not a licence delay." and "The agent is set up and can sign in, but it has no Teams
    licence". The licence note says the agent can sign in only when the test saw it do so."""

    LICENCE_ONLY = (
        "The agent is set up and can sign in, but it has no Teams licence, so it cannot use "
        "Teams yet."
    )
    ALSO = "The agent also has no Teams licence, so it cannot use Teams yet."
    WHAT_TO_DO = "Run setup again. It gives the agent the licence by itself."

    @staticmethod
    def _finish(world: Scenario) -> str:
        out = world.out
        start = max(
            out.rfind("Setup finished, with something to check"), out.rfind("Entrabot is set up")
        )
        return out[start : out.index("To start your agent:")]

    @staticmethod
    def _sign_in_fails(world: Scenario, detail: str) -> None:
        checks = [dict(check) for check in PASSING_SMOKE]
        checks[0].update(status="fail", detail=detail)
        for later in checks[1:]:
            later.update(status="skip", detail="Skipped - token mint failed.")
        world.spawn.on(snippet("run_smoke_checks"), _checks(checks))

    @pytest.mark.parametrize(
        ("failure", "said_first"),
        [
            (
                "hop1:blueprint failed — invalid_client: AADSTS7000215: Invalid client.",
                "The agent could not sign in. This is not a licence delay.",
            ),
            (
                "Unexpected error: (1312, 'CredWrite', 'A specified logon session does not "
                "exist.')",
                "The test could not use Windows Credential Manager",
            ),
            (NOT_ACCEPTED_YET, "The agent could not sign in yet:"),
            (None, "The end-to-end test could not run."),
        ],
        ids=["sign-in-failed", "credential-manager", "certificate-not-accepted-yet", "no-test"],
    )
    def test_when_the_agent_did_not_sign_in_the_licence_note_comes_second_and_claims_none(
        self, world: Scenario, failure: str | None, said_first: str
    ) -> None:
        world.licence = "none"
        if failure is None:
            world.spawn.on(snippet("run_smoke_checks"), Reply(returncode=1, stderr="Traceback"))
        else:
            self._sign_in_fails(world, failure)

        assert world.run(new_chain=True, upn_suffix="sati") == 3

        finish = self._finish(world)
        assert "can sign in" not in finish
        assert finish.index(said_first) < finish.index(self.ALSO)
        assert "https://admin.microsoft.com" in finish and self.WHAT_TO_DO in finish

    def test_a_skipped_test_claims_no_sign_in_either(self, world: Scenario) -> None:
        world.licence = "none"

        assert world.run(new_chain=True, upn_suffix="sati", skip_smoke=True) == 0

        finish = self._finish(world)
        assert "can sign in" not in finish and self.ALSO not in finish
        assert (
            "The agent is set up, but it has no Teams licence, so it cannot use Teams yet."
            in finish
        )
        assert self.WHAT_TO_DO in finish

    def test_when_the_agent_signed_in_the_licence_note_is_unchanged(self, world: Scenario) -> None:
        world.licence = "none"
        checks = [dict(check) for check in PASSING_SMOKE]
        checks[2].update(status="fail", detail="403 Forbidden")
        world.spawn.on(snippet("run_smoke_checks"), _checks(checks))

        assert world.run(new_chain=True, upn_suffix="sati") == 3

        finish = self._finish(world)
        assert self.LICENCE_ONLY in finish and self.ALSO not in finish
        assert "could not sign in" not in finish and self.WHAT_TO_DO in finish


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
        assert "Teams licence assigned (Teams Essentials)" in world.out

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


class TestFirstStartAndThingsToTry:
    """From a desktop run on Windows 11: setup ended, Claude Code opened and asked questions
    nobody had been told about, and then there was nothing to say what to do with the agent."""

    def _finish(self, world: Scenario) -> str:
        return world.out[world.out.index("To start your agent:") :]

    def _with_work_iq_tools(self, world: Scenario) -> None:
        world.with_work_iq_tools()

    def test_claude_codes_first_start_questions_are_explained(self, world: Scenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        finish = self._finish(world)
        assert "sign in to Claude" in finish
        assert "trust this folder" in finish
        assert 'the "entrabot" MCP server' in finish
        assert "development channel" in finish
        assert "Say yes to each" in finish
        assert (
            finish.index("1. Double-click start-entrabot.cmd")
            < finish.index("2. Answer Claude Code's questions")
            < finish.index("3. Then say:  Who are you?")
        )

    def test_the_questions_are_explained_without_the_launcher_too(self, world: Scenario) -> None:
        (world.root / "start-entrabot.cmd").unlink()
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        finish = self._finish(world)
        assert finish.index("Open a terminal in") < finish.index("Answer Claude Code's questions")
        assert finish.index("Answer Claude Code's questions") < finish.index("Then say:  Who are")

    def test_a_copilot_only_setup_is_not_told_about_claude_codes_questions(
        self, world: Scenario
    ) -> None:
        assert world.run(new_chain=True, upn_suffix="sati", hosts="copilot") == 0
        finish = self._finish(world)
        assert "Claude Code's questions" not in finish and "development channel" not in finish
        assert "2. Then say:  Who are you?" in finish

    def test_things_to_try_come_after_the_first_words_and_before_later(
        self, world: Scenario
    ) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        finish = self._finish(world)
        assert (
            finish.index("Then say:  Who are you?")
            < finish.index("Things to try next")
            < finish.index("Later:")
        )
        things = finish[finish.index("Things to try next") : finish.index("Later:")]
        assert "Send me a Teams message to say hello." in things
        assert "answer it in Teams" in things
        assert "Send me an email" in things and "Outlook mail" in things

    def test_the_first_words_no_longer_carry_the_teams_request(self, world: Scenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        assert "Who are you? Send me a Teams message" not in world.out

    def test_a_new_licence_delay_is_said_with_the_teams_suggestion(self, world: Scenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        things = self._finish(world)
        things = things[things.index("Things to try next") : things.index("Later:")]
        assert "10-15 minutes" in things

    def test_licences_are_explained_with_where_to_add_one(self, world: Scenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        things = self._finish(world)
        things = things[things.index("Things to try next") : things.index("Later:")]
        assert "only the Microsoft 365 products it has a licence for" in things
        assert "https://admin.microsoft.com" in things

    def test_without_work_iq_a_word_document_is_not_suggested(self, world: Scenario) -> None:
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        finish = self._finish(world)
        assert "Create a Word document" not in finish
        assert "Word, Excel and OneDrive files are not switched on" in finish
        assert "run setup again" in finish

    def test_with_work_iq_a_word_document_is_suggested(self, world: Scenario) -> None:
        self._with_work_iq_tools(world)
        assert world.run(new_chain=True, upn_suffix="sati", configure_work_iq=True) == 0, world.out
        finish = self._finish(world)
        assert "Create a Word document called Hello and share it with me." in finish
        assert "are not switched on" not in finish

    def test_work_iq_set_up_by_an_earlier_run_counts(self, world: Scenario) -> None:
        (world.root / "ToolingManifest.json").write_text(
            json.dumps({"mcpServers": [{"mcpServerName": "mcp_WordServer"}]}), encoding="utf-8"
        )
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        assert "Create a Word document called Hello" in self._finish(world)

    def test_an_empty_work_iq_manifest_does_not_count(self, world: Scenario) -> None:
        (world.root / "ToolingManifest.json").write_text('{"mcpServers":[]}\n', encoding="utf-8")
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        assert "Create a Word document" not in self._finish(world)

    def test_without_a_teams_licence_teams_is_something_for_later(self, world: Scenario) -> None:
        world.licence = "none"
        checks = [dict(check) for check in PASSING_SMOKE]
        checks[2].update(status="fail", detail="403 Forbidden")
        world.spawn.on(snippet("run_smoke_checks"), _checks(checks))
        assert world.run(new_chain=True, upn_suffix="sati") == 3
        finish = self._finish(world)
        assert "Send me a Teams message" not in world.out
        things = finish[finish.index("Things to try next") : finish.index("Later:")]
        # Mail needs a licence too: one line for both, not an email that would not arrive.
        assert "Once the agent has its licence (see above)" in things
        assert "Teams" in things and "email" in things
        assert "Send me an email" not in things and "Outlook mail" not in things
        assert "Then say:  Who are you?" in finish

    def test_the_lines_under_a_suggestion_line_up_with_its_words(self, world: Scenario) -> None:
        # From a real screen: console.detail's own indent pushed these five columns too far.
        assert world.run(new_chain=True, upn_suffix="sati") == 0
        lines = self._finish(world).splitlines()

        def line(text: str) -> str:
            return next(item for item in lines if text in item)

        def indent(text: str) -> int:
            return len(text) - len(text.lstrip())

        teams = line("- Send me a Teams message to say hello.")
        words = teams.index("Send")
        assert indent(line("Then answer it in Teams.")) == words
        assert indent(line("If Teams doesn't answer straight away")) == words
        assert indent(line("Needs a licence that includes Outlook mail.")) == words
        note = line("Word, Excel and OneDrive files are not switched on")
        assert indent(note) == teams.index("-")
        step = line("1. Double-click start-entrabot.cmd")
        assert indent(line("(or, in a terminal there:")) == step.index("Double-click")

    def test_nothing_to_try_is_listed_when_the_agent_cannot_sign_in(self, world: Scenario) -> None:
        checks = [dict(check) for check in PASSING_SMOKE]
        checks[0].update(status="fail", detail="Hop 1 failed - invalid_client")
        world.spawn.on(snippet("run_smoke_checks"), _checks(checks))
        assert world.run(new_chain=True, upn_suffix="sati") == 3
        assert "Things to try next" not in world.out


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


# An older setup kept the Provisioner's private key in a plain file. On Windows,
# scripts/entra_provisioning.py moves this computer onto a key in the certificate store, then
# removes the old certificate from the Provisioner app, which holds tenant-wide Microsoft Graph
# permissions. When that removal fails, _retire_old_certificates records the certificate under
# PROVISIONER_CERT_REVOKE_PENDING (lowercase SHA-1 thumbprints, comma-separated) and
# PROVISIONER_CERT_REVOKE_PENDING_EXPIRES (their expiry dates, in the same order, "" if unknown).
PENDING = "PROVISIONER_CERT_REVOKE_PENDING"
PENDING_EXPIRES = "PROVISIONER_CERT_REVOKE_PENDING_EXPIRES"
OLD_KEY_SHA1 = "c3f1a2b4d5e6f708192a3b4c5d6e7f8091a2b3c4"
SECOND_OLD_KEY_SHA1 = "0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b9e8d"
THIS_COMPUTERS_KEY_SHA1 = "77aa88bb99cc00dd11ee22ff33aa44bb55cc66dd"  # PROVISIONER_CERT_THUMBPRINT
# What the script prints when the removal fails (_print_revocation_failed). It goes to the log.
REMOVAL_FAILED = (
    "  WARNING: The old Provisioner certificate (…B3C4, expires 2027-02-01) could not be\n"
    "  removed from the app: Entra did not remove certificate …B3C4: Request_Throttled\n"
    "  Its private key was kept in a plaintext file by an earlier setup, so a copy of\n"
    "  that file can still get tokens for the Provisioner app until it is removed.\n"
    "  Remove it in the Entra admin center: App registrations >\n"
    "  'EntraBot Agent ID Provisioner' (application ID prov-app) >\n"
    "  Certificates & secrets > Certificates: the certificate whose thumbprint ends in\n"
    "  …B3C4 (expires 2027-02-01). Setup tries again on its next run.\n"
)


def _write_state(world: Scenario, **values: str) -> None:
    current = json.loads(world.state_file.read_text("utf-8")) if world.state_file.exists() else {}
    current.update(values)
    world.state_file.write_text(json.dumps(current), encoding="utf-8")


def _clear_state(world: Scenario, *keys: str) -> None:
    current = json.loads(world.state_file.read_text("utf-8"))
    for key in keys:
        current.pop(key, None)
    world.state_file.write_text(json.dumps(current), encoding="utf-8")


class TestAnOlderSetupKeyStillTrusted:
    """The defect: the provisioning helper's output goes to the setup log, not the screen. When
    it could not remove an older setup's certificate from the Provisioner app, its warning was
    log-only, the screen said "Setup has the access it needs", and setup exited 0. A credential
    that still worked, on an app with tenant-wide permissions, went unnoticed."""

    @staticmethod
    def _provisioning_records(world: Scenario, **values: str) -> None:
        """The provisioning helper leaves *values* in the state file, as the script does."""

        def provision(_request: RunRequest) -> Reply:
            _write_state(
                world,
                PROVISIONER_CLIENT_ID="prov-app",
                PROVISIONER_CERT_THUMBPRINT=THIS_COMPUTERS_KEY_SHA1,
                **values,
            )
            return Reply(stdout=PROVISION_CHATTER + REMOVAL_FAILED)

        world.spawn.on(script("entra_provisioning.py"), provision)

    def _removal_failed(self, world: Scenario, expires: str | None = "2027-02-01") -> None:
        values = {PENDING: OLD_KEY_SHA1}
        if expires is not None:
            values[PENDING_EXPIRES] = expires
        self._provisioning_records(world, **values)

    @staticmethod
    def _things_to_check(world: Scenario) -> str:
        finish = world.out[world.out.index("Setup finished, with something to check") :]
        return finish[: finish.index("To start your agent:")]

    @pytest.mark.parametrize("verbose", [False, True])
    def test_the_step_says_it_plainly_in_every_mode(self, world: Scenario, verbose: bool) -> None:
        self._removal_failed(world)
        world.run(new_chain=True, upn_suffix="sati", verbose=verbose)
        step = world.out[
            world.out.index("Connecting setup to your tenant") : world.out.index("[6/11]")
        ]
        assert "Setup has the access it needs" in step
        warning = step[step.index("[!!] An older setup key") :]
        assert warning.startswith(
            "[!!] An older setup key for this computer could not be switched off in "
            "Microsoft Entra."
        )
        for expected in (
            "kept that key in an unprotected file",
            "Anyone with a copy of that file can still use it to make changes in your tenant.",
            "the certificate whose thumbprint ends in B3C4 (expires 2027-02-01).",
            "Microsoft Entra admin center (https://entra.microsoft.com):",
            "App registrations > 'EntraBot Agent ID Provisioner' (application ID prov-app) >",
            "Certificates & secrets > Certificates.",
            "Setup will try again the next time it runs.",
            "setup.log",  # the helper's own account of what went wrong
        ):
            assert expected in warning, expected

    def test_quietly_the_helpers_own_words_stay_in_the_log(self, world: Scenario) -> None:
        self._removal_failed(world)
        world.run(new_chain=True, upn_suffix="sati")
        assert "Request_Throttled" not in world.out and "Request_Throttled" in world.log
        assert "could not be switched off in Microsoft Entra" in world.log  # the warning too

    def test_the_finish_screen_lists_it_and_the_run_is_exit_3(self, world: Scenario) -> None:
        self._removal_failed(world)
        assert world.run(new_chain=True, upn_suffix="sati") == 3
        assert "Entrabot is set up" not in world.out
        things = self._things_to_check(world)
        assert (
            "[!!] An older setup key for this computer could not be switched off in Microsoft "
            "Entra." in things
        )
        assert "thumbprint ends in B3C4 (expires 2027-02-01)" in things
        assert "App registrations > 'EntraBot Agent ID Provisioner'" in things
        assert "Setup will try again the next time it runs." in things
        assert world.dotenv["ENTRABOT_AGENT_ID"] == "agent-app"  # setup itself is complete
        assert (world.root / ".mcp.json").is_file()

    def test_no_full_thumbprint_reaches_the_screen_or_the_log(self, world: Scenario) -> None:
        self._provisioning_records(
            world, **{PENDING: f"{OLD_KEY_SHA1},{SECOND_OLD_KEY_SHA1}", PENDING_EXPIRES: ","}
        )
        world.run(new_chain=True, upn_suffix="sati")
        assert "B3C4" in world.out and "9E8D" in world.out
        for full in (OLD_KEY_SHA1, SECOND_OLD_KEY_SHA1, THIS_COMPUTERS_KEY_SHA1):
            assert full not in world.out.lower()
            assert full not in world.log.lower()

    def test_two_keys_are_both_named(self, world: Scenario) -> None:
        self._provisioning_records(
            world,
            **{PENDING: f"{OLD_KEY_SHA1},{SECOND_OLD_KEY_SHA1}", PENDING_EXPIRES: "2027-02-01,"},
        )
        assert world.run(new_chain=True, upn_suffix="sati") == 3
        things = self._things_to_check(world)
        assert "2 older setup keys for this computer could not be switched off" in things
        assert (
            "thumbprints end in B3C4 (expires 2027-02-01) and 9E8D (expiry date unknown)" in things
        )

    def test_a_record_without_expiry_dates_still_names_the_key(self, world: Scenario) -> None:
        self._removal_failed(world, expires=None)
        assert world.run(new_chain=True, upn_suffix="sati") == 3
        assert "thumbprint ends in B3C4 (expiry date unknown)" in self._things_to_check(world)

    @pytest.mark.parametrize("value", ["not-a-thumbprint", OLD_KEY_SHA1[:-1], "['x', 'y']"])
    def test_a_record_that_cannot_be_read_is_a_soft_warning_not_a_crash(
        self, world: Scenario, value: str
    ) -> None:
        self._provisioning_records(world, **{PENDING: value})
        # Setup still finishes, and asks the person to check: what the record named may
        # still be trusted.
        assert world.run(new_chain=True, upn_suffix="sati") == 3
        assert world.dotenv["ENTRABOT_AGENT_ID"] == "agent-app"
        things = self._things_to_check(world)
        assert "Setup could not read its record of older setup keys for this computer." in things
        assert "Remove any certificate there that you can't account for." in things
        assert "Keep the one whose thumbprint ends in 66DD: this computer uses it now." in things
        assert "try again" not in things  # it can't: it does not know which certificate
        assert "Traceback" not in world.out + world.log

    def test_an_unreadable_record_is_still_reported_after_a_later_step_rewrites_it(
        self, world: Scenario
    ) -> None:
        # The script drops an entry it can't read the next time it writes the record, without
        # knowing what that entry named: a clean record afterwards proves nothing.
        self._provisioning_records(world, **{PENDING: "not-a-thumbprint"})

        def register(_request: RunRequest) -> Reply:
            _clear_state(world, PENDING, PENDING_EXPIRES)
            return Reply(
                stderr="  WARNING: ignoring a malformed entry in PROVISIONER_CERT_REVOKE_PENDING "
                "in the state file."
            )

        world.spawn.on(script("register_windows_blueprint_cert.py"), register)
        assert world.run(new_chain=True, upn_suffix="sati") == 3
        things = self._things_to_check(world)
        assert "Setup could not read its record of older setup keys for this computer." in things
        assert "switched off now" not in world.out

    def test_a_state_file_that_cannot_be_read_at_the_end_is_a_soft_warning(
        self, world: Scenario
    ) -> None:
        def register(_request: RunRequest) -> Reply:
            world.state_file.write_text("{half written", encoding="utf-8")
            return Reply()

        world.spawn.on(script("register_windows_blueprint_cert.py"), register)
        assert world.run(new_chain=True, upn_suffix="sati") == 3
        assert "Setup could not read its record of older setup keys" in world.out

    @pytest.mark.parametrize(
        "values",
        [
            {},
            {PENDING: "", PENDING_EXPIRES: ""},  # what an empty record looks like
            {PENDING: " , "},
            {PENDING_EXPIRES: "2027-02-01"},  # an expiry with no certificate names nothing
        ],
    )
    def test_nothing_pending_changes_nothing(self, world: Scenario, values: dict) -> None:
        self._provisioning_records(world, **values)
        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out
        assert "Entrabot is set up" in world.out
        assert "setup key" not in world.out and "switched off" not in world.out

    def test_a_key_switched_off_by_a_later_step_is_not_left_as_a_warning(
        self, world: Scenario
    ) -> None:
        # The certificate step's helper asks for a provisioner token too, and that retries
        # the removal. When it works, the finish screen says so instead of the warning.
        self._removal_failed(world)

        def register(_request: RunRequest) -> Reply:
            _clear_state(world, PENDING, PENDING_EXPIRES)
            return Reply(stderr="  Removed the old Provisioner certificate (…B3C4) from the app.")

        world.spawn.on(script("register_windows_blueprint_cert.py"), register)
        assert world.run(new_chain=True, upn_suffix="sati") == 0, world.out
        finish = world.out[world.out.index("Entrabot is set up") :]
        assert "could not be switched off" not in finish
        assert (
            "The older setup key ending in B3C4 is switched off now: a later step in this run "
            "removed it." in finish
        )

    def test_a_key_the_certificate_step_could_not_switch_off_is_on_the_finish_screen(
        self, world: Scenario
    ) -> None:
        # That same helper can be the first to retire the old key: when Entra had not yet
        # accepted this computer's new key during the provisioning step, the old one was kept.
        def register(_request: RunRequest) -> Reply:
            _write_state(world, **{PENDING: OLD_KEY_SHA1, PENDING_EXPIRES: "2027-02-01"})
            return Reply(stderr=REMOVAL_FAILED)

        world.spawn.on(script("register_windows_blueprint_cert.py"), register)
        assert world.run(new_chain=True, upn_suffix="sati") == 3
        assert "thumbprint ends in B3C4 (expires 2027-02-01)" in self._things_to_check(world)

    def test_a_repair_run_is_told_too(self, world: Scenario) -> None:
        world.existing()
        self._removal_failed(world)
        assert world.run() == 3
        assert "could not be switched off in Microsoft Entra" in self._things_to_check(world)

    def test_a_health_check_does_not_end_with_all_checks_passed(self, world: Scenario) -> None:
        # The health check reports the key as a warning (entrabot.preflight), which fails no
        # check; the last line must not read as if nothing were left to do.
        world.existing(
            state={**CREATED_STATE, PENDING: OLD_KEY_SHA1, PENDING_EXPIRES: "2027-02-01"}
        )
        world.spawn.on(
            snippet("run_diagnostics"),
            Reply(stdout="[ WARN ] Older setup keys: An older setup key for this computer ..."),
        )

        assert world.run(answers={"existing": "diagnose"}) == 0  # a warning, not a failure

        assert "All checks passed." not in world.out
        assert (
            "No check failed, but an older setup key still has to be switched off in Microsoft "
            "Entra (see above)." in world.out
        )
        assert world.ran("entra_provisioning.py") == []

    def test_a_health_check_with_nothing_pending_passes_as_before(self, world: Scenario) -> None:
        world.existing()
        world.spawn.on(snippet("run_diagnostics"), Reply(stdout="[ PASS ] everything"))
        assert world.run(answers={"existing": "diagnose"}) == 0
        assert "All checks passed." in world.out and "setup key" not in world.out


class TestOlderSetupKeyRecord:
    """Reading the record the way scripts/entra_provisioning.py's _pending_revocations does."""

    def test_one_key_with_its_expiry(self) -> None:
        note = old_setup_keys(
            {
                PENDING: OLD_KEY_SHA1,
                PENDING_EXPIRES: "2027-02-01",
                "PROVISIONER_CLIENT_ID": "prov-app",
                "PROVISIONER_CERT_THUMBPRINT": THIS_COMPUTERS_KEY_SHA1,
            }
        )
        assert note.keys == (OldSetupKey(ending="B3C4", expires="2027-02-01"),)
        assert note.pending and not note.unreadable
        assert note.app_id == "prov-app" and note.current == "66DD"

    def test_expiry_dates_line_up_by_position_as_the_script_writes_them(self) -> None:
        note = old_setup_keys(
            {PENDING: f"{OLD_KEY_SHA1}, ,{SECOND_OLD_KEY_SHA1}", PENDING_EXPIRES: "2027-02-01,,"}
        )
        assert note.keys == (
            OldSetupKey("B3C4", "2027-02-01"),
            OldSetupKey("9E8D", ""),  # the blank entry kept its place
        )

    def test_case_and_padding_do_not_matter_and_a_repeat_is_named_once(self) -> None:
        note = old_setup_keys({PENDING: f" {OLD_KEY_SHA1.upper()} ,{OLD_KEY_SHA1}"})
        assert note.keys == (OldSetupKey("B3C4", ""),)

    @pytest.mark.parametrize(
        "recorded", [{}, {PENDING: ""}, {PENDING: " , ,"}, {PENDING_EXPIRES: "2027-02-01"}]
    )
    def test_nothing_recorded_is_nothing_pending(self, recorded: dict) -> None:
        note = old_setup_keys(recorded)
        assert not note.pending
        assert old_setup_key_lines(note) == []

    @pytest.mark.parametrize(
        "value",
        [
            "not-a-thumbprint",
            OLD_KEY_SHA1[:-1],
            OLD_KEY_SHA1 + "0",
            OLD_KEY_SHA1[:-1] + "g",
            f"['{OLD_KEY_SHA1}']",  # a JSON list, as the wizard's reader turns it into text
            "True",
        ],
    )
    def test_an_entry_that_is_not_a_thumbprint_leaves_the_record_unreadable(
        self, value: str
    ) -> None:
        note = old_setup_keys({PENDING: value})
        assert note.pending and note.unreadable and note.keys == ()

    def test_a_readable_key_is_still_named_beside_an_unreadable_entry(self) -> None:
        note = old_setup_keys({PENDING: f"junk,{OLD_KEY_SHA1}", PENDING_EXPIRES: ",2027-02-01"})
        assert note.keys == (OldSetupKey("B3C4", "2027-02-01"),) and note.unreadable
        text = "\n".join(old_setup_key_lines(note))
        assert "thumbprint ends in B3C4 (expires 2027-02-01)" in text
        assert "an entry it could not read" in text
        assert text.endswith("Setup will try again the next time it runs.")

    @pytest.mark.parametrize(
        "expires", ["soon", "2027-13-45", "20270201", "2027-02-01T00:00:00", "٢027-02-01"]
    )
    def test_an_expiry_that_is_not_a_date_is_unknown(self, expires: str) -> None:
        note = old_setup_keys({PENDING: OLD_KEY_SHA1, PENDING_EXPIRES: expires})
        assert note.keys == (OldSetupKey("B3C4", ""),)

    def test_an_unknown_app_id_and_key_are_left_out_not_guessed(self) -> None:
        text = "\n".join(old_setup_key_lines(old_setup_keys({PENDING: "junk"})))
        assert "App registrations > 'EntraBot Agent ID Provisioner' >" in text
        assert "application ID" not in text and "Keep the one" not in text

    def test_the_record_never_holds_a_full_thumbprint(self) -> None:
        note = old_setup_keys(
            {
                PENDING: f"{OLD_KEY_SHA1},{SECOND_OLD_KEY_SHA1}",
                "PROVISIONER_CERT_THUMBPRINT": THIS_COMPUTERS_KEY_SHA1,
            }
        )
        text = (repr(note) + "\n".join(old_setup_key_lines(note))).lower()
        for full in (OLD_KEY_SHA1, SECOND_OLD_KEY_SHA1, THIS_COMPUTERS_KEY_SHA1):
            assert full not in text
            assert full[:36] not in text  # nor most of one

    def test_three_keys_read_as_a_list(self) -> None:
        third = "f" * 36 + "a1b2"
        note = old_setup_keys({PENDING: f"{OLD_KEY_SHA1},{SECOND_OLD_KEY_SHA1},{third}"})
        text = "\n".join(old_setup_key_lines(note))
        assert text.startswith("3 older setup keys")
        assert (
            "end in B3C4 (expiry date unknown), 9E8D (expiry date unknown) and A1B2 "
            "(expiry date unknown)." in text
        )


# ── The licence walkthrough, Work IQ's tools and its Copilot licence ─────────────────────────

SPB_ID = "11111111-0000-4000-8000-0000000000aa"
E3_ID = "22222222-0000-4000-8000-0000000000bb"
COPILOT_ID = "33333333-0000-4000-8000-0000000000cc"
TEAMS_REFUSED = [dict(check) for check in PASSING_SMOKE]
TEAMS_REFUSED[2] = {**TEAMS_REFUSED[2], "status": "fail", "detail": "Graph returned 403"}
WALKTHROUGH = "Licences for Microsoft 365"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _last_screen(world: Scenario) -> str:
    out = world.out
    start = max(
        out.rfind("Setup finished, with something to check"), out.rfind("Entrabot is set up")
    )
    return out[start:]


class TestTheLicenceWalkthrough:
    """From a desktop run: the agent was created with no licence, the test failed on Teams, and
    the last screen said only what to do. Now setup offers to walk the person through it."""

    def _no_licence(self, world: Scenario, *skus: dict) -> None:
        world.licence = "none"
        world.licences.skus = list(skus)
        world.spawn.on(snippet("run_smoke_checks"), _checks(TEAMS_REFUSED))

    def test_it_comes_after_the_test_and_before_the_summary(self, world: Scenario) -> None:
        self._no_licence(world, tenant_sku(SPB_ID, "SPB", BUSINESS_PLANS, total=3, used=2))
        world.run(typed="\n" * 40)
        out = world.out
        assert out.index("[11/11] Testing the agent end to end") < out.index(WALKTHROUGH)
        assert out.index(WALKTHROUGH) < out.index("Setup finished")
        assert "[12/" not in out  # not a numbered step

    def test_a_free_licence_is_given_and_the_summary_says_teams_needs_time(
        self, world: Scenario
    ) -> None:
        self._no_licence(world, tenant_sku(SPB_ID, "SPB", BUSINESS_PLANS, total=3, used=2))
        assert world.run(typed="\n" * 40) == 3  # Teams was not reached yet: still a check
        assert world.licences.assigned == [SPB_ID]
        assert json.loads(world.state_file.read_text("utf-8"))["AGENT_USER_LICENSE_SKU"] == "SPB"
        last = _last_screen(world)
        assert "Teams is not ready for it yet" in last and "10-15 minutes" in last
        assert "no Teams licence" not in last and "What to do" not in last
        things = last[last.index("Things to try next") : last.index("Later:")]
        assert "Send me a Teams message to say hello." in things
        assert "a new licence takes 10-15 minutes" in things

    def test_the_licence_step_reuses_the_agent_script_and_the_provisioner(
        self, world: Scenario
    ) -> None:
        self._no_licence(world, tenant_sku(SPB_ID, "SPB", BUSINESS_PLANS, total=3, used=2))
        world.run(typed="\n" * 40)
        calls = world.ran("create_entra_agent_ids.py")
        assert [call.argv[2:] for call in calls] == [
            [],
            ["--licence-report"],
            ["--assign-licence", SPB_ID],
        ]
        for call in calls[1:]:
            assert call.env is not None and call.env["ENTRABOT_TENANT_ID"] == TENANT
        assert world.az("rest", "--method", "POST") == []  # never the Azure CLI's token

    def test_unattended_it_never_asks_and_the_last_screen_is_tailored(
        self, world: Scenario
    ) -> None:
        self._no_licence(world, tenant_sku(E3_ID, "SPE_E3", E3_PLANS, total=25, used=25))
        assert world.run(new_chain=True, upn_suffix="sati") == 3
        assert WALKTHROUGH not in world.out and world.licences.assigned == []
        last = _flat(_last_screen(world))
        assert "Microsoft 365 E3 includes Teams, but all 25 of its licences are in use." in last
        assert "Buy licenses" in last and "Unassign licenses" in last
        assert "Run setup again. It gives the agent the licence by itself." in last

    def test_advice_shown_in_the_walkthrough_is_not_repeated_on_the_last_screen(
        self, world: Scenario
    ) -> None:
        # From a real run: the full add-or-free advice, then the same block again in full.
        self._no_licence(world, tenant_sku(E3_ID, "SPE_E3", E3_PLANS, total=25, used=25))
        answers = {"identity": "new", "upn_suffix": "sati", "licence_recheck": "s"}
        assert world.run(answers=answers, typed="\n" * 40) == 3
        assert world.out.count("Buy licenses") == 1  # in the walkthrough only
        last = _flat(_last_screen(world))
        assert "it has no Teams licence, so it cannot use Teams yet" in last
        assert 'What to do is above, under "Licences for Microsoft 365".' in last
        assert "When a licence is free, run setup again." in last

    def test_copilot_is_left_out_while_word_excel_and_onedrive_stay_off(
        self, world: Scenario
    ) -> None:
        # From a real run: the tools could not be installed, so Work IQ stayed off, yet the
        # walkthrough talked about Copilot and listed it first.
        self._no_licence(
            world,
            tenant_sku(COPILOT_ID, "Microsoft_365_Copilot", COPILOT_PLANS, total=3, used=3),
            tenant_sku(E3_ID, "SPE_E3", E3_PLANS, total=4, used=4),
        )
        answers = {
            "identity": "new",
            "upn_suffix": "sati",
            "work_iq": "yes",
            "install_work_iq": "no",
            "licence_recheck": "s",
        }
        assert world.run(answers=answers, typed="\n" * 40) == 3
        walk = world.out[world.out.index(WALKTHROUGH) : world.out.index("Setup finished")]
        walk = walk[: walk.rindex("=====")].rstrip(" =\n")  # up to the summary's banner
        assert "Copilot" not in walk
        assert walk.endswith("What to do is on the last screen.")

    def test_saying_no_skips_it_and_the_last_screen_says_what_to_do(self, world: Scenario) -> None:
        self._no_licence(world)  # nothing in the tenant includes Teams
        answers = {"identity": "new", "upn_suffix": "sati", "licence_walkthrough": "no"}
        assert world.run(answers=answers, typed="\n" * 40) == 3
        assert "Skipped. What to do is on the last screen." in world.out
        last = _flat(_last_screen(world))
        assert "None of your organization's licences includes Teams." in last
        assert world.licences.assigned == []

    def test_after_a_failed_sign_in_it_is_not_offered(self, world: Scenario) -> None:
        world.licence = "none"
        checks = [dict(check) for check in PASSING_SMOKE]
        checks[0].update(status="fail", detail="hop1:blueprint failed - invalid_client")
        world.spawn.on(snippet("run_smoke_checks"), _checks(checks))
        assert world.run(typed="\n" * 40) == 3
        assert WALKTHROUGH not in world.out and world.licences.assigned == []
        assert "Run setup again. It gives the agent the licence by itself." in world.out

    @pytest.mark.parametrize("licence", ["assigned", "existing"])
    def test_an_agent_with_a_licence_is_not_walked_through_anything(
        self, world: Scenario, licence: str
    ) -> None:
        world.licence = licence
        assert world.run(typed="\n" * 40) == 0
        assert WALKTHROUGH not in world.out
        assert world.licences.reports == 0

    def test_it_can_be_driven_by_answers(self, world: Scenario) -> None:
        self._no_licence(world, tenant_sku(SPB_ID, "SPB", BUSINESS_PLANS, total=3, used=2))
        answers = {
            "identity": "new",
            "upn_suffix": "sati",
            "licence_walkthrough": "yes",
            "licence_teams": "yes",
        }
        assert world.run(answers=answers) == 3
        assert world.licences.assigned == [SPB_ID]


class TestTheCopilotLicence:
    def _work_iq(self, world: Scenario, copilot: str) -> None:
        world.with_work_iq_tools()
        world.copilot = copilot

    def test_the_agent_step_says_what_happened_to_it(self, world: Scenario) -> None:
        self._work_iq(world, "assigned")
        assert world.run(new_chain=True, upn_suffix="sati", configure_work_iq=True) == 0
        created = world.out[world.out.index("Creating your agent") : world.out.index("[7/11]")]
        assert "Microsoft 365 Copilot licence assigned" in created

    def test_none_free_is_walked_through_and_given_when_one_is(self, world: Scenario) -> None:
        self._work_iq(world, "none")
        world.licences.skus = [
            tenant_sku(SPB_ID, "SPB", BUSINESS_PLANS, total=3, used=3),
            tenant_sku(COPILOT_ID, "Microsoft_365_Copilot", COPILOT_PLANS, total=2, used=1),
        ]
        world.licences.agent = [SPB_ID]
        answers = {"identity": "new", "upn_suffix": "sati", "work_iq": "yes"}
        assert world.run(answers=answers, typed="\n" * 40) == 0, world.out
        assert world.licences.assigned == [COPILOT_ID]
        recorded = json.loads(world.state_file.read_text("utf-8"))
        assert recorded["AGENT_USER_WORK_IQ_LICENSE_SKU"] == "Microsoft_365_Copilot"
        things = _last_screen(world)
        things = things[things.index("Things to try next") : things.index("Later:")]
        assert "Create a Word document called Hello and share it with me." in things
        assert "A new Microsoft 365 Copilot licence can take 10-15 minutes." in things

    def test_without_one_the_last_screen_says_so_and_it_is_something_to_check(
        self, world: Scenario
    ) -> None:
        self._work_iq(world, "none")
        assert world.run(new_chain=True, upn_suffix="sati", configure_work_iq=True) == 3
        last = _last_screen(world)
        assert last.startswith("Setup finished, with something to check")
        assert (
            "Word, Excel and OneDrive files are switched on, but the agent has no Microsoft 365 "
            "Copilot licence" in _flat(last)
        )
        things = last[last.index("Things to try next") : last.index("Later:")]
        assert "Create a Word document" not in things
        assert "Microsoft 365 Copilot licence first" in _flat(things)


class TestWorkIqToolsAreOfferedNotDropped:
    """From a desktop run: yes to Word, Excel and OneDrive, then "Work IQ needs tools that are
    not installed, so it is skipped for now", nothing more about it, and later the agent's
    Word tool failed. The person thought it was switched on."""

    def test_the_question_says_what_it_is_and_what_it_needs_before_asking(
        self, world: Scenario
    ) -> None:
        world.with_work_iq_tools()
        world.run(typed="\n" * 40)
        out = world.out
        question = out.index("Switch on Word, Excel and OneDrive files? [y/N]")
        explained = out.rfind("read and edit Word, Excel and OneDrive files", 0, question)
        assert explained != -1
        assert "Microsoft 365 Copilot" in out[explained:question]

    def test_missing_tools_are_installed_on_enter_and_work_iq_goes_on(
        self, world: Scenario
    ) -> None:
        world.a365_lookups()
        answers = {"identity": "new", "upn_suffix": "sati", "work_iq": "yes"}
        assert world.run(answers=answers, typed="\n" * 40) == 0, world.out
        helper = next(call for call in world.spawn.calls if "-WithWorkIq" in call.argv)
        assert helper.interactive and Path(helper.argv[5]).name == "prereqs-windows.ps1"
        assert helper.argv[6:] == ["-WithWorkIq"]
        out = world.out
        offer = out[out.index("not on this PC yet") : out.index("Install them now?")]
        assert ".NET SDK" in offer and "Agent 365 CLI" in offer and "PowerShell 7" in offer
        # From a real run: winget's .NET installer needed elevation and failed. The helper
        # installs for the account only now, and the offer says so, with size and time.
        offer = _flat(offer)
        assert "no administrator rights" in offer and "administrator approval" not in offer
        assert "about 1 GB" in offer and "a few minutes" in offer
        # The a365 steps ran, with the tools the helper just installed on their PATH, and
        # a365.exe pointed at the .NET the helper installed for it.
        a365 = [call for call in world.spawn.calls if call.argv[0] == str(world.installed_a365)]
        assert a365 and a365[0].argv[1:3] == ["setup", "requirements"]
        for call in a365:
            assert call.env is not None
            assert call.env["DOTNET_ROOT"] == str(world.installed_dotnet.parent)
            path = call.env["PATH"].split(os.pathsep)
            assert path[0] == str(world.installed_dotnet.parent)
            assert str(world.installed_pwsh.parent) in path
        assert "were asked for" not in world.out

    def test_declining_the_install_is_said_at_the_end_as_something_to_check(
        self, world: Scenario
    ) -> None:
        answers = {
            "identity": "new",
            "upn_suffix": "sati",
            "work_iq": "yes",
            "install_work_iq": "no",
        }
        assert world.run(answers=answers, typed="\n" * 40) == 3
        assert not any("-WithWorkIq" in call.argv for call in world.spawn.calls)
        # Its Copilot licence only matters once it is on: no walkthrough for it now.
        assert WALKTHROUGH not in world.out and world.licences.reports == 0
        last = _flat(_last_screen(world))
        assert last.startswith("Setup finished, with something to check")
        assert "Word, Excel and OneDrive files were asked for, but they are not switched on" in last
        assert workiq_command() in last and "answer yes to the Word, Excel and OneDrive" in last
        things = last[last.index("Things to try next") : last.index("Later:")]
        assert "were asked for but are not switched on yet" in things

    def test_an_install_that_does_not_finish_is_said_at_the_end_too(self, world: Scenario) -> None:
        world.helper_installs_work_iq = False
        answers = {"identity": "new", "upn_suffix": "sati", "work_iq": "yes"}
        assert world.run(answers=answers, typed="\n" * 40) == 3
        assert "The install did not finish, so Word, Excel and OneDrive files" in world.out
        assert "Word, Excel and OneDrive files were asked for" in _flat(_last_screen(world))

    def test_the_review_says_what_was_installed_before_it(self, world: Scenario) -> None:
        # "Nothing has been changed yet." was untrue moments after an install.
        world.a365_lookups()
        answers = {"identity": "new", "upn_suffix": "sati", "work_iq": "yes", "start": "no"}
        assert world.run(answers=answers, typed="\n" * 40) == 130
        review = _flat(world.out[world.out.index("[4/11] Review") :])
        assert "Nothing has been changed yet." not in review
        assert "Nothing has been changed in your tenant yet." in review
        assert (
            "On this PC setup has only installed the tools for Word, Excel and OneDrive files."
            in review
        )
        assert not world.mutated()

    def test_an_install_that_did_not_finish_is_said_as_tried(self, world: Scenario) -> None:
        world.helper_installs_work_iq = False
        answers = {"identity": "new", "upn_suffix": "sati", "work_iq": "yes", "start": "no"}
        assert world.run(answers=answers, typed="\n" * 40) == 130
        review = _flat(world.out[world.out.index("[4/11] Review") :])
        assert "only tried to install the tools for Word, Excel and OneDrive files" in review

    def test_the_review_says_whether_it_is_on(self, world: Scenario) -> None:
        answers = {
            "identity": "new",
            "upn_suffix": "sati",
            "work_iq": "yes",
            "install_work_iq": "no",
            "start": "no",
        }
        assert world.run(answers=answers, typed="\n" * 40) == 130
        assert "Work IQ:     no: its tools are not installed" in world.out


def workiq_command() -> str:
    from entrabot.winsetup.workiq import INSTALL_COMMAND

    return INSTALL_COMMAND
