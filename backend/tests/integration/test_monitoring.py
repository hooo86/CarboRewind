"""Monitoring: storage trend/forecast, accounts at risk, copy 2 lag, integrity checks,
Prometheus metrics, safety of automatic locks."""

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
    AppSetting,
    BackupType,
    Job,
    JobKind,
    JobStatus,
    MailAlert,
    MailStat,
    Notification,
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
