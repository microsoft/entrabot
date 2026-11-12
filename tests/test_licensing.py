"""What a Microsoft 365 product gives the agent, read from its service plans.

The products and their service plans here are copied from Microsoft's reference, "Product
names and service plan identifiers for licensing"
(https://learn.microsoft.com/entra/identity/users/licensing-service-plan-reference), trimmed
to the plans that matter plus a few look-alikes that must not count.
"""

from __future__ import annotations

from typing import Any

import pytest

from entrabot.licensing import (
    COPILOT_CAPABLE_SKUS,
    TEAMS_CAPABLE_SKUS,
    Product,
    Report,
    describe,
    free_seats,
    friendly_name,
    includes_copilot,
    includes_teams,
)


def _sku(
    part: str,
    plans: tuple[str, ...] | None = (),
    *,
    total: int = 5,
    used: int = 0,
    applies: str = "User",
    sku_id: str = "",
    plan_applies: str = "User",
) -> dict[str, Any]:
    sku: dict[str, Any] = {
        "skuId": sku_id or f"id-{part.lower()}",
        "skuPartNumber": part,
        "appliesTo": applies,
        "capabilityStatus": "Enabled",
        "prepaidUnits": {"enabled": total, "suspended": 0, "warning": 0},
        "consumedUnits": used,
    }
    if plans is not None:
        sku["servicePlans"] = [
            {
                "servicePlanId": f"plan-{name.lower()}",
                "servicePlanName": name,
                "provisioningStatus": "Success",
                "appliesTo": plan_applies,
            }
            for name in plans
        ]
    return sku


# Trimmed from the reference. Each keeps its Teams, Exchange and SharePoint plans, and the
# plans whose names say "Teams" without being Teams.
BUSINESS_BASIC = ("TEAMS1", "EXCHANGE_S_STANDARD", "SHAREPOINTSTANDARD", "SHAREPOINTWAC")
E3_NO_TEAMS = (
    "MESH_AVATARS_FOR_TEAMS",
    "MESH_IMMERSIVE_FOR_TEAMS",
    "CDS_O365_P2",  # "Common Data Service for Teams"
    "EXCHANGE_S_ENTERPRISE",
    "SHAREPOINTENTERPRISE",
    "SHAREPOINTWAC",
)
TEAMS_ENTERPRISE = ("TEAMS1", "EXCHANGE_S_DESKLESS", "ONEDRIVE_BASIC_P2", "SHAREPOINTWAC")
TEAMS_ESSENTIALS = ("TeamsEss",)  # Teams_Ess has this one plan and nothing else
TEAMS_PREMIUM = ("TEAMSPRO_MGMT", "TEAMSPRO_CUST", "TEAMSPRO_WEBINAR", "MICROSOFT_ECDN")
E5_EXTRA_FEATURES = ("MESH_AVATARS_FOR_TEAMS", "CLIPCHAMP", "MICROSOFT_LOOP")
F3_EEA_NO_TEAMS = ("TEAMS_DESKLESS", "EXCHANGE_S_DESKLESS", "SHAREPOINTDESKLESS")
SHARED_DEVICES = ("MCOEV", "TEAMS1", "MCOSTANDARD", "EXCHANGE_S_ENTERPRISE")
ROOMS_PRO = ("TEAMS1", "MCOEV", "WHITEBOARD_PLAN3")
COPILOT = ("M365_COPILOT_APPS", "M365_COPILOT_TEAMS", "M365_COPILOT_BUSINESS_CHAT")
COPILOT_FOR_SALES = (*COPILOT, "Microsoft_Copilot_for_Sales")


class TestWhatAProductIncludes:
    @pytest.mark.parametrize(
        ("part", "plans", "teams", "mailbox", "files"),
        [
            ("SMB_BUSINESS_ESSENTIALS", BUSINESS_BASIC, True, True, True),
            ("O365_BUSINESS_ESSENTIALS", BUSINESS_BASIC, True, True, True),
            ("SPB", BUSINESS_BASIC, True, True, True),
            ("Microsoft_Teams_Enterprise_New", TEAMS_ENTERPRISE, True, True, True),
            ("Teams_Ess", TEAMS_ESSENTIALS, True, False, False),
            ("Microsoft_365_E3_(no_Teams)", E3_NO_TEAMS, False, True, True),
            ("Microsoft_365_F3_EEA_(no_Teams)", F3_EEA_NO_TEAMS, False, True, True),
            ("Microsoft_Teams_Premium", TEAMS_PREMIUM, False, False, False),
            ("M365_E5_SUITE_COMPONENTS", E5_EXTRA_FEATURES, False, False, False),
        ],
        ids=lambda value: value if isinstance(value, str) else None,
    )
    def test_it_is_read_from_the_service_plans(
        self, part: str, plans: tuple[str, ...], teams: bool, mailbox: bool, files: bool
    ) -> None:
        product = describe(_sku(part, plans))
        assert (product.teams, product.mailbox, product.files) == (teams, mailbox, files)
        assert product.for_people

    def test_only_a_plan_that_applies_to_users_counts(self) -> None:
        assert not describe(_sku("SPB", ("TEAMS1",), plan_applies="Company")).teams
        assert describe(_sku("SPB", ("TEAMS1",), plan_applies="User")).teams

    def test_a_plan_whose_name_says_teams_is_not_teams(self) -> None:
        # Avatars for Teams, Immersive spaces for Teams and Common Data Service for Teams are
        # in suites that do not include Teams itself.
        assert not describe(_sku("X", ("MESH_AVATARS_FOR_TEAMS", "CDS_O365_P1"))).teams

    def test_exchange_foundation_is_not_a_mailbox_and_office_for_the_web_is_not_files(
        self,
    ) -> None:
        product = describe(_sku("M365_F1", ("TEAMS1", "EXCHANGE_S_FOUNDATION", "SHAREPOINTWAC")))
        assert product.teams and not product.mailbox and not product.files

    @pytest.mark.parametrize(
        ("part", "plans"),
        [
            ("MCOCAP", SHARED_DEVICES),
            ("Microsoft_Teams_Rooms_Pro", ROOMS_PRO),
            ("Microsoft_Teams_Rooms_Basic_without_Audio_Conferencing", ROOMS_PRO),
            ("MEETING_ROOM", (*ROOMS_PRO, "Teams_Room_Standard")),
            ("SOME_NEW_ROOM_SKU", ("TEAMS1", "Teams_Rooms_Pro")),
        ],
    )
    def test_room_and_shared_device_licences_are_never_for_the_agent(
        self, part: str, plans: tuple[str, ...]
    ) -> None:
        # They carry Microsoft Teams, but they are for meeting rooms and shared devices.
        product = describe(_sku(part, plans))
        assert not product.for_people
        assert not (product.teams or product.mailbox or product.files or product.copilot)

    def test_a_licence_for_the_whole_organization_is_not_for_the_agent(self) -> None:
        product = describe(_sku("SPB", BUSINESS_BASIC, applies="Company"))
        assert not product.for_people and not product.teams

    @pytest.mark.parametrize(
        ("part", "plans", "copilot"),
        [
            ("Microsoft_365_Copilot", COPILOT, True),
            ("M365_Copilot", COPILOT, True),
            ("Microsoft_365_Copilot_EDU", COPILOT, True),
            # A Copilot for one line of work includes the same plans; it is not offered.
            ("Microsoft_Copilot_for_Sales", COPILOT_FOR_SALES, False),
            ("SPE_E3", ("TEAMS1", "M365_COPILOT_TEAMS"), False),
        ],
    )
    def test_microsoft_365_copilot(self, part: str, plans: tuple[str, ...], copilot: bool) -> None:
        assert describe(_sku(part, plans)).copilot is copilot
        assert includes_copilot(_sku(part, plans)) is copilot

    def test_includes_teams_is_the_same_reading(self) -> None:
        assert includes_teams(_sku("SPB", BUSINESS_BASIC))
        assert not includes_teams(_sku("Microsoft_365_E3_(no_Teams)", E3_NO_TEAMS))


class TestWithoutServicePlans:
    """When Graph's answer carries no service plans, the part number decides."""

    @pytest.mark.parametrize(
        "part",
        [
            # On the list before, and still on it.
            "ENTERPRISEPREMIUM",
            "ENTERPRISEPACK",
            "SPE_E3",
            "SPE_E5",
            "SPE_F1",
            "STANDARDPACK",
            "DEVELOPERPACK",
            "DEVELOPERPACK_E5",
            "O365_BUSINESS_PREMIUM",
            "Microsoft_Teams_Enterprise",
            "TEAMS_ESSENTIALS_AAD",
            "TEAMS_EXPLORATORY",
            # Missing before.
            "SPB",
            "O365_BUSINESS_ESSENTIALS",
            "SMB_BUSINESS_ESSENTIALS",
            "Teams_Ess",
            "Microsoft_Teams_Enterprise_New",
            "Microsoft_Teams_EEA_New",
            "M365_F1",
            "DESKLESSPACK",
        ],
    )
    def test_known_teams_products_count(self, part: str) -> None:
        assert part in TEAMS_CAPABLE_SKUS
        assert describe(_sku(part, None)).teams

    @pytest.mark.parametrize(
        "part",
        [
            "M365_E5_SUITE_COMPONENTS",  # "Microsoft 365 E5 Extra Features": no Teams
            "TEAMS_PREMIUM",  # Teams Premium is an add-on to Teams, not Teams
            "Microsoft_Teams_Premium",
            "Microsoft_365_E3_(no_Teams)",
            "MCOCAP",
        ],
    )
    def test_products_without_teams_do_not(self, part: str) -> None:
        assert part not in TEAMS_CAPABLE_SKUS
        assert not describe(_sku(part, None)).teams

    def test_an_empty_plan_list_is_read_as_missing(self) -> None:
        assert describe(_sku("SPE_E3", ())).teams

    def test_the_part_number_is_matched_in_any_letter_case(self) -> None:
        assert describe(_sku("spe_e3", None)).teams
        assert describe(_sku("microsoft_365_copilot", None)).copilot

    def test_copilot_by_name(self) -> None:
        assert COPILOT_CAPABLE_SKUS == ("MICROSOFT_365_COPILOT", "Microsoft_365_Copilot")
        assert describe(_sku("MICROSOFT_365_COPILOT", None)).copilot
        assert not describe(_sku("SPE_E3", None)).copilot

    def test_what_a_mailbox_or_files_come_with_is_not_guessed(self) -> None:
        product = describe(_sku("SPE_E3", None))
        assert not product.mailbox and not product.files


class TestSeats:
    def test_free_is_enabled_less_assigned(self) -> None:
        assert describe(_sku("SPB", BUSINESS_BASIC, total=5, used=2)).free == 3
        assert free_seats(_sku("SPB", BUSINESS_BASIC, total=5, used=2)) == 3

    def test_more_assigned_than_enabled_is_none_free(self) -> None:
        # Seats move out of "enabled" when a subscription lapses; assignments stay.
        assert describe(_sku("SPB", BUSINESS_BASIC, total=3, used=5)).free == 0

    @pytest.mark.parametrize("units", [None, {}, {"enabled": "many"}, "5"])
    def test_an_answer_without_counts_is_none_free(self, units: object) -> None:
        sku = _sku("SPB", BUSINESS_BASIC)
        sku["prepaidUnits"] = units
        assert describe(sku).free == 0


class TestNames:
    @pytest.mark.parametrize(
        ("part", "name"),
        [
            ("SPB", "Microsoft 365 Business Premium"),
            ("O365_BUSINESS_PREMIUM", "Microsoft 365 Business Standard"),
            ("O365_BUSINESS_ESSENTIALS", "Microsoft 365 Business Basic"),
            ("SPE_E3", "Microsoft 365 E3"),
            ("ENTERPRISEPACK", "Office 365 E3"),
            ("SPE_F1", "Microsoft 365 F3"),
            ("Microsoft_365_E3_(no_Teams)", "Microsoft 365 E3 (no Teams)"),
            ("Microsoft_Teams_Enterprise_New", "Microsoft Teams Enterprise"),
            ("Teams_Ess", "Microsoft Teams Essentials"),
            ("Microsoft_365_Copilot", "Microsoft 365 Copilot"),
            ("spb", "Microsoft 365 Business Premium"),
            # Products with mail or files but no Teams, which the table lists too.
            ("EXCHANGESTANDARD", "Exchange Online (Plan 1)"),
            ("EXCHANGEENTERPRISE", "Exchange Online (Plan 2)"),
            ("OFFICESUBSCRIPTION", "Microsoft 365 Apps for enterprise"),
            ("SMB_BUSINESS", "Microsoft 365 Apps for business"),
            ("Microsoft_365_F3_EEA_(no_Teams)", "Microsoft 365 F3 EEA (no Teams)"),
            ("AGENT_365", "Agent 365"),
        ],
    )
    def test_common_products_by_the_name_people_know(self, part: str, name: str) -> None:
        assert friendly_name(part) == name
        assert describe(_sku(part, ())).name == name
        assert describe(_sku(part, ())).known_name

    @pytest.mark.parametrize(
        ("part", "name"),
        [
            # Seen on a real tenant; Microsoft's reference does not list either.
            ("MICROSOFT_AGENT_365_ROW_DEPT", "Microsoft Agent 365 ROW Dept"),
            ("MICROSOFT_AGENT_FRONTIER_NO_TEAMS", "Microsoft Agent Frontier (no Teams)"),
            ("CONTOSO_SPECIAL", "Contoso Special"),
            ("Contoso_Suite_(no_Teams)", "Contoso Suite (no Teams)"),
            ("POWER_BI_PRO", "Power BI PRO"),  # short words may be abbreviations: kept
        ],
    )
    def test_anything_else_gets_a_readable_name_never_the_raw_part_number(
        self, part: str, name: str
    ) -> None:
        # An executive must never see MICROSOFT_AGENT_365_ROW_DEPT as the only name; the part
        # number stays on the product (and in setup's log) for whoever needs it.
        assert friendly_name(part) == name
        product = describe(_sku(part, ()))
        assert product.name == name and "_" not in product.name
        assert product.part_number == part and not product.known_name

    def test_a_part_number_with_nothing_to_read_is_kept(self) -> None:
        assert friendly_name("") == ""
        assert friendly_name("E3") == "E3"


def _report(*skus: dict[str, Any], agent: tuple[str, ...] = ()) -> Report:
    return Report.from_graph(
        list(skus), [{"skuId": sku_id, "disabledPlans": []} for sku_id in agent]
    )


class TestReport:
    def test_the_agents_own_licences_are_read_against_the_tenants(self) -> None:
        report = _report(
            _sku("SPB", BUSINESS_BASIC, sku_id="spb"),
            _sku("Microsoft_365_Copilot", COPILOT, sku_id="cop"),
            agent=("cop",),
        )
        assert report.agent_has("copilot") and not report.agent_has("teams")
        assert [product.name for product in report.agent_products()] == ["Microsoft 365 Copilot"]

    def test_a_licence_the_tenant_does_not_list_is_kept_but_counts_for_nothing(self) -> None:
        report = _report(_sku("SPB", BUSINESS_BASIC, sku_id="spb"), agent=("gone",))
        assert report.agent == ("gone",) and not report.agent_has("teams")

    def test_free_licences_for_teams_prefer_the_one_with_mail_and_files(self) -> None:
        report = _report(
            _sku("Teams_Ess", TEAMS_ESSENTIALS, sku_id="ess"),
            _sku("SPB", BUSINESS_BASIC, sku_id="spb", total=2, used=2),  # none free
            _sku("Microsoft_Teams_Enterprise_New", TEAMS_ENTERPRISE, sku_id="ent"),
            _sku("SPE_E3", ("TEAMS1", "EXCHANGE_S_ENTERPRISE"), sku_id="e3"),  # no files
            _sku("MCOCAP", SHARED_DEVICES, sku_id="dev"),
        )
        assert [product.sku_id for product in report.free_for("teams")] == ["ent", "e3", "ess"]
        assert [product.sku_id for product in report.products_with("teams")] == [
            "ess",
            "spb",
            "ent",
            "e3",
        ]

    def test_free_copilot_licences(self) -> None:
        report = _report(
            _sku("Microsoft_365_Copilot", COPILOT, sku_id="a", total=1, used=1),
            _sku("M365_Copilot", COPILOT, sku_id="b"),
        )
        assert [product.sku_id for product in report.free_for("copilot")] == ["b"]

    def test_it_travels_as_json_and_back(self) -> None:
        report = _report(
            _sku("SPB", BUSINESS_BASIC, sku_id="spb", total=10, used=3),
            _sku("MCOCAP", SHARED_DEVICES, sku_id="dev"),
            agent=("spb",),
        )
        assert Report.from_json(report.to_json()) == report

    @pytest.mark.parametrize(
        "payload",
        [None, [], {"products": "x", "agent": []}, {"products": [{"sku_id": 1}], "agent": []}],
    )
    def test_a_malformed_payload_is_refused(self, payload: object) -> None:
        with pytest.raises(ValueError):
            Report.from_json(payload)


def test_a_product_says_what_it_has_by_kind() -> None:
    product = Product(sku_id="x", part_number="X", name="X", teams=True)
    assert product.has("teams") and not product.has("copilot")
