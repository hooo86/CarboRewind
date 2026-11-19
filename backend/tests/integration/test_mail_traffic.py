"""Mail traffic: counters from the agent, sending limits, alerts, account lock/unlock."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select, update
from sqlalchemy.orm import Session

from carborewind_api.config import Settings
from carborewind_api.db.models import (
    Account,
    AccountAction,
    AccountStatus,
    AppSetting,
    AuditEvent,
    Job,
    JobStatus,
    MailAlert,
    MailStat,
    Notification,
    Role,
    SpamSender,
)
from carborewind_api.main import create_app
from carborewind_api.notifications import SmtpConfig
from carborewind_api.pki import init_ca

from .test_migrations import enroll, login
from .test_scheduler import run_tick

pytestmark = pytest.mark.integration


def hour(delta: int = 0) -> str:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    return (now + timedelta(hours=delta)).isoformat()


@pytest.fixture
def api(migrated: str, tmp_path: Path, engine: Engine) -> Iterator[TestClient]:
    init_ca(tmp_path / "pki")
    settings = Settings(  # type: ignore[call-arg]
        environment="test",
        database_url=migrated,
        pki_dir=tmp_path / "pki",
        agent_proxy_ips=["testclient"],
        secret_key="t" * 40,
    )
    with Session(engine) as s, s.begin():
        for model in (AccountAction, MailAlert, MailStat, SpamSender, Notification, AppSetting):
            s.execute(delete(model))
        s.execute(
            update(Job)
            .where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
            .values(status=JobStatus.CANCELLED, lease_expires_at=None)
        )
        s.add(
            AppSetting(
                key="smtp",
                value=SmtpConfig(
                    enabled=True, host="smtp.test", sender="cr@example.com", recipients=["o@x.io"]
                ).to_json(),
            )
        )
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        yield client


def accounts(agent: object, tag: str) -> dict[str, str]:
    emails = {k: f"{k}{tag}@example.com" for k in ("ana", "ben", "news")}
    agent.post(  # type: ignore[attr-defined]
        "/inventory",
        {
            "accounts": [
                {"carbonio_id": f"{k}-{tag}", "email": e, "status": "active"}
                for k, e in emails.items()
            ]
        },
    )
    return emails


def test_counters_overview_and_limits(api: TestClient, engine: Engine) -> None:
    h = login(api, engine)
    agent = enroll(api, h)
    tag = uuid.uuid4().hex[:6]
    e = accounts(agent, tag)
    report = {
        "hours": [
            {
                "hour": hour(-1),
                "sent": {e["ana"]: [3, 5]},
                "spam_in": {e["ben"]: 4},
                "spam_senders": {"promo@spam.example": {"count": 4, "ips": ["203.0.113.9"]}},
            },
            {
                "hour": hour(),
                "sent": {e["ana"]: [12, 300], e["news"]: [40, 4000]},
                "spam_out": {e["ben"]: 6},
                "spam_senders": {
                    "promo@spam.example": {"count": 2, "ips": ["198.51.100.7"]},
                    "x@bad.example": {"count": 1, "ips": []},
                },
            },
        ],
        "log": "/var/log/carbonio.log",
    }
    r = agent.post("/mail-stats", report)
    assert r.status_code == 200 and r.json() == {"actions": []}  # limits off by default
    # Re-sending the same absolute counters does not double them.
    agent.post("/mail-stats", report)
    ov = api.get("/api/mail/overview", params={"period": "24h"}).json()
    assert ov["totals"] == {
        "sent_messages": 55,
        "sent_recipients": 4305,
        "spam_sent": 6,
        "spam_received": 4,
        "spam_senders": 2,
    }
    top = {s["email"]: s for s in ov["top_senders"]}
    assert top[e["news"]]["messages"] == 40 and top[e["ana"]]["recipients"] == 305
    assert top[e["ben"]]["spam_sent"] == 6 and top[e["ana"]]["status"] == "active"
    assert ov["spam_recipients"] == [{"email": e["ben"], "count": 4}]
    spam = ov["spam_senders"][0]
    assert spam["sender"] == "promo@spam.example" and spam["count"] == 6
    assert sorted(spam["ips"]) == ["198.51.100.7", "203.0.113.9"]
    assert any(a.get("log") == "/var/log/carbonio.log" for a in ov["agents"])
    assert len(ov["hourly"]) == 2
    hour_only = api.get("/api/mail/overview", params={"period": "1h"}).json()
    assert hour_only["totals"]["sent_messages"] == 52
    for period in ("7d", "30d"):
        assert api.get("/api/mail/overview", params={"period": period}).status_code == 200

    # Limits: 10 messages/hour, lock; the newsletter account is exempted.
    limits = {
        "enabled": True,
        "sent_messages_hour": 10,
        "sent_recipients_day": 0,
        "spam_sent_day": 5,
        "action": "lock",
        "exempt": [e["news"].upper()],
    }
    r = api.put("/api/mail/limits", json=limits, headers=h)
    assert r.status_code == 200 and r.json()["exempt"] == [e["news"]]
    actions = agent.post("/mail-stats", report).json()["actions"]
    locked = {a["email"]: a for a in actions}
    assert set(locked) == {e["ana"], e["ben"]}  # news is exempted
    assert locked[e["ana"]]["action"] == "lock" and locked[e["ana"]]["carbonio_id"] == f"ana-{tag}"
    assert "sent_messages_hour: 12 >= 10" in locked[e["ana"]]["reason"]
    # Same window again: no new alert, the pending action is answered again (idempotent).
    again = agent.post("/mail-stats", report).json()["actions"]
    assert len(again) == 2
    with Session(engine) as s:
        alerts = list(s.scalars(select(MailAlert).order_by(MailAlert.metric)))
        assert [(a.email, a.metric) for a in alerts] == [
            (e["ana"], "sent_messages_hour"),
            (e["ben"], "spam_sent_day"),
        ]
        notes = list(s.scalars(select(Notification).where(Notification.event == "mail_limit")))
        assert len(notes) == 2
        assert any("messages envoyés dans l'heure" in n.body for n in notes)
        assert any("compte verrouillé" in n.body for n in notes)
    # The agent locked them.
    for action in again:
        assert (
            agent.post(f"/account-actions/{action['id']}/done", {"error": None}).status_code == 204
        )
        agent.post(f"/account-actions/{action['id']}/done", {"error": None})  # idempotent
    assert agent.post("/mail-stats", {"hours": []}).json()["actions"] == []
    with Session(engine) as s:
        ana = s.scalar(select(Account).where(Account.email == e["ana"]))
        assert ana is not None and ana.status is AccountStatus.LOCKED
        actions_log = [a.action for a in s.scalars(select(AuditEvent))]
        assert "mail.limit_exceeded" in actions_log and "account.lock" in actions_log
    ov = api.get("/api/mail/overview").json()
    alert = next(a for a in ov["alerts"] if a["email"] == e["ana"])
    assert alert["account_status"] == "locked" and alert["acknowledged_at"] is None
    assert api.post(f"/api/mail/alerts/{alert['id']}/ack", headers=h).status_code == 204
    assert api.post(f"/api/mail/alerts/{uuid.uuid4()}/ack", headers=h).status_code == 404
    # Unlock by hand; the agent fails once, then succeeds.
    ana_id = alert["account_id"]
    r = api.post(
        f"/api/mail/accounts/{ana_id}/unlock", json={"reason": "password changed"}, headers=h
    )
    assert r.json()["queued"] is True
    dup = api.post(f"/api/mail/accounts/{ana_id}/unlock", json={}, headers=h).json()
    assert dup["queued"] is False
    (pending,) = agent.post("/mail-stats", {"hours": []}).json()["actions"]
    assert pending["action"] == "unlock"
    agent.post(f"/account-actions/{pending['id']}/done", {"error": "service.PERM_DENIED"})
    with Session(engine) as s:
        failed = s.get(AccountAction, uuid.UUID(pending["id"]))
        assert failed is not None and failed.status == "failed"
        assert s.scalar(select(Notification).where(Notification.event == "job_failed"))
    api.post(f"/api/mail/accounts/{ana_id}/unlock", json={}, headers=h)
    # A lock request supersedes a pending unlock.
    api.post(f"/api/mail/accounts/{ana_id}/lock", json={}, headers=h)
    (only,) = agent.post("/mail-stats", {"hours": []}).json()["actions"]
    assert only["action"] == "lock"
    assert (
        api.post(f"/api/mail/accounts/{uuid.uuid4()}/lock", json={}, headers=h).status_code == 404
    )
    assert agent.post(f"/account-actions/{uuid.uuid4()}/done", {"error": None}).status_code == 404


def test_validation_rbac_and_retention(api: TestClient, engine: Engine, migrated: str) -> None:
    h = login(api, engine)
    agent = enroll(api, h)
    assert api.get("/api/mail/limits").json()["enabled"] is False
    for bad in ({"action": "delete"}, {"sent_messages_hour": -1}, {"exempt": ["not-an-email"]}):
        assert api.put("/api/mail/limits", json=bad, headers=h).status_code == 422
    assert api.get("/api/mail/overview", params={"period": "2y"}).status_code == 422
    bad_counts = {"hours": [{"hour": hour(), "sent": {"a@b.test": [1]}}]}
    assert agent.post("/mail-stats", bad_counts).status_code == 422
    old = {"hours": [{"hour": hour(-24 * 100), "sent": {"old@b.test": [1, 1]}}]}
    assert agent.post("/mail-stats", old).status_code == 200
    run_tick(migrated, datetime.now(UTC), retention=True)
    with Session(engine) as s:
        assert s.scalar(select(MailStat).where(MailStat.email == "old@b.test")) is None
    viewer = login(api, engine, Role.DOMAIN_ADMIN)
    assert api.get("/api/mail/overview").status_code == 403
    assert api.put("/api/mail/limits", json={}, headers=viewer).status_code == 403
