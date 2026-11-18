"""Fixed-window rate limiter backed by Redis, with an in-memory fallback for tests/dev."""

from __future__ import annotations

import time
from typing import Protocol

from redis.asyncio import Redis


class RateLimiter(Protocol):
    async def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        """Count one hit on ``key``. Returns False when the limit is exceeded."""


class RedisRateLimiter:
    def __init__(self, client: Redis, prefix: str = "carborewind:rl:") -> None:
        self._client = client
        self._prefix = prefix

    async def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        bucket = int(time.time() // window_seconds)
        redis_key = f"{self._prefix}{key}:{bucket}"
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.incr(redis_key)
            pipe.expire(redis_key, window_seconds)
            count, _ = await pipe.execute()
        return int(count) <= limit


class MemoryRateLimiter:
    """Single-process limiter. Not suitable for multi-replica deployments."""

    def __init__(self) -> None:
        self._counts: dict[tuple[str, int], int] = {}

    async def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        bucket = int(time.time() // window_seconds)
        # Drop expired buckets to bound memory.
        self._counts = {k: v for k, v in self._counts.items() if k[1] >= bucket}
        count = self._counts.get((key, bucket), 0) + 1
        self._counts[(key, bucket)] = count
        return count <= limit
