"""Storage settings from the interface (NAS, copy 02), agent hand-off, replication,
manual backups (all accounts, policy run now)."""

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
    AppSetting,
    AuditEvent,
    Job,
    JobKind,
    JobStatus,
    Policy,
    Role,
)
from carborewind_api.main import create_app
from carborewind_api.pki import init_ca

from .test_migrations import enroll, login
from .test_scheduler import run_tick

pytestmark = pytest.mark.integration
KEY = "s" * 40
NAS = {"share": "//nas1.lnhc.dz/backups/carborewind", "username": "svc", "password": "p@ss"}
NAS2 = {"share": "//nas2.lnhc.dz/copie02", "username": "svc2", "password": "s3cret", "seal": True}


@pytest.fixture
def settings(migrated: str, tmp_path: Path) -> Settings:
    init_ca(tmp_path / "pki")
    return Settings(  # type: ignore[call-arg]
        environment="test",
        database_url=migrated,
        pki_dir=tmp_path / "pki",
        agent_proxy_ips=["testclient"],
        job_lease_seconds=60,
        secret_key=KEY,
    )


@pytest.fixture
def api(settings: Settings, engine: Engine) -> Iterator[TestClient]:
    with Session(engine) as s, s.begin():
        s.execute(delete(AppSetting))
        s.execute(
            update(Job)
            .where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
            .values(status=JobStatus.CANCELLED, lease_expires_at=None)
        )
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        yield client
    with Session(engine) as s, s.begin():
        s.execute(delete(AppSetting))


def test_storage_settings_round_trip(api: TestClient, engine: Engine) -> None:
    h = login(api, engine)
    agent = enroll(api, h)
    out = api.get("/api/settings/storage").json()
    assert out["revision"] == 0 and out["primary_kind"] is None and out["applied"]
    assert agent.api.get("/api/agent/storage", headers=agent.headers).status_code == 204
    body = {"primary": {"kind": "smb", "smb": NAS, "migrate": True}, "copy_02": NAS2}
    r = api.put("/api/settings/storage", json=body, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["revision"] == 1 and out["primary_kind"] == "smb" and not out["applied"]
    assert out["primary_smb"] == {
        "share": NAS["share"],
        "username": "svc",
        "domain": "",
        "seal": False,
        "password_set": True,
    }
    assert out["copy_02"]["seal"] is True and "p@ss" not in str(out) and "s3cret" not in str(out)
    with Session(engine) as s:
        stored = str(s.get(AppSetting, "storage").value)  # type: ignore[union-attr]
        assert "p@ss" not in stored and "s3cret" not in stored
        audit = " ".join(str(a.details) for a in s.scalars(select(AuditEvent)))
        assert "p@ss" not in audit
    # The agent gets the passwords.
    desired = agent.api.get("/api/agent/storage", headers=agent.headers).json()
    assert desired["primary"]["password"] == "p@ss" and desired["copy"]["password"] == "s3cret"
    assert (desired["revision"], desired["primary_revision"], desired["copy_revision"]) == (1, 1, 1)
    # Same settings without passwords: nothing changes; copy 02 only: only it is bumped.
    keep = {"primary": {"kind": "smb", "smb": {**NAS, "password": None}}, "copy_02": NAS2}
    keep["copy_02"] = {**NAS2, "password": None}
    assert api.put("/api/settings/storage", json=keep, headers=h).json()["revision"] == 1
    keep["copy_02"] = {**NAS2, "password": None, "share": "//nas2.lnhc.dz/autre"}
    assert api.put("/api/settings/storage", json=keep, headers=h).json()["revision"] == 2
    desired = agent.api.get("/api/agent/storage", headers=agent.headers).json()
    assert (desired["primary_revision"], desired["copy_revision"]) == (1, 2)
    assert desired["copy"]["password"] == "s3cret"  # kept
    # The agent reports the outcome.
    report = {
        "path": "/var/lib/carborewind/nas/repository",
        "kind": "network",
        "fstype": "cifs",
        "source": NAS["share"],
        "total_bytes": 1000,
        "free_bytes": 400,
        "replica": {"path": "/var/lib/carborewind/copy02/repository", "kind": "network"},
        "apply": {"revision": 2, "ok": True, "at": "now", "changed": ["copy"]},
    }
    assert agent.post("/storage-status", report).status_code == 204
    out = api.get("/api/settings/storage").json()
    assert out["applied"] and out["agents"][-1]["replica"]["kind"] == "network"
    dash = api.get("/api/dashboard").json()
    assert any(st.get("replica") for st in dash["storage"])
    failed = {**report, "apply": {"revision": 3, "ok": False, "error": "mount error(13)"}}
    assert agent.post("/storage-status", failed).status_code == 204
    with Session(engine) as s:
        events = [a.action for a in s.scalars(select(AuditEvent).order_by(AuditEvent.id))]
        assert events.count("storage.apply") == 2
    # Back to the local disk, without copy 02.
    body = {"primary": {"kind": "local", "migrate": True}, "copy_02": None}
    out = api.put("/api/settings/storage", json=body, headers=h).json()
    assert out["revision"] == 3 and out["primary_kind"] == "local" and out["copy_02"] is None
    # Validation.
    for bad in (
        {"primary": {"kind": "smb"}},
        {"primary": {"kind": "smb", "smb": {**NAS, "share": "nas/x"}}},
        {"primary": {"kind": "smb", "smb": {**NAS, "share": "//nas/../etc"}}},
        {"primary": {"kind": "smb", "smb": {**NAS, "username": "a,b"}}},
        {"primary": {"kind": "smb", "smb": {**NAS, "password": "a\nb"}}},
        {"primary": {"kind": "smb", "smb": NAS}, "copy_02": NAS},  # same share twice
        {"primary": {"kind": "local"}, "copy_02": {**NAS2, "password": None, "share": "//x/y"}},
        {"primary": {"kind": "nfs"}},
    ):
        r = api.put("/api/settings/storage", json=bad, headers=h)
        assert r.status_code == 422, (bad, r.text)
    viewer = login(api, engine, Role.DOMAIN_ADMIN)
    assert api.get("/api/settings/storage").status_code == 403
    assert api.put("/api/settings/storage", json=body, headers=viewer).status_code == 403


def test_secret_key_is_required(migrated: str, engine: Engine) -> None:
    settings = Settings(environment="test", database_url=migrated)  # type: ignore[arg-type]
    with TestClient(create_app(settings), base_url="https://testserver") as api:
        h = login(api, engine)
        body = {"primary": {"kind": "smb", "smb": NAS}}
        assert api.put("/api/settings/storage", json=body, headers=h).status_code == 409


def test_replication_jobs(api: TestClient, engine: Engine, migrated: str) -> None:
    h = login(api, engine)
    agent = enroll(api, h)
    assert api.post("/api/settings/storage/replicate", headers=h).status_code == 409
    body = {"primary": {"kind": "local"}, "copy_02": NAS2}
    api.put("/api/settings/storage", json=body, headers=h)
    job_id = api.post("/api/settings/storage/replicate", headers=h).json()["job_id"]
    # Asking again while it is pending returns the same job.
    assert api.post("/api/settings/storage/replicate", headers=h).json()["job_id"] == job_id
    claimed = agent.post("/jobs/claim").json()
    assert claimed["kind"] == "replicate" and claimed["job_id"] == job_id
    result = {"files": 10, "copied": 4, "copied_bytes": 4096, "deleted": 1, "error_count": 0}
    assert agent.post(f"/jobs/{job_id}/replicate-complete", result).status_code == 204
    assert agent.post(f"/jobs/{job_id}/replicate-complete", result).status_code == 204
    out = api.get("/api/settings/storage").json()
    assert out["last_replication"]["status"] == "succeeded"
    assert out["last_replication"]["progress"]["copied"] == 4
    # The scheduler refreshes copy 02 after a backup finished later than the last refresh.
    now = datetime.now(UTC)
    with Session(engine) as s, s.begin():
        s.add(
            Job(
                kind=JobKind.BACKUP,
                status=JobStatus.SUCCEEDED,
                started_at=now + timedelta(minutes=1),
                finished_at=now + timedelta(minutes=2),
            )
        )
    report = run_tick(migrated, now + timedelta(minutes=3), retention=False)
    assert report.replicate_job is not None
    assert run_tick(migrated, now + timedelta(minutes=4), retention=False).replicate_job is None
    # A failed refresh is not retried for an hour.
    claimed = agent.post("/jobs/claim").json()
    assert claimed["kind"] == "replicate"
    agent.post(f"/jobs/{claimed['job_id']}/fail", {"error": "not mounted", "retryable": False})
    with Session(engine) as s, s.begin():
        s.add(
            Job(
                kind=JobKind.BACKUP,
                status=JobStatus.SUCCEEDED,
                started_at=now + timedelta(minutes=5),
                finished_at=now + timedelta(minutes=6),
            )
        )
    assert run_tick(migrated, now + timedelta(minutes=7), retention=False).replicate_job is None
    late = now + timedelta(hours=2)
    assert run_tick(migrated, late, retention=False).replicate_job is not None
    # Errors while copying are reported.
    claimed = agent.post("/jobs/claim").json()
    bad = {**result, "errors": ["blobs/aa/aa: No space left"], "error_count": 1}
    agent.post(f"/jobs/{claimed['job_id']}/replicate-complete", bad)
    with Session(engine) as s:
        job = s.get(Job, uuid.UUID(claimed["job_id"]))
        assert job is not None and job.error == "1 file(s) not copied"
    # Without copy 02, nothing is scheduled.
    api.put("/api/settings/storage", json={"primary": {"kind": "local"}}, headers=h)
    assert run_tick(migrated, late + timedelta(hours=2), retention=False).replicate_job is None


def test_manual_backups(api: TestClient, engine: Engine) -> None:
    h = login(api, engine)
    agent = enroll(api, h)
    tag = uuid.uuid4().hex[:6]
    emails = [f"m{i}@man{tag}.test" for i in range(3)]
    accounts = [{"carbonio_id": uuid.uuid4().hex, "email": e, "status": "active"} for e in emails]
    agent.post("/inventory", {"accounts": accounts})
    domain = next(
        d["id"] for d in api.get("/api/domains").json()["items"] if d["name"] == f"man{tag}.test"
    )
    r = api.post("/api/backups/all", json={"backup_type": "full", "domain_id": domain}, headers=h)
    assert r.status_code == 202 and r.json() == {
        "queued": 3,
        "skipped_busy": 0,
        "skipped_deleted": 0,
    }
    again = api.post("/api/backups/all", json={"domain_id": domain}, headers=h).json()
    assert again["queued"] == 0 and again["skipped_busy"] == 3
    with Session(engine) as s, s.begin():
        s.execute(
            update(Job)
            .where(Job.kind == JobKind.BACKUP, Job.status == JobStatus.QUEUED)
            .values(status=JobStatus.CANCELLED)
        )
    # Run a policy now.
    policy = api.post(
        "/api/policies",
        json={
            "name": f"p-{tag}",
            "target_type": "domain",
            "target_id": domain,
            "schedule": "0 2 * * *",
            "backup_type": "incremental",
        },
        headers=h,
    ).json()
    with Session(engine) as s:
        before = s.get(Policy, uuid.UUID(policy["id"]))
        assert before is not None
        last_run = before.last_run_at
    r = api.post(f"/api/policies/{policy['id']}/run", json={}, headers=h)
    assert r.status_code == 202 and r.json()["queued"] == 3
    with Session(engine) as s:
        jobs = list(
            s.scalars(
                select(Job).where(
                    Job.policy_id == uuid.UUID(policy["id"]), Job.status == JobStatus.QUEUED
                )
            )
        )
        assert len(jobs) == 3 and jobs[0].params == {"backup_type": "incremental", "manual": True}
        stored = s.get(Policy, uuid.UUID(policy["id"]))
        assert stored is not None and stored.last_run_at == last_run  # schedule untouched
    assert api.post(f"/api/policies/{uuid.uuid4()}/run", json={}, headers=h).status_code == 403
    # Domain admins only on their domain; viewers not at all.
    other = login(api, engine, Role.VIEWER)
    assert api.post("/api/backups/all", json={}, headers=other).status_code == 403
    scoped = _domain_admin(api, engine, uuid.UUID(domain))
    r = api.post("/api/backups/all", json={"domain_id": str(uuid.uuid4())}, headers=scoped)
    assert r.status_code == 403
    r = api.post("/api/backups/all", json={"backup_type": "incremental"}, headers=scoped)
    assert r.json()["skipped_busy"] == 3  # its own domain only


def _domain_admin(api: TestClient, engine: Engine, domain: uuid.UUID) -> dict[str, str]:
    from carborewind_api.db.models import AuthSource, RoleAssignment, User
    from carborewind_api.security.passwords import hash_password

    name = f"d{uuid.uuid4().hex[:8]}"
    with Session(engine) as s, s.begin():
        s.add(
            User(
                username=name,
                auth_source=AuthSource.LOCAL,
                password_hash=hash_password("a-long-enough-password"),
                roles=[RoleAssignment(role=Role.DOMAIN_ADMIN, domain_id=domain)],
            )
        )
    api.cookies.clear()
    r = api.post("/api/auth/login", json={"username": name, "password": "a-long-enough-password"})
    return {"X-CSRF-Token": r.json()["csrf_token"]}


def test_profile_and_usage_from_inventory(api: TestClient, engine: Engine) -> None:
    h = login(api, engine)
    agent = enroll(api, h)
    tag = uuid.uuid4().hex[:6]
    email = f"prof@p{tag}.test"
    entry = {
        "carbonio_id": uuid.uuid4().hex,
        "email": email,
        "status": "active",
        "quota_bytes": 1000,
        "used_bytes": 900,
        "profile": {"title": "Chimiste", "department": "Labo", "phone": "021", "evil": "x"},
    }
    assert agent.post("/inventory", {"accounts": [entry]}).status_code == 200
    page = api.get("/api/accounts", params={"q": email}).json()
    (item,) = page["items"]
    assert item["profile"] == {"title": "Chimiste", "department": "Labo", "phone": "021"}
    assert (item["quota_bytes"], item["used_bytes"], item["usage_pct"]) == (1000, 900, 90.0)
    found = api.get(
        "/api/accounts", params={"f": ["department:contains:lab", f"domain:eq:p{tag}.test"]}
    )
    assert [i["email"] for i in found.json()["items"]] == [email]
    assert api.get("/api/accounts", params={"q": "chimiste"}).json()["total"] >= 1
    sort = api.get("/api/accounts", params={"sort": "title", "f": [f"domain:eq:p{tag}.test"]})
    assert sort.status_code == 200
    # An agent that does not know the usage keeps the last value; no profile keeps it too.
    del entry["used_bytes"], entry["profile"]
    agent.post("/inventory", {"accounts": [entry]})
    (item,) = api.get("/api/accounts", params={"q": email}).json()["items"]
    assert item["used_bytes"] == 900 and item["profile"]["title"] == "Chimiste"
