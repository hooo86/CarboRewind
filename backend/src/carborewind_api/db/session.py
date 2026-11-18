"""Async engine factory."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def make_engine(url: str) -> AsyncEngine:
    """Create the async engine (psycopg 3 driver)."""
    return create_async_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)
