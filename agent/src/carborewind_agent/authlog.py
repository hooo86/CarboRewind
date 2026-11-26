"""Failed logins and blocked IP addresses, counted per hour and per client IP.

Two sources, read incrementally like the MTA log (see :mod:`maillog`):

* the **mailbox log** (``/opt/zextras/log/mailbox.log``) of the webmail, IMAP, POP3 and
  ActiveSync logins:

  - ``authentication failed for [user@domain]`` — the client address is the ``oip=`` of the
    bracketed context (original IP behind the proxy), else its ``ip=``;
  - ``Access to IP 203.0.113.9 suspended, for repeated failed login`` — Carbonio's
    *invalid login filter* blocked the address (``zimbraInvalidLoginFilter*`` attributes);

* the **MTA log**: ``warning: host[203.0.113.9]: SASL LOGIN authentication failed`` (SMTP
  submission with a wrong password).

Loopback addresses are ignored: they are the proxy or local tools, not the attacker.
Mailbox log timestamps (``2026-11-20 10:12:01,123``) are local time.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

from .maillog import hour_of, parse_time

_MAILBOX_TIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})")
# "Access to IP 203.0.113.9 suspended, for repeated failed login." and close variants
# ("Suspending IP address 203.0.113.9 …", "IP 203.0.113.9 has been suspended …").
_SUSPENDED = re.compile(
    r"(?:IP|address)\s+(?:address\s+)?([0-9A-Fa-f]{1,4}[0-9A-Fa-f.:]*[0-9A-Fa-f])\b[^\n]*?suspend"
    r"|suspend\w*\s+(?:access\s+(?:to|from)\s+)?(?:IP|address)\s+(?:address\s+)?"
    r"([0-9A-Fa-f]{1,4}[0-9A-Fa-f.:]*[0-9A-Fa-f])\b",
    re.IGNORECASE,
)
_AUTH_FAILED = re.compile(r"authentication failed for \[?([^\]\s,]+)\]?", re.IGNORECASE)
_CONTEXT_IP = re.compile(r"[\[;]oip=([0-9A-Fa-f.:]+)")
_CONTEXT_FALLBACK = re.compile(r"[\[;]ip=([0-9A-Fa-f.:]+)")
_SASL = re.compile(
    r"warning: [^\[\s]*\[([0-9A-Fa-f.:]+)\]: SASL \S+ authentication failed", re.IGNORECASE
)
_SASL_USER = re.compile(r"sasl_username=([^\s,]+)")
MAX_IPS = 2000  # per hour
MAX_ACCOUNTS = 10  # per IP and hour
KEEP_HOURS = 48


def _valid_ip(raw: str) -> str | None:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return None
    return None if ip.is_loopback or ip.is_unspecified else str(ip)


def mailbox_time(line: str, local: tzinfo) -> datetime | None:
    m = _MAILBOX_TIME.match(line)
    if not m:
        return None
    y, mo, d, h, mi, sec = (int(g) for g in m.groups())
    try:
        stamp = datetime(y, mo, d, h, mi, sec)
    except ValueError:
        return None
    return stamp.replace(tzinfo=local).astimezone(UTC)


@dataclass
class IpCounts:
    failed_mailbox: int = 0
    failed_smtp: int = 0
    suspended: int = 0
    last_suspended: str | None = None
    last_failed: str | None = None
    accounts: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "failed_mailbox": self.failed_mailbox,
            "failed_smtp": self.failed_smtp,
            "suspended": self.suspended,
            "last_suspended": self.last_suspended,
            "last_failed": self.last_failed,
            "accounts": self.accounts,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> IpCounts:
        return cls(
            failed_mailbox=int(data.get("failed_mailbox", 0)),
            failed_smtp=int(data.get("failed_smtp", 0)),
            suspended=int(data.get("suspended", 0)),
            last_suspended=data.get("last_suspended"),
            last_failed=data.get("last_failed"),
            accounts=list(data.get("accounts", []))[:MAX_ACCOUNTS],
        )


class AuthAggregator:
    """Failed logins and suspensions per UTC hour and client IP."""

    def __init__(self, tz: tzinfo | None = None) -> None:
        self.tz = tz or datetime.now().astimezone().tzinfo or UTC
        self.hours: dict[str, dict[str, IpCounts]] = {}
        self.dirty: set[str] = set()

    def _counts(self, stamp: datetime, ip: str) -> IpCounts | None:
        hour = hour_of(stamp)
        ips = self.hours.setdefault(hour, {})
        if ip not in ips and len(ips) >= MAX_IPS:
            return None
        self.dirty.add(hour)
        return ips.setdefault(ip, IpCounts())

    @staticmethod
    def _account(counts: IpCounts, account: str | None) -> None:
        if account and account not in counts.accounts and len(counts.accounts) < MAX_ACCOUNTS:
            counts.accounts.append(account.lower()[:320])

    def feed_mailbox(self, line: str) -> None:
        """One line of mailbox.log."""
        lowered = line.lower()
        if "suspend" not in lowered and "authentication failed" not in lowered:
            return
        stamp = mailbox_time(line, self.tz)
        if stamp is None:
            return
        if "suspend" in lowered and (m := _SUSPENDED.search(line)) is not None:
            ip = _valid_ip(m.group(1) or m.group(2))
            counts = self._counts(stamp, ip) if ip else None
            if counts is not None:
                counts.suspended += 1
                counts.last_suspended = stamp.isoformat()
            return
        if (m := _AUTH_FAILED.search(line)) is None:
            return
        found = _CONTEXT_IP.search(line) or _CONTEXT_FALLBACK.search(line)
        ip = _valid_ip(found.group(1)) if found else None
        counts = self._counts(stamp, ip) if ip else None
        if counts is not None:
            counts.failed_mailbox += 1
            counts.last_failed = max(counts.last_failed or "", stamp.isoformat())
            self._account(counts, m.group(1))

    def feed_mta(self, line: str, now: datetime | None = None) -> None:
        """One line of the MTA log (SASL failures of SMTP submission)."""
        if "SASL" not in line or "authentication failed" not in line:
            return
        m = _SASL.search(line)
        if m is None:
            return
        stamp = parse_time(line, now or datetime.now(UTC), self.tz)
        ip = _valid_ip(m.group(1))
        if stamp is None or ip is None:
            return
        counts = self._counts(stamp, ip)
        if counts is not None:
            counts.failed_smtp += 1
            counts.last_failed = max(counts.last_failed or "", stamp.isoformat())
            user = _SASL_USER.search(line)
            self._account(counts, user.group(1) if user else None)

    def prune(self, now: datetime) -> None:
        horizon = hour_of(now - timedelta(hours=KEEP_HOURS))
        for hour in [h for h in self.hours if h < horizon]:
            del self.hours[hour]
            self.dirty.discard(hour)

    def payload(self) -> list[dict[str, Any]]:
        """Absolute counters of the hours changed since the last successful send."""
        return [
            {"hour": hour, "ips": {ip: c.to_json() for ip, c in self.hours[hour].items()}}
            for hour in sorted(self.dirty)
            if hour in self.hours
        ]

    def to_json(self) -> dict[str, Any]:
        return {
            hour: {ip: c.to_json() for ip, c in ips.items()} for hour, ips in self.hours.items()
        }

    def restore(self, data: dict[str, Any]) -> None:
        self.hours = {
            hour: {ip: IpCounts.from_json(c) for ip, c in ips.items()}
            for hour, ips in (data or {}).items()
        }
