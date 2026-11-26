"""Reverse DNS (PTR) of client IP addresses, with a small in-memory cache.

Used to show which networks (domains) spam and login attacks come from. Lookups run in the
resolver's thread pool with a short timeout and a bounded concurrency; answers are kept a
day, failures an hour. The *organisation domain* of a host name is guessed without the
public suffix list: the last two labels, or three when the second-level label is a common
registry one (``example.co.uk``, ``djaweb.com.dz``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections import OrderedDict
from collections.abc import Iterable

TIMEOUT = 2.0
CONCURRENCY = 20
HIT_TTL = 24 * 3600.0
MISS_TTL = 3600.0
MAX_ENTRIES = 20_000
_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go", "gob", "nic"}

_cache: OrderedDict[str, tuple[float, str | None]] = OrderedDict()
# Internal networks: no public PTR to look for (documentation ranges stay resolvable).
_INTERNAL = [
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
]


def _internal(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return any(addr.version == n.version and addr in n for n in _INTERNAL)


def organisation(host: str | None) -> str | None:
    """``mail-1.mx.example.co.uk`` -> ``example.co.uk``."""
    if not host:
        return None
    labels = [label for label in host.lower().rstrip(".").split(".") if label]
    if len(labels) < 2:
        return None
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _cached(ip: str) -> tuple[bool, str | None]:
    entry = _cache.get(ip)
    if entry is None or entry[0] < time.monotonic():
        return False, None
    return True, entry[1]


def _store(ip: str, host: str | None) -> None:
    ttl = HIT_TTL if host else MISS_TTL
    _cache[ip] = (time.monotonic() + ttl, host)
    _cache.move_to_end(ip)
    while len(_cache) > MAX_ENTRIES:
        _cache.popitem(last=False)


async def _lookup(ip: str) -> str | None:
    loop = asyncio.get_running_loop()
    try:
        host, _ = await asyncio.wait_for(
            loop.getnameinfo((ip, 0), socket.NI_NAMEREQD), timeout=TIMEOUT
        )
    except (OSError, TimeoutError, ValueError):
        return None
    return host.rstrip(".").lower() or None


async def resolve(ips: Iterable[str]) -> dict[str, str | None]:
    """PTR name of each IP (``None`` when there is none or it timed out)."""
    out: dict[str, str | None] = {}
    todo: list[str] = []
    for ip in dict.fromkeys(ips):
        try:
            if _internal(ip):
                out[ip] = None
                continue
        except ValueError:
            out[ip] = None
            continue
        hit, host = _cached(ip)
        if hit:
            out[ip] = host
        else:
            todo.append(ip)
    limit = asyncio.Semaphore(CONCURRENCY)

    async def one(ip: str) -> None:
        async with limit:
            host = await _lookup(ip)
        _store(ip, host)
        out[ip] = host

    await asyncio.gather(*(one(ip) for ip in todo))
    return out
