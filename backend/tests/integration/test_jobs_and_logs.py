"""Jobs list (filters, sort, pages, throughput) and logs read from the interface."""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session

from carborewind_api.db.models import Job, JobKind, JobStatus, Role

from .test_archives import _claim, world  # noqa: F401 - fixture
from .test_migrations import login
from .test_monitoring import api  # noqa: F401 - fixture

pytestmark = pytest.mark.integration


def test_jobs_filters_sort_and_throughput(
    api: TestClient,  # noqa: F811
    engine: Engine,
    world: dict[str, Any],  # noqa: F811
) -> None:
    h = world["admin"]
    now = datetime.now(UTC)
    with Session(engine) as s, s.begin():
        for n, (name, seconds, size) in enumerate(
            [("ana", 100, 50 * 2**20), ("ben", 10, 0), ("ana", 1000, 10 * 2**20)]
        ):
            s.add(
                Job(
                    kind=JobKind.BACKUP,
                    status=JobStatus.SUCCEEDED,
                    account_id=world["ids"][name],
                    started_at=now - timedelta(seconds=seconds + n),
                    finished_at=now - timedelta(seconds=n),
                    progress={"items": 3, "read_bytes": size},
                    params={"backup_type": "incremental"},
                )
            )
    q = {"q": world["domain"], "kind": "backup", "limit": 2}
    page = api.get("/api/jobs", params=q, headers=h).json()
    assert page["total"] == 3 and len(page["items"]) == 2
    second = api.get("/api/jobs", params={**q, "offset": 2}, headers=h).json()
    assert len(second["items"]) == 1
    longest = api.get(
        "/api/jobs", params={**q, "sort": "duration", "order": "desc"}, headers=h
    ).json()["items"][0]
    assert longest["bytes_done"] == 10 * 2**20
    assert round(longest["throughput_bps"]) == round(10 * 2**20 / 1000)
    by_account = api.get(
        "/api/jobs", params={**q, "sort": "account", "order": "desc", "limit": 1}, headers=h
    ).json()["items"][0]
    assert by_account["account_email"].startswith("ben@")
    assert by_account["throughput_bps"] is None  # nothing read: no rate
    assert api.get("/api/jobs", params={"sort": "nope"}, headers=h).status_code == 422
    assert (
        api.get("/api/jobs", params={"kind": "restore", "q": world["domain"]}, headers=h).json()[
            "total"
        ]
        == 0
    )


def test_logs_from_the_interface(
    api: TestClient,  # noqa: F811
    engine: Engine,
    world: dict[str, Any],  # noqa: F811
) -> None:
    h = world["admin"]
    # No Redis in the tests: the server logs are reported as unavailable.
    out = api.get("/api/logs/server", params={"process": "scheduler"}, headers=h).json()
    assert out == {"process": "scheduler", "available": False, "lines": []}
    assert api.post("/api/logs/agent", json={"source": "/etc/shadow"}, headers=h).status_code == 422
    r = api.post("/api/logs/agent", json={"source": "mail", "lines": 50}, headers=h)
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert api.get(f"/api/logs/agent/{job_id}", headers=h).json()["status"] == "queued"
    claimed = _claim(world, engine, job_id)
    assert claimed["kind"] == "fetch" and claimed["params"] == {"log": "mail", "lines": 50}
    text = "Nov 19 11:08:38 mail postfix/smtpd[1]: connect from unknown"
    world["agent"].post(
        f"/jobs/{job_id}/fetch-complete", {"data": base64.b64encode(text.encode()).decode()}
    )
    assert api.get(f"/api/logs/agent/{job_id}", headers=h).json()["text"] == text
    other = login(api, engine)
    assert api.get(f"/api/logs/agent/{job_id}", headers=other).status_code == 403
    domain_admin = login(api, engine, Role.DOMAIN_ADMIN)
    assert api.get("/api/logs/server", headers=domain_admin).status_code == 403
    with Session(engine) as s, s.begin():
        s.execute(update(Job).where(Job.id == uuid.UUID(job_id)).values(status=JobStatus.CANCELLED))
