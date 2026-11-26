"""Login logic: local or LDAP credentials, second factor (TOTP), account lockout, audit."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import anyio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .. import audit
from ..config import Settings
from ..db.models import AuthSource, Outcome, User
from ..security import totp
from ..security.passwords import hash_password, needs_rehash, verify_password
from .ldap import LdapAuthenticator


class LoginFailure(enum.StrEnum):
    BAD_CREDENTIALS = "bad_credentials"
    LOCKED = "locked"
    INACTIVE = "inactive"
    NO_ROLE = "no_role"
    OTP_REQUIRED = "otp_required"  # password right, second factor missing: ask for it
    BAD_OTP = "bad_otp"


@dataclass
class LoginResult:
    user: User | None
    failure: LoginFailure | None = None


def normalise_username(username: str) -> str:
    return username.strip().lower()


def _lock_duration(settings: Settings, failures: int) -> timedelta:
    """1, 2, 4, 8... times the base minutes once the threshold is reached, capped at 24 h."""
    exponent = max(0, failures - settings.login_max_failures)
    return min(timedelta(minutes=settings.login_lock_base_minutes * 2**exponent), timedelta(days=1))


async def authenticate(
    db: AsyncSession,
    settings: Settings,
    ldap: LdapAuthenticator | None,
    username: str,
    password: str,
    source_ip: str | None,
    otp: str | None = None,
) -> LoginResult:
    """Check credentials and record the attempt in the audit trail.

    Every failure looks the same to the caller (generic 401) so that the response does not
    reveal whether the account exists, is locked or uses LDAP; the precise reason is audited.
    """
    login = normalise_username(username)
    now = datetime.now(UTC)
    user = await db.scalar(
        select(User)
        .options(selectinload(User.roles))
        .where(func.lower(User.username) == login)
        .with_for_update()
    )

    async def fail(reason: LoginFailure, target: User | None) -> LoginResult:
        await audit.record(
            db,
            action="auth.login",
            outcome=Outcome.FAILURE
            if reason in (LoginFailure.BAD_CREDENTIALS, LoginFailure.BAD_OTP)
            else Outcome.DENIED,
            actor_id=str(target.id) if target else None,
            actor_name=login,
            source_ip=source_ip,
            details={"reason": reason.value},
        )
        return LoginResult(None, reason)

    if user is not None and user.locked_until is not None and user.locked_until > now:
        verify_password(None, password)  # keep timing comparable
        return await fail(LoginFailure.LOCKED, user)

    ok = False
    if user is not None and user.auth_source is AuthSource.LOCAL:
        ok = verify_password(user.password_hash, password)
        if ok and user.password_hash and needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
    elif ldap is not None and (user is None or user.auth_source is AuthSource.LDAP):
        identity = await anyio.to_thread.run_sync(ldap.authenticate, username.strip(), password)
        ok = identity is not None
        if identity is not None and user is None:
            # First LDAP login: provision the user without any role; a super-admin grants roles.
            user = User(
                username=login,
                auth_source=AuthSource.LDAP,
                email=identity.email,
                display_name=identity.display_name,
                roles=[],
            )
            db.add(user)
            await db.flush()
    else:
        verify_password(None, password)

    def count_failure(target: User | None) -> None:
        if target is not None:
            target.failed_logins += 1
            if target.failed_logins >= settings.login_max_failures:
                target.locked_until = now + _lock_duration(settings, target.failed_logins)

    if not ok:
        count_failure(user)
        return await fail(LoginFailure.BAD_CREDENTIALS, user)

    assert user is not None  # noqa: S101 - ok implies a user
    if not user.is_active:
        return await fail(LoginFailure.INACTIVE, user)
    if not user.roles:
        return await fail(LoginFailure.NO_ROLE, user)
    if user.totp_enabled:
        if not otp:
            return await fail(LoginFailure.OTP_REQUIRED, user)  # not a failure: next step
        if not check_second_factor(settings, user, otp):
            count_failure(user)  # the counter is only reset after both factors
            return await fail(LoginFailure.BAD_OTP, user)
    user.failed_logins = 0
    user.locked_until = None

    user.last_login_at = now
    await audit.record(
        db,
        action="auth.login",
        outcome=Outcome.SUCCESS,
        actor_id=str(user.id),
        actor_name=user.username,
        source_ip=source_ip,
        details={"auth_source": AuthSource(user.auth_source).value},
    )
    return LoginResult(user)


def check_second_factor(settings: Settings, user: User, code: str) -> bool:
    """A TOTP code (never replayed) or one of the unused recovery codes (consumed)."""
    from ..notifications import SecretKeyError, decrypt

    code = code.strip()
    if any(c.isalpha() for c in code):  # recovery code
        digest = totp.hash_recovery(code)
        if digest in (user.recovery_codes or []):
            user.recovery_codes = [c for c in user.recovery_codes if c != digest]
            return True
        return False
    if not user.totp_secret_enc:
        return False
    try:
        secret = decrypt(settings, user.totp_secret_enc)
    except SecretKeyError:
        return False
    step = totp.verify(secret, code, user.totp_last_step)
    if step is None:
        return False
    user.totp_last_step = step
    return True
