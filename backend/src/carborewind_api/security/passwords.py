"""Argon2id password hashing (OWASP-recommended parameters)."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# RFC 9106 / OWASP: Argon2id, m=19 MiB, t=2, p=1 is the minimum; we use 64 MiB, t=3, p=4.
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=4)

# Verified against when the user does not exist, so that response time does not reveal
# whether a username is valid.
_DUMMY_HASH = _hasher.hash("carborewind-timing-equaliser")

MIN_LENGTH = 12


class WeakPasswordError(ValueError):
    """The password does not meet the minimum policy."""


def check_policy(password: str) -> None:
    """Minimal policy: length only (NIST SP 800-63B). Raises :class:`WeakPasswordError`."""
    if len(password) < MIN_LENGTH:
        raise WeakPasswordError(f"password must be at least {MIN_LENGTH} characters")
    if len(password) > 1024:
        raise WeakPasswordError("password is too long")


def hash_password(password: str) -> str:
    check_policy(password)
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Constant-time-ish verification. ``password_hash`` None runs a dummy verification."""
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
