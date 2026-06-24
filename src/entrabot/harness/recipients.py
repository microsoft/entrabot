"""Federated-recipient (``ENTRABOT_HUMAN_*``) list management — the people this agent talks to on
Teams.

Recipients live in the shared global config (``globalcfg.split`` keeps ``HUMAN_*`` global) as
several parallel, positionally-aligned CSV env vars (one cell per recipient across
``_USER_IDS`` / ``_UPNS`` / ``_USER_TENANT_IDS`` / ``_USER_MAILS`` / ``_USER_TYPES``). That packed
form is awkward to edit by hand, so this module parses it into :class:`Recipient` records, edits
them, and serializes back. It is the single representation shared by the ``entrabot init`` wizard
stage and the ``entrabot users`` / ``/users`` management surfaces.

B2B guests (external/Microsoft-tenant users invited here) carry their HOME tenant GUID so
federated chat reaches their real identity — never the local guest object id (hard-won
learning #28).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import globalcfg


@dataclass
class Recipient:
    """One person the agent may talk to on Teams (the recipient / "talk-to" list).

    ``user_type`` is the Entra account *type* (Guest = B2B / Member = internal) — informational,
    auto-detected, used for federated chat addressing. This list is NOT the sponsor list: who can
    authorize the agent comes from the Entra Agent-Identity sponsor relationship
    (``identity.sponsors``), never a flag stored here.
    """

    upn: str
    user_id: str = ""
    tenant_id: str = ""
    mail: str = ""
    user_type: str = "Member"

    @property
    def is_guest(self) -> bool:
        return self.user_type.strip().lower() == "guest"

    @property
    def key(self) -> str:
        """Identity key for de-dup / removal — the UPN, falling back to the home SMTP alias."""
        return (self.upn or self.mail).strip().lower()


def _cells(value: str | None, n: int) -> list[str]:
    """A stored CSV → exactly ``n`` positionally-aligned, stripped cells (pad/truncate, preserve
    empties so a member's blank tenant slot can't shift the columns out of sync)."""
    cells = [c.strip() for c in value.split(",")] if value else []
    if len(cells) < n:
        cells += [""] * (n - len(cells))
    return cells[:n]


def parse(env: dict) -> list[Recipient]:
    """Parse the ``ENTRABOT_HUMAN_*`` block into records (empty block → [])."""
    upns_raw = env.get("ENTRABOT_HUMAN_UPNS") or env.get("ENTRABOT_HUMAN_UPN") or ""
    upns = [u.strip() for u in upns_raw.split(",") if u.strip()]
    if not upns:
        return []
    n = len(upns)
    ids = _cells(env.get("ENTRABOT_HUMAN_USER_IDS") or env.get("ENTRABOT_HUMAN_USER_ID"), n)
    tids = _cells(env.get("ENTRABOT_HUMAN_USER_TENANT_IDS"), n)
    mails = _cells(env.get("ENTRABOT_HUMAN_USER_MAILS"), n)
    types = _cells(env.get("ENTRABOT_HUMAN_USER_TYPES"), n)
    return [
        Recipient(upn=upns[i], user_id=ids[i], tenant_id=tids[i], mail=mails[i],
                  user_type=types[i] or "Member")
        for i in range(n)
    ]


def to_env(recips: list[Recipient]) -> dict:
    """Serialize records back into the packed ``ENTRABOT_HUMAN_*`` block (empty list → {})."""
    if not recips:
        return {}
    return {
        "ENTRABOT_HUMAN_USER_IDS": ",".join(r.user_id for r in recips),
        "ENTRABOT_HUMAN_UPNS": ",".join(r.upn for r in recips),
        "ENTRABOT_HUMAN_USER_TENANT_IDS": ",".join(r.tenant_id for r in recips),
        "ENTRABOT_HUMAN_USER_MAILS": ",".join(r.mail for r in recips),
        "ENTRABOT_HUMAN_USER_TYPES": ",".join(r.user_type for r in recips),
        # backward-compat singulars track the primary (first) recipient
        "ENTRABOT_HUMAN_USER_ID": recips[0].user_id,
        "ENTRABOT_HUMAN_UPN": recips[0].upn,
    }


def upsert(recips: list[Recipient], new: list[Recipient]) -> list[Recipient]:
    """Merge ``new`` into ``recips``, replacing any with a matching key (case-insensitive)."""
    by_key: dict[str, Recipient] = {r.key: r for r in recips}
    for r in new:
        by_key[r.key] = r
    return list(by_key.values())


def remove(recips: list[Recipient], email: str) -> tuple[list[Recipient], bool]:
    """Drop the recipient matching ``email`` (by UPN or home SMTP alias). Returns
    (kept, changed)."""
    k = email.strip().lower()
    kept = [r for r in recips if k not in (r.upn.strip().lower(), r.mail.strip().lower())]
    return kept, len(kept) != len(recips)


def load_global() -> list[Recipient]:
    """Read the recipient list from the shared global config."""
    return parse(globalcfg.read_global())


def save_global(recips: list[Recipient]) -> None:
    """Write ``recips`` as the recipient list in the shared global config (replacing any prior
    ``HUMAN_*`` block, preserving tenant/blueprint/prefs) and mirror it into the live process env
    so an in-flight wizard's connection re-test sees it. Empty list clears the block."""
    gpath = globalcfg.global_env_path()
    current = {
        k: v for k, v in globalcfg.read_env(gpath).items()
        if not k.startswith(globalcfg.HUMAN_PREFIX)
    }
    block = to_env(recips)
    current.update(block)
    globalcfg.write_env(
        gpath, current,
        header="ENTRABOT global config — shared tenant + Blueprint + recipients. Do not commit.",
    )
    import os

    for k in list(os.environ):
        if k.startswith(globalcfg.HUMAN_PREFIX):
            del os.environ[k]
    for k, v in block.items():
        os.environ[k] = v
