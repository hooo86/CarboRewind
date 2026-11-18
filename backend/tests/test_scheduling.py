"""Cron expressions and retention rules (pure functions)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from carborewind_api.scheduling import Cron, CronError, Retention, snapshots_to_keep, zone

PARIS = zone("Europe/Paris")
ALGIERS = zone("Africa/Algiers")


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)  # type: ignore[misc]


@pytest.mark.parametrize(
    "expr",
    [
        "* * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * * 13 *",
        "* * * * 8",
        "*/0 * * * *",
        "a * * * *",
        "5-1 * * * *",
        ",1 * * * *",
        "1-x * * * *",
        "1/x * * * *",
    ],
)
def test_invalid_expressions(expr: str) -> None:
    with pytest.raises(CronError):
        Cron.parse(expr)


def test_unknown_zone() -> None:
    with pytest.raises(CronError):
        zone("Mars/Olympus")


def test_fields_lists_ranges_steps() -> None:
    c = Cron.parse("*/15 1-3,22 * * 1-5")
    assert c.minute == {0, 15, 30, 45}
    assert c.hour == {1, 2, 3, 22}
    assert c.dow == {1, 2, 3, 4, 5}
    assert Cron.parse("5/20 * * * 7").minute == {5, 25, 45}
    assert Cron.parse("0 0 * * 7").dow == {0}  # 7 is Sunday
    monday = datetime(2026, 11, 16, 1, 30, tzinfo=PARIS)
    assert c.matches(monday)
    assert not c.matches(monday.replace(minute=31))
    assert not c.matches(datetime(2026, 11, 15, 1, 30, tzinfo=PARIS))  # Sunday


def test_day_of_month_or_day_of_week() -> None:
    c = Cron.parse("0 2 1 * 0")  # the 1st, or any Sunday (Vixie cron)
    assert c.matches(datetime(2026, 12, 1, 2, 0, tzinfo=PARIS))  # a Tuesday
    assert c.matches(datetime(2026, 11, 22, 2, 0, tzinfo=PARIS))  # a Sunday
    assert not c.matches(datetime(2026, 11, 24, 2, 0, tzinfo=PARIS))
    only_day = Cron.parse("0 2 1 * *")
    assert not only_day.matches(datetime(2026, 11, 22, 2, 0, tzinfo=PARIS))


def test_last_slot_in_policy_zone() -> None:
    nightly = Cron.parse("0 1 * * *")  # 01:00 in Algiers = 00:00 UTC
    now = utc(2026, 11, 18, 13, 0)
    slot = nightly.last_slot(utc(2026, 11, 17, 0, 0), now, ALGIERS)
    assert slot == utc(2026, 11, 18, 0, 0)
    # Already done: nothing new until the next night.
    assert nightly.last_slot(slot, now, ALGIERS) is None  # type: ignore[arg-type]
    # Missed several nights: only the latest is returned.
    assert nightly.last_slot(utc(2026, 11, 10, 0, 0), now, ALGIERS) == slot
    # Never more than a week back.
    weekly_old = Cron.parse("0 1 1 1 *")
    assert weekly_old.last_slot(utc(2020, 1, 1), now, ALGIERS) is None


def test_next_slot() -> None:
    monthly = Cron.parse("30 2 1 * *")
    after = utc(2026, 11, 18, 13, 0)
    assert monthly.next_slot(after, PARIS) == datetime(2026, 12, 1, 2, 30, tzinfo=PARIS)
    every = Cron.parse("*/10 * * * *")
    assert every.next_slot(after, PARIS) == utc(2026, 11, 18, 13, 10)
    never = Cron.parse("0 0 31 2 *")
    assert never.next_slot(after, PARIS, horizon_days=400) is None


def test_retention_parsing() -> None:
    assert Retention.from_json(None).unlimited
    r = Retention.from_json({"last": 3, "daily": 7})
    assert (r.last, r.daily, r.weekly, r.monthly) == (3, 7, 0, 0)
    assert r.merge(Retention(weekly=4, daily=2)) == Retention(3, 7, 4, 0)
    for bad in ({"last": -1}, {"daily": "7"}, {"weekly": True}, {"monthly": 10_001}):
        with pytest.raises(ValueError, match="retention"):
            Retention.from_json(bad)


def test_grandfather_father_son() -> None:
    start = utc(2026, 1, 1, 0, 30)
    # Two snapshots a day for 90 days (seq 1..180).
    snaps = [(n + 1, start + timedelta(hours=12 * n)) for n in range(180)]
    newest = snaps[-1][0]
    assert snapshots_to_keep([], Retention(last=3), PARIS) == set()
    assert snapshots_to_keep(snaps, Retention(), PARIS) == {s for s, _ in snaps}
    assert snapshots_to_keep(snaps, Retention(last=3), PARIS) == {178, 179, 180}
    daily = snapshots_to_keep(snaps, Retention(daily=7), PARIS)
    assert len(daily) == 7 and newest in daily
    assert all(seq % 2 == 0 for seq in daily)  # the newest of each day
    weekly = snapshots_to_keep(snaps, Retention(weekly=4), PARIS)
    assert len(weekly) == 4
    monthly = snapshots_to_keep(snaps, Retention(monthly=12), PARIS)
    assert len(monthly) == 3  # only three months of history
    combined = snapshots_to_keep(snaps, Retention(last=2, daily=7, weekly=4, monthly=12), PARIS)
    assert combined == {179, 180} | daily | weekly | monthly
    # The newest is kept even by a rule that would select nothing recent.
    assert newest in snapshots_to_keep(snaps, Retention(monthly=1), PARIS)
