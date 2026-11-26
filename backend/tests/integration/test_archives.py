"""Archiving: policies and the simulation gate, the agent protocol, search, reading,
restoring, retention and purge."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select, update
from sqlalchemy.orm import Session

from carborewind_api.db.models import (
    Account,
    ArchivedMessage,
    ArchivePolicy,
    AuthSource,
    Domain,
    FetchedContent,
    Job,
    JobKind,
    JobStatus,
    Notification,
    Role,
    RoleAssignment,
    User,
)
from carborewind_api.security.passwords import hash_password

from .test_migrations import PASSWORD, enroll, login
from .test_monitoring import api as api
from .test_scheduler import NOW, run_tick

pytestmark = pytest.mark.integration

EML = (
    b"From: Bob <bob@example.test>\r\nTo: ana@example.test\r\nSubject: Facture 2024\r\n"
    b"Date: Tue, 05 Mar 2024 10:00:00 +0100\r\nMIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n--b\r\n'
    b"Content-Type: text/html; charset=utf-8\r\n\r\n"
    b"<p>Bonjour <b>Ana</b></p><script>alert(1)</script><img src=https://x.test/t.gif>\r\n"
    b'--b\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename="f.pdf"'
    b"\r\nContent-Transfer-Encoding: base64\r\n\r\nJVBERi0x\r\n--b--\r\n"
)


def blob(n: int) -> str:
    return f"{n:064x}"


@pytest.fixture
def world(api: TestClient, engine: Engine) -> Iterator[dict[str, Any]]:
    tag = uuid.uuid4().hex[:6]
    admin = login(api, engine)
    agent = enroll(api, admin)
    domain = f"arch{tag}.test"
    accounts = [
        {"carbonio_id": uuid.uuid4().hex, "email": f"ana@{domain}", "status": "active"},
        {"carbonio_id": uuid.uuid4().hex, "email": f"ben@{domain}", "status": "active"},
        {
            "carbonio_id": uuid.uuid4().hex,
            "email": f"admin@{domain}",
            "status": "active",
            "is_admin": True,
        },
    ]
    assert agent.post("/inventory", {"accounts": accounts}).status_code == 200
    with Session(engine) as s:
        rows = s.execute(select(Account).where(Account.email.like(f"%@{domain}"))).scalars()
        ids = {a.email.split("@")[0]: a.id for a in rows}
        domain_id = s.scalar(select(Domain.id).where(Domain.name == domain))
    yield {"admin": admin, "agent": agent, "domain": domain, "domain_id": domain_id, "ids": ids}


def _cancel_others(engine: Engine, keep: set[uuid.UUID]) -> None:
    with Session(engine) as s, s.begin():
        s.execute(
            update(Job)
            .where(Job.status == JobStatus.QUEUED, Job.id.not_in(keep or {uuid.uuid4()}))
            .values(status=JobStatus.CANCELLED)
        )


def _claim(
    world: dict[str, Any],
    engine: Engine,
    job_id: uuid.UUID | str,
    keep: set[uuid.UUID] | None = None,
) -> dict[str, Any]:
    keep = keep or set()
    _cancel_others(engine, {uuid.UUID(str(job_id)), *keep})
    r = world["agent"].post("/jobs/claim")
    assert r.status_code == 200, r.text
    job: dict[str, Any] = r.json()
    assert job["job_id"] == str(job_id) or job["job_id"] in {str(k) for k in keep}
    return job


def _jobs(engine: Engine, kind: JobKind, run: str | None = None) -> list[Job]:
    with Session(engine) as s:
        query = select(Job).where(Job.kind == kind, Job.status == JobStatus.QUEUED)
        if run:
            query = query.where(Job.params["run"].astext == run)
        return list(s.scalars(query))


def _policy(api: TestClient, h: dict[str, str], **kw: Any) -> dict[str, Any]:
    body = {"name": f"arch-{uuid.uuid4().hex[:6]}", **kw}
    r = api.post("/api/archives/policies", json=body, headers=h)
    assert r.status_code == 201, r.text
    return dict(r.json())


def _simulate(
    api: TestClient, world: dict[str, Any], engine: Engine, policy: dict[str, Any]
) -> None:
    h = world["admin"]
    r = api.post(f"/api/archives/policies/{policy['id']}/run", json={"simulate": True}, headers=h)
    assert r.status_code == 202, r.text
    assert r.json()["mode"] == "simulate"
    run = {j.id for j in _jobs(engine, JobKind.ARCHIVE, r.json()["run"])}
    for job_id in sorted(run):
        claimed = _claim(world, engine, job_id, keep=run)
        assert claimed["params"]["mode"] == "simulate"
        done = world["agent"].post(
            f"/jobs/{claimed['job_id']}/archive-complete",
            {
                "mode": "simulate",
                "candidates": 3,
                "candidate_bytes": 3000,
                "by_folder": {"/Inbox": [3, 3000]},
            },
        )
        assert done.status_code == 204, done.text


def test_policies_and_the_simulation_gate(
    api: TestClient,
    engine: Engine,
    world: dict[str, Any],
) -> None:
    h = world["admin"]
    scope = {"domain_ids": [str(world["domain_id"])]}
    for bad in (
        {"older_than_days": 10},
        {"schedule": "61 * * * *"},
        {"timezone": "Mars/Base"},
        {"exclude_folders": ["/"]},
        {"max_per_run": 0},
        {"domain_ids": [str(uuid.uuid4())]},
    ):
        r = api.post("/api/archives/policies", json={"name": "x", **bad}, headers=h)
        assert r.status_code == 422, (bad, r.text)
    # Deletion is refused until a simulation of these criteria finished.
    r = api.post("/api/archives/policies", json={"name": "direct", "mode": "archive"}, headers=h)
    assert r.status_code == 409
    policy = _policy(api, h, exclude_folders=["Inbox/Privé/", "/Projets"], **scope)
    assert policy["exclude_folders"] == ["/Inbox/Privé", "/Projets"]
    assert policy["mode"] == "simulate" and not policy["simulation_valid"]
    r = api.patch(f"/api/archives/policies/{policy['id']}", json={"mode": "archive"}, headers=h)
    assert r.status_code == 409

    _simulate(api, world, engine, policy)
    # Administrators are left out unless the policy includes them: 2 accounts, not 3.
    with Session(engine) as s:
        runs = s.scalars(select(Job).where(Job.params["policy_id"].astext == policy["id"])).all()
        assert len(runs) == 2 and all(j.status is JobStatus.SUCCEEDED for j in runs)
        report = s.scalar(
            select(Notification)
            .where(Notification.event == "archive_report")
            .order_by(Notification.created_at.desc())
        )
        assert report is not None and "simulation" in report.body
    listed = {p["id"]: p for p in api.get("/api/archives/policies", headers=h).json()}
    assert listed[policy["id"]]["simulation_valid"] is True
    r = api.patch(
        f"/api/archives/policies/{policy['id']}",
        json={"mode": "archive", "enabled": True},
        headers=h,
    )
    assert r.status_code == 200 and r.json()["effective_mode"] == "archive"
    # Changing the criteria makes the simulation stale: runs simulate again.
    r = api.patch(
        f"/api/archives/policies/{policy['id']}", json={"older_than_days": 400}, headers=h
    )
    assert r.json()["mode"] == "archive" and r.json()["effective_mode"] == "simulate"
    r = api.post(f"/api/archives/policies/{policy['id']}/run", json={}, headers=h)
    assert r.status_code == 409
    # A name change is not a criterion.
    _simulate(api, world, engine, policy)
    r = api.patch(
        f"/api/archives/policies/{policy['id']}",
        json={"name": "renamed " + policy["id"]},
        headers=h,
    )
    assert r.json()["effective_mode"] == "archive"
    # Domain administrators cannot manage archive policies.
    domain_admin = login(api, engine, Role.DOMAIN_ADMIN)
    assert api.get("/api/archives/policies", headers=domain_admin).status_code == 403
    h = login(api, engine)
    assert api.delete(f"/api/archives/policies/{policy['id']}", headers=h).status_code == 204


def _archive_run(
    api: TestClient,
    world: dict[str, Any],
    engine: Engine,
    mode: str = "archive",
) -> tuple[dict[str, Any], dict[str, Any]]:
    h = world["admin"]
    policy = _policy(api, h, account_ids=[str(world["ids"]["ana"])])
    _simulate(api, world, engine, policy)
    r = api.patch(f"/api/archives/policies/{policy['id']}", json={"mode": mode}, headers=h)
    assert r.status_code == 200, r.text
    r = api.post(f"/api/archives/policies/{policy['id']}/run", json={}, headers=h)
    assert r.json() == {"run": r.json()["run"], "mode": mode, "jobs": 1}
    (job,) = _jobs(engine, JobKind.ARCHIVE, r.json()["run"])
    return policy, _claim(world, engine, job.id)


def _entries(n: int, start: int = 1) -> list[dict[str, Any]]:
    return [
        {
            "item_id": 300 + i,
            "folder": 2,
            "folder_path": "/Inbox" if i % 2 else "/Inbox/Projets",
            "blob": blob(1000 + i),
            "meta_blob": blob(5000 + i),
            "size": 1000 * i,
            "meta_size": 80,
            "flags": "a" if i == start else "",
            "subject": f"Facture {i}",
            "sender": "Bob <bob@example.test>",
            "recipients": "ana@example.test",
            "date": f"2024-03-0{i}T09:00:00+00:00",
            "has_attachments": i == start,
        }
        for i in range(start, start + n)
    ]


def test_archive_run_indexes_then_removes(
    api: TestClient,
    engine: Engine,
    world: dict[str, Any],
) -> None:
    policy, job = _archive_run(api, world, engine)
    assert job["kind"] == "archive" and job["params"]["max_messages"] == 5000
    assert job["params"]["before"].endswith("T00:00:00+00:00")
    assert "skip_ids" not in job["params"]  # only for copy mode
    agent, jid = world["agent"], job["job_id"]
    entries = _entries(3)
    assert agent.post(f"/jobs/{jid}/archive-stored", {"entries": entries}).status_code == 204
    # Idempotent (a retried job reports the same messages again).
    assert agent.post(f"/jobs/{jid}/archive-stored", {"entries": entries}).status_code == 204
    done = {
        "mode": "archive",
        "candidates": 3,
        "archived": 3,
        "archived_bytes": 6000,
        "removed": [301, 302],
        "removed_bytes": 3000,
        "errors": {"303": "mail.QUOTA"},
        "error_count": 1,
        "manifest_ref": f"archives/x/{jid}.json",
        "replica_checked": True,
    }
    assert agent.post(f"/jobs/{jid}/archive-complete", done).status_code == 204
    assert agent.post(f"/jobs/{jid}/archive-complete", done).status_code == 204  # idempotent
    h = world["admin"]
    found = api.get("/api/archives/messages", params={"account": "ana@"}, headers=h).json()
    mine = [m for m in found["items"] if m["account_email"] == f"ana@{world['domain']}"]
    assert len(mine) == 3
    assert sum(1 for m in mine if m["removed_at"]) == 2
    first = next(m for m in mine if m["subject"] == "Facture 1")
    assert first["has_attachments"] and first["folder_path"] == "/Inbox"
    params = {"account_id": str(world["ids"]["ana"])}
    assert (
        api.get(
            "/api/archives/messages", params={**params, "folder": "/inbox/projets"}, headers=h
        ).json()["total"]
        == 1
    )
    assert (
        api.get("/api/archives/messages", params={**params, "q": "facture 3"}, headers=h).json()[
            "total"
        ]
        == 1
    )
    assert (
        api.get("/api/archives/messages", params={**params, "removed": "false"}, headers=h).json()[
            "total"
        ]
        == 1
    )
    assert (
        api.get(
            "/api/archives/messages",
            params={**params, "date_from": "2024-03-02T00:00:00Z"},
            headers=h,
        ).json()["total"]
        == 2
    )
    assert (
        api.get("/api/archives/messages", params={**params, "q": "%"}, headers=h).json()["total"]
        == 0
    )
    runs = api.get("/api/archives/runs", params={"policy_id": policy["id"]}, headers=h).json()
    last = runs["items"][0]
    assert last["mode"] == "archive" and last["progress"]["removed_count"] == 2
    assert last["error"] == "1 message(s) not archived"
    summary = api.get("/api/archives/summary", headers=h).json()
    assert summary["messages"] >= 3 and summary["removed_bytes"] >= 3000
    dash = api.get("/api/dashboard", headers=h).json()
    assert dash["archived_messages"] >= 3 and dash["archived_removed_bytes"] >= 3000
    # Copy mode: messages archived and still in the mailbox are not read again.
    with Session(engine) as s, s.begin():
        s.execute(
            update(ArchivePolicy)
            .where(ArchivePolicy.id == uuid.UUID(policy["id"]))
            .values(mode="copy")
        )
    r = api.post(f"/api/archives/policies/{policy['id']}/run", json={}, headers=h)
    (copy_job,) = _jobs(engine, JobKind.ARCHIVE, r.json()["run"])
    claimed = _claim(world, engine, copy_job.id)
    assert claimed["params"]["skip_ids"] == [303]
    r = agent.post(f"/jobs/{copy_job.id}/archive-complete", {"mode": "archive"})
    assert r.status_code == 422  # mode differs from the job's


def test_reading_downloading_and_restoring(
    api: TestClient,
    engine: Engine,
    world: dict[str, Any],
) -> None:
    _, job = _archive_run(api, world, engine)
    agent, jid, h = world["agent"], job["job_id"], world["admin"]
    agent.post(f"/jobs/{jid}/archive-stored", {"entries": _entries(2, start=7)})
    agent.post(f"/jobs/{jid}/archive-complete", {"mode": "archive", "removed": [307, 308]})
    with Session(engine) as s:
        ids = sorted(
            s.scalars(select(ArchivedMessage.id).where(ArchivedMessage.job_id == uuid.UUID(jid)))
        )
    viewer = login(api, engine, Role.VIEWER)
    assert api.get("/api/archives/messages", headers=viewer).status_code == 403
    assert api.get("/api/archives/summary", headers=viewer).status_code == 200
    assert api.post(f"/api/archives/messages/{ids[0]}/open", headers=viewer).status_code == 403

    h = login(api, engine)  # a fresh session for the super administrator
    r = api.post(f"/api/archives/messages/{ids[0]}/open", headers=h)
    assert r.status_code == 202, r.text
    fetch = r.json()["job_id"]
    assert api.get(f"/api/archives/open/{fetch}", headers=h).json()["status"] == "queued"
    claimed = _claim(world, engine, fetch)
    assert claimed["kind"] == "fetch" and claimed["params"]["blob"] == blob(1007)
    data = base64.b64encode(EML).decode()
    assert agent.post(f"/jobs/{fetch}/fetch-complete", {"data": data}).status_code == 204
    shown = api.get(f"/api/archives/open/{fetch}", headers=h).json()
    msg = shown["message"]
    assert shown["status"] == "succeeded" and msg["subject"] == "Facture 2024"
    assert msg["html_only"] and "Bonjour Ana" in msg["text"]
    assert "alert" not in msg["text"] and "<" not in msg["text"]
    assert msg["attachments"] == [{"name": "f.pdf", "content_type": "application/pdf", "size": 6}]
    eml = api.get(f"/api/archives/open/{fetch}/eml", headers=h)
    assert eml.status_code == 200 and eml.content == EML
    assert eml.headers["content-type"].startswith("message/rfc822")
    assert "attachment" in eml.headers["content-disposition"]
    # Fetch jobs are not listed with the others, and only their requester reads them.
    listed = api.get("/api/jobs", params={"limit": 200}, headers=h).json()["items"]
    assert all(j["kind"] != "fetch" for j in listed)
    with Session(engine) as s, s.begin():
        s.execute(delete(FetchedContent).where(FetchedContent.job_id == uuid.UUID(fetch)))
    assert api.get(f"/api/archives/open/{fetch}", headers=h).status_code == 410

    # Restore: one job per source account, with where each message lives.
    r = api.post("/api/archives/restore", json={"ids": ids, "mode": "original"}, headers=h)
    assert r.status_code == 202, r.text
    (restore,) = r.json()
    assert restore["kind"] == "restore" and restore["account_email"] == f"ana@{world['domain']}"
    claimed = _claim(world, engine, restore["id"])
    items = claimed["params"]["archive_items"]
    assert [i["id"] for i in items] == [307, 308] and items[0]["blob"] == blob(1007)
    assert claimed["params"]["label"].startswith("Archives restaurées")
    r = api.post(
        "/api/archives/restore",
        json={"ids": ids, "mode": "original", "target_account_id": str(world["ids"]["ben"])},
        headers=h,
    )
    assert r.status_code == 422
    r = api.post("/api/archives/restore", json={"ids": [ids[0], 10**12]}, headers=h)
    assert r.status_code == 404
    other = login(api, engine)  # last: the test client now holds this user's session
    assert api.get(f"/api/archives/open/{fetch}", headers=other).status_code == 403


def test_retention_and_purge(
    api: TestClient,
    engine: Engine,
    world: dict[str, Any],
    migrated: str,
) -> None:
    policy, job = _archive_run(api, world, engine)
    agent, jid, h = world["agent"], job["job_id"], world["admin"]
    agent.post(f"/jobs/{jid}/archive-stored", {"entries": _entries(1, start=9)})
    agent.post(
        f"/jobs/{jid}/archive-complete",
        {"mode": "archive", "removed": [309], "manifest_ref": f"archives/a/{jid}.json"},
    )
    r = api.patch(f"/api/archives/policies/{policy['id']}", json={"retention_days": 30}, headers=h)
    assert r.json()["effective_mode"] == "archive"  # retention is not a criterion
    with Session(engine) as s, s.begin():
        s.execute(
            update(ArchivedMessage)
            .where(ArchivedMessage.job_id == uuid.UUID(jid))
            .values(archived_at=NOW - timedelta(days=31))
        )
        s.execute(delete(Job).where(Job.kind == JobKind.PURGE))
        s.execute(
            update(Job)
            .where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
            .values(status=JobStatus.CANCELLED, lease_expires_at=None)
        )
        s.execute(update(ArchivePolicy).values(enabled=False))
    report = run_tick(migrated, NOW)
    assert report.purge_job is not None
    with Session(engine) as s:
        assert (
            s.scalar(select(ArchivedMessage.id).where(ArchivedMessage.job_id == uuid.UUID(jid)))
            is None
        )
    claimed = _claim(world, engine, report.purge_job)
    archives = claimed["params"]["archives"]
    assert {"account": job["carbonio_id"], "archive": jid} in archives
    assert blob(1009) in claimed["params"]["blobs"]
    r = agent.post(
        f"/jobs/{report.purge_job}/purge-complete",
        {"archives_deleted": [jid, str(uuid.uuid4())], "blobs_deleted": [blob(1009)]},
    )
    assert r.status_code == 204, r.text
    with Session(engine) as s:
        purged = s.get(Job, uuid.UUID(jid))
        assert purged is not None and purged.progress["manifest_purged"] is True


def test_scheduled_runs_simulate_until_a_simulation_is_valid(
    api: TestClient,
    engine: Engine,
    world: dict[str, Any],
    migrated: str,
) -> None:
    h = world["admin"]
    policy = _policy(
        api, h, account_ids=[str(world["ids"]["ben"])], enabled=True, schedule="0 * * * *"
    )
    with Session(engine) as s, s.begin():
        s.execute(
            update(ArchivePolicy)
            .where(ArchivePolicy.id != uuid.UUID(policy["id"]))
            .values(enabled=False)
        )
        # Configured for deletion, but no simulation: the scheduler only simulates.
        s.execute(
            update(ArchivePolicy)
            .where(ArchivePolicy.id == uuid.UUID(policy["id"]))
            .values(mode="archive", created_at=NOW - timedelta(hours=2))
        )
    run_tick(migrated, NOW)
    with Session(engine) as s:
        jobs = s.scalars(select(Job).where(Job.params["policy_id"].astext == policy["id"])).all()
        assert len(jobs) == 1 and jobs[0].params["mode"] == "simulate"
        assert s.get(ArchivePolicy, uuid.UUID(policy["id"])).last_run_at is not None  # type: ignore[union-attr]


def test_domain_scope_of_the_search(
    api: TestClient,
    engine: Engine,
    world: dict[str, Any],
) -> None:
    _, job = _archive_run(api, world, engine, mode="copy")
    world["agent"].post(f"/jobs/{job['job_id']}/archive-stored", {"entries": _entries(1, start=4)})
    name = f"d{uuid.uuid4().hex[:8]}"
    with Session(engine) as s, s.begin():
        other = Domain(name=f"elsewhere{name}.test")
        s.add(other)
        s.flush()
        s.add(
            User(
                username=name,
                auth_source=AuthSource.LOCAL,
                password_hash=hash_password(PASSWORD),
                roles=[RoleAssignment(role=Role.DOMAIN_ADMIN, domain_id=other.id)],
            )
        )
    api.cookies.clear()
    r = api.post("/api/auth/login", json={"username": name, "password": PASSWORD})
    h = {"X-CSRF-Token": r.json()["csrf_token"]}
    found = api.get("/api/archives/messages", params={"account": world["domain"]}, headers=h)
    assert found.status_code == 200 and found.json()["total"] == 0
    with Session(engine) as s:
        mid = s.scalar(
            select(ArchivedMessage.id).where(ArchivedMessage.job_id == uuid.UUID(job["job_id"]))
        )
    assert api.post(f"/api/archives/messages/{mid}/open", headers=h).status_code == 403
    assert api.post("/api/archives/restore", json={"ids": [mid]}, headers=h).status_code == 403


def test_legal_hold(api: TestClient, engine: Engine, world: dict[str, Any], migrated: str) -> None:
    h, agent = world["admin"], world["agent"]
    ana = str(world["ids"]["ana"])
    # Messages archived by a policy first, kept 30 days.
    policy, job = _archive_run(api, world, engine, mode="copy")
    agent.post(f"/jobs/{job['job_id']}/archive-stored", {"entries": _entries(2, start=1)})
    agent.post(f"/jobs/{job['job_id']}/archive-complete", {"mode": "copy"})
    api.patch(f"/api/archives/policies/{policy['id']}", json={"retention_days": 30}, headers=h)

    r = api.post(
        f"/api/archives/accounts/{ana}/legal-hold", json={"note": "Dossier 2026-17"}, headers=h
    )
    assert r.status_code == 202, r.text
    held = r.json()
    assert (
        api.post(f"/api/archives/accounts/{ana}/legal-hold", json={}, headers=h).status_code == 409
    )
    claimed = _claim(world, engine, held["id"])
    p = claimed["params"]
    assert p["legal_hold"] and p["all_folders"] and p["mode"] == "copy"
    assert not p["keep_flagged"] and not p["keep_unread"] and p["max_messages"] == 200_000
    assert sorted(p["skip_ids"]) == [301, 302]  # already archived, still in the mailbox
    agent.post(f"/jobs/{held['id']}/archive-stored", {"entries": _entries(2, start=3)})
    assert agent.post(f"/jobs/{held['id']}/archive-complete", {"mode": "copy"}).status_code == 204

    found = api.get(
        "/api/archives/messages", params={"account_id": ana, "legal_hold": "true"}, headers=h
    ).json()
    assert found["total"] == 4 and all(m["legal_hold"] for m in found["items"])
    holds = api.get("/api/archives/legal-holds", headers=h).json()
    mine = next(x for x in holds if x["account_id"] == ana)
    assert mine["messages"] == 4 and mine["pending"] is False
    summary = api.get("/api/archives/summary", headers=h).json()
    assert summary["legal_hold_messages"] >= 4 and summary["legal_hold_accounts"] >= 1

    # Retention never deletes held messages.
    with Session(engine) as s, s.begin():
        s.execute(
            update(ArchivedMessage)
            .where(ArchivedMessage.account_id == uuid.UUID(ana))
            .values(archived_at=NOW - timedelta(days=60))
        )
        s.execute(update(ArchivePolicy).values(enabled=False))
        s.execute(
            update(Job)
            .where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
            .values(status=JobStatus.CANCELLED, lease_expires_at=None)
        )
    run_tick(migrated, NOW)
    with Session(engine) as s:
        assert (
            len(
                s.scalars(
                    select(ArchivedMessage).where(ArchivedMessage.account_id == uuid.UUID(ana))
                ).all()
            )
            == 4
        )

    # Released: the policy's retention applies again to its own messages.
    assert api.delete(f"/api/archives/accounts/{ana}/legal-hold", headers=h).status_code == 204
    run_tick(migrated, NOW)
    with Session(engine) as s:
        left = s.scalars(
            select(ArchivedMessage).where(ArchivedMessage.account_id == uuid.UUID(ana))
        ).all()
        assert sorted(m.carbonio_item_id for m in left) == [303, 304]  # no policy: kept
    domain_admin = login(api, engine, Role.DOMAIN_ADMIN)
    assert (
        api.post(
            f"/api/archives/accounts/{ana}/legal-hold", json={}, headers=domain_admin
        ).status_code
        == 403
    )


def test_archives_by_mailbox_and_full_restore(
    api: TestClient, engine: Engine, world: dict[str, Any]
) -> None:
    _, job = _archive_run(api, world, engine)
    agent, jid, h = world["agent"], job["job_id"], world["admin"]
    agent.post(f"/jobs/{jid}/archive-stored", {"entries": _entries(3, start=5)})
    agent.post(f"/jobs/{jid}/archive-complete", {"mode": "archive", "removed": [305, 306]})
    boxes = api.get(
        "/api/archives/mailboxes", params={"q": f"ana@{world['domain']}"}, headers=h
    ).json()
    mine = next(b for b in boxes["items"] if b["account_email"] == f"ana@{world['domain']}")
    assert mine["messages"] >= 3 and mine["removed_messages"] >= 2
    assert mine["oldest"] is not None and mine["legal_hold"] in (True, False)
    viewer = login(api, engine, Role.VIEWER)
    assert api.get("/api/archives/mailboxes", headers=viewer).status_code == 200
    h = login(api, engine)
    ana = str(world["ids"]["ana"])
    r = api.post(f"/api/archives/accounts/{ana}/restore", json={}, headers=h)
    assert r.status_code == 202, r.text
    (restore,) = r.json()
    with Session(engine) as s:
        params = s.get(Job, uuid.UUID(restore["id"])).params  # type: ignore[union-attr]
    restored_ids = sorted(i["id"] for i in params["archive_items"])
    assert params["mode"] == "original" and {305, 306} <= set(restored_ids)
    assert 307 not in restored_ids  # still in the mailbox: not restored by default
    r = api.post(
        f"/api/archives/accounts/{ana}/restore",
        json={"only_removed": False, "mode": "new_folder"},
        headers=h,
    )
    with Session(engine) as s:
        params = s.get(Job, uuid.UUID(r.json()[0]["id"])).params  # type: ignore[union-attr]
    assert 307 in {i["id"] for i in params["archive_items"]}
    nobody = str(world["ids"]["admin"])
    assert (
        api.post(f"/api/archives/accounts/{nobody}/restore", json={}, headers=h).status_code == 404
    )


def test_archive_mailbox_then_remove(
    api: TestClient, engine: Engine, world: dict[str, Any]
) -> None:
    h, agent = world["admin"], world["agent"]
    ana = world["ids"]["ana"]
    with Session(engine) as s:
        email = s.get(Account, ana).email  # type: ignore[union-attr]
    url = f"/api/archives/accounts/{ana}/legal-hold"
    body = {"remove_from_mailbox": True, "older_than_days": 365}
    assert api.post(url, json=body, headers=h).status_code == 422  # not confirmed
    assert api.post(url, json={**body, "confirm": "x@y.z"}, headers=h).status_code == 422
    r = api.post(url, json={**body, "confirm": email.upper()}, headers=h)
    assert r.status_code == 202, r.text
    job = r.json()
    p = _claim(world, engine, job["id"])["params"]
    assert p["mode"] == "archive" and p["legal_hold"] and p["all_folders"]
    before = datetime.fromisoformat(p["before"])
    assert abs((before - (datetime.now(UTC) - timedelta(days=365))).total_seconds()) < 60
    agent.post(f"/jobs/{job['id']}/archive-stored", {"entries": _entries(2, start=5)})
    done = agent.post(
        f"/jobs/{job['id']}/archive-complete",
        {"mode": "archive", "removed": [305, 306], "removed_bytes": 2048},
    )
    assert done.status_code == 204, done.text
    found = api.get(
        "/api/archives/messages", params={"account_id": str(ana), "legal_hold": "true"}, headers=h
    ).json()["items"]
    removed = [m for m in found if m["removed_at"]]
    assert len(removed) >= 2
