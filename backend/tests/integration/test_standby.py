"""Standby server: roles, replays queued by the scheduler, results, promotion."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from carborewind_api.db.models import (
    Account,
    Agent,
    AgentStatus,
    BackupType,
    Job,
    JobKind,
    JobStatus,
    Snapshot,
    SnapshotStatus,
    StandbyState,
)

from .test_agent_protocol import AgentHandle, _csr
from .test_mail_traffic import accounts, api  # noqa: F401 - fixture
from .test_migrations import enroll, login
from .test_scheduler import run_tick

pytestmark = pytest.mark.integration


def enroll_standby(client: TestClient, admin: dict[str, str]) -> tuple[AgentHandle, str]:
    name = f"secours-{uuid.uuid4().hex[:6]}"
    r = client.post(
        "/api/agents",
        json={"name": name, "hostname": "mail.example.test", "role": "standby"},
        headers=admin,
    )
    assert r.status_code == 201 and r.json()["role"] == "standby", r.text
    r = client.post(
        "/api/agent/enroll", json={"token": r.json()["enrollment_token"], "csr_pem": _csr()}
    )
    body = r.json()
    return AgentHandle(client, body["agent_id"], body["certificate_pem"]), name


def claim_all(agent: AgentHandle) -> list[dict[str, Any]]:
    """Claim until nothing is left; jobs of other tests are failed out of the way."""
    jobs = []
    for _ in range(200):
        r = agent.post("/jobs/claim")
        if r.status_code == 204:
            break
        jobs.append(r.json())
    return jobs


def test_standby_replication_and_promotion(
    api: TestClient,  # noqa: F811 - fixture
    engine: Engine,
    migrated: str,
) -> None:
    h = login(api, engine)
    primary = enroll(api, h)
    e = accounts(primary, uuid.uuid4().hex[:6])
    with Session(engine) as s, s.begin():
        rows = {a.email: a for a in s.scalars(select(Account).where(Account.email.in_(e.values())))}
        ids = {k: rows[v].id for k, v in e.items()}
        carbonio = {k: rows[v].carbonio_id for k, v in e.items()}
        snaps = {}
        for key in ("ana", "ben"):
            snap = Snapshot(
                account_id=ids[key],
                seq=1,
                backup_type=BackupType.FULL,
                status=SnapshotStatus.COMPLETE,
                taken_at=datetime.now(UTC),
                manifest_ref="m",
            )
            s.add(snap)
            s.flush()
            snaps[key] = snap.id
        rows[e["ana"]].display_name = "Ana Lopez"
    standby: AgentHandle | None = None
    try:
        # Not enabled without a standby agent... then with one.
        assert api.put(
            "/api/standby/settings", json={"enabled": True, "interval_minutes": 15}, headers=h
        ).status_code in (200, 409)
        standby, name = enroll_standby(api, h)
        r = api.put(
            "/api/standby/settings", json={"enabled": True, "interval_minutes": 15}, headers=h
        )
        assert r.status_code == 200, r.text
        assert (
            api.put(
                "/api/standby/settings", json={"enabled": True, "interval_minutes": 2}, headers=h
            ).status_code
            == 422
        )

        # The clone's inventory is not merged into the accounts.
        before = api.get("/api/standby", headers=h).json()["accounts"]
        out = standby.post(
            "/inventory",
            {
                "accounts": [
                    {"carbonio_id": "clone-only", "email": "x@clone.test", "status": "active"}
                ]
            },
        ).json()
        assert out["accounts_created"] == 0
        assert api.get("/api/standby", headers=h).json()["accounts"] == before
        assert standby.post("/mail-stats", {"hours": []}).json() == {
            "actions": [],
            "ip_actions": [],
        }

        run_tick(migrated, datetime.now(UTC), retention=False)
        # The primary never takes the replays; the standby takes only them.
        for job in claim_all(primary):
            assert job["kind"] != "standby_sync"
            primary.post(f"/jobs/{job['job_id']}/fail", {"error": "x", "retryable": False})
        jobs = claim_all(standby)
        assert jobs and all(j["kind"] == "standby_sync" for j in jobs)
        mine = {
            j["account_id"]: j for j in jobs if j["account_id"] in {str(i) for i in ids.values()}
        }
        ana, ben = mine[str(ids["ana"])], mine[str(ids["ben"])]
        assert str(ids["news"]) not in mine  # no snapshot yet
        assert ana["carbonio_id"] == carbonio["ana"]
        assert ana["params"] == {
            "snapshot_id": str(snaps["ana"]),
            "attrs": {"displayName": "Ana Lopez"},
        }
        for job in jobs:
            if job not in (ana, ben):
                standby.post(f"/jobs/{job['job_id']}/fail", {"error": "x", "retryable": False})

        done = {"snapshot_id": str(snaps["ana"]), "added": 3, "items": 3, "failed_count": 0}
        wrong = {**done, "snapshot_id": str(uuid.uuid4())}
        assert standby.post(f"/jobs/{ana['job_id']}/standby-complete", wrong).status_code == 422
        assert primary.post(f"/jobs/{ana['job_id']}/standby-complete", done).status_code == 404
        assert standby.post(f"/jobs/{ana['job_id']}/standby-complete", done).status_code == 204
        assert standby.post(f"/jobs/{ana['job_id']}/standby-complete", done).status_code == 204
        standby.post(
            f"/jobs/{ben['job_id']}/fail", {"error": "mail.QUOTA_EXCEEDED", "retryable": False}
        )
        with Session(engine) as s:
            st = s.get(StandbyState, ids["ana"])
            assert st is not None and st.status == "ok" and st.snapshot_id == snaps["ana"]
            assert st.stats["added"] == 3
            bad = s.get(StandbyState, ids["ben"])
            assert bad is not None and bad.status == "failed" and "QUOTA" in (bad.error or "")
        view = api.get("/api/standby", headers=h).json()
        assert view["config"] == {"enabled": True, "interval_minutes": 15}
        assert any(p["email"] == e["ben"] and p["status"] == "failed" for p in view["problems"])
        assert any(a["name"] == name and a["role"] == "standby" for a in view["agents"])

        # Up to date: no new replay; ben failed a moment ago: waits for the interval...
        run_tick(migrated, datetime.now(UTC), retention=False)
        with Session(engine) as s:
            queued = set(
                s.scalars(
                    select(Job.account_id).where(
                        Job.kind == JobKind.STANDBY_SYNC, Job.status == JobStatus.QUEUED
                    )
                )
            )
        assert ids["ana"] not in queued and ids["ben"] not in queued
        # ... unless asked now.
        assert api.post("/api/standby/sync-now", headers=h).json()["queued"] >= 1
        with Session(engine) as s:
            assert s.scalar(
                select(Job.id).where(
                    Job.kind == JobKind.STANDBY_SYNC,
                    Job.status == JobStatus.QUEUED,
                    Job.account_id == ids["ben"],
                )
            )

        # Disaster: the standby becomes the primary.
        url = "/api/standby/promote"
        assert (
            api.post(url, json={"agent_id": standby.id, "confirm": "nope"}, headers=h).status_code
            == 422
        )
        assert (
            api.post(url, json={"agent_id": primary.id, "confirm": "x"}, headers=h).status_code
            == 404
        )
        r = api.post(url, json={"agent_id": standby.id, "confirm": name}, headers=h)
        assert r.status_code == 200, r.text
        with Session(engine) as s:
            assert s.get(Agent, uuid.UUID(standby.id)).role == "primary"  # type: ignore[union-attr]
            assert s.get(Agent, uuid.UUID(primary.id)).role == "standby"  # type: ignore[union-attr]
            assert not s.scalar(
                select(Job.id).where(
                    Job.kind == JobKind.STANDBY_SYNC, Job.status == JobStatus.QUEUED
                )
            )
        assert api.get("/api/standby", headers=h).json()["config"]["enabled"] is False
        r = api.put(f"/api/standby/agents/{primary.id}/role", json={"role": "primary"}, headers=h)
        assert r.json()["role"] == "primary"
    finally:
        with Session(engine) as s, s.begin():  # other tests expect primary agents
            s.execute(update(Agent).values(role="primary"))
            s.execute(  # nor an agent that never reports its storage
                update(Agent)
                .where(
                    Agent.id.in_(
                        [uuid.UUID(primary.id), *([uuid.UUID(standby.id)] if standby else [])]
                    )
                )
                .values(status=AgentStatus.REVOKED)
            )
            s.execute(
                update(Job)
                .where(
                    Job.kind == JobKind.STANDBY_SYNC,
                    Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                )
                .values(status=JobStatus.CANCELLED, lease_expires_at=None)
            )
