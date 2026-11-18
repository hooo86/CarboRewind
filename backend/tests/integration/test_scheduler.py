"""Policies API, scheduler ticks (backups, retention, purge planning) against PostgreSQL."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from carborewind_api import cli
from carborewind_api.config import Settings
from carborewind_api.db.models import (
    Account,
    AccountStatus,
    AuditEvent,
    AuthSource,
    BackupType,
    Blob,
    Domain,
    FolderVersion,
    ItemKind,
    ItemVersion,
    Job,
    JobKind,
    JobStatus,
    Policy,
    Role,
    RoleAssignment,
    Snapshot,
    SnapshotStatus,
    TargetType,
    User,
)
from carborewind_api.db.session import make_engine
from carborewind_api.main import create_app
from carborewind_api.scheduler import (
    TickReport,
    orphan_blobs_query,
    purge_plan,
    run_forever,
    tick,
)
from carborewind_api.security.passwords import hash_password

pytestmark = pytest.mark.integration

PASSWORD = "a-long-enough-password"
NOW = datetime(2026, 11, 18, 13, 0, tzinfo=UTC)


def run_tick(url: str, now: datetime = NOW, retention: bool = True) -> TickReport:
    async def go() -> TickReport:
        engine = make_engine(url)
        try:
            async with async_sessionmaker(engine)() as db, db.begin():
                return await tick(db, now, retention=retention)
        finally:
            await engine.dispose()

    return asyncio.run(go())


@pytest.fixture
def api(migrated: str) -> Iterator[TestClient]:
    settings = Settings(environment="test", database_url=migrated)  # type: ignore[arg-type]
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        yield client


def _login(api: TestClient, engine: Engine, role: Role, domain: uuid.UUID | None) -> dict[str, str]:
    name = f"p{uuid.uuid4().hex[:8]}"
    with Session(engine) as s, s.begin():
        s.add(
            User(
                username=name,
                auth_source=AuthSource.LOCAL,
                password_hash=hash_password(PASSWORD),
                roles=[RoleAssignment(role=role, domain_id=domain)],
            )
        )
    api.cookies.clear()
    r = api.post("/api/auth/login", json={"username": name, "password": PASSWORD})
    return {"X-CSRF-Token": r.json()["csrf_token"]}


def _domain(engine: Engine, *statuses: AccountStatus) -> tuple[uuid.UUID, list[uuid.UUID]]:
    with Session(engine) as s, s.begin():
        d = Domain(name=f"s{uuid.uuid4().hex[:8]}.test")
        s.add(d)
        s.flush()
        accounts = [
            Account(
                domain_id=d.id,
                email=f"u{n}@{d.name}",
                carbonio_id=uuid.uuid4().hex,
                status=st,
            )
            for n, st in enumerate(statuses)
        ]
        s.add_all(accounts)
        s.flush()
        return d.id, [a.id for a in accounts]


def _policy(engine: Engine, **kw: object) -> uuid.UUID:
    with Session(engine) as s, s.begin():
        p = Policy(
            name=f"pol-{uuid.uuid4().hex[:8]}",
            backup_type=kw.pop("backup_type", BackupType.INCREMENTAL),
            schedule=kw.pop("schedule", "0 1 * * *"),
            timezone=kw.pop("timezone", "Africa/Algiers"),
            **kw,
        )
        s.add(p)
        s.flush()
        return p.id


# ------------------------------------------------------------------ policies API


def test_policy_crud_validation_and_scope(api: TestClient, engine: Engine) -> None:
    d1, (a1,) = _domain(engine, AccountStatus.ACTIVE)
    d2, _ = _domain(engine, AccountStatus.ACTIVE)
    admin = _login(api, engine, Role.SUPER_ADMIN, None)
    body = {
        "name": f"nightly-{uuid.uuid4().hex[:6]}",
        "target_type": "domain",
        "target_id": str(d1),
        "schedule": "0  1 * * *",
        "timezone": "Africa/Algiers",
        "retention": {"daily": 14, "weekly": 8},
    }
    r = api.post("/api/policies", json=body, headers=admin)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["schedule"] == "0 1 * * *"
    assert created["target_name"].startswith("s")
    assert created["retention"] == {"last": 0, "daily": 14, "weekly": 8, "monthly": 0}
    assert created["next_run_at"] is not None and created["last_run_at"] is not None
    assert api.post("/api/policies", json=body, headers=admin).status_code == 409
    for bad in (
        {"schedule": "61 * * * *"},
        {"timezone": "Mars/Base"},
        {"retention": {"daily": -1}},
        {"target_type": "global", "target_id": str(d1)},
        {"target_type": "account", "target_id": None},
    ):
        assert api.post("/api/policies", json={**body, **bad}, headers=admin).status_code == 422

    # Patch, disable (no next run), re-enable.
    pid = created["id"]
    r = api.patch(f"/api/policies/{pid}", json={"enabled": False, "priority": 10}, headers=admin)
    assert r.json()["enabled"] is False and r.json()["next_run_at"] is None
    assert (
        api.patch(f"/api/policies/{pid}", json={"schedule": "x"}, headers=admin).status_code == 422
    )
    r = api.patch(
        f"/api/policies/{pid}", json={"enabled": True, "schedule": "30 2 * * 0"}, headers=admin
    )
    assert r.json()["schedule"] == "30 2 * * 0"

    # A domain admin manages its own domain only, and no global policy.
    dadmin = _login(api, engine, Role.DOMAIN_ADMIN, d1)
    names = {p["name"] for p in api.get("/api/policies").json()}
    assert body["name"] in names
    own = {
        **body,
        "name": f"own-{uuid.uuid4().hex[:6]}",
        "target_type": "account",
        "target_id": str(a1),
    }
    assert api.post("/api/policies", json=own, headers=dadmin).status_code == 201
    other = {**body, "name": "x", "target_id": str(d2)}
    assert api.post("/api/policies", json=other, headers=dadmin).status_code == 403
    glob = {**body, "name": "g", "target_type": "global", "target_id": None}
    assert api.post("/api/policies", json=glob, headers=dadmin).status_code == 403
    unknown = {**body, "name": "u", "target_id": str(uuid.uuid4())}
    assert api.post("/api/policies", json=unknown, headers=dadmin).status_code == 403

    # Viewers of another domain do not see it; deletion is audited.
    _login(api, engine, Role.VIEWER, d2)
    assert body["name"] not in {p["name"] for p in api.get("/api/policies").json()}
    admin = _login(api, engine, Role.SUPER_ADMIN, None)
    assert api.delete(f"/api/policies/{pid}", headers=admin).status_code == 204
    assert api.delete(f"/api/policies/{pid}", headers=admin).status_code == 403
    with Session(engine) as s:
        actions = {
            e.action for e in s.scalars(select(AuditEvent).where(AuditEvent.target_id == pid))
        }
    assert {"policy.create", "policy.update", "policy.delete"} <= actions


# ------------------------------------------------------------------ scheduling


def test_tick_queues_due_backups_once(migrated: str, engine: Engine) -> None:
    domain, (a, b, deleted) = _domain(
        engine, AccountStatus.ACTIVE, AccountStatus.ACTIVE, AccountStatus.DELETED
    )
    pid = _policy(
        engine,
        target_type=TargetType.DOMAIN,
        target_id=domain,
        backup_type=BackupType.FULL,
        priority=70,
        last_run_at=NOW - timedelta(days=1),
    )
    # b already has a pending backup: not doubled.
    with Session(engine) as s, s.begin():
        s.add(Job(kind=JobKind.BACKUP, status=JobStatus.QUEUED, account_id=b))
    report = run_tick(migrated, retention=False)
    assert report.jobs_queued >= 1
    with Session(engine) as s:
        jobs = s.scalars(select(Job).where(Job.policy_id == pid)).all()
        assert [j.account_id for j in jobs] == [a]
        assert jobs[0].params == {"backup_type": "full"} and jobs[0].priority == 70
        # 01:00 in Algiers is 00:00 UTC.
        assert jobs[0].idempotency_key == f"policy:{pid}:20261118T0000:{a}"
        policy = s.get(Policy, pid)
        assert policy is not None and policy.last_run_at == datetime(2026, 11, 18, tzinfo=UTC)
    # Same tick again, and a tick before the next night: nothing new.
    run_tick(migrated, retention=False)
    run_tick(migrated, NOW + timedelta(hours=10), retention=False)
    with Session(engine) as s:
        assert len(s.scalars(select(Job).where(Job.policy_id == pid)).all()) == 1
        assert deleted not in {j.account_id for j in s.scalars(select(Job))}


def test_broken_schedule_is_reported_not_fatal(migrated: str, engine: Engine) -> None:
    pid = _policy(
        engine,
        target_type=TargetType.GLOBAL,
        schedule="not a cron",
        last_run_at=NOW - timedelta(days=1),
    )
    try:
        report = run_tick(migrated, retention=False)
        assert any("5 fields" in e for e in report.errors)
    finally:
        with Session(engine) as s, s.begin():
            s.execute(update(Policy).where(Policy.id == pid).values(enabled=False))


def _snapshots(engine: Engine, account: uuid.UUID, days: int) -> list[uuid.UUID]:
    """One snapshot per day; item 1 changes every day (one blob per version)."""
    ids = []
    with Session(engine) as s, s.begin():
        for seq in range(1, days + 1):
            snap = Snapshot(
                account_id=account,
                seq=seq,
                backup_type=BackupType.FULL if seq == 1 else BackupType.INCREMENTAL,
                status=SnapshotStatus.COMPLETE,
                taken_at=NOW - timedelta(days=days - seq),
                manifest_ref=f"m/{seq}",
            )
            s.add(snap)
            blob = uuid.uuid4().hex + uuid.uuid4().hex
            s.add(Blob(id=blob, size=10, stored_size=10))
            s.flush()
            s.execute(
                update(ItemVersion)
                .where(ItemVersion.account_id == account, ItemVersion.to_seq.is_(None))
                .values(to_seq=seq)
            )
            s.add(
                ItemVersion(
                    account_id=account,
                    carbonio_item_id=1,
                    carbonio_folder_id=2,
                    kind=ItemKind.MESSAGE,
                    content_blob_id=blob,
                    size=10,
                    from_seq=seq,
                )
            )
            ids.append(snap.id)
        s.add(
            FolderVersion(
                account_id=account,
                carbonio_folder_id=2,
                path="/Inbox",
                view=ItemKind.MESSAGE,
                from_seq=1,
            )
        )
    return ids


def test_retention_expires_prunes_and_plans_purge(migrated: str, engine: Engine) -> None:
    domain, (acc, keepall) = _domain(engine, AccountStatus.ACTIVE, AccountStatus.ACTIVE)
    snaps = _snapshots(engine, acc, 6)
    _snapshots(engine, keepall, 3)
    _policy(
        engine,
        target_type=TargetType.ACCOUNT,
        target_id=acc,
        retention={"last": 2},
        last_run_at=NOW,
    )
    # A second, more generous policy on the same account wins.
    _policy(
        engine,
        target_type=TargetType.DOMAIN,
        target_id=domain,
        retention={"last": 3},
        last_run_at=NOW,
    )
    # An unlimited policy keeps every snapshot of its account.
    _policy(engine, target_type=TargetType.ACCOUNT, target_id=keepall, last_run_at=NOW)
    # A pending restore protects the snapshot it reads. Purge jobs left by other tests
    # sharing this database would hide ours.
    with Session(engine) as s, s.begin():
        s.execute(
            update(Job)
            .where(Job.kind == JobKind.PURGE, Job.status.in_(["queued", "running"]))
            .values(status=JobStatus.FAILED, agent_id=None, lease_expires_at=None)
        )
        s.add(
            Job(
                kind=JobKind.RESTORE,
                status=JobStatus.QUEUED,
                account_id=acc,
                params={"snapshot_id": str(snaps[0])},
            )
        )
    report = run_tick(migrated)
    assert report.snapshots_expired >= 2
    assert report.purge_job is not None
    with Session(engine) as s:
        status = {
            x.seq: x.status for x in s.scalars(select(Snapshot).where(Snapshot.account_id == acc))
        }
        assert status == {
            1: "complete",  # protected by the pending restore
            2: "expired",
            3: "expired",
            4: "complete",
            5: "complete",
            6: "complete",
        }
        assert all(
            x.status == "complete"
            for x in s.scalars(select(Snapshot).where(Snapshot.account_id == keepall))
        )
        # Versions only valid for seq 2 and 3 are gone; the others remain.
        froms = sorted(
            v.from_seq for v in s.scalars(select(ItemVersion).where(ItemVersion.account_id == acc))
        )
        assert froms == [1, 4, 5, 6]
        assert s.scalar(select(FolderVersion).where(FolderVersion.account_id == acc))
        pending_purge = s.scalars(
            select(Job).where(Job.kind == JobKind.PURGE, Job.status == JobStatus.QUEUED)
        ).all()
        assert len(pending_purge) == 1
    # Only one purge job is ever pending.
    run_tick(migrated)
    with Session(engine) as s:
        assert (
            len(
                s.scalars(
                    select(Job).where(Job.kind == JobKind.PURGE, Job.status == JobStatus.QUEUED)
                ).all()
            )
            == 1
        )

    async def plan() -> dict[str, object]:
        eng = make_engine(migrated)
        try:
            async with async_sessionmaker(eng)() as db:
                return await purge_plan(db)
        finally:
            await eng.dispose()

    # Purge plan: manifests of the expired snapshots and their orphan blobs, except blobs
    # held by the checkpoint of an unfinished backup.
    with Session(engine) as s:
        orphan_blobs = set(s.scalars(orphan_blobs_query(1_000_000)))
        held = sorted(orphan_blobs)[0]
        s.add(
            Job(
                kind=JobKind.BACKUP,
                status=JobStatus.QUEUED,
                account_id=keepall,
                checkpoint={"done": {"9": {"blob": held, "meta_blob": None}}},
            )
        )
        s.commit()
    result = asyncio.run(plan())
    planned = {m["snapshot"] for m in result["manifests"]}  # type: ignore[union-attr,index]
    assert {str(snaps[1]), str(snaps[2])} <= planned
    assert set(result["blobs"]) == orphan_blobs - {held}  # type: ignore[arg-type]
    assert held not in result["blobs"]  # type: ignore[operator]


def test_cli_scheduler_once(
    migrated: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from carborewind_api.config import get_settings

    monkeypatch.setenv("CARBOREWIND_DATABASE_URL", migrated)
    get_settings.cache_clear()
    try:
        assert cli.main(["scheduler", "--once"]) in (0, 1)
        assert "job(s) queued" in capsys.readouterr().out
    finally:
        get_settings.cache_clear()


def test_run_forever_stops_and_survives_errors(migrated: str) -> None:
    async def go() -> None:
        engine = make_engine(migrated)
        factory = async_sessionmaker(engine)
        stop = asyncio.Event()
        calls = {"n": 0}

        class Flaky:
            def __call__(self) -> object:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("database restarting")
                if calls["n"] >= 2:
                    stop.set()
                return factory()

        try:
            await run_forever(Flaky(), interval=0.01, stop=stop)  # type: ignore[arg-type]
        finally:
            await engine.dispose()
        assert calls["n"] >= 2

    asyncio.run(go())
