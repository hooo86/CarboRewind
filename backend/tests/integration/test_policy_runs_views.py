"""Full + incremental schedules, policy history and figures, account groups, saved views."""

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

from carborewind_api.config import Settings
from carborewind_api.db.models import (
    Account,
    AccountStatus,
    AuthSource,
    BackupType,
    Domain,
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
from carborewind_api.scheduler import TickReport, tick
from carborewind_api.security.passwords import hash_password

pytestmark = pytest.mark.integration

PASSWORD = "a-long-enough-password"
NOW = datetime(2026, 11, 18, 13, 0, tzinfo=UTC)  # a Wednesday


def run_tick(url: str, now: datetime) -> TickReport:
    async def go() -> TickReport:
        engine = make_engine(url)
        try:
            async with async_sessionmaker(engine)() as db, db.begin():
                return await tick(db, now, retention=False)
        finally:
            await engine.dispose()

    return asyncio.run(go())


@pytest.fixture
def api(migrated: str) -> Iterator[TestClient]:
    settings = Settings(environment="test", database_url=migrated)  # type: ignore[arg-type]
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        yield client


def _login(api: TestClient, engine: Engine, role: Role, domain: uuid.UUID | None = None) -> dict:
    name = f"v{uuid.uuid4().hex[:8]}"
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


def _domain(engine: Engine, *statuses: AccountStatus) -> tuple[uuid.UUID, str, list[uuid.UUID]]:
    with Session(engine) as s, s.begin():
        d = Domain(name=f"g{uuid.uuid4().hex[:8]}.test")
        s.add(d)
        s.flush()
        accounts = [
            Account(
                domain_id=d.id,
                email=f"u{n}@{d.name}",
                carbonio_id=uuid.uuid4().hex,
                status=st,
                used_bytes=1000,
            )
            for n, st in enumerate(statuses)
        ]
        s.add_all(accounts)
        s.flush()
        return d.id, d.name, [a.id for a in accounts]


def test_full_schedule_takes_precedence_then_incremental(migrated: str, engine: Engine) -> None:
    domain, _, (a,) = _domain(engine, AccountStatus.ACTIVE)
    with Session(engine) as s, s.begin():
        p = Policy(
            name=f"mix-{uuid.uuid4().hex[:6]}",
            target_type=TargetType.DOMAIN,
            target_id=domain,
            backup_type=BackupType.INCREMENTAL,
            schedule="0 1 * * *",  # every night
            full_schedule="0 1 * * 3",  # Wednesdays: full instead
            timezone="UTC",
            last_run_at=NOW - timedelta(days=1),
            full_last_run_at=NOW - timedelta(days=1),
        )
        s.add(p)
        s.flush()
        pid = p.id
    run_tick(migrated, NOW)
    with Session(engine) as s:
        jobs = s.scalars(select(Job).where(Job.policy_id == pid)).all()
        assert [(j.params["backup_type"], j.account_id) for j in jobs] == [("full", a)]
        assert jobs[0].idempotency_key == f"policy-full:{pid}:20261118T0100:{a}"
        policy = s.get(Policy, pid)
        assert policy is not None
        assert (
            policy.full_last_run_at == policy.last_run_at == datetime(2026, 11, 18, 1, tzinfo=UTC)
        )
    with Session(engine) as s, s.begin():
        s.execute(update(Job).where(Job.policy_id == pid).values(status=JobStatus.SUCCEEDED))
    run_tick(migrated, NOW + timedelta(days=1))  # Thursday: incremental only
    with Session(engine) as s:
        types = sorted(
            j.params["backup_type"] for j in s.scalars(select(Job).where(Job.policy_id == pid))
        )
        assert types == ["full", "incremental"]


def test_policy_history_and_figures(api: TestClient, engine: Engine) -> None:
    h = _login(api, engine, Role.SUPER_ADMIN)
    domain, _, (a, b, c) = _domain(
        engine, AccountStatus.ACTIVE, AccountStatus.ACTIVE, AccountStatus.ACTIVE
    )
    r = api.post(
        "/api/policies",
        json={
            "name": f"hist-{uuid.uuid4().hex[:6]}",
            "target_type": "domain",
            "target_id": str(domain),
            "schedule": "0 1 * * *",
            "full_schedule": "0 2 * * 0",
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["full_schedule"] == "0 2 * * 0" and r.json()["next_full_run_at"]
    assert (
        api.post(
            "/api/policies",
            json={
                "name": "bad",
                "target_type": "global",
                "schedule": "0 1 * * *",
                "full_schedule": "nope",
            },
            headers=h,
        ).status_code
        == 422
    )
    at = datetime(2026, 11, 17, 1, tzinfo=UTC)
    with Session(engine) as s, s.begin():
        for account, status, read in (
            (a, JobStatus.SUCCEEDED, 100 + 5 * 1024**3),
            (b, JobStatus.SUCCEEDED, 50),
            (c, JobStatus.FAILED, 0),
        ):
            s.add(
                Job(
                    kind=JobKind.BACKUP,
                    status=status,
                    account_id=account,
                    policy_id=uuid.UUID(pid),
                    params={"backup_type": "incremental"},
                    progress={"read_bytes": read, "new_bytes": read // 2, "size_bytes": 1000},
                    created_at=at,
                    finished_at=at + timedelta(minutes=5),
                )
            )
        s.add(
            Snapshot(
                account_id=a,
                seq=1,
                backup_type=BackupType.FULL,
                status=SnapshotStatus.COMPLETE,
                taken_at=at,
                manifest_ref="m",
                size_bytes=4000,
                new_bytes=3000,
            )
        )
    runs = api.get(f"/api/policies/{pid}/runs", headers=h).json()
    assert runs["total"] == 1
    run = runs["items"][0]
    assert run["status"] == "partial" and run["trigger"] == "schedule"
    assert (run["accounts"], run["succeeded"], run["failed"]) == (3, 2, 1)
    assert run["read_bytes"] == 150 + 5 * 1024**3  # beyond 32-bit integers
    assert run["new_bytes"] == 75 + 5 * 1024**3 // 2
    policy = next(p for p in api.get("/api/policies", headers=h).json() if p["id"] == pid)
    assert policy["account_count"] == 3
    assert policy["protected_bytes"] == 4000 and policy["stored_bytes"] == 3000
    assert policy["last_run"]["status"] == "partial"
    # "" removes the full schedule.
    out = api.patch(f"/api/policies/{pid}", json={"full_schedule": ""}, headers=h).json()
    assert out["full_schedule"] is None and out["next_full_run_at"] is None


def test_account_groups(api: TestClient, engine: Engine) -> None:
    h = _login(api, engine, Role.SUPER_ADMIN)
    _, name, _ = _domain(engine, AccountStatus.ACTIVE, AccountStatus.ACTIVE, AccountStatus.LOCKED)
    groups = api.get(
        "/api/accounts/groups",
        params={"group_by": "status", "f": f"domain:eq:{name}"},
        headers=h,
    ).json()
    assert [(g["value"], g["count"], g["used_bytes"]) for g in groups] == [
        ("active", 2, 2000),
        ("locked", 1, 1000),
    ]
    never = api.get(
        "/api/accounts/groups",
        params={"group_by": "last_backup_at", "f": f"domain:eq:{name}"},
        headers=h,
    ).json()
    assert never == [{"value": None, "count": 3, "used_bytes": 3000, "stored_bytes": 0}]
    assert (
        api.get("/api/accounts/groups", params={"group_by": "email"}, headers=h).status_code == 422
    )
    assert "status" in api.get("/api/accounts/facets", headers=h).json()["groupable"]


def test_saved_views(api: TestClient, engine: Engine) -> None:
    owner = _login(api, engine, Role.SUPER_ADMIN)
    mine = api.post(
        "/api/views",
        json={"page": "accounts", "name": " Sans  sauvegarde ", "query": "?f=has_policy:is:false"},
        headers=owner,
    )
    assert mine.status_code == 201, mine.text
    assert (
        mine.json()["name"] == "Sans sauvegarde" and mine.json()["query"] == "f=has_policy:is:false"
    )
    shared = api.post(
        "/api/views",
        json={"page": "accounts", "name": "Par domaine", "query": "group=domain", "shared": True},
        headers=owner,
    ).json()
    assert (
        api.post(
            "/api/views",
            json={"page": "accounts", "name": "Par domaine", "query": "x"},
            headers=owner,
        ).status_code
        == 409
    )
    assert (
        api.post(
            "/api/views", json={"page": "nope", "name": "x", "query": ""}, headers=owner
        ).status_code
        == 422
    )
    names = [v["name"] for v in api.get("/api/views?page=accounts", headers=owner).json()]
    assert {"Sans sauvegarde", "Par domaine"} <= set(names)

    other = _login(api, engine, Role.VIEWER)
    seen = api.get("/api/views?page=accounts", headers=other).json()
    ids = {v["id"] for v in seen}
    assert shared["id"] in ids and mine.json()["id"] not in ids
    assert all(not v["mine"] for v in seen if v["id"] == shared["id"])
    assert api.delete(f"/api/views/{shared['id']}", headers=other).status_code == 404
