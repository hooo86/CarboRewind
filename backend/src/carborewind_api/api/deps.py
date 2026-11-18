"""Shared FastAPI dependencies: database session, client IP, authenticated principal, CSRF."""

from __future__ import annotations

import ipaddress
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..auth.sessions import resolve_session
from ..config import Settings
from ..db.models import User, UserSession

SESSION_COOKIE = "__Host-carborewind_session"
CSRF_COOKIE = "__Host-carborewind_csrf"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """Transaction per request: commit on success, rollback on exception."""
    factory: async_sessionmaker[AsyncSession] | None = request.app.state.sessionmaker
    if factory is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database not configured")
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


def client_ip(request: Request) -> str | None:
    """Peer IP address, or None when the peer is not an IP (unix socket, test client).

    Behind the reverse proxy, uvicorn ``--proxy-headers`` (restricted by
    ``--forwarded-allow-ips``) rewrites it from X-Forwarded-For.
    """
    if request.client is None:
        return None
    try:
        return str(ipaddress.ip_address(request.client.host))
    except ValueError:
        return None


@dataclass
class Principal:
    user: User
    session: UserSession


DbSession = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


async def current_principal(request: Request, db: DbSession, settings: AppSettings) -> Principal:
    """Authenticated user from the session cookie; enforces CSRF on unsafe methods."""
    token = request.cookies.get(SESSION_COOKIE)
    resolved = await resolve_session(db, token, settings) if token else None
    if resolved is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "authentication required",
            headers={"WWW-Authenticate": "Session"},
        )
    session, user = resolved
    if request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not secrets.compare_digest(sent.encode(), session.csrf_token.encode()):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid CSRF token")
    return Principal(user=user, session=session)


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
