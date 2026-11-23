"""TLS certificate of the mail server: expiry date, and its renewal when it gets close.

Every hour the agent connects to the server's own TLS ports (HTTPS 443, IMAPS 993, SMTP 25
and submission 587 with STARTTLS), with the configured server name as SNI, and reads the
certificate presented (even an expired or invalid one): subject, names, issuer, end of
validity. The server alerts every hour once fewer than N days are left.

Renewal (Settings → Agent, off by default): when the certificate of the configured name
has fewer than N days left — or when asked from the interface — the agent writes
``/var/lib/carborewind/cert/request.json`` and the ``carborewind-cert.path`` unit runs
``carborewind-agent cert apply`` as root (see :mod:`.cert_apply`): certbot renewal of that
name, deployment into Carbonio, restart of the proxy and the MTA. At most one automatic
attempt every :data:`RETRY_AFTER`.
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import socket
import ssl
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.x509.oid import NameOID

CERT_DIR = Path("/var/lib/carborewind/cert")
CHECK_EVERY = 3600  # seconds between two checks
RETRY_AFTER = 6 * 3600  # seconds between two automatic renewal attempts
PORTS: tuple[tuple[str, int, bool], ...] = (  # (label, port, STARTTLS)
    ("https", 443, False),
    ("imaps", 993, False),
    ("smtp", 25, True),
    ("submission", 587, True),
)
HOSTNAME = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def valid_name(name: str) -> bool:
    return bool(HOSTNAME.fullmatch(name.strip().lower()))


def _context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # read it even when expired or not trusted
    return ctx


def fetch_der(host: str, port: int, name: str, starttls: bool, timeout: float = 10.0) -> bytes:
    """The certificate (DER) presented on ``host:port`` for the SNI ``name``."""
    ctx = _context()
    if starttls:
        with smtplib.SMTP(host, port, timeout=timeout, local_hostname="carborewind.local") as s:
            s.starttls(context=ctx)
            sock = s.sock
            assert isinstance(sock, ssl.SSLSocket)  # noqa: S101 - starttls wraps it
            der = sock.getpeercert(binary_form=True)
    else:
        with (
            socket.create_connection((host, port), timeout=timeout) as raw,
            ctx.wrap_socket(raw, server_hostname=name or None) as tls,
        ):
            der = tls.getpeercert(binary_form=True)
    if not der:
        raise OSError("no certificate presented")
    return der


def describe(der: bytes, name: str, now: datetime | None = None) -> dict[str, Any]:
    """What the interface shows of one certificate."""
    now = now or datetime.now(UTC)
    cert = x509.load_der_x509_certificate(der)
    try:
        sans = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        sans = []
    cn = [a.value for a in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]
    issuer = [a.value for a in cert.issuer.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)]
    issuer_cn = [a.value for a in cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)]
    not_after = cert.not_valid_after_utc
    names = [str(n).lower() for n in sans] or [str(c).lower() for c in cn]
    wanted = name.lower()
    matches = not wanted or any(
        n == wanted or (n.startswith("*.") and wanted.split(".", 1)[-1] == n[2:]) for n in names
    )
    return {
        "subject": str(cn[0]) if cn else None,
        "names": names[:20],
        "issuer": " ".join(str(v) for v in (issuer + issuer_cn)[:2]) or None,
        "not_before": cert.not_valid_before_utc.isoformat(),
        "not_after": not_after.isoformat(),
        "days_left": round((not_after - now).total_seconds() / 86400, 1),
        "matches": matches,
        "self_signed": cert.issuer == cert.subject,
    }


def check(
    name: str,
    *,
    host: str = "127.0.0.1",
    ports: tuple[tuple[str, int, bool], ...] = PORTS,
    fetch: Callable[[str, int, str, bool], bytes] = fetch_der,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for label, port, starttls in ports:
        entry: dict[str, Any] = {"service": label, "port": port, "name": name}
        try:
            entry.update(describe(fetch(host, port, name, starttls), name, now))
        except (OSError, smtplib.SMTPException, ValueError, ssl.SSLError) as exc:
            entry["error"] = str(exc)[:200] or type(exc).__name__
        out.append(entry)
    return out


@dataclass
class CertWatch:
    """Hourly check, and the decision to ask root for a renewal."""

    cert_dir: Path = CERT_DIR
    name: str = ""
    alert_days: int = 7
    renew_enabled: bool = False
    renew_days: int = 7
    checker: Callable[..., list[dict[str, Any]]] = check
    certificates: list[dict[str, Any]] = field(default_factory=list)
    checked_at: float | None = None
    last_request: dict[str, Any] | None = None
    last_result: dict[str, Any] | None = None
    last_auto: float | None = None
    note: str | None = None

    def configure(self, cfg: dict[str, Any], default_name: str = "") -> None:
        name = str(cfg.get("cert_name") or "").strip().lower() or default_name.lower()
        if name != self.name:
            self.checked_at = None  # check the new name at once
        self.name = name if valid_name(name) else ""
        self.alert_days = max(1, min(90, int(cfg.get("cert_alert_days") or 7)))
        self.renew_enabled = bool(cfg.get("cert_renew_enabled", False))
        self.renew_days = max(1, min(60, int(cfg.get("cert_renew_days") or 7)))

    def days_left(self) -> float | None:
        values = [
            c["days_left"] for c in self.certificates if "days_left" in c and c.get("matches")
        ]
        return min(values) if values else None

    def _write_request(self, now: float, reason: str, request_id: str | None) -> None:
        request = {
            "id": request_id or uuid.uuid4().hex,
            "action": "renew",
            "name": self.name,
            "reason": reason,
            "at": datetime.fromtimestamp(now, tz=UTC).isoformat(),
        }
        self.cert_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.cert_dir / ".request.json.tmp"
        tmp.write_text(json.dumps(request), encoding="utf-8")
        os.replace(tmp, self.cert_dir / "request.json")
        self.last_request, self.last_result = request, None

    def _read_result(self) -> None:
        try:
            data = json.loads((self.cert_dir / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if (
            isinstance(data, dict)
            and self.last_request
            and data.get("id") == self.last_request.get("id")
            and self.last_result is None
        ):
            self.last_result = {
                k: data.get(k)
                for k in ("id", "at", "ok", "renewed", "not_after", "output", "error")
            }
            self.checked_at = None  # look at the new certificate at once

    def renew(self, now: float, reason: str = "manual", request_id: str | None = None) -> bool:
        if not self.name:
            self.note = "no valid server name (Settings → Agent)"
            return False
        try:
            self._write_request(now, reason, request_id)
        except OSError as exc:
            self.note = f"cannot ask for the renewal: {exc}"
            return False
        self.note = "renewal asked"
        return True

    def evaluate(self, now: float) -> None:
        self._read_result()
        if self.checked_at is None or now - self.checked_at >= CHECK_EVERY:
            self.certificates = self.checker(self.name) if self.name else []
            self.checked_at = now
        left = self.days_left()
        if not self.renew_enabled or left is None or left > self.renew_days:
            return
        if self.last_auto is not None and now - self.last_auto < RETRY_AFTER:
            return
        if self.renew(now, "auto"):
            self.last_auto = now

    def report(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "alert_days": self.alert_days,
            "renew_enabled": self.renew_enabled,
            "renew_days": self.renew_days,
            "checked_at": datetime.fromtimestamp(self.checked_at, tz=UTC).isoformat()
            if self.checked_at
            else None,
            "days_left": self.days_left(),
            "certificates": self.certificates,
            "last_request": self.last_request,
            "last_result": self.last_result,
            "note": self.note,
        }
