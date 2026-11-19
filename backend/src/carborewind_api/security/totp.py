"""Time-based one-time passwords (RFC 6238: HMAC-SHA1, 30 s, 6 digits) and recovery codes.

Compatible with every authenticator app (Google/Microsoft Authenticator, FreeOTP, Aegis,
1Password…). A code is accepted in the current 30-second step or the one before/after
(clock drift), and never twice: the last accepted step is stored per user.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import secrets
import struct
import time
from urllib.parse import quote

DIGITS = 6
PERIOD = 30
WINDOW = 1  # steps accepted before/after the current one
RECOVERY_CODES = 10


def new_secret() -> str:
    """Random 160-bit secret, base32 without padding (what the apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    return base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // PERIOD)


def verify(secret: str, code: str, last_step: int | None, now: float | None = None) -> int | None:
    """The step the code belongs to if valid and not replayed, else None."""
    code = "".join(c for c in code if c.isdigit())
    if len(code) != DIGITS:
        return None
    step = current_step(now)
    for candidate in range(step - WINDOW, step + WINDOW + 1):
        if last_step is not None and candidate <= last_step:
            continue
        if hmac.compare_digest(code_at(secret, candidate), code):
            return candidate
    return None


def provisioning_uri(secret: str, account: str, issuer: str = "CarboRewind") -> str:
    label = quote(f"{issuer}:{account}")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={DIGITS}&period={PERIOD}"
    )


def qr_svg(uri: str) -> str:
    """QR code of the provisioning URI as an SVG document (scanned by the app)."""
    import segno

    buffer = io.BytesIO()
    segno.make(uri, error="m").save(buffer, kind="svg", scale=5, border=2, dark="#111")
    return buffer.getvalue().decode()


def new_recovery_codes() -> list[str]:
    """One-time codes for a lost phone, like ``k7m2-x9qp``."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return [
        "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(2))
        for _ in range(RECOVERY_CODES)
    ]


def hash_recovery(code: str) -> str:
    return hashlib.sha256(code.strip().lower().encode()).hexdigest()
