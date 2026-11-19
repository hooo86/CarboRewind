"""Blacklist (DNSBL) monitoring of the mail server's public addresses.

For each address and each list, the standard DNSBL lookup: the address reversed (IPv4
``4.3.2.1``, IPv6 by nibbles) followed by the list's zone, e.g. ``4.3.2.1.zen.spamhaus.org``.
An answer in ``127.0.0.0/8`` means *listed*, "no such name" means *not listed*.

Spamhaus answers ``127.255.255.252-255`` when it refuses the query (open/public resolver such
as 8.8.8.8, too many queries): reported as an error, never as a listing, so the server's
resolver must be a local or ISP one.

The check runs from the scheduler every ``interval_minutes`` (and on demand). A new listing
is notified (``blacklisted``), and so is its removal (``blacklist_cleared``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .db.models import ActorType, AppSetting, Outcome
from .notifications import notify

SETTINGS_KEY = "blacklist"
STATUS_KEY = "blacklist_status"
DEFAULT_ZONES = [
    "zen.spamhaus.org",
    "bl.spamcop.net",
    "b.barracudacentral.org",
    "psbl.surriel.com",
    "ix.dnsbl.manitu.net",
    "dnsbl-1.uceprotect.net",
]
TIMEOUT = 5.0
PARALLEL = 10

# Resolves a name to its IPv4 addresses; raises socket.gaierror when the name does not exist.
Resolver = Callable[[str], Awaitable[list[str]]]


@dataclass
class BlacklistSettings:
    enabled: bool = False
    ips: list[str] = field(default_factory=list)
    zones: list[str] = field(default_factory=lambda: list(DEFAULT_ZONES))
    interval_minutes: int = 60

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> BlacklistSettings:
        out = cls()
        for key, value in (data or {}).items():
            if hasattr(out, key):
                setattr(out, key, value)
        return out


async def _get(db: AsyncSession, key: str) -> dict[str, Any] | None:
    row = await db.get(AppSetting, key)
    return row.value if row else None


async def _put(db: AsyncSession, key: str, value: dict[str, Any]) -> None:
    await db.execute(
        insert(AppSetting)
        .values(key=key, value=value, updated_at=func.now())
        .on_conflict_do_update(
            index_elements=["key"], set_={"value": value, "updated_at": func.now()}
        )
    )


async def load_settings(db: AsyncSession) -> BlacklistSettings:
    return BlacklistSettings.from_json(await _get(db, SETTINGS_KEY))


async def save_settings(db: AsyncSession, cfg: BlacklistSettings) -> None:
    await _put(db, SETTINGS_KEY, asdict(cfg))


async def load_status(db: AsyncSession) -> dict[str, Any]:
    return await _get(db, STATUS_KEY) or {"checked_at": None, "results": []}


def query_name(ip: str, zone: str) -> str:
    address = ipaddress.ip_address(ip)
    if address.version == 4:
        reversed_ip = ".".join(reversed(str(address).split(".")))
    else:
        reversed_ip = ".".join(reversed(address.exploded.replace(":", "")))
    return f"{reversed_ip}.{zone.strip('.')}"


async def system_resolver(name: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(
        loop.getaddrinfo(name, None, family=socket.AF_INET, type=socket.SOCK_STREAM),
        TIMEOUT,
    )
    return sorted({str(info[4][0]) for info in infos})


async def lookup(ip: str, zone: str, resolver: Resolver) -> dict[str, Any]:
    """``{"ip", "zone", "listed": True/False/None, "codes", "error"}`` for one pair."""
    out: dict[str, Any] = {"ip": ip, "zone": zone, "listed": None, "codes": [], "error": None}
    try:
        answers = await resolver(query_name(ip, zone))
    except socket.gaierror as exc:
        if exc.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", -5)):
            out["listed"] = False
        else:
            out["error"] = f"DNS: {exc.strerror or exc}"
        return out
    except (TimeoutError, OSError) as exc:
        out["error"] = f"DNS: {type(exc).__name__}"
        return out
    out["codes"] = answers
    refused = [a for a in answers if a.startswith("127.255.255.")]
    if refused:
        out["error"] = "query refused by the list (public or overloaded DNS resolver)"
    elif any(a.startswith("127.") for a in answers):
        out["listed"] = True
    else:
        out["error"] = f"unexpected answer {', '.join(answers)}"
    return out


async def run_check(
    db: AsyncSession, now: datetime, resolver: Resolver | None = None
) -> dict[str, Any]:
    """Check every address on every list; notify listings and removals; store the status."""
    cfg = await load_settings(db)
    resolve = resolver or system_resolver
    semaphore = asyncio.Semaphore(PARALLEL)

    async def one(ip: str, zone: str) -> dict[str, Any]:
        async with semaphore:
            return await lookup(ip, zone, resolve)

    results = await asyncio.gather(*(one(ip, z) for ip in cfg.ips for z in cfg.zones))
    previous = {(r["ip"], r["zone"]): r for r in (await load_status(db)).get("results", [])}
    for r in results:
        old = previous.get((r["ip"], r["zone"]), {})
        if r["listed"] is None:  # unknown this time: keep what we knew
            r["since"] = old.get("since")
            r["was_listed"] = bool(old.get("listed") or old.get("was_listed"))
            continue
        if r["listed"]:
            r["since"] = old.get("since") if old.get("listed") else now.isoformat()
            if not (old.get("listed") or old.get("was_listed")):
                await notify(
                    db,
                    "blacklisted",
                    dedup_key=f"blacklisted:{r['ip']}:{r['zone']}:{r['since']}",
                    ip=r["ip"],
                    zone=r["zone"],
                    codes=", ".join(r["codes"]),
                )
                await audit.record(
                    db,
                    action="blacklist.listed",
                    outcome=Outcome.FAILURE,
                    actor_type=ActorType.SYSTEM,
                    actor_name="blacklist",
                    target_type="ip",
                    target_id=r["ip"],
                    details={"zone": r["zone"], "codes": r["codes"]},
                )
        else:
            r["since"] = None
            if old.get("listed") or old.get("was_listed"):
                await notify(
                    db,
                    "blacklist_cleared",
                    dedup_key=f"blacklist_cleared:{r['ip']}:{r['zone']}:{now:%Y%m%dT%H%M}",
                    ip=r["ip"],
                    zone=r["zone"],
                )
    status = {"checked_at": now.isoformat(), "results": list(results)}
    await _put(db, STATUS_KEY, status)
    return status


async def due_check(db: AsyncSession, now: datetime) -> bool:
    """From the scheduler: check when enabled and the interval elapsed."""
    cfg = await load_settings(db)
    if not cfg.enabled or not cfg.ips or not cfg.zones:
        return False
    last = (await load_status(db)).get("checked_at")
    if last and datetime.fromisoformat(last) > now - timedelta(minutes=cfg.interval_minutes):
        return False
    await run_check(db, now)
    return True


def listed(status: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in status.get("results", []) if r.get("listed")]


def utcnow() -> datetime:
    return datetime.now(UTC)
