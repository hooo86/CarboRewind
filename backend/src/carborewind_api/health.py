"""Readiness checks of external dependencies."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

log = logging.getLogger(__name__)

Check = Callable[[], Awaitable[None]]


def database_check(engine: AsyncEngine) -> Check:
    async def check() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    return check


def redis_check(client: Redis) -> Check:
    async def check() -> None:
        if not await client.ping():
            raise RuntimeError("PING failed")

    return check


async def run_checks(checks: Mapping[str, Check], timeout: float) -> dict[str, str]:
    """Run all checks concurrently. Returns ``{name: "ok" | "error"}``.

    Error details are logged, never returned to the caller (no information disclosure).
    """

    async def one(name: str, check: Check) -> tuple[str, str]:
        try:
            await asyncio.wait_for(check(), timeout)
        except Exception as exc:  # any failure means "not ready"
            log.warning("readiness check failed", extra={"check": name, "error": repr(exc)})
            return name, "error"
        return name, "ok"

    results = await asyncio.gather(*(one(n, c) for n, c in checks.items()))
    return dict(results)
