"""Server-side sessions: opaque random token in a cookie, SHA-256 of it in the database."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import Settings
from ..db.models import User, UserSession

# Avoid one UPDATE per request: refresh last_seen_at at most this often.
_TOUCH_INTERVAL = timedelta(seconds=60)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_session(
    db: AsyncSession,
    user: User,
    settings: Settings,
    source_ip: str | None,
    user_agent: str | None,
) -> tuple[str, UserSession]:
    """Create a session and return ``(token, session)``. The token is shown only once."""
    now = datetime.now(UTC)
    token = secrets.token_urlsafe(32)
    session = UserSession(
        user_id=user.id,
        token_hash=_digest(token),
        csrf_token=secrets.token_urlsafe(32),
        last_seen_at=now,
        expires_at=now + timedelta(hours=settings.session_absolute_hours),
        source_ip=source_ip,
        user_agent=(user_agent or "")[:512] or None,
    )
    db.add(session)
    await db.flush()
    return token, session


async def resolve_session(
    db: AsyncSession, token: str, settings: Settings
) -> tuple[UserSession, User] | None:
    """Return the live session and its active user, or None (expired, revoked, unknown)."""
    now = datetime.now(UTC)
    session = await db.scalar(
        select(UserSession).where(
            UserSession.token_hash == _digest(token),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        )
    )
    if session is None:
        return None
    if now - session.last_seen_at > timedelta(minutes=settings.session_idle_minutes):
        session.revoked_at = now
        await db.flush()
        return None
    user = await db.scalar(
        select(User).options(selectinload(User.roles)).where(User.id == session.user_id)
    )
    if user is None or not user.is_active:
        return None
    if now - session.last_seen_at > _TOUCH_INTERVAL:
        session.last_seen_at = now
        await db.flush()
    return session, user


async def revoke_session(db: AsyncSession, session: UserSession) -> None:
    session.revoked_at = datetime.now(UTC)
    await db.flush()


async def revoke_all_sessions(db: AsyncSession, user_id: object, keep: object = None) -> int:
    """Revoke every live session of a user (deactivation, role change), except ``keep``.
    Returns the count."""
    query = update(UserSession).where(
        UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
    )
    if keep is not None:
        query = query.where(UserSession.id != keep)
    result = await db.execute(query.values(revoked_at=datetime.now(UTC)))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
