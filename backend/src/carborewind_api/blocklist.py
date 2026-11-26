"""Blocked and trusted senders: addresses or domains whose mail Carbonio always treats as
spam (``block``) or never treats as spam (``trust``).

The list is kept here (menu Blocages → Expéditeurs bloqués) and applied by the agent to the
``amavisBlacklistSender`` attribute of every local domain of Carbonio: Amavis then gives
their messages a spam score high enough for the Junk folder (or the quarantine), whatever
their content. Entries use the Amavis syntax: ``user@example.com`` for one address,
``@example.com`` for a whole domain.

The trusted list (menu Blocages → Expéditeurs de confiance) goes to ``amavisWhitelistSender``
the same way. An entry is in one list at most.

Blocking or trusting the domain of a big mail provider (outlook.com, gmail.com…) would apply
to every sender of that provider: it is refused unless confirmed (``force``).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import Agent, AppSetting, Domain

KEY = "sender_blocklist"
# list -> (setting key, prefix of the agent's storage keys and of the agent answer)
LISTS = {"block": ("sender_blocklist", "blocklist"), "trust": ("sender_trustlist", "trustlist")}
MAX_ENTRIES = 2000
MAX_REMOVED = 500
ADDRESS = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}@(?:[a-z0-9-]{1,63}\.)+[a-z]{2,63}$")
DOMAIN = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
# Shared by millions of legitimate senders: blocking one blocks them all.
PROVIDERS = {
    "outlook.com",
    "hotmail.com",
    "hotmail.fr",
    "live.com",
    "live.fr",
    "msn.com",
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "yahoo.fr",
    "icloud.com",
    "me.com",
    "aol.com",
    "gmx.com",
    "gmx.fr",
    "orange.fr",
    "free.fr",
    "laposte.net",
    "protonmail.com",
    "proton.me",
}


class BlocklistError(ValueError):
    pass


def normalise(value: str) -> tuple[str, str]:
    """(Amavis entry, kind) for an address or a domain (``@`` prefix optional)."""
    text = value.strip().lower().strip("<>")
    if "@" in text and not text.startswith("@"):
        if not ADDRESS.match(text):
            raise BlocklistError(f"invalid address {value!r}")
        return text, "address"
    domain = text.lstrip("@")
    if not DOMAIN.match(domain):
        raise BlocklistError(f"invalid address or domain {value!r}")
    return f"@{domain}", "domain"


def prefix(which: str) -> str:
    return LISTS[which][1]


async def load(db: AsyncSession, which: str = "block") -> dict[str, Any]:
    row = await db.get(AppSetting, LISTS[which][0])
    data = dict(row.value) if row else {}
    return {
        "revision": int(data.get("revision") or 0),
        "entries": list(data.get("entries") or []),
        "removed": list(data.get("removed") or []),
    }


async def _save(db: AsyncSession, data: dict[str, Any], which: str = "block") -> None:
    await db.execute(
        insert(AppSetting)
        .values(key=LISTS[which][0], value=data, updated_at=func.now())
        .on_conflict_do_update(
            index_elements=["key"], set_={"value": data, "updated_at": func.now()}
        )
    )


async def add(
    db: AsyncSession,
    value: str,
    *,
    by: str,
    reason: str | None,
    force: bool,
    which: str = "block",
) -> dict[str, Any]:
    entry, kind = normalise(value)
    verb = "blocking" if which == "block" else "trusting"
    if kind == "domain":
        domain = entry[1:]
        if await db.scalar(select(Domain.id).where(Domain.name == domain)):
            # Trusting it would let anyone forging one of your addresses through.
            raise BlocklistError(f"{domain} is one of your own domains")
        if domain in PROVIDERS and not force:
            raise BlocklistError(
                f"{domain} is a public mail provider: {verb} it applies to all its users "
                "(use the address instead, or confirm)"
            )
    other = "trust" if which == "block" else "block"
    if any(e["value"] == entry for e in (await load(db, other))["entries"]):
        raise BlocklistError(
            f"{entry} is already in the {'trusted' if other == 'trust' else 'blocked'} senders: "
            "remove it there first"
        )
    data = await load(db, which)
    if any(e["value"] == entry for e in data["entries"]):
        return data
    if len(data["entries"]) >= MAX_ENTRIES:
        raise BlocklistError("the list is full")
    data["entries"].append(
        {
            "value": entry,
            "kind": kind,
            "by": by,
            "at": datetime.now(UTC).isoformat(),
            "reason": (reason or "")[:300] or None,
        }
    )
    data["removed"] = [v for v in data["removed"] if v != entry]
    data["revision"] += 1
    await _save(db, data, which)
    return data


async def remove(db: AsyncSession, value: str, which: str = "block") -> dict[str, Any]:
    entry, _ = normalise(value)
    data = await load(db, which)
    before = len(data["entries"])
    data["entries"] = [e for e in data["entries"] if e["value"] != entry]
    if len(data["entries"]) == before:
        raise BlocklistError("not in the list")
    data["removed"] = [*[v for v in data["removed"] if v != entry], entry][-MAX_REMOVED:]
    data["revision"] += 1
    await _save(db, data, which)
    return data


async def for_agent(db: AsyncSession, agent: Agent, which: str = "block") -> dict[str, Any] | None:
    """What the agent must apply, when it is behind the current list."""
    data = await load(db, which)
    applied = int((agent.storage or {}).get(f"{prefix(which)}_revision") or 0)
    if data["revision"] == 0 or applied >= data["revision"]:
        return None
    return {
        "revision": data["revision"],
        "add": [e["value"] for e in data["entries"]],
        "remove": data["removed"],
    }
