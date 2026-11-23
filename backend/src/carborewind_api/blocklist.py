"""Blocked senders: addresses or domains whose mail is treated as spam by Carbonio.

The list is kept here (menu Blocages → Expéditeurs bloqués) and applied by the agent to the
``amavisBlacklistSender`` attribute of every local domain of Carbonio: Amavis then gives
their messages a spam score high enough for the Junk folder (or the quarantine), whatever
their content. Entries use the Amavis syntax: ``user@example.com`` for one address,
``@example.com`` for a whole domain.

Blocking the domain of a big mail provider (outlook.com, gmail.com…) would also block every
legitimate sender of that provider: it is refused unless confirmed (``force``).
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


async def load(db: AsyncSession) -> dict[str, Any]:
    row = await db.get(AppSetting, KEY)
    data = dict(row.value) if row else {}
    return {
        "revision": int(data.get("revision") or 0),
        "entries": list(data.get("entries") or []),
        "removed": list(data.get("removed") or []),
    }


async def _save(db: AsyncSession, data: dict[str, Any]) -> None:
    await db.execute(
        insert(AppSetting)
        .values(key=KEY, value=data, updated_at=func.now())
        .on_conflict_do_update(
            index_elements=["key"], set_={"value": data, "updated_at": func.now()}
        )
    )


async def add(
    db: AsyncSession, value: str, *, by: str, reason: str | None, force: bool
) -> dict[str, Any]:
    entry, kind = normalise(value)
    if kind == "domain":
        domain = entry[1:]
        if await db.scalar(select(Domain.id).where(Domain.name == domain)):
            raise BlocklistError(f"{domain} is one of your own domains")
        if domain in PROVIDERS and not force:
            raise BlocklistError(
                f"{domain} is a public mail provider: blocking it blocks all its users "
                "(block the address instead, or confirm)"
            )
    data = await load(db)
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
    await _save(db, data)
    return data


async def remove(db: AsyncSession, value: str) -> dict[str, Any]:
    entry, _ = normalise(value)
    data = await load(db)
    before = len(data["entries"])
    data["entries"] = [e for e in data["entries"] if e["value"] != entry]
    if len(data["entries"]) == before:
        raise BlocklistError("not in the list")
    data["removed"] = [*[v for v in data["removed"] if v != entry], entry][-MAX_REMOVED:]
    data["revision"] += 1
    await _save(db, data)
    return data


async def for_agent(db: AsyncSession, agent: Agent) -> dict[str, Any] | None:
    """What the agent must apply, when it is behind the current list."""
    data = await load(db)
    applied = int((agent.storage or {}).get("blocklist_revision") or 0)
    if data["revision"] == 0 or applied >= data["revision"]:
        return None
    return {
        "revision": data["revision"],
        "add": [e["value"] for e in data["entries"]],
        "remove": data["removed"],
    }
