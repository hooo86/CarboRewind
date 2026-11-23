"""Failed logins / blocked IPs, spam sources by network, personal dashboard layout."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select, update
from sqlalchemy.orm import Session

from carborewind_api import rdns
from carborewind_api.config import Settings
from carborewind_api.db.models import (
    Agent,
    AgentStatus,
    AppSetting,
    IpAction,
    LoginFailure,
    Notification,
    SpamSender,
)
from carborewind_api.main import create_app
from carborewind_api.notifications import SmtpConfig
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
        s.execute(delete(IpAction))
        s.execute(delete(Notification).where(Notification.event == "ip_blocked"))
        s.execute(delete(AppSetting).where(AppSetting.key == "smtp"))
        s.execute(delete(SpamSender))
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        yield client
    with Session(engine) as s, s.begin():  # leave no extra agent or pending action behind
        s.execute(delete(IpAction))
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
                        "last_failed": (now - timedelta(minutes=6)).isoformat(),
                        "last_suspended": (now - timedelta(minutes=5)).isoformat(),
                        "accounts": ["ana@lnhc.dz", "contact@lnhc.dz"],
                    }
                },
            },
        ],
    }
    assert agent.post("/mail-stats", report).status_code == 200
    agent.post("/mail-stats", report)  # absolute counters: not doubled
    assert api.get("/api/mail/security").json()["log_status"] == "agent_outdated"
    log = {"path": "/opt/zextras/log/mailbox.log", "exists": True, "readable": False}
    agent.post("/mail-stats", {"hours": [], "mailbox_log": log})
    assert api.get("/api/mail/security").json()["log_status"] == "denied"
    agent.post("/mail-stats", {"hours": [], "mailbox_log": {**log, "exists": False}})
    assert api.get("/api/mail/security").json()["log_status"] == "missing"
    agent.post("/mail-stats", {"hours": [], "mailbox_log": {**log, "readable": True}})
    out = api.get("/api/mail/security", params={"period": "24h"}).json()
    assert out["log_status"] == "ok" and out["log_path"] == log["path"]
    assert out["totals"] == {"ips": 2, "failed": 47, "blocked": 1, "blocked_now": 1}
    assert out["reported"]
    first, second = out["ips"]
    assert first["ip"] == "203.0.113.9" and first["suspended"] == 1
    assert first["recently_blocked"] is True  # no settings yet: up to an hour
    # Carbonio blocks for 2 minutes (+ 1 for the task that lifts blocks): over now.
    lf = {"safe_ips": [], "max_failed": 5, "block_minutes": 2, "task_minutes": 1}
    agent.post("/mail-stats", {"hours": [], "login_filter": lf})
    over = api.get("/api/mail/security").json()
    assert over["ips"][0]["recently_blocked"] is False and over["ips"][0]["blocked_until"] is None
    assert over["totals"]["blocked_now"] == 0 and over["totals"]["blocked"] == 1
    lf["block_minutes"] = 30
    agent.post("/mail-stats", {"hours": [], "login_filter": lf})
    still = api.get("/api/mail/security").json()["ips"][0]
    assert still["recently_blocked"] is True
    end = datetime.fromisoformat(still["blocked_until"])
    assert abs((end - (now + timedelta(minutes=26))).total_seconds()) < 5
    assert (first["failed_mailbox"], first["failed_smtp"]) == (42, 3)
    assert first["ptr"] == "mail-1.mx.spammer.co.uk" and first["domain"] == "spammer.co.uk"
    assert first["accounts"] == ["ana@lnhc.dz", "contact@lnhc.dz"]
    assert second["ip"] == "198.51.100.7" and second["suspended"] == 0
    seen = datetime.fromisoformat(first["last_seen"])
    assert abs((seen - (now - timedelta(minutes=6))).total_seconds()) < 2
    assert first["last_seen_exact"] is True
    # Old agents: the hour it happened in, flagged as not exact.
    assert second["last_seen_exact"] is False
    assert datetime.fromisoformat(second["last_seen"]) == datetime.fromisoformat(hour(-2))
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
                SpamSender(
                    hour=now,
                    sender="x@other.example",
                    count=2,
                    ips=["203.0.113.9"],
                    rcpts=["ana@lnhc.dz"],
                ),
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
    # The addresses behind a network (promo counted once per IP: 2 IPs x 5).
    assert spammer["senders"] == [
        {"address": "promo@deals.example", "count": 10, "recipients": []},
        {"address": "x@other.example", "count": 2, "recipients": ["ana@lnhc.dz"]},
    ]


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


def test_trust_and_untrust_ips(api: TestClient, engine: Engine) -> None:
    h = login(api, engine)
    agent = enroll(api, h)
    assert api.get("/api/mail/login-filter").json() == {"config": None, "actions": []}
    lf = {"safe_ips": ["192.168.8.0/24"], "max_failed": 10, "block_minutes": 15, "task_minutes": 5}
    blocked = {
        "hour": hour(),
        "ips": {
            "203.0.113.9": {"failed_mailbox": 12, "suspended": 1},
            "192.168.8.20": {"failed_mailbox": 1},
        },
    }
    agent.post("/mail-stats", {"hours": [], "security": [blocked], "login_filter": lf})
    out = api.get("/api/mail/login-filter").json()
    assert out["config"]["safe_ips"] == ["192.168.8.0/24"] and out["config"]["block_minutes"] == 15
    ips = {i["ip"]: i for i in api.get("/api/mail/security").json()["ips"]}
    assert ips["192.168.8.20"]["trusted"] is True  # inside a trusted network
    assert ips["203.0.113.9"]["trusted"] is False and ips["203.0.113.9"]["pending"] is None
    # Bad input, then a trust request (asked twice: one request).
    for bad in ("nope", "300.1.1.1", "10.0.0.0/99"):
        assert (
            api.post("/api/mail/login-filter/trust", json={"ip": bad}, headers=h).status_code == 422
        )
    first = api.post("/api/mail/login-filter/trust", json={"ip": " 203.0.113.9 "}, headers=h)
    assert first.status_code == 202 and first.json()["ip"] == "203.0.113.9"
    again = api.post("/api/mail/login-filter/trust", json={"ip": "203.0.113.9"}, headers=h)
    assert again.json()["id"] == first.json()["id"]
    assert api.get("/api/mail/security").json()["ips"][0]["pending"] == "trust"
    # The agent gets it with the next report (the filter settings are kept meanwhile).
    answer = agent.post("/mail-stats", {"hours": []}).json()
    assert answer["ip_actions"] == [
        {"id": first.json()["id"], "action": "trust", "ip": "203.0.113.9"}
    ]
    assert api.get("/api/mail/login-filter").json()["config"]["max_failed"] == 10
    done = agent.post(f"/ip-actions/{first.json()['id']}/done", {"error": None})
    assert done.status_code == 204
    agent.post(f"/ip-actions/{first.json()['id']}/done", {"error": None})  # idempotent
    assert agent.post(f"/ip-actions/{uuid.uuid4()}/done", {"error": None}).status_code == 404
    out = api.get("/api/mail/login-filter").json()
    assert out["config"]["safe_ips"] == ["192.168.8.0/24", "203.0.113.9"]
    assert out["actions"][0]["status"] == "done"
    ips = {i["ip"]: i for i in api.get("/api/mail/security").json()["ips"]}
    assert ips["203.0.113.9"]["trusted"] is True and ips["203.0.113.9"]["pending"] is None
    # Untrust: a failure is recorded with its reason.
    gone = api.post("/api/mail/login-filter/untrust", json={"ip": "203.0.113.9"}, headers=h).json()
    agent.post(f"/ip-actions/{gone['id']}/done", {"error": "service.FAILURE: x"})
    out = api.get("/api/mail/login-filter").json()
    assert out["actions"][0]["status"] == "failed" and "FAILURE" in out["actions"][0]["error"]
    assert "203.0.113.9" in out["config"]["safe_ips"]  # unchanged
    # A pending request is replaced by the opposite one.
    api.post("/api/mail/login-filter/untrust", json={"ip": "10.1.0.0/16"}, headers=h)
    api.post("/api/mail/login-filter/trust", json={"ip": "10.1.0.0/16"}, headers=h)
    pending = agent.post("/mail-stats", {"hours": []}).json()["ip_actions"]
    assert [(p["action"], p["ip"]) for p in pending] == [("trust", "10.1.0.0/16")]


def test_alert_on_new_blocks(api: TestClient, engine: Engine) -> None:
    h = login(api, engine)
    agent = enroll(api, h)
    with Session(engine) as s, s.begin():
        s.execute(delete(Notification))
        s.merge(
            AppSetting(
                key="smtp",
                value=SmtpConfig(
                    enabled=True, host="smtp.test", sender="cr@example.com", recipients=["o@x.io"]
                ).to_json(),
            )
        )
    now = datetime.now(UTC)

    def report(ip: str, last: datetime) -> None:
        agent.post(
            "/mail-stats",
            {
                "hours": [],
                "security": [
                    {
                        "hour": last.replace(minute=0, second=0, microsecond=0).isoformat(),
                        "ips": {
                            ip: {
                                "failed_mailbox": 11,
                                "suspended": 1,
                                "last_suspended": last.isoformat(),
                                "accounts": ["ana@lnhc.dz"],
                            }
                        },
                    }
                ],
            },
        )

    def alerts() -> list[Notification]:
        with Session(engine) as s:
            return list(s.scalars(select(Notification).where(Notification.event == "ip_blocked")))

    report("203.0.113.9", now - timedelta(minutes=1))
    report("203.0.113.9", now - timedelta(minutes=1))  # same block reported again
    report("198.51.100.7", now - timedelta(hours=3))  # old block (first read of the log)
    [alert] = alerts()
    assert "203.0.113.9" in alert.subject and "ana@lnhc.dz" in alert.body
    report("203.0.113.9", now)  # blocked again
    assert len(alerts()) == 2
