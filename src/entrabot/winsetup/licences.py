"""After setup: get the agent the Microsoft 365 licences it needs, with the person who runs it.

The agent's user account is a user in Microsoft 365, and like a person it needs licences of
its own: one that includes Teams (most also bring mail and files), and a Microsoft 365
Copilot licence for Word, Excel and OneDrive files (Work IQ). The agent step gives it those
by itself when one is free. When none was, this walks the person through it: it shows what
the organization has, gives the agent a free licence when the person says yes, or says who
can add or free one and where, and checks again when the person says that is done.

Everything that reads or changes the tenant runs in a child, ``create_entra_agent_ids.py``'s
licence-only mode (``--licence-report``, ``--assign-licence``), with the Provisioner's token
and the agent step's own assignment code (usage location first). The signed-in person's Azure
CLI token never changes the agent's account. What a product includes is
``entrabot.licensing``'s reading of its service plans.

The admin center's menus and roles are from Microsoft Learn: "Buy or remove licenses for a
Microsoft business subscription", "Try or buy a Microsoft 365 for business subscription",
and "Assign or unassign licenses for users in the Microsoft 365 admin center". Which products
include Teams since April 2024 is from "Assign Teams add-on licenses to users", and Copilot
as an add-on from "License options for Microsoft Copilot". No prices: they change.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from entrabot.licensing import Kind, Product, Report, preference
from entrabot.winsetup.console import Cancelled, Choice, Console
from entrabot.winsetup.helpers import RESULT_MARKER, Helpers, marked_line

SCRIPT = "create_entra_agent_ids.py"
REPORT_ARG = "--licence-report"
ASSIGN_ARG = "--assign-licence"

# What the script prints after RESULT_MARKER. Tests on both sides pin them.
ASSIGNED = "licence-assigned"
ALREADY_ASSIGNED = "licence-already-assigned"
NO_FREE_SEAT = "licence-no-free-seat"
NOT_FOUND = "licence-not-found"
ASSIGN_FAILED = "licence-assign-failed"
COULD_NOT_READ = "licence-could-not-read"

# Answer keys, so a run can be driven without a keyboard.
OFFER_KEY = "licence_walkthrough"  # walk through it now? yes/no
CONFIRM_KEYS: dict[Kind, str] = {"teams": "licence_teams", "copilot": "licence_copilot"}
PICK_KEYS: dict[Kind, str] = {"teams": "licence_teams_pick", "copilot": "licence_copilot_pick"}
RECHECK_KEY = "licence_recheck"  # "" checks again, "s" skips
NONE_FOR_NOW = "none"
# How many times the person can say "check again" before setup stops asking.
MAX_CHECKS = 10

ADMIN_CENTER = "https://admin.microsoft.com"
DIAGNOSE = "scripts\\setup-windows.cmd -Diagnose"
SECTION = "Licences for Microsoft 365"  # the walkthrough's heading, which the last screen names
# Text width after the console's own indent (seven characters): lines stay under 96.
_WIDTH = 86
# Joins the words of a product name or a menu path, so a line never breaks inside one. Only
# textwrap sees it: _wrapped turns it back into a space.
_JOIN = "\u00a0"


def whole(text: str) -> str:
    """*text* kept on one line when wrapped: a product name, or a menu path."""
    return text.replace(" ", _JOIN)


_LABEL: dict[Kind, str] = {"teams": "Teams", "copilot": "Microsoft 365 Copilot"}
_READY: dict[Kind, tuple[str, ...]] = {
    "teams": (
        "A new licence takes 10-15 minutes to reach Teams. To check later, run:",
        f"  {DIAGNOSE}",
    ),
    "copilot": ("Word, Excel and OneDrive files can take 10-15 minutes to be ready with it.",),
}
_LAST_SCREEN = "What to do is on the last screen."
_WAIT = "Press Enter when that is done and setup will check again, or type s to skip for now"
_BUY = (
    f"A billing administrator buys one in the {whole('Microsoft 365 admin center')} "
    f"({ADMIN_CENTER}): Marketplace. If your organization buys through a partner, ask them "
    "instead."
)

Got = Literal["assigned", "had", "skipped", "failed", "not-needed"]


class ReadFailed(Exception):
    """The licences could not be read. The message is the helper's own reason."""


@dataclass
class Outcome:
    """What the walkthrough left the agent with, for each kind of licence it was run for."""

    teams: Got = "not-needed"
    copilot: Got = "not-needed"
    products: dict[str, Product] = field(default_factory=dict)  # kind -> the agent's licence
    report: Report | None = None  # the last one read: the last screen's advice uses it
    # The kinds whose add-or-free advice the person was shown: the last screen points back
    # to it instead of printing it again.
    explained: set[Kind] = field(default_factory=set)


def _wrapped(text: str, *, rest: str = "") -> list[str]:
    lines = textwrap.wrap(
        text, width=_WIDTH, subsequent_indent=rest, break_long_words=False, break_on_hyphens=False
    )
    return [line.replace(_JOIN, " ") for line in lines]


def _listed(items: Sequence[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _licences(count: int) -> str:
    return f"{count} free licence" + ("" if count == 1 else "s")


def includes(product: Product, *, copilot: bool) -> str:
    """What *product* gives the agent, in words: "Teams, mail and files"."""
    if not product.for_people:
        return "not for a person"
    parts = [
        label
        for present, label in (
            (product.teams, "Teams"),
            (product.mailbox, "mail"),
            (product.files, "files"),
            (copilot and product.copilot, "Copilot"),
        )
        if present
    ]
    return _listed(parts) if parts else "nothing the agent uses"


# ── Reading and assigning, through the agent script ─────────────────────────────────────────


def _last_words(result: subprocess.CompletedProcess[str]) -> str:
    for line in result.stdout.splitlines():
        text = line.strip()
        if text.startswith("ERROR:"):
            return text[len("ERROR:") :].strip()[:300]
    errors = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    return errors[-1][:300] if errors else "the helper said nothing about why"


def read_report(helpers: Helpers, env: Mapping[str, str]) -> Report:
    """What the organization has and which of it the agent has. Changes nothing."""
    result = helpers.script(
        SCRIPT,
        [REPORT_ARG],
        env=env,
        capture=True,
        quiet=True,
        heartbeat="Still reading your organization's licences",
    )
    raw = (marked_line(result.stdout, RESULT_MARKER) or "").strip()
    if raw.startswith("{"):
        try:
            return Report.from_json(json.loads(raw))
        except ValueError as exc:
            raise ReadFailed("the helper's answer could not be read") from exc
    raise ReadFailed(_last_words(result))


def read_quietly(helpers: Helpers, env: Mapping[str, str]) -> Report | None:
    """:func:`read_report`, or None: for advice, where not knowing is not a problem."""
    try:
        return read_report(helpers, env)
    except ReadFailed:
        return None


def _assign(
    helpers: Helpers, env: Mapping[str, str], product: Product
) -> tuple[str, subprocess.CompletedProcess[str]]:
    result = helpers.script(
        SCRIPT,
        [ASSIGN_ARG, product.sku_id],
        env=env,
        capture=True,
        quiet=True,
        heartbeat="Still giving the agent its licence",
    )
    return (marked_line(result.stdout, RESULT_MARKER) or "").strip(), result


# ── What to say ─────────────────────────────────────────────────────────────────────────────


def _mail(products: Sequence[Product]) -> str:
    """What freeing one of *products* from a person does to their mail. Microsoft Learn,
    "Assign or unassign licenses for users": their Exchange Online data is held for 30 days,
    then deleted. Said only of a licence that brings the mailbox: Teams Enterprise, say,
    does not, and taking it away leaves the person's mail alone."""
    with_mail = [product for product in products if product.mailbox]
    if not with_mail:
        return ""
    if len(with_mail) == len(products):
        return " That person's mail is kept for 30 days, then deleted."
    return " If that licence includes their mail, it is kept for 30 days, then deleted."


def _two_ways(products: Sequence[Product], kind: Kind) -> list[str]:
    """Add a licence or free one, with the admin center's menus (Dashboard view, which most
    people see). For several products, the menus name the first as an example."""
    if len(products) == 1:
        name = products[0].name
        buy = whole(f"Billing > Your products > {name} > Buy licenses")
        unassign = f"{whole(f'Billing > Licenses > {name}')}, select that person"
    else:
        example = whole(f"(for example {products[0].name})")
        buy = (
            f"{whole('Billing > Your products')}, select the product {example}, then "
            f"{whole('Buy licenses')}"
        )
        unassign = f"{whole('Billing > Licenses')}, select the product {example} and that person"
    add = (
        "- Add a licence. A billing administrator does this in the "
        f"{whole('Microsoft 365 admin center')} ({ADMIN_CENTER}): {buy}. If your organization "
        "buys through a partner, ask them instead."
    )
    free = (
        "- Or free one from someone who no longer needs it. A licence or user administrator "
        f"does this in the admin center: {unassign}, then {whole('Unassign licenses')}."
    )
    if kind == "teams":
        free += _mail(products)
    return [
        "There are two ways to get the agent one:",
        *_wrapped(add, rest="  "),
        *_wrapped(free, rest="  "),
    ]


def _ordered(products: Sequence[Product], kind: Kind) -> list[Product]:
    """Ones the admin center shows by these names first, so the example can be found there,
    then the best for the agent."""
    return sorted(
        products, key=lambda product: (not product.known_name, *preference(product, kind))
    )


def _all_in_use(products: Sequence[Product], kind: Kind) -> tuple[str, list[str]]:
    products = _ordered(products, kind)
    first = products[0]
    if len(products) > 1:
        names = _listed([whole(product.name) for product in products])
        headline = f"{names} include {whole(_LABEL[kind])}, but all their licences are in use."
    elif kind == "teams":
        headline = (
            f"{whole(first.name)} includes Teams, but all {first.total} of its licences are in use."
        )
    else:
        headline = (
            f"{whole(first.name)} is in your organization, but all {first.total} of its "
            "licences are in use."
        )
    return headline, _two_ways(products, kind)


def _nothing(report: Report | None, kind: Kind) -> tuple[str, list[str]]:
    if kind == "copilot":
        return "Your organization has no Microsoft 365 Copilot licences.", [
            "Word, Excel and OneDrive files need one for the agent.",
            *_wrapped(
                "Microsoft 365 Copilot is an add-on to a licence like the agent's Teams licence. "
                + _BUY
            ),
        ]
    suites = [
        product
        for product in (report.products if report is not None else ())
        if product.for_people and product.sold_without_teams
    ][:2]
    business = (
        f"- {whole('Microsoft 365 Business Basic')}, Standard or Premium, which also bring mail "
        "and files. They are for organizations of up to 300 people, and each is also sold "
        "without Teams: choose the version with Teams."
    )
    essentials = f"- {whole('Microsoft Teams Essentials')}."
    enterprise = (
        f'- {whole("Microsoft Teams Enterprise")}, which goes with a "(no Teams)" '
        f"{whole('Microsoft 365')} or {whole('Office 365 E1, E3 or E5')} "
        f"({whole('Microsoft Teams EEA')} in the {whole('European Economic Area')} and "
        "Switzerland)."
    )
    if suites:
        verb = "does" if len(suites) == 1 else "do"
        headline = (
            f"Your organization's {_listed([whole(product.name) for product in suites])} {verb} "
            "not include Teams, and none of its other licences does."
        )
        options = [enterprise, business, essentials]
    else:
        headline = "None of your organization's licences includes Teams."
        options = [business, essentials, enterprise]
    body = [
        "The agent needs one licence of its own that includes Teams, as a person does.",
        "These include it:",
    ]
    for option in options:
        body += _wrapped(option, rest="  ")
    return headline, [*body, *_wrapped(_BUY)]


def say(console: Console, headline: str, body: Sequence[str]) -> None:
    """A thing to check, the way the last screen shows them: the headline as a warning,
    wrapped to the console, then *body* under it."""
    first, *more = _wrapped(headline) or [""]
    console.warn(first)
    for line in [*more, *body]:
        console.info(f"     {line}")


def _explain(console: Console, report: Report, kind: Kind) -> None:
    in_use = report.products_with(kind)
    headline, body = _all_in_use(in_use, kind) if in_use else _nothing(report, kind)
    console.blank()
    say(console, headline, body)


def last_screen_lines(report: Report | None, kind: Kind, *, shown: bool = False) -> list[str]:
    """What to do, for the last screen, when the agent still lacks a licence of *kind*:
    for what the organization has when *report* says, in general when it is None. *shown*:
    the walkthrough in this run showed the person what to do, so this points back to it
    instead of printing it twice. An unattended run never saw it and gets all of it."""
    if shown:
        again = (
            "When a licence is free, run setup again. It gives the agent the licence by itself."
            if kind == "teams"
            else "When a licence is free, run setup again and answer yes to the Word, Excel and "
            "OneDrive question. It gives the agent the licence by itself."
        )
        return [f'What to do is above, under "{SECTION}".', *_wrapped(again)]
    if kind == "teams":
        closing = [
            "Run setup again. It gives the agent the licence by itself.",
            f"Then check with:  {DIAGNOSE}",
        ]
    else:
        closing = [
            "Then run setup again and answer yes to the Word, Excel and OneDrive question.",
            "It gives the agent the licence by itself.",
        ]
    if report is None:
        if kind == "teams":
            general = (
                "Make a licence that includes Teams available for the agent, for example "
                "Microsoft 365 Business Basic or Microsoft Teams Essentials. A billing "
                f"administrator does this in the Microsoft 365 admin center ({ADMIN_CENTER})."
            )
        else:
            general = (
                "Make a Microsoft 365 Copilot licence available for the agent. A billing "
                f"administrator does this in the Microsoft 365 admin center ({ADMIN_CENTER}): "
                "Marketplace."
            )
        return [*_wrapped(general), *closing]
    free = report.free_for(kind)
    if free:
        product = free[0]
        return [f"{product.name} has {_licences(product.free)}.", *closing]
    in_use = report.products_with(kind)
    headline, body = _all_in_use(in_use, kind) if in_use else _nothing(report, kind)
    return [*_wrapped(headline), *body, *closing]


# ── The walkthrough ─────────────────────────────────────────────────────────────────────────


def _rank(product: Product) -> int:
    """Where *product* goes in the table: Teams first, then mail or files, then the rest."""
    if product.teams:
        return 0
    return 1 if product.mailbox or product.files else 2


def _table(console: Console, report: Report, *, with_copilot: bool) -> None:
    def matters(product: Product) -> bool:
        wanted = product.teams or product.mailbox or product.files
        return product.for_people and (wanted or (with_copilot and product.copilot))

    for product in report.products:  # the log keeps the part numbers the screen leaves out
        console.debug(
            f"Licence {product.name!r}: part number {product.part_number}, "
            f"{product.free} of {product.total} free"
        )
    rows = [product for product in report.products if matters(product)] or list(report.products)
    rows = sorted(rows, key=_rank)
    console.blank()
    if not rows:
        console.info("Your organization has no Microsoft 365 licences yet.")
        return
    console.info("Your organization's licences, and what each gives the agent:")
    what = [includes(product, copilot=with_copilot) for product in rows]
    name_width = min(max(len(product.name) for product in rows), 38)
    what_width = max(len(item) for item in what)
    for product, item in zip(rows, what, strict=True):
        seats = f"{product.free} of {product.total} free"
        if len(product.name) > name_width:
            console.info(f"  {product.name}")
            console.info(f"  {'':<{name_width}}  {item:<{what_width}}  {seats}")
        else:
            console.info(f"  {product.name:<{name_width}}  {item:<{what_width}}  {seats}")


def _choose(
    console: Console, free: Sequence[Product], kind: Kind, *, intro: str = ""
) -> Product | None:
    """Which free licence to give the agent: a yes for the one there is, else a choice with
    the best one suggested. None: the person said no, or "None for now"."""
    if intro or len(free) == 1:  # a choice puts a blank line before its own question
        console.blank()
    if intro:
        console.info(intro)
    if len(free) == 1:
        product = free[0]
        question = f"Give the agent one {product.name} licence?"
        return product if console.confirm(question, default=True, key=CONFIRM_KEYS[kind]) else None
    choices = [
        Choice(
            product.sku_id.casefold(),
            f"{product.name}: {includes(product, copilot=kind == 'copilot')} ({product.free} free)",
        )
        for product in free
    ]
    choices.append(Choice(NONE_FOR_NOW, "None for now"))
    picked = console.ask_choice(
        f"Several of these include {_LABEL[kind]}. Which one should the agent have?",
        choices,
        default=choices[0].key,
        key=PICK_KEYS[kind],
    )
    return next((item for item in free if item.sku_id.casefold() == picked.casefold()), None)


def _give(
    console: Console, helpers: Helpers, env: Mapping[str, str], product: Product, kind: Kind
) -> Literal["assigned", "taken", "failed"]:
    console.info(f"Giving the agent one {product.name} licence...")
    outcome, result = _assign(helpers, env, product)
    if outcome in (ASSIGNED, ALREADY_ASSIGNED):
        console.ok(f"The agent has its {product.name} licence now.")
        for line in _READY[kind]:
            console.detail(line)
        return "assigned"
    if outcome == NO_FREE_SEAT:
        return "taken"
    console.warn("Setup could not give the agent the licence.")
    console.show_tail(result, lines=8)
    return "failed"


def _wait(console: Console) -> bool:
    """Has the person said "check again"? Never asks without a keyboard (or an answer)."""
    if not console.interactive and RECHECK_KEY not in console.answers:
        return False
    answer = console.ask_text(
        _WAIT,
        default="",
        key=RECHECK_KEY,
        validate=lambda value: (
            None
            if value.strip().lower() in ("s", "skip")
            else "Press Enter to check again, or type s to skip."
        ),
    )
    return answer.strip().lower() not in ("s", "skip")


def _could_not_read(console: Console, reason: str) -> None:
    console.blank()
    console.warn("Setup could not read your organization's licences.")
    console.detail(reason)
    console.detail(_LAST_SCREEN)


def _get(
    console: Console,
    helpers: Helpers,
    env: Mapping[str, str],
    outcome: Outcome,
    kind: Kind,
    *,
    second: bool = False,
) -> Got:
    """Get the agent a licence of *kind*: give it a free one, or say how to get one and
    check again until the person skips. Never more than MAX_CHECKS checks. *second*: another
    kind of licence came first in this walkthrough, so the question says what this one is for."""
    explained = False
    checks = 0
    while True:
        report = outcome.report
        assert report is not None
        had = next((item for item in report.agent_products() if item.has(kind)), None)
        if had is not None:
            outcome.products[kind] = had
            console.ok(f"The agent already has {had.name}, which includes {_LABEL[kind]}.")
            return "had"
        free = report.free_for(kind)
        if free:
            if explained:
                console.ok("There is a free licence now.")
            intro = (
                "Word, Excel and OneDrive files need a Microsoft 365 Copilot licence."
                if kind == "copilot" and second
                else ""
            )
            product = _choose(console, free, kind, intro=intro)
            if product is None:
                console.info(f"The agent was not given a licence. {_LAST_SCREEN}")
                return "skipped"
            given = _give(console, helpers, env, product, kind)
            if given == "assigned":
                outcome.products[kind] = product
                return "assigned"
            if given == "failed":
                return "failed"
            console.warn(f"The last free {product.name} licence was taken just now.")
        elif explained:
            console.info("Setup does not see a free licence yet. If you added or freed one just")
            console.info("now, wait a minute, then press Enter again.")
        else:
            _explain(console, report, kind)
            explained = True
            outcome.explained.add(kind)
        if checks >= MAX_CHECKS:
            console.info(f"Setup has checked {MAX_CHECKS} times. It stops here for now.")
            console.info(_LAST_SCREEN)
            return "skipped"
        if free:  # taken while setup was giving it: look again at once
            checks += 1
        elif not _wait(console):
            console.info(f"Skipped for now. {_LAST_SCREEN}")
            return "skipped"
        else:
            checks += 1
        try:
            outcome.report = read_report(helpers, env)
        except ReadFailed as exc:
            _could_not_read(console, str(exc))
            return "failed"


def _offer(*, teams: bool, copilot: bool) -> list[str]:
    lines: list[str] = []
    if teams:
        lines += [
            "The agent has no licence for Teams yet. Like a person, it needs a licence of its own",
            "to use Teams, mail and files.",
        ]
    if copilot:
        lines += [
            "Word, Excel and OneDrive files need a Microsoft 365 Copilot licence for the agent,",
            "and it has none yet.",
        ]
    return [
        *lines,
        "Setup can walk you through it now: it shows the licences your organization has, then",
        "gives the agent a free one or says how to get one.",
    ]


def walk_through(
    console: Console,
    helpers: Helpers,
    env: Mapping[str, str],
    *,
    teams: bool,
    copilot: bool,
) -> Outcome:
    """Offer, then walk the person through getting the agent the licences it lacks: one that
    includes Teams (*teams*), then Microsoft 365 Copilot (*copilot*: only when Word, Excel and
    OneDrive files are switched on in this setup). When Teams is left for later, so is
    Copilot, and nothing more is said: the last screen has both. Never raises for a person
    who stops answering: what was done stays done, and the rest is on the last screen."""
    outcome = Outcome(
        teams="skipped" if teams else "not-needed",
        copilot="skipped" if copilot else "not-needed",
    )
    console.section(SECTION)
    for line in _offer(teams=teams, copilot=copilot):
        console.info(line)
    question = (
        "Set up the agent's licences now?"
        if teams and copilot
        else ("Set up the agent's licence now?")
    )
    try:
        if not console.confirm(question, default=True, key=OFFER_KEY):
            console.info(f"Skipped. {_LAST_SCREEN}")
            outcome.report = read_quietly(helpers, env)
            return outcome
        console.info("Reading your organization's licences...")
        try:
            outcome.report = read_report(helpers, env)
        except ReadFailed as exc:
            _could_not_read(console, str(exc))
            outcome.teams = "failed" if teams else outcome.teams
            outcome.copilot = "failed" if copilot else outcome.copilot
            return outcome
        _table(console, outcome.report, with_copilot=copilot)
        if teams:
            outcome.teams = _get(console, helpers, env, outcome, "teams")
            if outcome.teams not in ("assigned", "had"):
                return outcome
        if copilot:
            outcome.copilot = _get(console, helpers, env, outcome, "copilot", second=teams)
    except Cancelled:
        console.blank()
        console.info(f"Skipped for now. {_LAST_SCREEN}")
    return outcome
