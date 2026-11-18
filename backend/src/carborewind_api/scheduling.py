"""Pure scheduling logic: cron expressions and snapshot retention.

No I/O here, so both are exhaustively unit-tested; :mod:`.scheduler` applies them.

Cron
----
Standard 5 fields ``minute hour day-of-month month day-of-week`` with ``*``, lists
(``1,15``), ranges (``1-5``) and steps (``*/15``, ``0-30/10``). Day of week 0-7 (0 and 7 are
Sunday). As in Vixie cron, when both day-of-month and day-of-week are restricted, a day
matching *either* is selected. Times are evaluated in the policy's IANA time zone.

Retention
---------
Grandfather-father-son over the complete (or partial) snapshots of one account:
``last`` most recent snapshots, plus the newest snapshot of each of the last ``daily`` days,
``weekly`` ISO weeks and ``monthly`` months. The newest snapshot is always kept (it is the
base of the next incremental backup).
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31), ("month", 1, 12), ("dow", 0, 7))
MAX_LOOKBACK = timedelta(days=7)


class CronError(ValueError):
    pass


def _parse_field(text: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise CronError("empty list element")
        base, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) == 0:
                raise CronError(f"invalid step {step_text!r}")
            step = int(step_text)
        if base == "*":
            start, end = low, high
        elif "-" in base:
            a, _, b = base.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise CronError(f"invalid range {base!r}")
            start, end = int(a), int(b)
        elif base.isdigit():
            start = end = int(base)
            if step_text:
                end = high
        else:
            raise CronError(f"invalid value {base!r}")
        if not (low <= start <= high and low <= end <= high and start <= end):
            raise CronError(f"{base!r} out of range {low}-{high}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    minute: frozenset[int]
    hour: frozenset[int]
    day: frozenset[int]
    month: frozenset[int]
    dow: frozenset[int]
    day_restricted: bool
    dow_restricted: bool

    @classmethod
    def parse(cls, expression: str) -> Cron:
        parts = expression.split()
        if len(parts) != 5:
            raise CronError("a cron expression has 5 fields: minute hour day month weekday")
        fields = {
            name: _parse_field(text, low, high)
            for text, (name, low, high) in zip(parts, _FIELDS, strict=True)
        }
        dow = frozenset(d % 7 for d in fields["dow"])  # 7 is Sunday too
        return cls(
            minute=fields["minute"],
            hour=fields["hour"],
            day=fields["day"],
            month=fields["month"],
            dow=dow,
            day_restricted=parts[2] != "*",
            dow_restricted=parts[4] != "*",
        )

    def matches(self, at: datetime) -> bool:
        return at.minute in self.minute and at.hour in self.hour and self._day_matches(at.date())

    def _day_matches(self, day: date) -> bool:
        if day.month not in self.month:
            return False
        day_ok = day.day in self.day
        dow_ok = (day.isoweekday() % 7) in self.dow
        if self.day_restricted and self.dow_restricted:
            return day_ok or dow_ok
        return day_ok and dow_ok

    def _times(self) -> list[tuple[int, int]]:
        return sorted((h, m) for h in self.hour for m in self.minute)

    def last_slot(self, after: datetime, until: datetime, tz: ZoneInfo) -> datetime | None:
        """Latest matching minute in ``(after, until]`` (aware datetimes), or None.

        Only the latest slot matters: missed runs (scheduler down) are caught up once,
        not replayed. The search never looks back more than :data:`MAX_LOOKBACK`.
        """
        start = max(after, until - MAX_LOOKBACK)
        local_until = until.astimezone(tz)
        times = list(reversed(self._times()))
        day = local_until.date()
        while True:
            if self._day_matches(day):
                for h, m in times:
                    slot = datetime.combine(day, time(h, m), tz)
                    if slot <= until and slot > start:
                        return slot
                    if slot <= start:
                        break
            day -= timedelta(days=1)
            if datetime.combine(day, time(23, 59), tz) <= start:
                return None

    def next_slot(self, after: datetime, tz: ZoneInfo, horizon_days: int = 1500) -> datetime | None:
        """First matching minute strictly after ``after`` (None if none within the horizon)."""
        local = after.astimezone(tz)
        times = self._times()
        day = local.date()
        for _ in range(horizon_days):
            if self._day_matches(day):
                for h, m in times:
                    slot = datetime.combine(day, time(h, m), tz)
                    if slot > after:
                        return slot
            day += timedelta(days=1)
        return None


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronError(f"unknown time zone {name!r}") from exc


@dataclass(frozen=True)
class Retention:
    last: int = 0
    daily: int = 0
    weekly: int = 0
    monthly: int = 0

    @classmethod
    def from_json(cls, data: dict[str, object] | None) -> Retention:
        data = data or {}
        values = {}
        for key in ("last", "daily", "weekly", "monthly"):
            raw = data.get(key, 0)
            if not isinstance(raw, int) or isinstance(raw, bool) or not 0 <= raw <= 10_000:
                raise ValueError(f"retention.{key} must be an integer between 0 and 10000")
            values[key] = raw
        return cls(**values)

    @property
    def unlimited(self) -> bool:
        return not (self.last or self.daily or self.weekly or self.monthly)

    def merge(self, other: Retention) -> Retention:
        """The most generous of two policies covering the same account."""
        return Retention(
            max(self.last, other.last),
            max(self.daily, other.daily),
            max(self.weekly, other.weekly),
            max(self.monthly, other.monthly),
        )


def snapshots_to_keep(
    snapshots: Sequence[tuple[int, datetime]], rule: Retention, tz: ZoneInfo
) -> set[int]:
    """Sequence numbers to keep among ``(seq, taken_at)`` of one account."""
    ordered = sorted(snapshots, key=lambda s: (s[1], s[0]), reverse=True)
    if not ordered:
        return set()
    if rule.unlimited:
        return {seq for seq, _ in ordered}
    keep = {ordered[0][0]}  # newest: base of the next incremental
    keep.update(seq for seq, _ in ordered[: rule.last])

    def newest_per(period: int, key: Callable[[datetime], Hashable]) -> Iterator[int]:
        seen: list[Hashable] = []
        for seq, taken in ordered:
            bucket = key(taken.astimezone(tz))
            if bucket not in seen:
                if len(seen) == period:
                    return
                seen.append(bucket)
                yield seq

    keep.update(newest_per(rule.daily, lambda t: t.date()))
    keep.update(newest_per(rule.weekly, lambda t: t.isocalendar()[:2]))
    keep.update(newest_per(rule.monthly, lambda t: (t.year, t.month)))
    return keep
