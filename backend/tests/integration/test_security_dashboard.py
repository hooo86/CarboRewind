"""Failed logins / blocked IPs, spam sources by network, personal dashboard layout."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, update
from sqlalchemy.orm import Session

from carborewind_api import rdns
from carborewind_api.config import Settings
from carborewind_api.db.models import Agent, AgentStatus, LoginFailure, SpamSender
from carborewind_api.main import create_app
from carborewind_api.pki import init_ca

from .test_mail_traffic import hour
from .test_migrations import enroll, login

pytestmark = pytest.mark.integration

PTR = {
    "203.0.113.9": "mail-1.mx.spammer.co.uk",
    "203.0.113.10": "smtp.spammer.co.uk",
    "198.51.100.7": "host7.isp.example",
}


@pytest.fixture
def api(
    migrated: str, tmp_path: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    async def fake_lookup(ip: str) -> str | None:
        return PTR.get(ip)

    monkeypatch.setattr(rdns, "_lookup", fake_lookup)
    rdns._cache.clear()
    init_ca(tmp_path / "pki")
    settings = Settings(  # type: ignore[call-arg]
        environment="test",
        database_url=migrated,
        pki_dir=tmp_path / "pki",
        agent_proxy_ips=["testclient"],
        secret_key="t" * 40,
    )
    with Session(engine) as s, s.begin():
        s.execute(delete(LoginFailure))
        s.execute(delete(SpamSender))
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        yield client
    with Session(engine) as s, s.begin():  # leave no extra active agent behind
        s.execute(
            update(Agent)
            .where(Agent.status == AgentStatus.ACTIVE)
            .values(status=AgentStatus.REVOKED)
        )


def test_failed_logins_and_blocked_ips(api: TestClient, engine: Engine) -> None:
    h = login(api, engine)
    agent = enroll(api, h)
    now = datetime.now(UTC)
    report = {
        "hours": [],
        "security": [
            {
                "hour": hour(-2),
                "ips": {
                    "203.0.113.9": {"failed_mailbox": 30, "accounts": ["ana@lnhc.dz"]},
                    "198.51.100.7": {"failed_smtp": 2, "accounts": ["ben@lnhc.dz"]},
                },
            },
            {
                "hour": hour(),
                "ips": {
                    "203.0.113.9": {
                        "failed_mailbox": 12,
                        "failed_smtp": 3,
                        "suspended": 1,
                        "last_suspended": (now - timedelta(minutes=5)).isoformat(),
                        "accounts": ["ana@lnhc.dz", "contact@lnhc.dz"],
                    }
                },
            },
        ],
    }
    assert agent.post("/mail-stats", report).status_code == 200
    agent.post("/mail-stats", report)  # absolute counters: not doubled
    out = api.get("/api/mail/security", params={"period": "24h"}).json()
    assert out["totals"] == {"ips": 2, "failed": 47, "blocked": 1} and out["reported"]
    first, second = out["ips"]
    assert first["ip"] == "203.0.113.9" and first["suspended"] == 1
    assert first["recently_blocked"] is True
    assert (first["failed_mailbox"], first["failed_smtp"]) == (42, 3)
    assert first["ptr"] == "mail-1.mx.spammer.co.uk" and first["domain"] == "spammer.co.uk"
    assert first["accounts"] == ["ana@lnhc.dz", "contact@lnhc.dz"]
    assert second["ip"] == "198.51.100.7" and second["suspended"] == 0
    blocked = api.get("/api/mail/security", params={"only_blocked": "true"}).json()
    assert [i["ip"] for i in blocked["ips"]] == ["203.0.113.9"]
    assert api.get("/api/mail/security", params={"period": "1h"}).json()["totals"]["ips"] == 1


def test_spam_sources_grouped_by_network(api: TestClient, engine: Engine) -> None:
    login(api, engine)
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    with Session(engine) as s, s.begin():
        s.add_all(
            [
                SpamSender(
                    hour=now,
                    sender="promo@deals.example",
                    count=5,
                    ips=["203.0.113.9", "203.0.113.10"],
                ),
                SpamSender(hour=now, sender="x@other.example", count=2, ips=["203.0.113.9"]),
                SpamSender(hour=now, sender="y@isp.example", count=1, ips=["198.51.100.7"]),
                SpamSender(hour=now, sender="z@nowhere.example", count=4, ips=["192.0.2.55"]),
                SpamSender(
                    hour=now - timedelta(days=3),
                    sender="old@x.example",
                    count=99,
                    ips=["192.0.2.1"],
                ),
            ]
        )
    out = api.get("/api/mail/spam-sources", params={"period": "24h"}).json()
    by_domain = {g["domain"]: g for g in out["sources"]}
    spammer = by_domain["spammer.co.uk"]
    assert spammer["count"] == 12  # 7 via .9 and 5 via .10
    assert [i["ip"] for i in spammer["ips"]] == ["203.0.113.9", "203.0.113.10"]
    assert spammer["sender_domains"] == ["deals.example", "other.example"]
    assert by_domain[None]["ips"][0] == {"ip": "192.0.2.55", "ptr": None, "count": 4}
    assert out["sources"][0]["domain"] == "spammer.co.uk"
    assert out["sender_domains"][0] == {"domain": "deals.example", "count": 5}
    assert "x.example" not in {d["domain"] for d in out["sender_domains"]}


def test_dashboard_layout(api: TestClient, engine: Engine) -> None:
    h = login(api, engine)
    assert api.get("/api/me/dashboard").json() == {"widgets": None}
    layout = {
        "widgets": [
            {"id": "k1", "type": "kpis", "options": {"items": ["accounts", "failed"]}},
            {
                "id": "j1",
                "type": "jobs",
                "size": "half",
                "title": "Échecs",
                "options": {"status": "failed", "count": 5},
            },
        ]
    }
    r = api.put("/api/me/dashboard", json=layout, headers=h)
    assert r.status_code == 200, r.text
    got = api.get("/api/me/dashboard").json()["widgets"]
    assert [w["id"] for w in got] == ["k1", "j1"] and got[1]["size"] == "half"
    assert got[0]["size"] == "full" and got[1]["options"]["count"] == 5
    for bad in (
        {"widgets": [{"id": "x", "type": "nope"}]},
        {"widgets": [{"id": "a", "type": "kpis"}, {"id": "a", "type": "jobs"}]},
        {"widgets": [{"id": "BAD ID", "type": "kpis"}]},
        {"widgets": [{"id": "a", "type": "kpis", "options": {"x": "y" * 3000}}]},
    ):
        assert api.put("/api/me/dashboard", json=bad, headers=h).status_code == 422, bad
    api.put("/api/me/dashboard", json={"widgets": None}, headers=h)
    assert api.get("/api/me/dashboard").json() == {"widgets": None}


def test_reverse_dns_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    assert rdns.organisation("a.b.example.com") == "example.com"
    assert rdns.organisation("x.djaweb.com.dz.") == "djaweb.com.dz"
    assert rdns.organisation("localhost") is None and rdns.organisation(None) is None
    calls: list[str] = []

    async def fake_lookup(ip: str) -> str | None:
        calls.append(ip)
        return "h.example.org" if ip == "203.0.113.1" else None

    monkeypatch.setattr(rdns, "_lookup", fake_lookup)
    rdns._cache.clear()
    first = asyncio.run(rdns.resolve(["203.0.113.1", "10.0.0.1", "nope", "203.0.113.2"]))
    assert first == {
        "203.0.113.1": "h.example.org",
        "10.0.0.1": None,
        "nope": None,
        "203.0.113.2": None,
    }
    asyncio.run(rdns.resolve(["203.0.113.1", "203.0.113.2"]))  # cached, hits and misses
    assert calls == ["203.0.113.1", "203.0.113.2"]
