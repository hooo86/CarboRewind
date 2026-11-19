"""Blacklist (DNSBL) monitoring: lookups, listings and removals notified, API."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from carborewind_api import blacklist
from carborewind_api.db.models import AppSetting, Notification, Role
from carborewind_api.notifications import SmtpConfig

from .test_migrations import login
from .test_monitoring import make_api
from .test_scheduler import run_tick

pytestmark = pytest.mark.integration

LISTED: dict[str, list[str]] = {}
BROKEN: set[str] = set()


async def fake_resolver(name: str) -> list[str]:
    if name in BROKEN:
        raise TimeoutError
    if name in LISTED:
        return LISTED[name]
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


def test_query_names() -> None:
    assert (
        blacklist.query_name("105.96.54.76", "zen.spamhaus.org") == "76.54.96.105.zen.spamhaus.org"
    )
    name = blacklist.query_name("2001:db8::1", "zen.spamhaus.org.")
    assert name.startswith("1.0.0.0.0.0.0.0") and name.endswith("8.b.d.0.1.0.0.2.zen.spamhaus.org")


def test_lookup_outcomes() -> None:
    LISTED.clear()
    LISTED["4.3.2.1.listed.test"] = ["127.0.0.2", "127.0.0.4"]
    LISTED["4.3.2.1.refused.test"] = ["127.255.255.254"]
    LISTED["4.3.2.1.odd.test"] = ["10.0.0.1"]
    BROKEN.clear()
    BROKEN.add("4.3.2.1.slow.test")

    def run(zone: str) -> dict[str, object]:
        return asyncio.run(blacklist.lookup("1.2.3.4", zone, fake_resolver))

    assert run("listed.test")["listed"] is True
    assert run("clean.test") == {
        "ip": "1.2.3.4",
        "zone": "clean.test",
        "listed": False,
        "codes": [],
        "error": None,
    }
    refused = run("refused.test")
    assert refused["listed"] is None and "public" in str(refused["error"])
    assert run("odd.test")["listed"] is None
    assert run("slow.test")["error"] == "DNS: TimeoutError"


@pytest.fixture
def api(
    migrated: str, tmp_path: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    with Session(engine) as s, s.begin():
        s.execute(delete(AppSetting).where(AppSetting.key.in_(["blacklist", "blacklist_status"])))
        s.execute(delete(Notification).where(Notification.event.like("blacklist%")))
        s.merge(
            AppSetting(
                key="smtp",
                value=SmtpConfig(
                    enabled=True, host="smtp.test", sender="cr@example.com", recipients=["o@x.io"]
                ).to_json(),
            )
        )
    monkeypatch.setattr(blacklist, "system_resolver", fake_resolver)
    LISTED.clear()
    BROKEN.clear()
    with make_api(migrated, tmp_path) as client:
        yield client


def _events(engine: Engine) -> list[str]:
    with Session(engine) as s:
        return [
            n.subject
            for n in s.scalars(select(Notification).order_by(Notification.id))
            if n.event.startswith("blacklist")
        ]


def test_listing_and_removal_are_notified(api: TestClient, engine: Engine, migrated: str) -> None:
    h = login(api, engine)
    for bad in (
        {"ips": ["192.168.8.40"]},
        {"ips": ["not-an-ip"]},
        {"zones": ["bad zone"]},
        {"interval_minutes": 5},
        {"enabled": True, "ips": []},
    ):
        assert api.put("/api/monitoring/blacklist", json=bad, headers=h).status_code == 422, bad
    r = api.put(
        "/api/monitoring/blacklist",
        json={
            "enabled": True,
            "ips": ["105.96.54.76", " 38.242.195.73"],
            "zones": ["zen.spamhaus.org", "BL.SPAMCOP.NET."],
            "interval_minutes": 60,
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["zones"] == ["zen.spamhaus.org", "bl.spamcop.net"]

    LISTED["76.54.96.105.bl.spamcop.net"] = ["127.0.0.2"]
    r = api.post("/api/monitoring/blacklist/check", headers=h)
    results = {(x["ip"], x["zone"]): x for x in r.json()["results"]}
    assert len(results) == 4
    assert results[("105.96.54.76", "bl.spamcop.net")]["listed"] is True
    assert results[("38.242.195.73", "zen.spamhaus.org")]["listed"] is False
    assert _events(engine) == ["[CarboRewind] Adresse 105.96.54.76 en liste noire : bl.spamcop.net"]
    dash = api.get("/api/dashboard", headers=h).json()
    assert dash["blacklisted"] == ["105.96.54.76 (bl.spamcop.net)"]

    # Still listed: no new alert. A failed lookup changes nothing either.
    api.post("/api/monitoring/blacklist/check", headers=h)
    BROKEN.add("76.54.96.105.bl.spamcop.net")
    api.post("/api/monitoring/blacklist/check", headers=h)
    assert len(_events(engine)) == 1
    # Removed from the list: notified once.
    BROKEN.clear()
    del LISTED["76.54.96.105.bl.spamcop.net"]
    api.post("/api/monitoring/blacklist/check", headers=h)
    assert _events(engine)[-1] == "[CarboRewind] Adresse 105.96.54.76 retirée de bl.spamcop.net"
    assert api.get("/api/dashboard", headers=h).json()["blacklisted"] == []

    # The scheduler checks when the interval elapsed, not before.
    LISTED["73.195.242.38.zen.spamhaus.org"] = ["127.0.0.4"]
    run_tick(migrated, datetime.now(UTC))
    assert len(_events(engine)) == 2  # checked just now: not due
    run_tick(migrated, datetime.now(UTC) + timedelta(minutes=61))
    assert _events(engine)[-1].startswith("[CarboRewind] Adresse 38.242.195.73 en liste noire")

    domain_admin = login(api, engine, Role.DOMAIN_ADMIN)
    assert api.get("/api/monitoring/blacklist", headers=domain_admin).status_code == 403
