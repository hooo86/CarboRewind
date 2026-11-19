"""``/api/auth``: login (password + optional second factor), logout, current user, own
password, two-factor enrolment (TOTP) and recovery codes."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from .. import audit
from ..auth import service
from ..auth.sessions import create_session, revoke_all_sessions, revoke_session
from ..db.models import AuthSource, Outcome
from ..notifications import SecretKeyError, encrypt
from ..security import policy as security_policy
from ..security import totp
from ..security.passwords import WeakPasswordError, hash_password, verify_password
from .deps import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    AppSettings,
    CurrentPrincipal,
    DbSession,
    Principal,
    client_ip,
)
from .schemas import UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])

GENERIC_FAILURE = "invalid credentials"


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024, repr=False)
    # Second factor: a 6-digit TOTP code or a recovery code (when 2FA is enabled).
    otp: str | None = Field(default=None, max_length=32, repr=False)


class SessionOut(BaseModel):
    user: UserOut
    csrf_token: str = Field(
        description="Echo this value in the X-CSRF-Token header of every unsafe request"
    )
    # The security policy requires 2FA and the user has not enrolled: only /api/auth/* works.
    totp_enrollment_required: bool = False


def _set_cookies(response: Response, token: str, csrf: str, max_age: int) -> None:
    common = {"secure": True, "samesite": "strict", "path": "/", "max_age": max_age}
    response.set_cookie(SESSION_COOKIE, token, httponly=True, **common)  # type: ignore[arg-type]
    # Readable by the SPA, which echoes it in the X-CSRF-Token header (double submit).
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, **common)  # type: ignore[arg-type]


@router.post(
    "/login",
    response_model=SessionOut,
    responses={
        401: {
            "description": "Invalid credentials (generic, whatever the reason), or "
            "'otp_required' when the password is right and a second factor is needed"
        },
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
        db, settings, request.app.state.ldap, body.username, body.password, ip, body.otp
    )
    if result.user is None:
        await db.commit()  # persist failure counter and audit before answering
        if result.failure is service.LoginFailure.OTP_REQUIRED:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "otp_required")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, GENERIC_FAILURE)

    token, session = await create_session(
        db, result.user, settings, ip, request.headers.get("user-agent")
    )
    _set_cookies(response, token, session.csrf_token, settings.session_absolute_hours * 3600)
    policy = await security_policy.load(db)
    return SessionOut(
        user=UserOut.of(result.user),
        csrf_token=session.csrf_token,
        totp_enrollment_required=not result.user.totp_enabled and policy.requires_2fa(result.user),
    )


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


class MeOut(UserOut):
    totp_enrollment_required: bool = False
    recovery_codes_left: int = 0


@router.get("/me", response_model=MeOut)
async def me(principal: CurrentPrincipal, db: DbSession) -> MeOut:
    """Current user and role assignments."""
    user = principal.user
    policy = await security_policy.load(db)
    return MeOut(
        **UserOut.of(user).model_dump(),
        totp_enrollment_required=not user.totp_enabled and policy.requires_2fa(user),
        recovery_codes_left=len(user.recovery_codes or []),
    )


async def _self_audit(
    db: DbSession, principal: Principal, request: Request, action: str, **details: object
) -> None:
    await audit.record(
        db,
        action=action,
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        target_type="user",
        target_id=str(principal.user.id),
        source_ip=client_ip(request),
        details=dict(details),
    )


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=1024, repr=False)
    new_password: str = Field(min_length=1, max_length=1024, repr=False)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChange, principal: CurrentPrincipal, db: DbSession, request: Request
) -> None:
    """Change one's own password (local accounts); the other sessions are closed."""
    user = principal.user
    if user.auth_source is not AuthSource.LOCAL:
        raise HTTPException(status.HTTP_409_CONFLICT, "the password is managed by the directory")
    if not verify_password(user.password_hash, body.current_password):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "current password is wrong")
    try:
        user.password_hash = hash_password(body.new_password)
    except WeakPasswordError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    user.password_changed_at = datetime.now(UTC)
    others = await revoke_all_sessions(db, user.id, keep=principal.session.id)
    await _self_audit(db, principal, request, "user.password_change", sessions_revoked=others)


# ------------------------------------------------------------------ two-factor authentication


class TotpSetupOut(BaseModel):
    secret: str
    uri: str
    qr_svg: str


@router.post("/2fa/setup", response_model=TotpSetupOut)
async def totp_setup(
    principal: CurrentPrincipal, db: DbSession, settings: AppSettings, request: Request
) -> TotpSetupOut:
    """Start (or restart) enrolment: a new secret, active once confirmed by a code."""
    user = principal.user
    if user.totp_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "two-factor authentication already enabled")
    secret = totp.new_secret()
    try:
        user.totp_secret_enc = encrypt(settings, secret)
    except SecretKeyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    user.totp_last_step = None
    uri = totp.provisioning_uri(secret, user.username)
    await _self_audit(db, principal, request, "user.2fa_setup")
    return TotpSetupOut(secret=secret, uri=uri, qr_svg=totp.qr_svg(uri))


class CodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=6, max_length=32, repr=False)


class RecoveryCodesOut(BaseModel):
    recovery_codes: list[str]


@router.post("/2fa/enable", response_model=RecoveryCodesOut)
async def totp_enable(
    body: CodeIn,
    principal: CurrentPrincipal,
    db: DbSession,
    settings: AppSettings,
    request: Request,
) -> RecoveryCodesOut:
    """Confirm enrolment with a first code; returns the recovery codes (shown once)."""
    user = principal.user
    if user.totp_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "two-factor authentication already enabled")
    if not user.totp_secret_enc or any(c.isalpha() for c in body.code):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "start the enrolment first")
    if not service.check_second_factor(settings, user, body.code):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid code")
    codes = totp.new_recovery_codes()
    user.totp_enabled = True
    user.recovery_codes = [totp.hash_recovery(c) for c in codes]
    await _self_audit(db, principal, request, "user.2fa_enable")
    return RecoveryCodesOut(recovery_codes=codes)


@router.post("/2fa/recovery-codes", response_model=RecoveryCodesOut)
async def totp_new_recovery_codes(
    body: CodeIn,
    principal: CurrentPrincipal,
    db: DbSession,
    settings: AppSettings,
    request: Request,
) -> RecoveryCodesOut:
    """Replace the recovery codes (a current code is required)."""
    user = principal.user
    if not user.totp_enabled or not service.check_second_factor(settings, user, body.code):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid code")
    codes = totp.new_recovery_codes()
    user.recovery_codes = [totp.hash_recovery(c) for c in codes]
    await _self_audit(db, principal, request, "user.2fa_recovery_codes")
    return RecoveryCodesOut(recovery_codes=codes)


@router.post("/2fa/disable", status_code=status.HTTP_204_NO_CONTENT)
async def totp_disable(
    body: CodeIn,
    principal: CurrentPrincipal,
    db: DbSession,
    settings: AppSettings,
    request: Request,
) -> None:
    """Turn two-factor authentication off (a current or recovery code is required)."""
    user = principal.user
    if not user.totp_enabled or not service.check_second_factor(settings, user, body.code):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid code")
    if (await security_policy.load(db)).requires_2fa(user):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "the security policy requires two-factor authentication"
        )
    user.totp_enabled, user.totp_secret_enc, user.recovery_codes = False, None, []
    user.totp_last_step = None
    await _self_audit(db, principal, request, "user.2fa_disable")
