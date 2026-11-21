"""Monitoring: storage trend/forecast, accounts at risk, copy 2 lag, integrity checks,
Prometheus metrics, safety of automatic locks."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select, update
from sqlalchemy.orm import Session

from carborewind_api.config import Settings
from carborewind_api.db.models import (
    Account,
    AccountAction,
    AppSetting,
    BackupType,
    Job,
    JobKind,
    JobStatus,
    MailAlert,
    MailStat,
    Notification,
    Role,
    Snapshot,
    SnapshotStatus,
    StorageSample,
)
from carborewind_api.main import create_app
from carborewind_api.monitoring import forecast
from carborewind_api.notifications import SmtpConfig
from carborewind_api.pki import init_ca

from .test_migrations import enroll, login
from .test_scheduler import run_tick

pytestmark = pytest.mark.integration
GIB = 1024**3


def make_api(migrated: str, tmp_path: Path, token: str | None = None) -> TestClient:
    init_ca(tmp_path / "pki")
    settings = Settings(  # type: ignore[call-arg]
        environment="test",
        database_url=migrated,
        pki_dir=tmp_path / "pki",
        agent_proxy_ips=["testclient"],
        secret_key="m" * 40,
        metrics_token=token,
    )
    return TestClient(create_app(settings), base_url="https://testserver")


@pytest.fixture
def api(migrated: str, tmp_path: Path, engine: Engine) -> Iterator[TestClient]:
    with Session(engine) as s, s.begin():
        for model in (AccountAction, MailAlert, MailStat, StorageSample, Notification, AppSetting):
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
    with make_api(migrated, tmp_path, token="prom-token") as client:
        yield client


def test_forecast() -> None:
    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    # 100 GiB disk, +1 GiB used per day from 50 GiB: full in 40 days after day 10.
    points = [(t0 + timedelta(days=d), 100 * GIB, (50 - d) * GIB) for d in range(11)]
    result = forecast(points)
    assert result["days_until_full"] == pytest.approx(40, abs=0.1)
    assert result["growth_per_day"] == pytest.approx(GIB, rel=0.01)
    flat = [(t0 + timedelta(days=d), 100 * GIB, 50 * GIB) for d in range(5)]
    assert forecast(flat)["days_until_full"] is None
    assert forecast([])["days_until_full"] is None
    assert forecast(points[:1])["growth_per_day"] is None


def test_overview_storage_risk_and_replica(api: TestClient, engine: Engine, migrated: str) -> None:
    h = login(api, engine)
    agent = enroll(api, h)
    tag = uuid.uuid4().hex[:6]
    emails = [f"{n}@r{tag}.test" for n in ("never", "old", "fresh", "partial")]
    report = {
        "path": "/var/lib/carborewind/repository",
        "kind": "local",
        "total_bytes": 100 * GIB,
        "free_bytes": 40 * GIB,
        "replica": {
            "path": "/c2",
            "kind": "network",
            "total_bytes": 200 * GIB,
            "free_bytes": 150 * GIB,
        },
    }
    agent.post(
        "/inventory",
        {
            "accounts": [
                {"carbonio_id": f"{e}-id", "email": e, "status": "active"} for e in emails
            ],
            "storage": report,
        },
    )
    now = datetime.now(UTC)
    with Session(engine) as s, s.begin():
        ids = {a.email: a for a in s.scalars(select(Account).where(Account.email.in_(emails)))}
        agent_id = uuid.UUID(agent.id)
        # A growing disk over 10 days.
        for d in range(1, 11):
            s.add(
                StorageSample(
                    hour=(now - timedelta(days=d)).replace(minute=0, second=0, microsecond=0),
                    agent_id=agent_id,
                    target="primary",
                    total_bytes=100 * GIB,
                    free_bytes=(40 + d) * GIB,
                )
            )

        def snap(email: str, days: float, st: SnapshotStatus) -> None:
            s.add(
                Snapshot(
                    account_id=ids[email].id,
                    seq=1,
                    backup_type=BackupType.FULL,
                    status=st,
                    taken_at=now - timedelta(days=days),
                    manifest_ref="m",
                )
            )

        snap(emails[1], 3, SnapshotStatus.COMPLETE)
        snap(emails[2], 0.1, SnapshotStatus.COMPLETE)
        snap(emails[3], 0.1, SnapshotStatus.PARTIAL)
        s.add(
            Job(
                kind=JobKind.BACKUP,
                status=JobStatus.FAILED,
                account_id=ids[emails[2]].id,
                finished_at=now,
            )
        )
    ov = api.get("/api/monitoring/overview").json()
    primary = next(x for x in ov["storage"] if x["target"] == "primary")
    assert primary["free_bytes"] == 40 * GIB and len(primary["points"]) == 11
    assert primary["days_until_full"] == pytest.approx(40, abs=1)
    assert any(x["target"] == "replica" for x in ov["storage"])
    risk = ov["at_risk"]
    mine = lambda key: {a["email"] for a in risk[key]["accounts"]} & set(emails)  # noqa: E731
    assert mine("never") == {emails[0]} or risk["never"]["count"] > 20
    assert (
        emails[1] in {a["email"] for a in risk["stale"]["accounts"]} or risk["stale"]["count"] > 20
    )
    assert mine("partial") == {emails[3]} or risk["partial"]["count"] > 20
    assert (
        emails[2] in {a["email"] for a in risk["failed"]["accounts"]}
        or risk["failed"]["count"] > 20
    )
    assert risk["never"]["filters"] == ["last_backup_at:not_set"]
    assert risk["no_policy"]["count"] >= 4
    assert ov["replica"] is None and ov["metrics_token_configured"] is True
    assert ov["next_integrity_at"] is not None and ov["settings"]["integrity_sample"] == 0.1
    # Copy 2 configured, never refreshed while backups ran 2 days ago: late.
    api.put(
        "/api/settings/storage",
        json={
            "primary": {"kind": "local"},
            "copy_02": {"share": "//nas2/c2", "username": "u", "password": "p"},
        },
        headers=h,
    )
    with Session(engine) as s, s.begin():
        s.add(
            Job(
                kind=JobKind.BACKUP,
                status=JobStatus.SUCCEEDED,
                started_at=now - timedelta(days=2),
                finished_at=now - timedelta(days=2),
            )
        )
    replica = api.get("/api/monitoring/overview").json()["replica"]
    assert replica["behind"] and replica["late"] and replica["lag_seconds"] > 40 * 3600
    # Daily alerts: copy 2 late; storage full soon once the forecast is short.
    with Session(engine) as s, s.begin():
        s.execute(delete(StorageSample).where(StorageSample.agent_id == agent_id))
        for d in range(5):  # +10 GiB a day, 20 GiB left: full in 2 days
            s.add(
                StorageSample(
                    hour=now.replace(minute=0, second=0, microsecond=0) - timedelta(days=d),
                    agent_id=agent_id,
                    target="primary",
                    total_bytes=100 * GIB,
                    free_bytes=(20 + 10 * d) * GIB,
                )
            )
    run_tick(migrated, now, retention=True)
    with Session(engine) as s:
        events = {n.event for n in s.scalars(select(Notification))}
        assert "replica_lag" in events and "storage_full" in events


def test_integrity_checks(api: TestClient, engine: Engine, migrated: str) -> None:
    h = login(api, engine)
    agent = enroll(api, h)
    assert (
        api.post("/api/monitoring/verify", json={"target": "replica"}, headers=h).status_code == 409
    )
    r = api.post("/api/monitoring/verify", json={"sample": 0.5}, headers=h)
    assert r.status_code == 202 and r.json()["queued"] is True
    assert api.post("/api/monitoring/verify", json={}, headers=h).json()["queued"] is False
    claimed = agent.post("/jobs/claim").json()
    assert claimed["kind"] == "verify" and claimed["params"] == {"target": "primary", "sample": 0.5}
    result = {
        "target": "primary",
        "sample": 0.5,
        "checked": 100,
        "corrupted": ["a" * 64],
        "corrupted_count": 1,
        "manifests_checked": 10,
        "manifests_corrupted": [],
    }
    path = f"/jobs/{claimed['job_id']}/verify-complete"
    assert agent.post(path, result).status_code == 204
    assert agent.post(path, result).status_code == 204  # idempotent
    with Session(engine) as s:
        note = s.scalar(select(Notification).where(Notification.event == "integrity_failed"))
        assert note is not None and "la copie 1 (dépôt)" in note.subject
    integrity = api.get("/api/monitoring/overview").json()["integrity"]
    assert integrity["primary"]["progress"]["corrupted_count"] == 1
    assert integrity["replica"] is None
    # Scheduled: every minute in this test; the first tick only records where it starts.
    body = {"integrity_cron": "* * * * *", "integrity_sample": 0.2}
    assert api.put("/api/monitoring/settings", json=body, headers=h).status_code == 200
    now = datetime.now(UTC)
    assert run_tick(migrated, now, retention=False).verify_jobs == []
    report = run_tick(migrated, now + timedelta(minutes=2), retention=False)
    assert len(report.verify_jobs) == 1
    with Session(engine) as s:
        job = s.get(Job, report.verify_jobs[0])
        assert job is not None and job.params == {"target": "primary", "sample": 0.2}
    for bad in (
        {"integrity_cron": "nope"},
        {"integrity_sample": 0},
        {"integrity_timezone": "Mars"},
    ):
        assert api.put("/api/monitoring/settings", json=bad, headers=h).status_code == 422
    api.put("/api/monitoring/settings", json={"integrity_enabled": False}, headers=h)
    assert run_tick(migrated, now + timedelta(minutes=5), retention=False).verify_jobs == []


def test_metrics(api: TestClient, engine: Engine, migrated: str, tmp_path: Path) -> None:
    h = login(api, engine)
    agent = enroll(api, h)
    agent.post(
        "/inventory",
        {
            "accounts": [],
            "storage": {"path": "/r", "kind": "local", "total_bytes": 10, "free_bytes": 4},
        },
    )
    direct = api.get("/metrics")
    assert direct.status_code == 200
    text = direct.text
    for name in (
        "carborewind_accounts{",
        'carborewind_accounts_at_risk{reason="never"}',
        "carborewind_storage_free_bytes{",
        "carborewind_agent_last_seen_seconds{",
        "carborewind_mail_alerts_open 0",
        "# TYPE carborewind_jobs_failed_24h gauge",
    ):
        assert name in text, name
    proxied = {"X-Forwarded-For": "203.0.113.1"}
    assert api.get("/metrics", headers=proxied).status_code == 404
    assert (
        api.get("/metrics", headers={**proxied, "Authorization": "Bearer nope"}).status_code == 404
    )
    ok = api.get("/metrics", headers={**proxied, "Authorization": "Bearer prom-token"})
    assert ok.status_code == 200 and "carborewind_accounts" in ok.text
    with make_api(migrated, tmp_path / "x") as no_token:
        assert no_token.get("/metrics", headers=proxied).status_code == 404


def test_automatic_locks_are_bounded(api: TestClient, engine: Engine) -> None:
    h = login(api, engine)
    agent = enroll(api, h)
    tag = uuid.uuid4().hex[:6]
    emails = [f"u{i}{tag}@example.com" for i in range(5)]
    accounts = [
        {"carbonio_id": f"{e}-id", "email": e, "status": "active", "is_admin": i == 0}
        for i, e in enumerate(emails)
    ]
    agent.post("/inventory", {"accounts": accounts})
    api.put(
        "/api/mail/limits",
        json={
            "enabled": True,
            "sent_messages_hour": 5,
            "action": "lock",
            "max_auto_locks_per_hour": 2,
        },
        headers=h,
    )
    hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0).isoformat()
    actions = agent.post(
        "/mail-stats", {"hours": [{"hour": hour, "sent": {e: [10, 10] for e in emails}}]}
    ).json()["actions"]
    assert len(actions) == 2 and emails[0] not in {a["email"] for a in actions}
    with Session(engine) as s:
        kinds = {a.email: a.action for a in s.scalars(select(MailAlert))}
        assert kinds[emails[0]] == "alert_admin"
        assert sorted(kinds.values()).count("alert_cap") == 2
        notes = " ".join(n.body for n in s.scalars(select(Notification)))
        assert "compte administrateur" in notes and "trop de verrouillages" in notes


def test_mail_storage_and_nearly_full_mailboxes(
    api: TestClient, engine: Engine, migrated: str
) -> None:
    h = login(api, engine)
    agent = enroll(api, h)
    tag = uuid.uuid4().hex[:6]
    accounts = [
        {
            "carbonio_id": f"f{tag}",
            "email": f"full@q{tag}.test",
            "status": "active",
            "quota_bytes": 10 * GIB,
            "used_bytes": 95 * GIB // 10,
        },
        {
            "carbonio_id": f"b{tag}",
            "email": f"big@q{tag}.test",
            "status": "active",
            "quota_bytes": 0,
            "used_bytes": 60 * GIB,
        },
        {
            "carbonio_id": f"s{tag}",
            "email": f"small@q{tag}.test",
            "status": "active",
            "quota_bytes": 10 * GIB,
            "used_bytes": GIB,
        },
    ]
    storage = {
        "path": "/var/lib/carborewind/nas/repository",
        "kind": "network",
        "total_bytes": 200 * GIB,
        "free_bytes": 100 * GIB,
        "mail_store": {
            "path": "/opt/zextras",
            "kind": "local",
            "total_bytes": 1000 * GIB,
            "free_bytes": 900 * GIB,
        },
    }
    agent.post("/inventory", {"accounts": accounts, "storage": storage})
    dash = api.get("/api/dashboard").json()
    assert dash["mail_used_bytes"] >= int(70.5 * GIB)
    assert dash["mailboxes_nearly_full"] >= 1 and dash["mailbox_alert_percent"] == 90
    st = next(s for s in dash["storage"] if s.get("mail_store"))
    assert st["mail_store"]["total_bytes"] == 1000 * GIB
    ov = api.get("/api/monitoring/overview").json()
    assert any(s["target"] == "server" for s in ov["storage"])
    quota = ov["at_risk"]["quota"]
    names = {a["email"] for a in quota["accounts"]}
    assert f"full@q{tag}.test" in names or quota["count"] > 20
    assert f"small@q{tag}.test" not in names
    assert quota["filters"] == ["usage_pct:ge:90"]
    # An absolute size also counts (mailboxes without quota).
    body = {**ov["settings"], "mailbox_alert_gb": 50}
    assert api.put("/api/monitoring/settings", json=body, headers=h).status_code == 200
    ov = api.get("/api/monitoring/overview").json()
    assert f"big@q{tag}.test" in {a["email"] for a in ov["at_risk"]["quota"]["accounts"]} or (
        ov["at_risk"]["quota"]["count"] > 20
    )
    assert ov["at_risk"]["quota"]["filters"] == []
    run_tick(migrated, datetime.now(UTC), retention=True)
    with Session(engine) as s:
        note = s.scalar(select(Notification).where(Notification.event == "mailbox_quota"))
        assert note is not None and f"full@q{tag}.test" in note.body and "GB" in note.body
    assert "carborewind_mailboxes_used_bytes" in api.get("/metrics").text
    assert (
        api.put(
            "/api/monitoring/settings", json={**body, "mailbox_alert_percent": 150}, headers=h
        ).status_code
        == 422
    )


def test_carbonio_services_and_mail_queue(api: TestClient, engine: Engine) -> None:
    h = login(api, engine)
    agent = enroll(api, h)

    def report(amavis: bool | None, queue: int = 3) -> None:
        body = {
            "checked_at": "2026-11-19T10:00:00+00:00",
            "services": [
                {"name": "mta", "ok": True, "source": "carbonio", "checked": 1},
                {"name": "amavis", "ok": amavis, "source": "carbonio", "checked": 1},
                {"name": "port:amavis", "ok": bool(amavis), "source": "port", "port": 10024},
            ],
            "queue": {"deferred": queue, "active": 0},
            "errors": [],
        }
        assert agent.post("/services", body).status_code == 200

    def events(name: str) -> list[Notification]:
        with Session(engine) as s:
            return list(s.scalars(select(Notification).where(Notification.event == name)))

    report(True)
    report(False)  # one report down: maybe a restart, no alert yet
    assert events("service_down") == []
    report(None)  # unknown (stale record) changes nothing
    report(False)
    down = events("service_down")
    assert {n.subject for n in down} == {
        "[CarboRewind] Service Carbonio arrêté : amavis",
        "[CarboRewind] Service Carbonio arrêté : port:amavis",
    }
    report(False)
    assert len(events("service_down")) == 2  # alerted once per outage
    ov = api.get("/api/monitoring/overview", headers=h).json()
    mine = next(v for v in ov["services"] if v["down"])
    assert mine["down"] == ["amavis", "port:amavis"] and mine["queue_total"] == 3
    assert mine["services"][0]["since"] is not None and mine["stale"] is False
    dash = api.get("/api/dashboard", headers=h).json()
    assert "amavis" in dash["services_down"] and dash["mail_queue_total"] >= 3
    metrics = api.get(
        "/metrics", headers={"X-Forwarded-For": "203.0.113.1", "Authorization": "Bearer prom-token"}
    )
    assert 'carborewind_carbonio_service_up{agent="' in metrics.text
    assert 'service="amavis"} 0' in metrics.text

    report(True, queue=450)
    assert {n.subject for n in events("service_up")} == {
        "[CarboRewind] Service Carbonio rétabli : amavis",
        "[CarboRewind] Service Carbonio rétabli : port:amavis",
    }
    (queue,) = events("mail_queue")
    assert "450" in queue.subject and "deferred: 450" in queue.body
    report(True, queue=500)
    assert len(events("mail_queue")) == 1  # at most every 3 hours
    r = api.put(
        "/api/monitoring/settings",
        json={"service_down_reports": 0},
        headers=h,
    )
    assert r.status_code == 422


def test_agent_settings_from_the_interface(api: TestClient, engine: Engine) -> None:
    h = login(api, engine)
    agent = enroll(api, h)
    with Session(engine) as s, s.begin():
        s.execute(delete(AppSetting).where(AppSetting.key == "agent_tuning"))
    assert agent.api.get("/api/agent/config", headers=agent.headers).status_code == 204
    current = api.get("/api/settings/agent", headers=h).json()
    assert current["revision"] == 0 and current["max_concurrent_accounts"] == 2
    body = {
        "max_concurrent_accounts": 3,
        "max_bandwidth_mbps": 200,
        "max_load_per_cpu": 4,
        "min_free_gb_local": 10,
        "min_free_percent_local": 10,
        "min_free_gb_network": 1,
        "service_monitor": True,
        "service_ports": {"mta": 25, "amavis": 10024},
        "mail_monitor": False,
    }
    for bad in (
        {"max_concurrent_accounts": 0},
        {"max_concurrent_accounts": 9},
        {"service_ports": {"bad name": 25}},
        {"service_ports": {"mta": 70000}},
        {"min_free_percent_local": 95},
    ):
        assert api.put("/api/settings/agent", json={**body, **bad}, headers=h).status_code == 422
    r = api.put("/api/settings/agent", json=body, headers=h)
    assert r.status_code == 200 and r.json()["revision"] == 1
    cfg = agent.api.get("/api/agent/config", headers=agent.headers).json()
    assert cfg["revision"] == 1 and cfg["max_concurrent_accounts"] == 3
    assert cfg["mail_monitor"] is False and cfg["service_ports"] == {"mta": 25, "amavis": 10024}
    listed = api.get("/api/settings/agent", headers=h).json()
    applied = [a["applied"] for a in listed["agents"] if a["applied"]]
    assert any(x["revision"] == 1 for x in applied)
    viewer = login(api, engine, Role.DOMAIN_ADMIN)
    assert api.get("/api/settings/agent", headers=viewer).status_code == 403


def test_postfix_checks_alert_with_readable_names(api: TestClient, engine: Engine) -> None:
    h = login(api, engine)
    agent = enroll(api, h)

    def report(ok: bool) -> None:
        body = {
            "services": [
                {
                    "name": "mta:antivirus",
                    "ok": ok,
                    "source": "mta",
                    **({} if ok else {"detail": "status=deferred (mail transport unavailable)"}),
                },
                {"name": "guard:master.cf", "ok": True, "source": "guard", "detail": "amavis: 3"},
            ],
        }
        assert agent.post("/services", body).status_code == 200

    def subjects(event: str) -> set[str]:
        with Session(engine) as s:
            return {
                n.subject
                for n in s.scalars(select(Notification).where(Notification.event == event))
                if "Postfix" in n.subject
            }

    report(True)
    report(False)
    report(False)
    assert subjects("service_down") == {
        "[CarboRewind] Service Carbonio arrêté : "
        "circuit antivirus de Postfix (envoi bloqué : vérifier master.cf)"
    }
    ov = api.get("/api/monitoring/overview", headers=h).json()
    mine = next(v for v in ov["services"] if v["down"] == ["mta:antivirus"])
    first = mine["services"][0]
    assert first["source"] == "mta" and "transport unavailable" in first["detail"]
    report(True)
    assert len(subjects("service_up")) == 1
    bad = {"services": [{"name": "x", "ok": True, "source": "shell"}]}
    assert agent.post("/services", bad).status_code == 422


def test_saved_agent_settings_gain_the_postfix_ports(api: TestClient, engine: Engine) -> None:
    h = login(api, engine)
    agent = enroll(api, h)
    with Session(engine) as s, s.begin():
        s.execute(delete(AppSetting).where(AppSetting.key == "agent_tuning"))
        s.add(
            AppSetting(
                key="agent_tuning",
                value={"revision": 4, "service_ports": {"mta": 25, "ldap": 389}},
            )
        )
    cfg = agent.api.get("/api/agent/config", headers=agent.headers).json()
    assert cfg["service_ports"] == {
        "submission": 587,
        "smtps": 465,
        "amavis_return": 10025,
        "mta": 25,
        "ldap": 389,
    }
    shown = api.get("/api/settings/agent", headers=h).json()
    assert "ports_defaults" not in shown and shown["service_ports"]["amavis_return"] == 10025
    body = {k: v for k, v in shown.items() if k not in ("revision", "agents")}
    body["service_ports"] = {"mta": 25}  # the administrator removes them: they stay removed
    assert api.put("/api/settings/agent", json=body, headers=h).status_code == 200
    cfg = agent.api.get("/api/agent/config", headers=agent.headers).json()
    assert cfg["service_ports"] == {"mta": 25}


def test_mail_queue_messages_and_actions(api: TestClient, engine: Engine) -> None:
    from carborewind_api.db.models import AuditEvent

    h = login(api, engine)
    agent = enroll(api, h)
    items = [
        {
            "id": f"6AE74141{i}C3",
            "from": "zextras@lnhc.dz",
            "to": "a@example.com",
            "time": 1_800_000_000,
            "reason": "mail transport unavailable",
            "origip": "127.0.0.1",
        }
        for i in range(3)
    ]

    def report() -> dict[str, Any]:
        r = agent.post(
            "/services",
            {
                "services": [{"name": "mta", "ok": True, "source": "carbonio"}],
                "queue": {"deferred": 3, "active": 0},
                "queue_items": [
                    {
                        "server": "mail.example.test",
                        "queue": "deferred",
                        "total": 3,
                        "items": items,
                        "reasons": [{"reason": "mail transport unavailable", "count": 3}],
                    }
                ],
            },
        )
        assert r.status_code == 200, r.text
        return r.json()

    assert report() == {"queue_actions": []}
    (mine,) = [q for q in api.get("/api/monitoring/queue", headers=h).json() if q["details"]]
    detail = mine["details"][0]
    assert detail["total"] == 3 and detail["items"][0]["from"] == "zextras@lnhc.dz"
    agent_id = mine["agent_id"]

    url = "/api/monitoring/queue/actions"
    first = items[0]["id"]
    for bad in (
        {"agent_id": agent_id, "op": "delete", "queue": "deferred", "ids": [first]},  # no confirm
        {"agent_id": agent_id, "op": "requeue", "queue": "deferred", "ids": []},
        {"agent_id": agent_id, "op": "requeue", "queue": "deferred", "ids": ["x; rm -rf"]},
        {"agent_id": agent_id, "op": "shell", "queue": "deferred", "ids": [first]},
    ):
        assert api.post(url, json=bad, headers=h).status_code == 422, bad
    body = {"agent_id": agent_id, "op": "delete", "queue": "deferred", "ids": [first], "confirm": 1}
    r = api.post(url, json=body, headers=h)
    assert r.status_code == 202, r.text
    assert api.post(url, json={"agent_id": agent_id, "op": "flush"}, headers=h).status_code == 202
    actions = report()["queue_actions"]
    assert [a["op"] for a in actions] == ["delete", "flush"]
    assert actions[0]["ids"] == [first] and actions[0]["queue"] == "deferred"
    assert report()["queue_actions"] == []  # handed out once (again only after 5 minutes)
    shown = next(
        q for q in api.get("/api/monitoring/queue", headers=h).json() if q["agent_id"] == agent_id
    )
    assert len(shown["pending"]) == 2

    assert agent.post(f"/queue-actions/{actions[0]['id']}/done", {"error": None}).status_code == 204
    r = agent.post(f"/queue-actions/{actions[1]['id']}/done", {"error": "queue busy"})
    assert r.status_code == 204
    shown = next(
        q for q in api.get("/api/monitoring/queue", headers=h).json() if q["agent_id"] == agent_id
    )
    assert shown["pending"] == []
    assert [(x["op"], x["error"]) for x in shown["results"]] == [
        ("flush", "queue busy"),
        ("delete", None),
    ]
    with Session(engine) as s:
        names = set(
            s.scalars(select(AuditEvent.action).where(AuditEvent.action.like("mail_queue%")))
        )
    assert {"mail_queue.delete", "mail_queue.flush", "mail_queue.delete.done"} <= names
    viewer = login(api, engine, Role.DOMAIN_ADMIN)
    assert api.post(url, json=body, headers=viewer).status_code == 403
    assert api.get("/api/monitoring/queue", headers=viewer).status_code == 403
