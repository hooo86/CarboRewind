"""Mail traffic from the Carbonio MTA log (Postfix + Amavis), counted per hour.

Read incrementally from ``/var/log/carbonio.log`` (or ``zimbra.log`` / ``mail.log``):

* **sent** — ``postfix/cleanup: <qid>: message-id=<…>`` then ``postfix/qmgr: <qid>:
  from=<sender>, size=…, nrcpt=N``. A message counts once per sender and Message-ID, so the
  copy re-injected by the content filter (same Message-ID, new queue id) is not counted
  twice. Only senders that are local accounts (or their aliases) are kept;
* **spam received** — ``amavis: … Blocked|Passed SPAM|SPAMMY {…Inbound…}, [ip] <sender> ->
  <rcpt>,…``: one per local recipient, and the sender with its client IP addresses;
* **spam sent** — the same with ``ORIGINATING`` / ``…Outbound…``: spam submitted by a local
  account, the classic sign of a stolen password.

Timestamps: RFC 3164 (``Nov 19 10:12:01``, local time, year guessed) or RFC 3339
(``2026-11-19T10:12:01.123+01:00``). Buckets are UTC hours.
"""

from __future__ import annotations

import json
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1
    )
}
_RFC3164 = re.compile(r"^([A-Z][a-z]{2}) +(\d{1,2}) (\d{2}):(\d{2}):(\d{2}) ")
_RFC3339 = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}))\s")
_CLEANUP = re.compile(r"postfix/cleanup\[\d+\]: ([0-9A-Za-z]+): message-id=<?([^>\s]*)>?")
_QMGR = re.compile(r"postfix/qmgr\[\d+\]: ([0-9A-Za-z]+): from=<([^>]*)>, size=\d+, nrcpt=(\d+)")
_AMAVIS = re.compile(
    r"amavis\[\d+\]: \([^)]*\) (?:Passed|Blocked) (SPAM|SPAMMY)\b[^{]*\{([^}]*)\},?"
    r"(?P<orig> ORIGINATING(?: LOCAL)?)?"
    r"(?: \[(?P<ip>[0-9A-Fa-f.:]+)\](?::\d+)?)?(?: \[[^\]]*\])?"
    r" <(?P<sender>[^>]*)> -> (?P<rcpts><[^>]*>(?:,<[^>]*>)*)"
)
MAX_QUEUE_IDS = 50_000
MAX_SPAM_SENDERS = 500  # per hour
KEEP_HOURS = 48
FIRST_READ = 20 * 1024 * 1024  # without state: only the end of the current log
MAX_READ = 50 * 1024 * 1024  # per pass


def parse_time(line: str, now: datetime, local: tzinfo) -> datetime | None:
    m = _RFC3339.match(line)
    if m:
        try:
            return datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None
    m = _RFC3164.match(line)
    if not m or m.group(1) not in _MONTHS:
        return None
    month, day = _MONTHS[m.group(1)], int(m.group(2))
    year = now.year - 1 if month > now.month else now.year  # December lines read in January
    try:
        stamp = datetime(year, month, day, int(m.group(3)), int(m.group(4)), int(m.group(5)))
    except ValueError:
        return None
    return stamp.replace(tzinfo=local).astimezone(UTC)


def hour_of(stamp: datetime) -> str:
    return stamp.replace(minute=0, second=0, microsecond=0).isoformat()


@dataclass
class Bucket:
    sent: dict[str, list[int]] = field(default_factory=dict)  # email -> [messages, recipients]
    spam_in: dict[str, int] = field(default_factory=dict)  # local recipient -> count
    spam_out: dict[str, int] = field(default_factory=dict)  # local sender -> count
    spam_senders: dict[str, dict[str, Any]] = field(default_factory=dict)  # -> {count, ips}
    seen: set[str] = field(default_factory=set)  # sender\0message-id already counted

    def to_json(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "spam_in": self.spam_in,
            "spam_out": self.spam_out,
            "spam_senders": self.spam_senders,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Bucket:
        return cls(
            sent={k: list(v) for k, v in data.get("sent", {}).items()},
            spam_in=dict(data.get("spam_in", {})),
            spam_out=dict(data.get("spam_out", {})),
            spam_senders=dict(data.get("spam_senders", {})),
        )


class Aggregator:
    """Turns log lines into hourly counters. ``local`` maps every local address (accounts
    and aliases, lower case) to the account's main address."""

    def __init__(self, local: dict[str, str] | None = None, tz: tzinfo | None = None) -> None:
        self.local = local or {}
        self.tz = tz or datetime.now().astimezone().tzinfo or UTC
        self.buckets: dict[str, Bucket] = {}
        self.dirty: set[str] = set()
        self._message_ids: OrderedDict[str, str] = OrderedDict()

    def _bucket(self, hour: str) -> Bucket:
        self.dirty.add(hour)
        return self.buckets.setdefault(hour, Bucket())

    def feed(self, line: str, now: datetime | None = None) -> None:
        if "postfix/" not in line and "amavis[" not in line:
            return
        if (m := _CLEANUP.search(line)) is not None:
            self._message_ids[m.group(1)] = m.group(2)
            if len(self._message_ids) > MAX_QUEUE_IDS:
                self._message_ids.popitem(last=False)
            return
        m_q = _QMGR.search(line)
        m_a = None if m_q else _AMAVIS.search(line)
        if m_q is None and m_a is None:
            return
        stamp = parse_time(line, now or datetime.now(UTC), self.tz)
        if stamp is None:
            return
        hour = hour_of(stamp)
        if m_q is not None:
            sender = self.local.get(m_q.group(2).lower())
            if sender is None:
                return  # external sender, bounce (<>) or unknown address
            message_id = self._message_ids.get(m_q.group(1)) or f"qid:{m_q.group(1)}"
            bucket = self._bucket(hour)
            key = f"{sender}\0{message_id}"
            if key in bucket.seen:
                return  # re-injected by the content filter
            bucket.seen.add(key)
            counts = bucket.sent.setdefault(sender, [0, 0])
            counts[0] += 1
            counts[1] += int(m_q.group(3))
            return
        assert m_a is not None  # noqa: S101
        flags = m_a.group(2)
        sender = m_a.group("sender").lower()
        outbound = bool(m_a.group("orig")) or "Outbound" in flags or "Originating" in flags
        bucket = self._bucket(hour)
        if outbound:
            local_sender = self.local.get(sender)
            if local_sender:
                bucket.spam_out[local_sender] = bucket.spam_out.get(local_sender, 0) + 1
            return
        rcpts = re.findall(r"<([^>]*)>", m_a.group("rcpts"))
        for rcpt in rcpts:
            local_rcpt = self.local.get(rcpt.lower())
            if local_rcpt:
                bucket.spam_in[local_rcpt] = bucket.spam_in.get(local_rcpt, 0) + 1
        key = sender or "<>"
        entry = bucket.spam_senders.get(key)
        if entry is None:
            if len(bucket.spam_senders) >= MAX_SPAM_SENDERS:
                return
            entry = bucket.spam_senders[key] = {"count": 0, "ips": []}
        entry["count"] += 1
        ip = m_a.group("ip")
        if ip and ip not in entry["ips"] and len(entry["ips"]) < 10:
            entry["ips"].append(ip)

    def prune(self, now: datetime) -> None:
        horizon = hour_of(now - timedelta(hours=KEEP_HOURS))
        for hour in [h for h in self.buckets if h < horizon]:
            del self.buckets[hour]
            self.dirty.discard(hour)

    def payload(self) -> list[dict[str, Any]]:
        """Absolute counters of the hours changed since the last successful send."""
        return [
            {"hour": hour, **self.buckets[hour].to_json()}
            for hour in sorted(self.dirty)
            if hour in self.buckets
        ]


class LogReader:
    """Reads what was appended to the log since last time, across rotations."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.inode: int | None = None
        self.offset = 0

    def state(self) -> dict[str, Any]:
        return {"path": str(self.path), "inode": self.inode, "offset": self.offset}

    def restore(self, state: dict[str, Any]) -> None:
        if state.get("path") == str(self.path):
            self.inode, self.offset = state.get("inode"), int(state.get("offset", 0))

    def _read(self, path: Path, start: int, limit: int) -> tuple[list[str], int]:
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(limit)
        end = data.rfind(b"\n")
        if end == -1:
            return [], start
        chunk = data[: end + 1]
        return chunk.decode("utf-8", "replace").splitlines(), start + len(chunk)

    def read_new(self) -> list[str]:
        try:
            info = os.stat(self.path)
        except FileNotFoundError:
            return []
        lines: list[str] = []
        if self.inode is None:  # first run: only the end of the log
            start = max(0, info.st_size - FIRST_READ)
            self.inode, self.offset = info.st_ino, start
            if start:
                with open(self.path, "rb") as fh:
                    fh.seek(start)
                    self.offset = start + len(fh.readline())
        elif self.inode != info.st_ino:  # rotated: finish the old file if it is still there
            rotated = self.path.with_name(self.path.name + ".1")
            try:
                if os.stat(rotated).st_ino == self.inode:
                    lines, _ = self._read(rotated, self.offset, MAX_READ)
            except FileNotFoundError:
                pass
            self.inode, self.offset = info.st_ino, 0
        elif info.st_size < self.offset:  # truncated in place
            self.offset = 0
        new, self.offset = self._read(self.path, self.offset, MAX_READ)
        return lines + new


def first_existing(paths: list[Path]) -> Path | None:
    return next((p for p in paths if p.exists()), None)


def save_state(path: Path, reader: LogReader, agg: Aggregator) -> None:
    data = {
        "reader": reader.state(),
        "buckets": {h: b.to_json() for h, b in agg.buckets.items()},
        "dirty": sorted(agg.dirty),
    }
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def load_state(path: Path, reader: LogReader, agg: Aggregator) -> None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    reader.restore(data.get("reader") or {})
    agg.buckets = {h: Bucket.from_json(b) for h, b in (data.get("buckets") or {}).items()}
    agg.dirty = set(data.get("dirty") or [])
