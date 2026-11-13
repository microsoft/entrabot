"""What a Microsoft 365 product gives the agent: Teams, a mailbox, files, Microsoft 365 Copilot.

A product is a subscribed SKU as Microsoft Graph lists it (``GET /subscribedSkus``). What it
includes is read from its service plans (``servicePlans``), by the plan names in Microsoft's
reference, "Product names and service plan identifiers for licensing"
(https://learn.microsoft.com/entra/identity/users/licensing-service-plan-reference). A product
"includes Teams" when it carries the Microsoft Teams plan for users, whatever it is called:
the "(no Teams)" suites sold since April 2024 carry plans named after Teams (Avatars for
Teams, Common Data Service for Teams) and not Teams itself, and Teams Premium is an add-on
to Teams. When an answer carries no service plans, the part number is looked up in
:data:`TEAMS_CAPABLE_SKUS` and :data:`COPILOT_CAPABLE_SKUS` instead.

Licences for meeting rooms and shared devices carry the Teams plan too, but they are not for
people, so they never count for the agent; nor does a product that applies to the whole
organization rather than to a user.

Both setup front ends use this: scripts/setup.sh through ``entrabot.preflight`` and
``scripts/create_entra_agent_ids.py``, and the Windows wizard. Nothing here talks to the
network or imports ``entrabot.config``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, fields
from typing import Any, Literal

Kind = Literal["teams", "copilot"]

# Service plans by servicePlanName, as the reference lists them. Gov names are the
# reference's own (Microsoft Teams for Government, for GCCHigh).
TEAMS_PLANS = frozenset({"TEAMS1", "TeamsEss", "TEAMS_GOV", "TEAMS_AR_GCCHIGH"})
# Exchange Online (Plan 1), (Plan 2) and Kiosk. Not Exchange Foundation, which has no mailbox.
MAILBOX_PLANS = frozenset(
    {
        "EXCHANGE_S_STANDARD",
        "EXCHANGE_S_ENTERPRISE",
        "EXCHANGE_S_DESKLESS",
        "EXCHANGE_S_DESKLESS_GOV",
    }
)
# SharePoint (Plan 1), (Plan 2), Kiosk, for developers, and OneDrive for Business (Basic 2).
# Not Office for the web (SHAREPOINTWAC), which is the apps, not storage.
FILES_PLANS = frozenset(
    {
        "SHAREPOINTSTANDARD",
        "SHAREPOINTENTERPRISE",
        "SHAREPOINTDESKLESS",
        "SHAREPOINTENTERPRISE_MIDMARKET",
        "SHAREPOINT_S_DEVELOPER",
        "SHAREPOINTDESKLESS_GOV",
        "ONEDRIVE_BASIC_P2",
    }
)
# Microsoft 365 Copilot in the apps, and its Graph-grounded chat. Copilot for one line of work
# (for Sales, for Service) carries the same plans; it is not offered to the agent.
COPILOT_PLANS = frozenset({"M365_COPILOT_APPS", "M365_COPILOT_BUSINESS_CHAT"})
_LINE_OF_WORK_COPILOT = re.compile(r"copilot_for_", re.IGNORECASE)
# Plans only meeting-room and shared-device licences carry, and their part numbers.
ROOM_AND_DEVICE_PLANS = frozenset(
    {
        "Teams_Room_Standard",
        "Teams_Room_Basic",
        "Teams_Room_Pro",
        "Teams_Rooms_Basic",
        "Teams_Rooms_Pro",
        "MTRProManagement",
        "SPECIALTY_DEVICES",
    }
)
_ROOM_AND_DEVICE_PREFIXES = ("mcocap", "meeting_room", "microsoft_teams_rooms")
_SOLD_WITHOUT_TEAMS = re.compile(r"no[ _]teams|w/o[ _]teams", re.IGNORECASE)

# The fallback when an answer lists no service plans: part numbers ("String ID" in the
# reference) of products that include Teams for a user. Kept from the list this replaced,
# less two that do not include Teams (M365_E5_SUITE_COMPONENTS is "Microsoft 365 E5 Extra
# Features"; Teams Premium is an add-on to Teams), plus common products it missed.
TEAMS_CAPABLE_SKUS: tuple[str, ...] = (
    "ENTERPRISEPREMIUM",  # Office 365 E5
    "ENTERPRISEPACK",  # Office 365 E3
    "STANDARDPACK",  # Office 365 E1
    "STANDARDWOFFPACK",  # Office 365 E2
    "DESKLESSPACK",  # Office 365 F3
    "SPE_E3",  # Microsoft 365 E3
    "SPE_E5",  # Microsoft 365 E5
    "SPE_E5_NOPSTNCONF",  # Microsoft 365 E5 without Audio Conferencing
    "SPE_F1",  # Microsoft 365 F3 (despite the name)
    "M365_F1",  # Microsoft 365 F1
    "M365_F1_COMM",  # Microsoft 365 F1
    "DEVELOPERPACK",  # Office 365 E3 Developer
    "DEVELOPERPACK_E5",  # Microsoft 365 E5 Developer
    "O365_BUSINESS_ESSENTIALS",  # Microsoft 365 Business Basic
    "SMB_BUSINESS_ESSENTIALS",  # Microsoft 365 Business Basic
    "O365_BUSINESS_PREMIUM",  # Microsoft 365 Business Standard (despite the name)
    "SPB",  # Microsoft 365 Business Premium
    "M365_BUSINESS_PREMIUM",  # kept from the original list; not in Microsoft's reference
    "Microsoft_Teams_Enterprise",  # Microsoft Teams Enterprise
    "Microsoft_Teams_Enterprise_New",  # Microsoft Teams Enterprise
    "Microsoft_Teams_EEA",  # Microsoft Teams Enterprise EEA
    "Microsoft_Teams_EEA_New",  # Microsoft Teams EEA
    "Teams_Ess",  # Microsoft Teams Essentials
    "TEAMS_ESSENTIALS_AAD",  # Microsoft Teams Essentials (Microsoft Entra identity)
    "Microsoft_Teams_Essentials",  # kept from the original list; not in Microsoft's reference
    "TEAMS_EXPLORATORY",  # Microsoft Teams Exploratory
    "Microsoft_Teams_Exploratory_Dept",  # Microsoft Teams Exploratory Dept
    "MS_TEAMS_IW",  # Microsoft Teams Trial
)

COPILOT_CAPABLE_SKUS: tuple[str, ...] = (
    "MICROSOFT_365_COPILOT",
    "Microsoft_365_Copilot",
)

# The names the Microsoft 365 admin center shows for common products, from the reference
# (and the Teams licensing page for Microsoft_Teams_Enterprise and Microsoft_Teams_EEA).
FRIENDLY_NAMES: dict[str, str] = {
    "O365_BUSINESS_ESSENTIALS": "Microsoft 365 Business Basic",
    "SMB_BUSINESS_ESSENTIALS": "Microsoft 365 Business Basic",
    "O365_BUSINESS_PREMIUM": "Microsoft 365 Business Standard",
    "SPB": "Microsoft 365 Business Premium",
    "Microsoft_365_Business_Basic_(no Teams)": "Microsoft 365 Business Basic (no Teams)",
    "Microsoft_365_Business_Basic_EEA_(no_Teams)": "Microsoft 365 Business Basic EEA (no Teams)",
    "Office_365_w/o_Teams_Bundle_Business_Standard": (
        "Microsoft 365 Business Standard EEA (no Teams)"
    ),
    "Office_365_w/o_Teams_Bundle_Business_Premium": (
        "Microsoft 365 Business Premium EEA (no Teams)"
    ),
    "SPE_E3": "Microsoft 365 E3",
    "SPE_E5": "Microsoft 365 E5",
    "SPE_E5_NOPSTNCONF": "Microsoft 365 E5 without Audio Conferencing",
    "SPE_F1": "Microsoft 365 F3",
    "M365_F1": "Microsoft 365 F1",
    "M365_F1_COMM": "Microsoft 365 F1",
    "Microsoft_365_E3_(no_Teams)": "Microsoft 365 E3 (no Teams)",
    "Microsoft_365_E5_(no_Teams)": "Microsoft 365 E5 (no Teams)",
    "O365_w/o Teams Bundle_M3": "Microsoft 365 E3 EEA (no Teams)",
    "STANDARDPACK": "Office 365 E1",
    "STANDARDWOFFPACK": "Office 365 E2",
    "ENTERPRISEPACK": "Office 365 E3",
    "ENTERPRISEPREMIUM": "Office 365 E5",
    "DESKLESSPACK": "Office 365 F3",
    "Office_365_E3_(no_Teams)": "Office 365 E3 (no Teams)",
    "O365_w/o_Teams_Bundle_E3": "Office 365 E3 EEA (no Teams)",
    "Office_365_w/o_Teams_Bundle_E5": "Office 365 E5 EEA (no Teams)",
    "DEVELOPERPACK": "Office 365 E3 Developer",
    "DEVELOPERPACK_E5": "Microsoft 365 E5 Developer",
    "M365_E5_SUITE_COMPONENTS": "Microsoft 365 E5 Extra Features",
    "Microsoft_Teams_Enterprise": "Microsoft Teams Enterprise",
    "Microsoft_Teams_Enterprise_New": "Microsoft Teams Enterprise",
    "Microsoft_Teams_EEA": "Microsoft Teams Enterprise EEA",
    "Microsoft_Teams_EEA_New": "Microsoft Teams EEA",
    "Teams_Ess": "Microsoft Teams Essentials",
    "TEAMS_ESSENTIALS_AAD": "Microsoft Teams Essentials",
    "TEAMS_EXPLORATORY": "Microsoft Teams Exploratory",
    "MS_TEAMS_IW": "Microsoft Teams Trial",
    "Microsoft_Teams_Premium": "Microsoft Teams Premium",
    "MCOCAP": "Microsoft Teams Shared Devices",
    "MEETING_ROOM": "Microsoft Teams Rooms Standard",
    "Microsoft_Teams_Rooms_Basic": "Microsoft Teams Rooms Basic",
    "Microsoft_Teams_Rooms_Pro": "Microsoft Teams Rooms Pro",
    "Microsoft_365_Copilot": "Microsoft 365 Copilot",
    "MICROSOFT_365_COPILOT": "Microsoft 365 Copilot",
    "M365_Copilot": "Microsoft 365 Copilot",
    "Microsoft_365_Copilot_EDU": "Microsoft 365 Copilot (Education Faculty)",
    # With mail or files but not Teams: the walkthrough's table lists these too.
    "Microsoft_365_F3_EEA_(no_Teams)": "Microsoft 365 F3 EEA (no Teams)",
    "EXCHANGESTANDARD": "Exchange Online (Plan 1)",
    "EXCHANGEENTERPRISE": "Exchange Online (Plan 2)",
    "OFFICESUBSCRIPTION": "Microsoft 365 Apps for enterprise",
    "SMB_BUSINESS": "Microsoft 365 Apps for business",
    # Microsoft Agent 365 as the reference names it. Its Frontier licences for agents
    # (MICROSOFT_AGENT_FRONTIER_NO_TEAMS, MICROSOFT_AGENT_365_ROW_DEPT on a real tenant) are
    # not in the reference, so they get a readable name from their part number instead.
    "AGENT_365": "Agent 365",
}
_NAMES_BY_KEY = {part.casefold(): name for part, name in FRIENDLY_NAMES.items()}
_SPACES = re.compile(r"[_\s]+")
_NO_TEAMS_WORDS = re.compile(r"\(?\bno teams\b\)?", re.IGNORECASE)


def _folded(names: Iterable[str]) -> frozenset[str]:
    return frozenset(name.casefold() for name in names)


_TEAMS = _folded(TEAMS_PLANS)
_MAILBOX = _folded(MAILBOX_PLANS)
_FILES = _folded(FILES_PLANS)
_COPILOT = _folded(COPILOT_PLANS)
_ROOMS = _folded(ROOM_AND_DEVICE_PLANS)
_TEAMS_BY_NAME = _folded(TEAMS_CAPABLE_SKUS)
_COPILOT_BY_NAME = _folded(COPILOT_CAPABLE_SKUS)


@dataclass(frozen=True)
class Product:
    """One product the organization subscribes to, as the agent would see it."""

    sku_id: str
    part_number: str
    name: str
    teams: bool = False
    mailbox: bool = False
    files: bool = False
    copilot: bool = False
    total: int = 0  # licences the subscription has
    used: int = 0  # licences assigned to someone
    for_people: bool = True  # not a meeting-room, shared-device or whole-organization licence

    @property
    def free(self) -> int:
        return max(0, self.total - self.used)

    def has(self, kind: Kind) -> bool:
        if kind == "teams":
            return self.teams
        if kind == "copilot":
            return self.copilot
        raise ValueError(f"unknown kind of licence: {kind!r}")

    @property
    def sold_without_teams(self) -> bool:
        """A "(no Teams)" version of a suite, by its part number or name."""
        return bool(
            _SOLD_WITHOUT_TEAMS.search(self.part_number) or _SOLD_WITHOUT_TEAMS.search(self.name)
        )

    @property
    def known_name(self) -> bool:
        """Is :attr:`name` the one the admin center shows? Otherwise it was made readable
        from the part number, which the admin center does not show."""
        return self.part_number.casefold() in _NAMES_BY_KEY


def _readable(part_number: str) -> str:
    """A name a person can read, for a part number Microsoft's reference does not list:
    spaces for underscores, "(no Teams)" written the way product names write it, and long
    words in capitals as words. Short ones (ROW, EEA, PRO) stay as they are: they may be
    abbreviations, and nothing here guesses what they stand for."""
    text = _NO_TEAMS_WORDS.sub("(no Teams)", _SPACES.sub(" ", part_number).strip())
    words = [
        word.capitalize() if word.isalpha() and word.isupper() and len(word) >= 4 else word
        for word in text.split(" ")
    ]
    return " ".join(words)


def friendly_name(part_number: str) -> str:
    """The name the Microsoft 365 admin center shows, for common products; else a readable
    name made from the part number (:func:`_readable`), never the raw part number."""
    return _NAMES_BY_KEY.get(part_number.casefold()) or _readable(part_number)


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def free_seats(sku: Mapping[str, Any]) -> int:
    """Licences of *sku* not assigned to anyone yet."""
    return describe(sku).free


def _user_plans(sku: Mapping[str, Any]) -> frozenset[str] | None:
    """The names of the service plans in *sku* that apply to a user, folded; None when the
    answer lists no service plans at all. A plan with no appliesTo counts as a user's."""
    plans = sku.get("servicePlans")
    if not isinstance(plans, list) or not plans:
        return None
    names = set()
    for plan in plans:
        if not isinstance(plan, Mapping):
            continue
        applies = str(plan.get("appliesTo") or "User")
        if applies.casefold() == "user":
            names.add(str(plan.get("servicePlanName") or "").casefold())
    return frozenset(names)


def _room_or_device(part: str, plans: frozenset[str] | None) -> bool:
    if part.casefold().startswith(_ROOM_AND_DEVICE_PREFIXES):
        return True
    return bool(plans and plans & _ROOMS)


def describe(sku: Mapping[str, Any]) -> Product:
    """What *sku* (one entry of Graph's subscribedSkus answer) gives the agent."""
    part = str(sku.get("skuPartNumber") or "")
    plans = _user_plans(sku)
    units = sku.get("prepaidUnits")
    total = _count(units.get("enabled")) if isinstance(units, Mapping) else 0
    for_people = str(sku.get("appliesTo") or "User").casefold() == "user" and not _room_or_device(
        part, plans
    )
    if not for_people:
        teams = mailbox = files = copilot = False
    elif plans is None:
        teams = part.casefold() in _TEAMS_BY_NAME
        copilot = part.casefold() in _COPILOT_BY_NAME
        mailbox = files = False  # not guessed from a name
    else:
        teams = bool(plans & _TEAMS)
        mailbox = bool(plans & _MAILBOX)
        files = bool(plans & _FILES)
        copilot = bool(plans & _COPILOT) and not _LINE_OF_WORK_COPILOT.search(part)
    return Product(
        sku_id=str(sku.get("skuId") or ""),
        part_number=part,
        name=friendly_name(part),
        teams=teams,
        mailbox=mailbox,
        files=files,
        copilot=copilot,
        total=total,
        used=_count(sku.get("consumedUnits")),
        for_people=for_people,
    )


def includes_teams(sku: Mapping[str, Any]) -> bool:
    return describe(sku).teams


def includes_copilot(sku: Mapping[str, Any]) -> bool:
    return describe(sku).copilot


def preference(product: Product, kind: Kind) -> tuple[int, ...]:
    """Sort key: for Teams, a product that also brings mail and files comes first, then one
    with mail; for Copilot there is no preference."""
    if kind == "teams":
        return (0 if product.mailbox and product.files else 1, 0 if product.mailbox else 1)
    return ()


_PRODUCT_FIELDS = {item.name: item.type for item in fields(Product)}


def _product_from_json(item: object) -> Product:
    if not isinstance(item, Mapping):
        raise ValueError("a product is not an object")
    values: dict[str, Any] = {}
    for name in _PRODUCT_FIELDS:
        if name not in item:
            raise ValueError(f"a product has no {name!r}")
        value = item[name]
        expected = {
            "sku_id": str,
            "part_number": str,
            "name": str,
            "total": int,
            "used": int,
        }.get(name, bool)
        if expected is int and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(f"a product's {name!r} is not a number")
        if not isinstance(value, expected):
            raise ValueError(f"a product's {name!r} is not a {expected.__name__}")
        values[name] = value
    return Product(**values)


@dataclass(frozen=True)
class Report:
    """What the organization has, and which of it the agent has."""

    products: tuple[Product, ...] = ()
    agent: tuple[str, ...] = ()  # the skuIds assigned to the agent

    @classmethod
    def from_graph(cls, skus: Iterable[object], assigned: Iterable[object]) -> Report:
        """From Graph's subscribedSkus values and the agent's assignedLicenses."""
        products = tuple(describe(sku) for sku in skus if isinstance(sku, Mapping))
        agent = tuple(
            str(item["skuId"])
            for item in assigned
            if isinstance(item, Mapping) and item.get("skuId")
        )
        return cls(products, agent)

    def to_json(self) -> dict[str, Any]:
        return {
            "products": [asdict(product) for product in self.products],
            "agent": list(self.agent),
        }

    @classmethod
    def from_json(cls, data: object) -> Report:
        """Read :meth:`to_json`'s output back. Anything else raises ValueError."""
        if not isinstance(data, Mapping):
            raise ValueError("the licence report is not an object")
        products, agent = data.get("products"), data.get("agent")
        if not isinstance(products, list) or not isinstance(agent, list):
            raise ValueError("the licence report has no product or agent list")
        if not all(isinstance(item, str) for item in agent):
            raise ValueError("the agent's licences are not IDs")
        return cls(tuple(_product_from_json(item) for item in products), tuple(agent))

    def agent_products(self) -> list[Product]:
        mine = {sku_id.casefold() for sku_id in self.agent}
        return [product for product in self.products if product.sku_id.casefold() in mine]

    def agent_has(self, kind: Kind) -> bool:
        return any(product.has(kind) for product in self.agent_products())

    def products_with(self, kind: Kind) -> list[Product]:
        """The organization's products of *kind*, free licences or not, as Graph lists them."""
        return [product for product in self.products if product.for_people and product.has(kind)]

    def free_for(self, kind: Kind) -> list[Product]:
        """Products of *kind* with a free licence, the best choice for the agent first."""
        free = [product for product in self.products_with(kind) if product.free > 0]
        return sorted(free, key=lambda product: preference(product, kind))
