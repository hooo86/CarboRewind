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
    BackupType,
    Job,
    JobStatus,
    MailAlert,
    MailStat,
    Notification,
    Role,
    Snapshot,
    SnapshotStatus,
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
    assert r.status_code == 200 and r.json() == {
        "actions": [],
        "ip_actions": [],
    }  # limits off by default
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


def test_delete_mailbox(api: TestClient, engine: Engine) -> None:
    h = login(api, engine)
    agent = enroll(api, h)
    e = accounts(agent, uuid.uuid4().hex[:6])
    with Session(engine) as s:
        ben = s.scalar(select(Account).where(Account.email == e["ben"]))
        assert ben is not None
        ben_id, carbonio_id = ben.id, ben.carbonio_id
    url = f"/api/mail/accounts/{ben_id}/delete"
    assert api.post(url, json={"confirm": "nope@x.io"}, headers=h).status_code == 422
    no_backup = api.post(url, json={"confirm": e["ben"]}, headers=h)
    assert no_backup.status_code == 409 and "back it up" in no_backup.json()["detail"]
    with Session(engine) as s, s.begin():
        s.add(
            Snapshot(
                account_id=ben_id,
                seq=1,
                backup_type=BackupType.FULL,
                status=SnapshotStatus.COMPLETE,
                taken_at=datetime.now(UTC),
                manifest_ref="m",
            )
        )
    r = api.post(url, json={"confirm": e["ben"].upper(), "reason": "départ"}, headers=h)
    assert r.status_code == 200 and r.json()["queued"] is True, r.text
    actions = agent.post("/mail-stats", {"hours": []}).json()["actions"]
    [delete] = [a for a in actions if a["action"] == "delete"]
    assert delete["carbonio_id"] == carbonio_id and delete["email"] == e["ben"]
    agent.post(f"/account-actions/{delete['id']}/done", {"error": None})
    with Session(engine) as s:
        assert s.get(Account, ben_id).status == AccountStatus.DELETED  # type: ignore[union-attr]
    assert api.post(url, json={"confirm": e["ben"]}, headers=h).status_code == 404


def test_set_quota_of_several_accounts(api: TestClient, engine: Engine) -> None:
    h = login(api, engine)
    agent = enroll(api, h)
    e = accounts(agent, uuid.uuid4().hex[:6])
    with Session(engine) as s, s.begin():
        rows = {a.email: a for a in s.scalars(select(Account).where(Account.email.in_(e.values())))}
        rows[e["news"]].quota_bytes = 2 << 30  # already at the quota asked
        ids = {k: rows[v].id for k, v in e.items()}
    five = 5 << 30
    body = {"account_ids": [str(i) for i in ids.values()] + [str(uuid.uuid4())]}
    assert (
        api.post(
            "/api/mail/accounts/quota", json={**body, "quota_bytes": -1}, headers=h
        ).status_code
        == 422
    )
    r = api.post("/api/mail/accounts/quota", json={**body, "quota_bytes": 2 << 30}, headers=h)
    assert r.json() == {"queued": 2, "unchanged": 1, "skipped_deleted": 0, "not_found": 1}, r.text
    # A new value replaces the pending one; a lock queued meanwhile is kept.
    lock = api.post(f"/api/mail/accounts/{ids['ana']}/lock", json={}, headers=h)
    assert lock.json()["queued"] is True
    r = api.post(
        "/api/mail/accounts/quota",
        json={"account_ids": [str(ids["ana"]), str(ids["ben"])], "quota_bytes": five},
        headers=h,
    )
    assert r.json()["queued"] == 2
    again = api.post(
        "/api/mail/accounts/quota",
        json={"account_ids": [str(ids["ana"])], "quota_bytes": five},
        headers=h,
    )
    assert again.json()["unchanged"] == 1  # same change already pending
    actions = agent.post("/mail-stats", {"hours": []}).json()["actions"]
    quotas = [a for a in actions if a["action"] == "quota"]
    assert sorted(a["email"] for a in quotas) == sorted([e["ana"], e["ben"]])
    assert {a["value"] for a in quotas} == {five}
    assert any(a["action"] == "lock" and a["email"] == e["ana"] for a in actions)
    for a in quotas:
        agent.post(f"/account-actions/{a['id']}/done", {"error": None})
    with Session(engine) as s:
        assert s.get(Account, ids["ana"]).quota_bytes == five  # type: ignore[union-attr]
        assert s.get(Account, ids["ana"]).status == AccountStatus.ACTIVE  # type: ignore[union-attr]
        audit = s.scalars(select(AuditEvent.action).where(AuditEvent.target_id == e["ana"])).all()
    assert "account.quota_request" in audit and "account.quota" in audit
    # Unlimited (0) is allowed.
    r = api.post(
        "/api/mail/accounts/quota",
        json={"account_ids": [str(ids["ben"])], "quota_bytes": 0},
        headers=h,
    )
    assert r.json()["queued"] == 1


def test_blocked_senders(api: TestClient, engine: Engine) -> None:
    h = login(api, engine)
    agent = enroll(api, h)
    e = accounts(agent, uuid.uuid4().hex[:6])
    own = e["ana"].split("@")[1]
    with Session(engine) as s, s.begin():
        s.execute(delete(AppSetting).where(AppSetting.key == "sender_blocklist"))
    url = "/api/mail/blocklist"
    for bad in ("not an address", "x@", own, f"@{own}"):
        assert api.post(url, json={"value": bad}, headers=h).status_code == 422, bad
    r = api.post(url, json={"value": "outlook.com"}, headers=h)
    assert r.status_code == 422 and "public mail provider" in r.json()["detail"]
    r = api.post(url, json={"value": " Spammer@Outlook.com ", "reason": "phishing"}, headers=h)
    assert r.status_code == 200, r.text
    r = api.post(url, json={"value": "@bad.example"}, headers=h)
    assert [x["value"] for x in r.json()["entries"]] == ["spammer@outlook.com", "@bad.example"]
    assert r.json()["entries"][1]["kind"] == "domain"
    api.post(url, json={"value": "bad.example"}, headers=h)  # already there: unchanged
    assert api.post(url, json={"value": "outlook.com", "force": True}, headers=h).status_code == 200
    assert api.post(f"{url}/remove", json={"value": "outlook.com"}, headers=h).status_code == 200
    assert api.post(f"{url}/remove", json={"value": "nope@x.example"}, headers=h).status_code == 404

    # Handed to the agent until it confirms.
    first = agent.post("/mail-stats", {"hours": []}).json()["blocklist"]
    assert first == {
        "revision": 4,
        "add": ["spammer@outlook.com", "@bad.example"],
        "remove": ["@outlook.com"],
    }
    r = agent.post("/blocklist-done", {"revision": 4, "domains": [own], "error": "x"})
    assert r.status_code == 204
    assert "blocklist" in agent.post("/mail-stats", {"hours": []}).json()
    agent.post("/blocklist-done", {"revision": 4, "domains": [own]})
    assert "blocklist" not in agent.post("/mail-stats", {"hours": []}).json()
    out = api.get(url, headers=h).json()
    mine = [a for a in out["agents"] if a["status"] and a["status"]["domains"] == [own]]
    assert mine and mine[0]["applied"] is True
    with Session(engine) as s:
        actions = set(
            s.scalars(select(AuditEvent.action).where(AuditEvent.target_type == "sender"))
        )
    assert {"blocklist.add", "blocklist.remove"} <= actions


def test_trusted_senders(api: TestClient, engine: Engine) -> None:
    h = login(api, engine)
    agent = enroll(api, h)
    e = accounts(agent, uuid.uuid4().hex[:6])
    own = e["ana"].split("@")[1]
    with Session(engine) as s, s.begin():
        s.execute(
            delete(AppSetting).where(AppSetting.key.in_(["sender_blocklist", "sender_trustlist"]))
        )
    url = "/api/mail/trustlist"
    # Never your own domains (forged senders would get through), providers only if confirmed.
    assert api.post(url, json={"value": own}, headers=h).status_code == 422
    r = api.post(url, json={"value": "gmail.com"}, headers=h)
    assert r.status_code == 422 and "public mail provider" in r.json()["detail"]
    r = api.post(url, json={"value": "partner.example", "reason": "supplier"}, headers=h)
    assert r.status_code == 200, r.text
    assert [x["value"] for x in r.json()["entries"]] == ["@partner.example"]
    # One list at most: trusted cannot be blocked, and the other way round.
    r = api.post("/api/mail/blocklist", json={"value": "@partner.example"}, headers=h)
    assert r.status_code == 422 and "trusted" in r.json()["detail"]
    api.post("/api/mail/blocklist", json={"value": "spam@bad.example"}, headers=h)
    r = api.post(url, json={"value": "spam@bad.example"}, headers=h)
    assert r.status_code == 422 and "blocked" in r.json()["detail"]

    answer = agent.post("/mail-stats", {"hours": []}).json()
    assert answer["trustlist"] == {"revision": 1, "add": ["@partner.example"], "remove": []}
    assert answer["blocklist"]["add"] == ["spam@bad.example"]
    assert agent.post("/trustlist-done", {"revision": 1, "domains": [own]}).status_code == 204
    assert "trustlist" not in agent.post("/mail-stats", {"hours": []}).json()
    out = api.get(url, headers=h).json()
    mine = [a for a in out["agents"] if a["status"] and a["status"]["domains"] == [own]]
    assert mine and mine[0]["applied"] is True
    r = api.post(f"{url}/remove", json={"value": "partner.example"}, headers=h)
    assert r.status_code == 200 and r.json()["entries"] == []
    assert agent.post("/mail-stats", {"hours": []}).json()["trustlist"]["remove"] == [
        "@partner.example"
    ]


def test_password_expiry_and_reset(api: TestClient, engine: Engine) -> None:
    h = login(api, engine)
    agent = enroll(api, h)
    tag = uuid.uuid4().hex[:6]
    now = datetime.now(UTC)
    ana, ben, cy = (f"{k}{tag}@example.com" for k in ("ana", "ben", "cy"))
    agent.post(
        "/inventory",
        {
            "accounts": [
                # 90 days, changed 87 days ago: expires in 3 days
                {
                    "carbonio_id": f"ana-{tag}",
                    "email": ana,
                    "status": "active",
                    "password_changed_at": (now - timedelta(days=87)).isoformat(),
                    "password_max_age_days": 90,
                    "password_must_change": False,
                },
                # never expires
                {
                    "carbonio_id": f"ben-{tag}",
                    "email": ben,
                    "status": "active",
                    "password_changed_at": (now - timedelta(days=400)).isoformat(),
                    "password_max_age_days": 0,
                },
                # expired 10 days ago
                {
                    "carbonio_id": f"cy-{tag}",
                    "email": cy,
                    "status": "active",
                    "password_changed_at": (now - timedelta(days=100)).isoformat(),
                    "password_max_age_days": 90,
                },
            ]
        },
    )
    with Session(engine) as s:
        rows = {
            a.email: a for a in s.scalars(select(Account).where(Account.email.in_([ana, ben, cy])))
        }
        assert rows[ben].password_expires_at is None
        left = rows[ana].password_expires_at - now  # type: ignore[operator]
        assert timedelta(days=2) < left < timedelta(days=4)
        ana_id = rows[ana].id
    # The dashboard counts them and gives the filter of the accounts list.
    d = api.get("/api/dashboard", headers=h).json()
    assert d["passwords_expiring"] >= 2 and d["password_alert_days"] == 7
    listed = api.get(
        "/api/accounts",
        params={"f": [*d["password_filter"], f"email:contains:{tag}"]},
        headers=h,
    ).json()["items"]
    assert sorted(a["email"] for a in listed) == [ana, cy]
    assert listed[0]["password_expires_at"]

    # Reset: refused too short; queued encrypted; handed to the agent; erased once applied.
    url = f"/api/mail/accounts/{ana_id}/password"
    assert api.post(url, json={"password": "short"}, headers=h).status_code == 422
    assert api.post(url, json={"password": " spaced-out "}, headers=h).status_code == 422
    r = api.post(url, json={"password": "Brand-New-Pass-42", "must_change": True}, headers=h)
    assert r.status_code == 200 and r.json()["queued"] is True, r.text
    with Session(engine) as s:
        row = s.scalars(
            select(AccountAction).where(
                AccountAction.account_id == ana_id, AccountAction.action == "password"
            )
        ).one()
        assert row.secret and "Brand-New-Pass-42" not in row.secret
        action_id = row.id
    actions = agent.post("/mail-stats", {"hours": []}).json()["actions"]
    mine = next(a for a in actions if a["id"] == str(action_id))
    assert mine["password"] == "Brand-New-Pass-42" and mine["must_change"] is True
    agent.post(f"/account-actions/{action_id}/done", {"error": None})
    with Session(engine) as s:
        row2 = s.get(AccountAction, action_id)
        assert row2 is not None and row2.status == "done" and row2.secret is None
        account = s.get(Account, ana_id)
        assert account is not None and account.password_expires_at is not None
        assert account.password_expires_at > now + timedelta(days=80)
        audit_details = s.scalars(
            select(AuditEvent.details).where(AuditEvent.target_id == ana)
        ).all()
    assert all("Brand-New-Pass-42" not in str(d) for d in audit_details)
    # Never applied within an hour: dropped, password erased.
    api.post(url, json={"password": "Another-Pass-77"}, headers=h)
    with Session(engine) as s, s.begin():
        s.execute(
            update(AccountAction)
            .where(AccountAction.account_id == ana_id, AccountAction.status == "pending")
            .values(created_at=now - timedelta(hours=2))
        )
    actions = agent.post("/mail-stats", {"hours": []}).json()["actions"]
    assert not any(a.get("password") for a in actions)
    with Session(engine) as s:
        late = s.scalars(
            select(AccountAction).where(
                AccountAction.account_id == ana_id, AccountAction.error.like("expired%")
            )
        ).one()
        assert late.secret is None
