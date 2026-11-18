"""``/api/auth``: login, logout, current user."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from .. import audit
from ..auth import service
from ..auth.sessions import create_session, revoke_session
from ..db.models import Outcome
from .deps import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    AppSettings,
    CurrentPrincipal,
    DbSession,
    client_ip,
)
from .schemas import UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])

GENERIC_FAILURE = "invalid credentials"


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024, repr=False)


class SessionOut(BaseModel):
    user: UserOut
    csrf_token: str = Field(
        description="Echo this value in the X-CSRF-Token header of every unsafe request"
    )


def _set_cookies(response: Response, token: str, csrf: str, max_age: int) -> None:
    common = {"secure": True, "samesite": "strict", "path": "/", "max_age": max_age}
    response.set_cookie(SESSION_COOKIE, token, httponly=True, **common)  # type: ignore[arg-type]
    # Readable by the SPA, which echoes it in the X-CSRF-Token header (double submit).
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, **common)  # type: ignore[arg-type]


@router.post(
    "/login",
    response_model=SessionOut,
    responses={
        401: {"description": "Invalid credentials (generic, whatever the reason)"},
        415: {"description": "Body must be application/json"},
        429: {"description": "Too many attempts from this address"},
    },
)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: DbSession,
    settings: AppSettings,
) -> SessionOut:
    """Open a session. Sets an HttpOnly session cookie and a CSRF cookie."""
    # JSON only: a cross-site HTML form cannot send it without a CORS preflight (login CSRF).
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "application/json required")
    ip = client_ip(request)
    allowed = await request.app.state.rate_limiter.hit(
        f"login:ip:{ip}", settings.login_ip_limit, settings.login_ip_window_seconds
    )
    if not allowed:
        await audit.record(
            db,
            action="auth.login",
            outcome=Outcome.DENIED,
            actor_name=service.normalise_username(body.username),
            source_ip=ip,
            details={"reason": "rate_limited"},
        )
        await db.commit()
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many attempts",
            headers={"Retry-After": str(settings.login_ip_window_seconds)},
        )

    result = await service.authenticate(
        db, settings, request.app.state.ldap, body.username, body.password, ip
    )
    if result.user is None:
        await db.commit()  # persist failure counter and audit before answering
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, GENERIC_FAILURE)

    token, session = await create_session(
        db, result.user, settings, ip, request.headers.get("user-agent")
    )
    _set_cookies(response, token, session.csrf_token, settings.session_absolute_hours * 3600)
    return SessionOut(user=UserOut.of(result.user), csrf_token=session.csrf_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    principal: CurrentPrincipal, request: Request, response: Response, db: DbSession
) -> None:
    """Revoke the current session (requires the CSRF header)."""
    await revoke_session(db, principal.session)
    await audit.record(
        db,
        action="auth.logout",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
    )
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/", secure=True, samesite="strict")


@router.get("/me", response_model=UserOut)
async def me(principal: CurrentPrincipal) -> UserOut:
    """Current user and role assignments."""
    return UserOut.of(principal.user)
