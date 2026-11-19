"""Automatic restore test: draw, restore into the test mailbox, judge, notify."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select, update
from sqlalchemy.orm import Session

from carborewind_api.db.models import (
    AppSetting,
    BackupType,
    Blob,
    ItemKind,
    ItemVersion,
    Job,
    JobStatus,
    Notification,
    Role,
    Snapshot,
    SnapshotStatus,
)

from .test_archives import _claim, world  # noqa: F401 - fixture
from .test_migrations import login
from .test_monitoring import api  # noqa: F401 - fixture
from .test_scheduler import run_tick

pytestmark = pytest.mark.integration


def _backup(engine: Engine, account_id: uuid.UUID, items: int) -> None:
    with Session(engine) as s, s.begin():
        snap = Snapshot(
            account_id=account_id,
            seq=1,
            backup_type=BackupType.FULL,
            status=SnapshotStatus.COMPLETE,
            taken_at=datetime.now(UTC),
            manifest_ref="manifests/x/1.json",
            item_count=items,
        )
        s.add(snap)
        for n in range(items):
            blob = f"{uuid.uuid4().hex}{uuid.uuid4().hex}"
            s.merge(Blob(id=blob, size=10, stored_size=10))
            s.flush()
            s.add(
                ItemVersion(
                    account_id=account_id,
                    carbonio_item_id=300 + n,
                    carbonio_folder_id=2,
                    kind=ItemKind.MESSAGE,
                    content_blob_id=blob,
                    size=10,
                    from_seq=1,
                )
            )


def _events(engine: Engine) -> list[Notification]:
    with Session(engine) as s:
        return list(
            s.scalars(select(Notification).where(Notification.event == "restore_test_failed"))
        )


def test_restore_tests(
    api: TestClient,  # noqa: F811
    engine: Engine,
    world: dict[str, Any],  # noqa: F811
    migrated: str,
) -> None:
    h = world["admin"]
    ids = world["ids"]
    with Session(engine) as s, s.begin():
        s.execute(delete(AppSetting).where(AppSetting.key == "restore_test"))
        s.execute(update(Snapshot).values(status=SnapshotStatus.EXPIRED))  # only ours count
    _backup(engine, ids["ana"], 30)
    # Not set up: no test mailbox.
    assert api.post("/api/monitoring/restore-tests/run", headers=h).status_code == 409
    bad = {"enabled": True, "target_account_id": None}
    assert api.put("/api/monitoring/restore-tests", json=bad, headers=h).status_code == 422
    assert (
        api.put("/api/monitoring/restore-tests", json={"schedule": "nope"}, headers=h).status_code
        == 422
    )
    r = api.put(
        "/api/monitoring/restore-tests",
        json={"enabled": True, "target_account_id": str(ids["ben"]), "messages": 5},
        headers=h,
    )
    assert r.status_code == 200 and r.json()["target_email"].startswith("ben@")

    r = api.post("/api/monitoring/restore-tests/run", headers=h)
    assert r.status_code == 202, r.text
    (run,) = r.json()["history"]
    assert run["source"].startswith("ana@") and run["target"].startswith("ben@")
    assert api.post("/api/monitoring/restore-tests/run", headers=h).status_code == 409  # busy
    claimed = _claim(world, engine, run["job_id"])
    p = claimed["params"]
    assert p["test"] and p["cleanup"] and p["mode"] == "new_folder"
    assert len(p["item_ids"]) == 5 and claimed["carbonio_id"] != p["source_carbonio_id"]
    ok = {
        "dry_run": False,
        "mode": "new_folder",
        "root": "/" + p["label"],
        "selected": 5,
        "restored": 5,
        "present": 0,
        "unsupported": 0,
        "cleaned": True,
    }
    assert world["agent"].post(f"/jobs/{run['job_id']}/restore-complete", ok).status_code == 204
    hist = api.get("/api/monitoring/restore-tests", headers=h).json()["history"]
    assert hist[0]["ok"] is True and hist[0]["cleaned"] is True
    assert _events(engine) == []

    # A partial restore is a failed test.
    r = api.post("/api/monitoring/restore-tests/run", headers=h)
    job = r.json()["history"][0]["job_id"]
    _claim(world, engine, job)
    bad_result = {**ok, "restored": 3, "failed": {"301": "mail.QUOTA_EXCEEDED"}}
    world["agent"].post(f"/jobs/{job}/restore-complete", bad_result)
    (note,) = _events(engine)
    assert "ana@" in note.subject and "3 message(s)" in note.body
    # A job that fails for good is a failed test too (no generic job alert).
    r = api.post("/api/monitoring/restore-tests/run", headers=h)
    job = r.json()["history"][0]["job_id"]
    _claim(world, engine, job)
    for _ in range(2):
        world["agent"].post(f"/jobs/{job}/fail", {"error": "boom", "retryable": False})
    assert len(_events(engine)) == 2

    # Scheduled: nothing on the first tick after enabling, then one per slot.
    with Session(engine) as s, s.begin():
        s.execute(
            update(Job)
            .where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
            .values(status=JobStatus.CANCELLED, lease_expires_at=None)
        )
    api.put(
        "/api/monitoring/restore-tests",
        json={"enabled": False, "target_account_id": str(ids["ben"])},
        headers=h,
    )
    api.put(
        "/api/monitoring/restore-tests",
        json={"enabled": True, "target_account_id": str(ids["ben"]), "schedule": "0 * * * *"},
        headers=h,
    )
    now = datetime.now(UTC)
    before = len(api.get("/api/monitoring/restore-tests", headers=h).json()["history"])
    run_tick(migrated, now)
    run_tick(migrated, now + timedelta(hours=1, minutes=5))
    after = api.get("/api/monitoring/restore-tests", headers=h).json()["history"]
    assert len(after) == before + 1 and after[0]["status"] == "queued"
    domain_admin = login(api, engine, Role.DOMAIN_ADMIN)
    assert api.get("/api/monitoring/restore-tests", headers=domain_admin).status_code == 403
