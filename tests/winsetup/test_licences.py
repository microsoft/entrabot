"""The licence walkthrough: after setup, get the agent the Microsoft 365 licences it needs.

The tenant is :class:`FakeLicences`, which answers the way ``create_entra_agent_ids.py``'s
licence-only mode prints. Nothing here reaches a tenant.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

from entrabot.licensing import Report
from entrabot.winsetup import licences
from entrabot.winsetup.console import Console
from entrabot.winsetup.helpers import Helpers, Paths
from tests.winsetup.fakes import (
    BUSINESS_PLANS,
    COPILOT_PLANS,
    E3_NO_TEAMS_PLANS,
    E3_PLANS,
    POWER_BI_PLANS,
    TEAMS_ESSENTIALS_PLANS,
    FakeLicences,
    FakeSpawn,
    tenant_sku,
)

VENV_PYTHON = Path("/venv/Scripts/python.exe")
ENV = {"ENTRABOT_TENANT_ID": "tenant", "PATH": "x"}
SPB = "11111111-0000-4000-8000-000000000001"
E3 = "22222222-0000-4000-8000-000000000002"
ESS = "33333333-0000-4000-8000-000000000003"
COP = "44444444-0000-4000-8000-000000000004"
E3NT = "55555555-0000-4000-8000-000000000005"
BI = "66666666-0000-4000-8000-000000000006"
DEV = "77777777-0000-4000-8000-000000000007"


class _Keyboard(io.StringIO):
    """What a person types, echoed to the screen the way a terminal does."""

    def __init__(self, typed: str, screen: io.StringIO) -> None:
        super().__init__(typed)
        self.screen = screen

    def readline(self, size: int | None = -1) -> str:  # type: ignore[override]
        line = super().readline(-1 if size is None else size)
        self.screen.write(line)
        return line


def _flat(text: str) -> str:
    """The screen's words with the line breaks and indents taken out."""
    return re.sub(r"\s+", " ", text)


class World:
    def __init__(self, tmp_path: Path, tenant: FakeLicences) -> None:
        self.tmp_path = tmp_path
        self.tenant = tenant
        self.spawn = FakeSpawn()
        tenant.attach(self.spawn)
        self.out = ""

    @property
    def flat(self) -> str:
        return _flat(self.out)

    @property
    def log(self) -> str:
        path = self.tmp_path / "setup.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def console(self, *, typed: str | None = None, answers: dict[str, str] | None = None):
        self._stream = io.StringIO()
        console = Console(
            stdin=_Keyboard(typed or "", self._stream),
            stdout=self._stream,
            interactive=typed is not None,
            answers=answers,
            spawn=self.spawn,
            color=False,
            unicode=False,
            log_file=self.tmp_path / "setup.log",
        )
        return console

    def helpers(self, console: Console) -> Helpers:
        paths = Paths(self.tmp_path, VENV_PYTHON, self.tmp_path / "data")
        return Helpers(console, paths, {})

    def walk(
        self,
        *,
        typed: str | None = None,
        answers: dict[str, str] | None = None,
        teams: bool = True,
        copilot: bool = False,
    ) -> licences.Outcome:
        console = self.console(typed=typed, answers=answers)
        try:
            return licences.walk_through(
                console, self.helpers(console), ENV, teams=teams, copilot=copilot
            )
        finally:
            console.close()
            self.out = self._stream.getvalue()

    def calls(self, flag: str) -> list[list[str]]:
        return [call.argv for call in self.spawn.calls if flag in call.argv]


@pytest.fixture
def tenant() -> FakeLicences:
    return FakeLicences()


@pytest.fixture
def world(tmp_path: Path, tenant: FakeLicences) -> World:
    return World(tmp_path, tenant)


def _lines_are_tidy(text: str) -> None:
    for line in text.splitlines():
        assert len(line) <= 96, line
        assert line.isascii(), line
        assert "!" not in line.replace("[!!]", ""), line  # calm: no exclamation marks


class TestTheMarkers:
    def test_they_are_the_lines_the_script_prints(self) -> None:
        # The script side pins the same lines (tests/scripts/test_create_entra_agent_ids.py).
        assert licences.ASSIGNED == "licence-assigned"
        assert licences.ALREADY_ASSIGNED == "licence-already-assigned"
        assert licences.NO_FREE_SEAT == "licence-no-free-seat"
        assert licences.NOT_FOUND == "licence-not-found"
        assert licences.ASSIGN_FAILED == "licence-assign-failed"
        assert licences.COULD_NOT_READ == "licence-could-not-read"


class TestReading:
    def test_the_report_comes_from_the_agent_script_with_the_provisioner(
        self, world: World, tenant: FakeLicences
    ) -> None:
        tenant.skus = [tenant_sku(SPB, "SPB", BUSINESS_PLANS, total=3, used=1)]
        console = world.console()
        report = licences.read_report(world.helpers(console), ENV)
        assert [product.name for product in report.products] == ["Microsoft 365 Business Premium"]
        (call,) = world.spawn.calls
        assert Path(call.argv[1]).name == "create_entra_agent_ids.py"
        assert call.argv[2:] == ["--licence-report"]
        assert call.env == ENV  # the tenant's environment: the script finds its provisioner
        assert world._stream.getvalue() == ""  # quiet: the log has it

    def test_a_report_that_cannot_be_read_says_why(
        self, world: World, tenant: FakeLicences
    ) -> None:
        tenant.unreadable = "The licences could not be read: Microsoft Graph answered HTTP 403."
        console = world.console()
        with pytest.raises(licences.ReadFailed, match="HTTP 403"):
            licences.read_report(world.helpers(console), ENV)

    def test_an_answer_that_is_not_a_report_is_not_trusted(self, world: World) -> None:
        world.spawn.on(lambda request: True, lambda request: _reply("ENTRABOT_RESULT={broken"))
        console = world.console()
        with pytest.raises(licences.ReadFailed):
            licences.read_report(world.helpers(console), ENV)
        assert licences.read_quietly(world.helpers(console), ENV) is None


def _reply(stdout: str):  # noqa: ANN202 - a Reply, from fakes
    from tests.winsetup.fakes import Reply

    return Reply(stdout=stdout + "\n")


class TestTheTable:
    def test_it_shows_what_each_licence_gives_the_agent_and_hides_the_rest(
        self, world: World, tenant: FakeLicences
    ) -> None:
        tenant.skus = [
            tenant_sku(SPB, "SPB", BUSINESS_PLANS, total=10, used=10),
            tenant_sku(E3NT, "Microsoft_365_E3_(no_Teams)", E3_NO_TEAMS_PLANS, total=25, used=25),
            tenant_sku(BI, "POWER_BI_PRO", POWER_BI_PLANS, total=4, used=0),
            tenant_sku(DEV, "MCOCAP", ("TEAMS1", "MCOEV"), total=2, used=0),
            tenant_sku(COP, "Microsoft_365_Copilot", COPILOT_PLANS, total=5, used=1),
        ]
        world.walk(answers={licences.OFFER_KEY: "yes", licences.RECHECK_KEY: "s"})
        table = world.out[world.out.index("Your organization's licences") :]
        table = table[: table.index("\n\n") if "\n\n" in table else len(table)]
        assert re.search(r"Business Premium +Teams, mail and files +0 of 10 free", table)
        assert re.search(r"Microsoft 365 E3 \(no Teams\) +mail +0 of 25 free", table)
        assert "POWER_BI_PRO" not in table and "Shared Devices" not in table
        assert "Copilot" not in table  # only shown when Word, Excel and OneDrive were asked for

    def test_copilot_is_shown_when_it_is_needed(self, world: World, tenant: FakeLicences) -> None:
        tenant.skus = [tenant_sku(COP, "Microsoft_365_Copilot", COPILOT_PLANS, total=5, used=1)]
        tenant.agent = []
        world.walk(
            answers={licences.OFFER_KEY: "yes", licences.RECHECK_KEY: "s"},
            teams=False,
            copilot=True,
        )
        assert re.search(r"Microsoft 365 Copilot +Copilot +4 of 5 free", world.out)

    def test_when_nothing_matters_everything_is_shown(
        self, world: World, tenant: FakeLicences
    ) -> None:
        tenant.skus = [tenant_sku(BI, "POWER_BI_PRO", POWER_BI_PLANS, total=4, used=0)]
        world.walk(answers={licences.OFFER_KEY: "yes", licences.RECHECK_KEY: "s"})
        assert re.search(r"Power BI PRO +nothing the agent uses +4 of 4 free", world.out)

    def test_a_product_microsoft_does_not_name_is_shown_readably_and_logged_as_it_is(
        self, world: World, tenant: FakeLicences
    ) -> None:
        # From a real tenant: these were shown as raw part numbers.
        tenant.skus = [
            tenant_sku(DEV, "MICROSOFT_AGENT_365_ROW_DEPT", ("TEAMS1", "EXCHANGE_S_STANDARD")),
            tenant_sku(E3NT, "MICROSOFT_AGENT_FRONTIER_NO_TEAMS", ("EXCHANGE_S_STANDARD",)),
        ]
        tenant.skus[0]["consumedUnits"] = 5
        world.walk(answers={licences.OFFER_KEY: "yes", licences.RECHECK_KEY: "s"})
        assert re.search(r"Microsoft Agent 365 ROW Dept +Teams and mail +0 of 5 free", world.out)
        assert re.search(r"Microsoft Agent Frontier \(no Teams\) +mail +5 of 5 free", world.out)
        assert "MICROSOFT_AGENT" not in world.out  # the screen gets names, never part numbers
        assert "MICROSOFT_AGENT_365_ROW_DEPT" in world.log  # the log keeps them
        assert "MICROSOFT_AGENT_FRONTIER_NO_TEAMS" in world.log

    def test_teams_licences_come_first(self, world: World, tenant: FakeLicences) -> None:
        # From a real run: the table led with Microsoft 365 Copilot.
        tenant.skus = [
            tenant_sku(COP, "Microsoft_365_Copilot", COPILOT_PLANS, total=3, used=3),
            tenant_sku(E3NT, "Microsoft_365_E3_(no_Teams)", E3_NO_TEAMS_PLANS, total=4, used=4),
            tenant_sku(E3, "SPE_E3", E3_PLANS, total=4, used=4),
        ]
        world.walk(answers={licences.OFFER_KEY: "yes", licences.RECHECK_KEY: "s"}, copilot=True)
        table = world.out[world.out.index("Your organization's licences") :]
        rows = [line.strip() for line in table.splitlines()[1:4]]
        assert [row.split("  ")[0] for row in rows] == [
            "Microsoft 365 E3",
            "Microsoft 365 E3 (no Teams)",
            "Microsoft 365 Copilot",
        ]

    def test_an_organization_with_no_licences_is_told_so(self, world: World) -> None:
        world.walk(answers={licences.OFFER_KEY: "yes", licences.RECHECK_KEY: "s"})
        assert "Your organization has no Microsoft 365 licences yet." in world.out


class TestAFreeLicence:
    def test_one_free_licence_is_given_on_enter(self, world: World, tenant: FakeLicences) -> None:
        tenant.skus = [tenant_sku(SPB, "SPB", BUSINESS_PLANS, total=3, used=2)]
        outcome = world.walk(typed="\n\n")
        assert outcome.teams == "assigned"
        assert outcome.products["teams"].name == "Microsoft 365 Business Premium"
        assert tenant.assigned == [SPB]
        (assign,) = world.calls("--assign-licence")
        assert assign[-2:] == ["--assign-licence", SPB]
        assert "Give the agent one Microsoft 365 Business Premium licence? [Y/n]" in world.out
        assert "The agent has its Microsoft 365 Business Premium licence now." in world.out
        assert "10-15 minutes" in world.out and "-Diagnose" in world.out
        _lines_are_tidy(world.out)

    def test_of_several_the_one_with_mail_and_files_is_suggested(
        self, world: World, tenant: FakeLicences
    ) -> None:
        tenant.skus = [
            tenant_sku(ESS, "Teams_Ess", TEAMS_ESSENTIALS_PLANS, total=5, used=0),
            tenant_sku(E3, "SPE_E3", E3_PLANS, total=25, used=20),
        ]
        outcome = world.walk(typed="\n\n")
        assert tenant.assigned == [E3] and outcome.teams == "assigned"
        choices = world.out[world.out.index("Which one should the agent have?") :]
        assert choices.index("1. Microsoft 365 E3: Teams, mail and files (5 free)") < choices.index(
            "2. Microsoft Teams Essentials: Teams (5 free)"
        )
        assert "3. None for now" in choices

    def test_the_person_can_pick_another(self, world: World, tenant: FakeLicences) -> None:
        tenant.skus = [
            tenant_sku(ESS, "Teams_Ess", TEAMS_ESSENTIALS_PLANS, total=5, used=0),
            tenant_sku(E3, "SPE_E3", E3_PLANS, total=25, used=20),
        ]
        world.walk(typed="\n2\n")
        assert tenant.assigned == [ESS]

    @pytest.mark.parametrize("typed", ["\nn\n", "\n3\n"], ids=["no", "none-for-now"])
    def test_saying_no_gives_nothing(self, world: World, tenant: FakeLicences, typed: str) -> None:
        tenant.skus = [tenant_sku(SPB, "SPB", BUSINESS_PLANS, total=3, used=2)]
        if typed == "\n3\n":
            tenant.skus.append(tenant_sku(ESS, "Teams_Ess", TEAMS_ESSENTIALS_PLANS))
        outcome = world.walk(typed=typed)
        assert outcome.teams == "skipped" and tenant.assigned == []
        assert "What to do is on the last screen." in world.out

    def test_a_licence_taken_meanwhile_is_looked_for_again(
        self, world: World, tenant: FakeLicences
    ) -> None:
        tenant.skus = [
            tenant_sku(SPB, "SPB", BUSINESS_PLANS, total=3, used=2),
            tenant_sku(ESS, "Teams_Ess", TEAMS_ESSENTIALS_PLANS, total=5, used=0),
        ]
        tenant.taken = {SPB}
        outcome = world.walk(typed="\n\n\n")
        assert "The last free Microsoft 365 Business Premium licence was taken" in world.out
        assert tenant.assigned == [ESS] and outcome.teams == "assigned"

    def test_a_refused_assignment_is_reported_and_not_retried(
        self, world: World, tenant: FakeLicences
    ) -> None:
        tenant.skus = [tenant_sku(SPB, "SPB", BUSINESS_PLANS, total=3, used=2)]
        tenant.refuse = '{"error": {"code": "Request_BadRequest"}}'
        outcome = world.walk(typed="\n\n")
        assert outcome.teams == "failed" and len(world.calls("--assign-licence")) == 1
        assert "Setup could not give the agent the licence." in world.out
        assert "Request_BadRequest" in world.out  # the helper's own words, from the log

    def test_an_agent_that_has_one_already_is_left_alone(
        self, world: World, tenant: FakeLicences
    ) -> None:
        tenant.skus = [tenant_sku(SPB, "SPB", BUSINESS_PLANS, total=3, used=3)]
        tenant.agent = [SPB]
        outcome = world.walk(typed="\n")
        assert outcome.teams == "had" and world.calls("--assign-licence") == []
        assert "The agent already has Microsoft 365 Business Premium" in world.out


FULL_E3 = [tenant_sku(E3, "SPE_E3", E3_PLANS, total=25, used=25)]


class TestAllLicencesInUse:
    def test_it_says_which_and_the_two_ways_out(self, world: World, tenant: FakeLicences) -> None:
        tenant.skus = [dict(item) for item in FULL_E3]
        world.walk(typed="\ns\n")
        out = world.flat
        assert "Microsoft 365 E3 includes Teams, but all 25 of its licences are in use." in out
        assert "billing administrator" in out and "Buy licenses" in out
        assert "Billing > Your products > Microsoft 365 E3" in out
        assert "licence or user administrator" in out and "Unassign licenses" in out
        assert "Billing > Licenses > Microsoft 365 E3" in out
        assert "30 days" in out and "https://admin.microsoft.com" in out
        assert "ask them" in out  # a partner who sells the licences
        _lines_are_tidy(world.out)

    def test_the_walkthrough_says_which_advice_it_showed(
        self, world: World, tenant: FakeLicences
    ) -> None:
        tenant.skus = [dict(item) for item in FULL_E3]
        assert world.walk(typed="\ns\n").explained == {"teams"}

    def test_several_products_read_naturally_with_one_as_the_example(
        self, world: World, tenant: FakeLicences
    ) -> None:
        # From a real run: two products in the headline, then menu paths for the first only.
        tenant.skus = [
            tenant_sku(DEV, "MICROSOFT_AGENT_365_ROW_DEPT", ("TEAMS1", "EXCHANGE_S_STANDARD")),
            tenant_sku(E3, "ENTERPRISEPACK", E3_PLANS, total=4, used=4),
        ]
        tenant.skus[0]["consumedUnits"] = 5
        world.walk(typed="\ns\n")
        out = world.flat
        # The example, named first, is one the admin center shows by that name.
        assert (
            "Office 365 E3 and Microsoft Agent 365 ROW Dept include Teams, but all their "
            "licences are in use." in out
        )
        assert (
            "Billing > Your products, select the product (for example Office 365 E3), then "
            "Buy licenses" in out
        )
        assert (
            "Billing > Licenses, select the product (for example Office 365 E3) and that "
            "person, then Unassign licenses" in out
        )
        assert "Microsoft Agent 365 ROW Dept > Buy licenses" not in out
        assert "mail is kept for 30 days, then deleted" in out  # both include mail
        _lines_are_tidy(world.out)

    def test_freeing_a_teams_only_licence_says_nothing_about_mail(
        self, world: World, tenant: FakeLicences
    ) -> None:
        # Unassigning Teams Enterprise leaves a person's mail alone: it comes with their suite.
        tenant.skus = [
            tenant_sku(DEV, "Microsoft_Teams_Enterprise_New", ("TEAMS1",), total=4, used=4)
        ]
        world.walk(typed="\ns\n")
        assert "Unassign licenses" in world.flat and "30 days" not in world.flat

    def test_of_several_only_some_with_mail_the_mail_is_said_with_an_if(
        self, world: World, tenant: FakeLicences
    ) -> None:
        tenant.skus = [
            tenant_sku(DEV, "Microsoft_Teams_Enterprise_New", ("TEAMS1",), total=4, used=4),
            tenant_sku(E3, "SPE_E3", E3_PLANS, total=4, used=4),
        ]
        world.walk(typed="\ns\n")
        assert "If that licence includes their mail, it is kept for 30 days" in world.flat

    def test_once_a_seat_is_free_setup_offers_it(self, world: World, tenant: FakeLicences) -> None:
        tenant.skus = [dict(item) for item in FULL_E3]

        def freed(number: int) -> None:
            if number == 2:  # by the first check again, someone has freed one
                tenant.skus[0]["consumedUnits"] = 24

        tenant.before_report = freed
        outcome = world.walk(typed="\n\n\n")
        assert outcome.teams == "assigned" and tenant.assigned == [E3]
        assert "There is a free licence now." in world.out

    def test_nothing_changed_is_said_and_it_asks_again(
        self, world: World, tenant: FakeLicences
    ) -> None:
        tenant.skus = [dict(item) for item in FULL_E3]
        outcome = world.walk(typed="\n\n\ns\n")
        assert outcome.teams == "skipped" and tenant.reports == 3
        assert world.out.count("Setup does not see a free licence yet.") == 2
        assert world.out.count("Press Enter when that is done") == 3

    def test_it_stops_asking_after_a_while(self, world: World, tenant: FakeLicences) -> None:
        tenant.skus = [dict(item) for item in FULL_E3]
        outcome = world.walk(typed="\n" * 40)
        assert outcome.teams == "skipped"
        assert tenant.reports == licences.MAX_CHECKS + 1
        assert f"Setup has checked {licences.MAX_CHECKS} times" in world.out

    def test_input_that_ends_is_a_skip_not_a_crash(
        self, world: World, tenant: FakeLicences
    ) -> None:
        tenant.skus = [dict(item) for item in FULL_E3]
        outcome = world.walk(typed="\n")  # yes to the offer, then nothing more
        assert outcome.teams == "skipped" and outcome.report is not None

    def test_without_a_keyboard_it_never_waits(self, world: World, tenant: FakeLicences) -> None:
        tenant.skus = [dict(item) for item in FULL_E3]
        outcome = world.walk(answers={licences.OFFER_KEY: "yes"})
        assert outcome.teams == "skipped" and tenant.reports == 1
        assert "Press Enter" not in world.out

    def test_an_unreadable_check_again_stops_the_walkthrough(
        self, world: World, tenant: FakeLicences
    ) -> None:
        tenant.skus = [dict(item) for item in FULL_E3]

        def broken(number: int) -> None:
            if number == 2:
                tenant.unreadable = "The licences could not be read: HTTP 503."

        tenant.before_report = broken
        outcome = world.walk(typed="\n\n")
        assert outcome.teams == "failed"
        assert "Setup could not read your organization's licences" in world.out


class TestNothingIncludesTeams:
    def test_it_says_what_to_get_and_who_buys_it(self, world: World, tenant: FakeLicences) -> None:
        tenant.skus = [tenant_sku(BI, "POWER_BI_PRO", POWER_BI_PLANS)]
        world.walk(typed="\ns\n")
        out = world.flat
        assert "None of your organization's licences includes Teams." in out
        assert "one licence of its own" in out and "as a person does" in out
        assert "Microsoft 365 Business Basic, Standard or Premium" in out
        assert "300 people" in out and "Microsoft Teams Essentials" in out
        assert "Microsoft Teams Enterprise" in out and "Microsoft Teams EEA" in out
        assert "billing administrator" in out and "Marketplace" in out
        # No prices: they change.
        assert not re.search(r"[$\u20ac\u00a3]|price|per user|/month", out, re.IGNORECASE)
        _lines_are_tidy(world.out)

    def test_a_no_teams_suite_is_named_first(self, world: World, tenant: FakeLicences) -> None:
        tenant.skus = [
            tenant_sku(E3NT, "Microsoft_365_E3_(no_Teams)", E3_NO_TEAMS_PLANS, total=9, used=9)
        ]
        world.walk(typed="\ns\n")
        out = world.flat
        assert "Your organization's Microsoft 365 E3 (no Teams) does not include Teams" in out
        advice = out[out.index("does not include Teams") :]
        assert advice.index("Microsoft Teams Enterprise") < advice.index("Business Basic")


class TestCopilot:
    def test_after_teams_a_free_copilot_licence_is_given_too(
        self, world: World, tenant: FakeLicences
    ) -> None:
        tenant.skus = [
            tenant_sku(SPB, "SPB", BUSINESS_PLANS, total=3, used=2),
            tenant_sku(COP, "Microsoft_365_Copilot", COPILOT_PLANS, total=2, used=1),
        ]
        outcome = world.walk(typed="\n\n\n", copilot=True)
        assert tenant.assigned == [SPB, COP]
        assert outcome.teams == "assigned" and outcome.copilot == "assigned"
        assert "Give the agent one Microsoft 365 Copilot licence? [Y/n]" in world.out

    def test_copilot_alone_when_the_agent_has_teams(
        self, world: World, tenant: FakeLicences
    ) -> None:
        tenant.skus = [
            tenant_sku(SPB, "SPB", BUSINESS_PLANS, total=3, used=3),
            tenant_sku(COP, "Microsoft_365_Copilot", COPILOT_PLANS, total=2, used=1),
        ]
        tenant.agent = [SPB]
        outcome = world.walk(typed="\n\n", teams=False, copilot=True)
        assert tenant.assigned == [COP] and outcome.copilot == "assigned"
        assert outcome.teams == "not-needed"

    @pytest.mark.parametrize(
        ("skus", "typed"),
        [
            ([tenant_sku(COP, "Microsoft_365_Copilot", COPILOT_PLANS, total=2, used=1)], "\ns\n"),
            (
                [
                    tenant_sku(SPB, "SPB", BUSINESS_PLANS, total=3, used=2),
                    tenant_sku(COP, "Microsoft_365_Copilot", COPILOT_PLANS, total=2, used=1),
                ],
                "\nn\n",
            ),
        ],
        ids=["teams-skipped", "teams-declined"],
    )
    def test_when_teams_is_left_for_later_nothing_comes_after_it(
        self, world: World, tenant: FakeLicences, skus: list[dict], typed: str
    ) -> None:
        # From a real run: "Skipped for now. What to do is on the last screen." and then two
        # more lines about Copilot. The last screen covers both.
        tenant.skus = skus
        outcome = world.walk(typed=typed, copilot=True)
        assert tenant.assigned == [] and outcome.copilot == "skipped"
        assert world.out.rstrip().endswith("What to do is on the last screen.")
        assert "add-on" not in world.out
        assert "Microsoft 365 Copilot licence?" not in world.out

    def test_no_copilot_licence_is_explained(self, world: World, tenant: FakeLicences) -> None:
        tenant.skus = [tenant_sku(SPB, "SPB", BUSINESS_PLANS, total=3, used=3)]
        tenant.agent = [SPB]
        outcome = world.walk(typed="\ns\n", teams=False, copilot=True)
        assert outcome.copilot == "skipped" and outcome.explained == {"copilot"}
        assert "Your organization has no Microsoft 365 Copilot licences." in world.out
        assert "Word, Excel and OneDrive files need one for the agent." in world.out
        _lines_are_tidy(world.out)


class TestTheOffer:
    def test_it_explains_first_and_enter_means_yes(
        self, world: World, tenant: FakeLicences
    ) -> None:
        tenant.skus = [tenant_sku(SPB, "SPB", BUSINESS_PLANS, total=3, used=2)]
        world.walk(typed="\n\n")
        out = world.out  # the order of lines matters here
        assert out.index("Licences for Microsoft 365") < out.index("[Y/n]")
        assert "The agent has no licence for Teams yet." in out
        assert "like a person" in out.lower()

    def test_no_reads_quietly_for_the_last_screen_and_changes_nothing(
        self, world: World, tenant: FakeLicences
    ) -> None:
        tenant.skus = [tenant_sku(SPB, "SPB", BUSINESS_PLANS, total=3, used=2)]
        outcome = world.walk(typed="n\n")
        assert outcome.teams == "skipped" and outcome.report is not None
        assert world.calls("--assign-licence") == [] and tenant.assigned == []
        assert "Your organization's licences" not in world.out

    def test_the_offer_for_copilot_alone_says_what_it_is_for(
        self, world: World, tenant: FakeLicences
    ) -> None:
        tenant.agent = []
        world.walk(typed="n\n", teams=False, copilot=True)
        assert "Word, Excel and OneDrive files need a Microsoft 365 Copilot licence" in world.out
        assert "once they are switched on" not in world.out  # only offered when they are on

    def test_without_copilot_it_is_never_mentioned(
        self, world: World, tenant: FakeLicences
    ) -> None:
        tenant.skus = [
            tenant_sku(COP, "Microsoft_365_Copilot", COPILOT_PLANS, total=3, used=3),
            tenant_sku(E3, "SPE_E3", E3_PLANS, total=4, used=4),
        ]
        world.walk(typed="\ns\n")
        assert "Copilot" not in world.out


class TestTheLastScreen:
    @pytest.mark.parametrize("report", [None, Report()], ids=["unread", "empty"])
    def test_without_teams_products_it_says_what_to_get(self, report: Report | None) -> None:
        text = _flat(" ".join(licences.last_screen_lines(report, "teams")))
        assert "https://admin.microsoft.com" in text and "administrator" in text
        assert "Run setup again. It gives the agent the licence by itself." in text
        assert "-Diagnose" in text

    def test_a_free_licence_that_was_not_given_is_named(self) -> None:
        report = Report.from_graph([tenant_sku(SPB, "SPB", BUSINESS_PLANS, total=3, used=1)], [])
        text = _flat(" ".join(licences.last_screen_lines(report, "teams")))
        assert "Microsoft 365 Business Premium has 2 free licences" in text
        assert "Run setup again. It gives the agent the licence by itself." in text

    def test_licences_all_in_use_get_the_two_ways_out(self) -> None:
        report = Report.from_graph(FULL_E3, [])
        text = _flat(" ".join(licences.last_screen_lines(report, "teams")))
        assert "all 25 of its licences are in use" in text and "Unassign licenses" in text

    def test_copilot(self) -> None:
        none = _flat(" ".join(licences.last_screen_lines(Report(), "copilot")))
        assert "Microsoft 365 Copilot" in none and "Marketplace" in none
        free = Report.from_graph([tenant_sku(COP, "Microsoft_365_Copilot", COPILOT_PLANS)], [])
        assert "has 5 free licences" in "\n".join(licences.last_screen_lines(free, "copilot"))

    @pytest.mark.parametrize("kind", ["teams", "copilot"])
    def test_after_the_walkthrough_showed_the_advice_it_is_not_repeated(self, kind: str) -> None:
        # From a real run: the full add-or-free advice in the walkthrough, then the same
        # block again on the last screen.
        report = Report.from_graph(FULL_E3, [])
        lines = licences.last_screen_lines(report, kind, shown=True)
        text = _flat(" ".join(lines))
        assert len(lines) <= 3
        assert 'above, under "Licences for Microsoft 365"' in text
        assert "run setup again" in text and "gives the agent the licence by itself" in text
        assert "Buy licenses" not in text and "Marketplace" not in text

    def test_every_line_fits_the_console(self) -> None:
        reports = [None, Report(), Report.from_graph(FULL_E3, [])]
        for report in reports:
            for kind in ("teams", "copilot"):
                for shown in (False, True):
                    for line in licences.last_screen_lines(report, kind, shown=shown):
                        assert len("       " + line) <= 96, line
                        assert line.isascii() and "!" not in line, line
