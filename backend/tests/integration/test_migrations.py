"""IMAP migrations: sources, batches, Excel/CSV import with automatic matching, runs and the
agent side (claim with credentials, result, notification)."""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from sqlalchemy import Engine, delete, select, update
from sqlalchemy.orm import Session

from carborewind_api.config import Settings
from carborewind_api.db.models import (
    Account,
    AppSetting,
    AuditEvent,
    AuthSource,
    Job,
    JobKind,
    JobStatus,
    MigrationMailbox,
    MigrationSource,
    Notification,
    Role,
    RoleAssignment,
    User,
)
from carborewind_api.main import create_app
from carborewind_api.migration_tools import ImportFileError, Matcher, Target, parse_file
from carborewind_api.notifications import SmtpConfig
from carborewind_api.pki import init_ca
from carborewind_api.security.passwords import hash_password

from .test_agent_protocol import AgentHandle, _csr

pytestmark = pytest.mark.integration
PASSWORD = "a-long-enough-password"
KEY = "m" * 40


# ------------------------------------------------------------------ pure helpers


def test_parse_csv_and_xlsx() -> None:
    csv_fr = (
        "Adresse source;Identifiant;Mot de passe;Destination;Dossier\r\n"
        "Ana@Old.test;ana.old;s3cret;;\r\n"
        ";;;;\r\n"
        "pas-une-adresse;;;;\r\n"
        "ben@old.test;;;ben@new.test;Ancien/Serveur\r\n"
        "cy@old.test;;;not-an-email;\r\n"
    ).encode("cp1252")
    parsed = parse_file(csv_fr)
    assert [(r.line, r.source_email, r.source_login) for r in parsed.rows] == [
        (2, "ana@old.test", "ana.old")
    ]
    assert parsed.rows[0].password == "s3cret" and "s3cret" not in repr(parsed.rows[0])
    assert [e.line for e in parsed.errors] == [4, 5, 6]
    no_header = parse_file(b"ana@old.test,ana@new.test\nben@old.test\n")
    assert [(r.source_email, r.target_email) for r in no_header.rows] == [
        ("ana@old.test", "ana@new.test"),
        ("ben@old.test", None),
    ]
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["source_email", "password", "target"])
    sheet.append(["dan@old.test", 1234, "dan@new.test"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    (row,) = parse_file(buffer.getvalue()).rows
    assert (row.source_email, row.password, row.target_email) == (
        "dan@old.test",
        "1234",
        "dan@new.test",
    )
    for bad in (b"", b"PK\x03\x04garbage", b"nom;prenom\nx;y\n", b"\x00\x01", b"x" * 6_000_000):
        with pytest.raises(ImportFileError):
            parse_file(bad)


def test_matcher() -> None:
    m = Matcher(
        [
            Target(1, "ana@new.test"),
            Target(2, "ben@new.test", ("ben@old.test",)),
            Target(3, "cy@new.test"),
            Target(4, "cy@other.test"),
            Target(5, "dan@old.test"),
        ]
    )
    assert (m.match("dan@old.test").how, m.match("dan@old.test").target) == (
        "exact",
        Target(5, "dan@old.test"),
    )
    assert m.match("ben@old.test").how == "alias"
    assert m.match("ana@old.test").how == "local_part"
    ambiguous = m.match("cy@old.test")
    assert ambiguous.target is None and ambiguous.candidates == ["cy@new.test", "cy@other.test"]
    assert m.match("cy@old.test", domain="other.test").target == Target(4, "cy@other.test")
    assert m.match("x@old.test", "ana@new.test").how == "manual"
    assert m.match("x@old.test", "ben@old.test").target == Target(
        2, "ben@new.test", ("ben@old.test",)
    )
    assert m.match("x@old.test", "nobody@new.test").error
    assert m.match("zed@old.test").target is None


# ------------------------------------------------------------------ API


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
    with Session(engine) as s, s.begin():  # the claim takes any queued job: start clean
        s.execute(
            update(Job)
            .where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
            .values(status=JobStatus.CANCELLED, lease_expires_at=None)
        )
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        yield client


def login(api: TestClient, engine: Engine, role: Role = Role.SUPER_ADMIN) -> dict[str, str]:
    name = f"m{uuid.uuid4().hex[:8]}"
    with Session(engine) as s, s.begin():
        s.add(
            User(
                username=name,
                auth_source=AuthSource.LOCAL,
                password_hash=hash_password(PASSWORD),
                roles=[RoleAssignment(role=role)],
            )
        )
    api.cookies.clear()
    r = api.post("/api/auth/login", json={"username": name, "password": PASSWORD})
    return {"X-CSRF-Token": r.json()["csrf_token"]}


def enroll(api: TestClient, admin: dict[str, str]) -> AgentHandle:
    r = api.post(
        "/api/agents",
        json={"name": f"mx-{uuid.uuid4().hex[:6]}", "hostname": "mail.example.test"},
        headers=admin,
    )
    r = api.post(
        "/api/agent/enroll", json={"token": r.json()["enrollment_token"], "csr_pem": _csr()}
    )
    body = r.json()
    return AgentHandle(api, body["agent_id"], body["certificate_pem"])


@pytest.fixture
def world(api: TestClient, engine: Engine) -> dict[str, Any]:
    """Carbonio accounts on new.test (+ an alias, an ambiguous name), an admin, an agent."""
    tag = uuid.uuid4().hex[:6]
    admin = login(api, engine)
    agent = enroll(api, admin)
    new, other = f"new{tag}.test", f"other{tag}.test"
    accounts = [
        {"carbonio_id": uuid.uuid4().hex, "email": f"ana{tag}@{new}", "status": "active"},
        {
            "carbonio_id": uuid.uuid4().hex,
            "email": f"ben{tag}@{new}",
            "status": "active",
            "aliases": [f"benoit@old{tag}.test"],
        },
        {"carbonio_id": uuid.uuid4().hex, "email": f"cy{tag}@{new}", "status": "active"},
        {"carbonio_id": uuid.uuid4().hex, "email": f"cy{tag}@{other}", "status": "active"},
    ]
    assert agent.post("/inventory", {"accounts": accounts}).status_code == 200
    return {"tag": tag, "admin": admin, "agent": agent, "new": new, "old": f"old{tag}.test"}


def make_source(api: TestClient, headers: dict[str, str], **kw: Any) -> dict[str, Any]:
    body = {"name": f"src-{uuid.uuid4().hex[:6]}", "host": "imap.old.test", **kw}
    r = api.post("/api/migrations/sources", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return dict(r.json())


def make_batch(api: TestClient, headers: dict[str, str], source: dict[str, Any], **kw: Any) -> str:
    body = {"name": f"lot-{uuid.uuid4().hex[:6]}", "source_id": source["id"], **kw}
    r = api.post("/api/migrations/batches", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def test_sources(api: TestClient, engine: Engine, world: dict[str, Any]) -> None:
    h = world["admin"]
    src = make_source(api, h)
    assert src["port"] == 993 and src["security"] == "ssl" and src["auth_mode"] == "password"
    for bad in (
        {"host": "imap://evil"},
        {"host": "a b"},
        {"port": 0},
        {"auth_mode": "master_plain"},  # no master user
        {"auth_mode": "master_plain", "master_username": "admin"},  # nor its password
        {"master_username": "a b"},
    ):
        r = api.post(
            "/api/migrations/sources", json={"name": "x", "host": "h.test", **bad}, headers=h
        )
        assert r.status_code == 422, bad
    master = make_source(
        api,
        h,
        host="10.0.0.5",
        auth_mode="master_plain",
        master_username="admin@old.test",
        master_password="m@ster",
    )
    assert master["master_password_set"] and "master_password" not in master
    with Session(engine) as s:
        row = s.get(MigrationSource, uuid.UUID(master["id"]))
        assert (
            row is not None and row.master_password_enc and "m@ster" not in row.master_password_enc
        )
    # Keep the password when omitted; switching to per-mailbox passwords drops it.
    body = {
        "name": master["name"],
        "host": "10.0.0.6",
        "auth_mode": "master_plain",
        "master_username": "admin@old.test",
    }
    r = api.put(f"/api/migrations/sources/{master['id']}", json=body, headers=h)
    assert r.json()["master_password_set"] and r.json()["host"] == "10.0.0.6"
    r = api.put(
        f"/api/migrations/sources/{master['id']}", json={**body, "auth_mode": "password"}, headers=h
    )
    assert not r.json()["master_password_set"] and r.json()["master_username"] is None
    assert (
        api.post(
            "/api/migrations/sources", json={"name": src["name"], "host": "x.test"}, headers=h
        ).status_code
        == 409
    )
    listed = {s["id"]: s for s in api.get("/api/migrations/sources").json()}
    assert src["id"] in listed
    make_batch(api, h, src)
    assert api.delete(f"/api/migrations/sources/{src['id']}", headers=h).status_code == 409
    assert api.delete(f"/api/migrations/sources/{master['id']}", headers=h).status_code == 204
    assert api.delete(f"/api/migrations/sources/{master['id']}", headers=h).status_code == 404


def test_import_matching_and_edits(api: TestClient, engine: Engine, world: dict[str, Any]) -> None:
    h, tag, new, old = world["admin"], world["tag"], world["new"], world["old"]
    src = make_source(api, h)
    batch = make_batch(
        api, h, src, options={"root": "/Ancien serveur/", "exclude_folders": [" Spam "]}
    )
    csv = (
        "source_email;password;destination\n"
        f"ana{tag}@{old};pw-ana;\n"  # local part -> ana@new
        f"benoit@{old};pw-ben;\n"  # alias of ben@new
        f"cy{tag}@{old};pw-cy;\n"  # ambiguous: two domains
        f"dan{tag}@{old};;\n"  # nobody
        f"ana{tag}@{old};pw-ana2;ben{tag}@{new}\n"  # one source into a second account
        f"eve@{old};;nobody@{new}\n"  # destination unknown
        "bad;;\n"
    )
    r = api.post(f"/api/migrations/batches/{batch}/import", content=csv.encode(), headers=h)
    assert r.status_code == 200, r.text
    report = r.json()
    assert (report["added"], report["matched"]) == (5, 3), report
    assert {u["source"]: u["candidates"] for u in report["unmatched"]} == {
        f"cy{tag}@{old}": sorted([f"cy{tag}@{new}", f"cy{tag}@other{tag}.test"]),
        f"dan{tag}@{old}": [],
    }
    assert sorted(e["line"] for e in report["errors"]) == [7, 8]
    detail = api.get(f"/api/migrations/batches/{batch}").json()
    assert detail["options"]["root"] == "Ancien serveur"
    assert detail["options"]["exclude_folders"] == ["Spam"]
    boxes = {(m["source_email"], m["target_email"]): m for m in detail["mailboxes"]}
    assert boxes[(f"ana{tag}@{old}", f"ana{tag}@{new}")]["match"] == "local_part"
    assert boxes[(f"benoit@{old}", f"ben{tag}@{new}")]["match"] == "alias"
    assert boxes[(f"ana{tag}@{old}", f"ben{tag}@{new}")]["match"] == "manual"
    assert all(
        m["password_set"] for m in detail["mailboxes"] if m["source_email"] != f"dan{tag}@{old}"
    )
    assert detail["counts"]["unmatched"] == 2 and detail["counts"]["new"] == 5
    with Session(engine) as s:
        stored = " ".join(m.password_enc or "" for m in s.scalars(select(MigrationMailbox)))
        assert "pw-ana" not in stored
        audit = " ".join(str(a.details) for a in s.scalars(select(AuditEvent)))
        assert "pw-ana" not in audit
    # Matching again with a destination domain resolves the ambiguity; re-import updates.
    r = api.post(f"/api/migrations/batches/{batch}/match", json={"target_domain": new}, headers=h)
    assert r.json()["updated"] == 1
    r = api.post(f"/api/migrations/batches/{batch}/import", content=csv.encode(), headers=h)
    assert r.json()["added"] == 0 and r.json()["updated"] == 5
    # Manual rows, then edits.
    r = api.post(
        f"/api/migrations/batches/{batch}/mailboxes",
        json={
            "rows": [
                {
                    "source_email": f"dan{tag}@{old}",
                    "target_email": f"ana{tag}@{new}",
                    "password": "pw-dan",
                    "root": "Dan",
                },
                {"source_email": "x"},
            ]
        },
        headers=h,
    )
    assert (r.json()["updated"], len(r.json()["errors"])) == (1, 1)
    dan = next(
        m
        for m in api.get(f"/api/migrations/batches/{batch}").json()["mailboxes"]
        if m["source_email"] == f"dan{tag}@{old}"
    )
    assert dan["target_email"] == f"ana{tag}@{new}" and dan["root"] == "Dan" and dan["password_set"]
    url = f"/api/migrations/mailboxes/{dan['id']}"
    assert api.patch(url, json={"target_email": f"nobody@{new}"}, headers=h).status_code == 422
    assert api.patch(url, json={"root": "a/b"}, headers=h).status_code == 422
    r = api.patch(
        url,
        json={"target_email": f"ben{tag}@{new}", "password": "", "source_login": "dan"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert (r.json()["target_email"], r.json()["password_set"], r.json()["source_login"]) == (
        f"ben{tag}@{new}",
        False,
        "dan",
    )
    r = api.patch(url, json={"clear_target": True}, headers=h)
    assert r.json()["target_email"] is None
    assert api.delete(url, headers=h).status_code == 204
    # Uploads: too large, not a list, bad domain; templates.
    big = b"source_email\n" + b"a@b.test\n" * 700_000
    assert (
        api.post(f"/api/migrations/batches/{batch}/import", content=big, headers=h).status_code
        == 413
    )
    assert (
        api.post(
            f"/api/migrations/batches/{batch}/import", content=b"nom\nx\n", headers=h
        ).status_code
        == 422
    )
    assert (
        api.post(
            f"/api/migrations/batches/{batch}/match", json={"target_domain": "a b"}, headers=h
        ).status_code
        == 422
    )
    xlsx = api.get("/api/migrations/template?format=xlsx")
    sheet = load_workbook(io.BytesIO(xlsx.content)).active
    assert sheet is not None and sheet["A1"].value == "source_email"
    assert parse_file(xlsx.content).rows[0].source_email == "ana@ancien-domaine.com"
    csv_t = api.get("/api/migrations/template?format=csv")
    assert csv_t.headers["content-type"].startswith("text/csv")
    assert parse_file(csv_t.content).rows


def test_batches_validation_and_rbac(
    api: TestClient, engine: Engine, world: dict[str, Any]
) -> None:
    h = world["admin"]
    src = make_source(api, h)
    for options in (
        {"root": "a/b"},
        {"since": "2026-02-01", "before": "2026-01-01"},
        {"exclude_folders": ["x\ny"]},
        {"coffee": True},
    ):
        r = api.post(
            "/api/migrations/batches",
            json={"name": "v", "source_id": src["id"], "options": options},
            headers=h,
        )
        assert r.status_code == 422, options
    batch = make_batch(api, h, src)
    r = api.put(
        f"/api/migrations/batches/{batch}",
        json={
            "name": "renamed-" + batch[:6],
            "source_id": src["id"],
            "options": {"since": "2025-01-01"},
        },
        headers=h,
    )
    assert r.status_code == 200 and r.json()["options"]["since"] == "2025-01-01"
    assert any(b["id"] == batch for b in api.get("/api/migrations/batches").json())
    assert api.get(f"/api/migrations/batches/{uuid.uuid4()}").status_code == 404
    domain_admin = login(api, engine, Role.DOMAIN_ADMIN)
    assert api.get("/api/migrations/batches").status_code == 403
    assert (
        api.post(
            "/api/migrations/sources", json={"name": "x", "host": "h.test"}, headers=domain_admin
        ).status_code
        == 403
    )
    api.cookies.clear()
    assert api.get("/api/migrations/sources").status_code == 401


def test_runs_and_agent_protocol(api: TestClient, engine: Engine, world: dict[str, Any]) -> None:
    h, agent, new, old = world["admin"], world["agent"], world["new"], world["old"]
    tag = world["tag"]
    with Session(engine) as s, s.begin():
        s.execute(delete(AppSetting))
        s.add(
            AppSetting(
                key="smtp",
                value=SmtpConfig(
                    enabled=True,
                    host="smtp.test",
                    sender="cr@example.com",
                    recipients=["ops@example.com"],
                ).to_json(),
            )
        )
    src = make_source(api, h, port=143, security="starttls", verify_tls=False)
    batch = make_batch(api, h, src, options={"root": "Import"})
    rows = [
        {"source_email": f"ana{tag}@{old}", "password": "pw-ana", "source_login": "ana"},
        {"source_email": f"ben{tag}@{old}"},  # matched, but no password
        {"source_email": f"zoe{tag}@{old}", "password": "pw-zoe"},  # no destination
    ]
    api.post(f"/api/migrations/batches/{batch}/mailboxes", json={"rows": rows}, headers=h)
    r = api.post(f"/api/migrations/batches/{batch}/start", json={"dry_run": True}, headers=h)
    assert r.json()["queued"] == 1
    assert sorted(x["reason"] for x in r.json()["skipped"]) == ["no destination", "no password"]
    again = api.post(f"/api/migrations/batches/{batch}/start", json={}, headers=h).json()
    assert "already queued or running" in [x["reason"] for x in again["skipped"]]
    with Session(engine) as s:
        job = s.scalar(
            select(Job).where(Job.kind == JobKind.MIGRATE, Job.params["batch_id"].astext == batch)
        )
        assert job is not None and "pw-ana" not in str(job.params)
    # Dry run: the claim carries the decrypted credentials, the result a plan.
    claimed = agent.post("/jobs/claim").json()
    assert claimed["kind"] == "migrate" and claimed["account_email"] == f"ana{tag}@{new}"
    spec = claimed["migration"]
    assert spec["imap"] == {
        "host": "imap.old.test",
        "port": 143,
        "security": "starttls",
        "verify_tls": False,
        "auth": "login",
        "username": "ana",
        "password": "pw-ana",
    }
    assert spec["options"]["root"] == "Import" and spec["dry_run"] is True
    plan = {
        "dry_run": True,
        "copied": 0,
        "folders": [{"source": "INBOX", "target": "/Import/Inbox", "total": 9, "new": 9}],
    }
    assert agent.post(f"/jobs/{claimed['job_id']}/migrate-complete", plan).status_code == 204
    box = next(
        m
        for m in api.get(f"/api/migrations/batches/{batch}").json()["mailboxes"]
        if m["source_email"] == f"ana{tag}@{old}"
    )
    assert box["status"] == "succeeded" and box["dry_run"]
    assert box["stats"]["plan"]["messages"] == 9
    # Real copy, interrupted by a cancellation: the state is kept anyway.
    api.post(f"/api/migrations/batches/{batch}/start", json={"mailbox_ids": [box["id"]]}, headers=h)
    claimed = agent.post("/jobs/claim").json()
    assert claimed["migration"]["dry_run"] is False and claimed["migration"]["state"] == {}
    job_id = claimed["job_id"]
    assert api.delete(f"/api/migrations/batches/{batch}", headers=h).status_code == 409
    assert (
        api.patch(
            f"/api/migrations/mailboxes/{box['id']}", json={"root": "x"}, headers=h
        ).status_code
        == 409
    )
    assert api.post(f"/api/migrations/batches/{batch}/cancel", headers=h).json()["cancelled"] == 1
    assert agent.post(f"/jobs/{job_id}/heartbeat", {"checkpoint": None}).status_code == 409
    state = {"folders": {"INBOX": {"uidvalidity": 7, "last_uid": 4, "failed": [3]}}}
    result = {
        "dry_run": False,
        "cancelled": True,
        "copied": 3,
        "bytes": 3000,
        "failed": {"INBOX:3": "mail.MESSAGE_TOO_BIG"},
        "failed_count": 1,
        "folders": [{"source": "INBOX", "target": "/Import/Inbox", "copied": 3}],
        "state": state,
    }
    assert agent.post(f"/jobs/{job_id}/migrate-complete", result).status_code == 204
    with Session(engine) as s:
        mailbox = s.get(MigrationMailbox, uuid.UUID(box["id"]))
        assert mailbox is not None and mailbox.state == state
        assert mailbox.stats and mailbox.stats["total_copied"] == 3 and mailbox.stats["plan"]
        cancelled = s.get(Job, uuid.UUID(job_id))
        assert cancelled is not None and cancelled.status is JobStatus.CANCELLED
        assert cancelled.error == "1 message(s) not copied"
    # Next run resumes from that state, finishes, and the batch notification is queued.
    api.post(f"/api/migrations/batches/{batch}/start", json={"mailbox_ids": [box["id"]]}, headers=h)
    claimed = agent.post("/jobs/claim").json()
    assert claimed["migration"]["state"] == state
    done = {**result, "cancelled": False, "copied": 2, "failed": {}, "failed_count": 0}
    path = f"/jobs/{claimed['job_id']}/migrate-complete"
    assert agent.post(path, done).status_code == 204
    assert agent.post(path, done).status_code == 204  # idempotent
    with Session(engine) as s:
        note = s.scalar(
            select(Notification)
            .where(Notification.event == "migration_done")
            .order_by(Notification.id.desc())
        )
        assert note is not None and "Messages copiés : 5" in note.body
    jobs = api.get("/api/jobs").json()["items"]
    mine = next(j for j in jobs if j["id"] == claimed["job_id"])
    assert (mine["kind"], mine["source_email"], mine["dry_run"]) == (
        "migrate",
        f"ana{tag}@{old}",
        False,
    )
    # Protocol errors.
    assert agent.post(f"/jobs/{uuid.uuid4()}/migrate-complete", done).status_code == 404
    api.post(f"/api/migrations/batches/{batch}/forget-passwords", headers=h)
    with Session(engine) as s:
        assert not s.scalars(
            select(MigrationMailbox.password_enc).where(
                MigrationMailbox.password_enc.is_not(None),
                MigrationMailbox.batch_id == uuid.UUID(batch),
            )
        ).all()
    # Without a password any more, a queued job fails at claim time.
    with Session(engine) as s, s.begin():
        s.add(
            Job(
                kind=JobKind.MIGRATE,
                status=JobStatus.QUEUED,
                priority=0,
                params={"batch_id": batch, "mailbox_id": box["id"]},
            )
        )
    assert agent.post("/jobs/claim").status_code == 204
    with Session(engine) as s:
        failed = s.scalar(
            select(Job).where(
                Job.kind == JobKind.MIGRATE,
                Job.priority == 0,
                Job.params["mailbox_id"].astext == box["id"],
            )
        )
        assert failed is not None and failed.status is JobStatus.FAILED
        assert failed.error == "no password for ana"
    assert api.delete(f"/api/migrations/batches/{batch}", headers=h).status_code == 204
    with Session(engine) as s, s.begin():
        s.execute(delete(AppSetting))


def test_master_user_credentials(api: TestClient, engine: Engine, world: dict[str, Any]) -> None:
    h, agent, old, tag = world["admin"], world["agent"], world["old"], world["tag"]
    plain = make_source(
        api, h, auth_mode="master_plain", master_username="admin", master_password="pw-master"
    )
    login_src = make_source(
        api,
        h,
        auth_mode="master_login",
        master_username="admin",
        master_password="pw-master",
        master_separator="*",
    )
    specs = {}
    for src in (plain, login_src):
        batch = make_batch(api, h, src)
        api.post(
            f"/api/migrations/batches/{batch}/mailboxes",
            json={"rows": [{"source_email": f"ana{tag}@{old}"}]},
            headers=h,
        )
        assert (
            api.post(f"/api/migrations/batches/{batch}/start", json={}, headers=h).json()["queued"]
            == 1
        )
        claimed = agent.post("/jobs/claim").json()
        specs[src["auth_mode"]] = claimed["migration"]["imap"]
        agent.post(f"/jobs/{claimed['job_id']}/fail", {"error": "boom", "retryable": False})
    assert specs["master_plain"]["auth"] == "plain"
    assert specs["master_plain"]["authzid"] == f"ana{tag}@{old}"
    assert specs["master_plain"]["password"] == "pw-master"
    assert specs["master_login"]["username"] == f"ana{tag}@{old}*admin"
    # A key change makes stored passwords unreadable: the job fails cleanly.
    with Session(engine) as s, s.begin():
        row = s.get(MigrationSource, uuid.UUID(plain["id"]))
        assert row is not None
        row.master_password_enc = "gAAAAA-not-a-valid-token"
    batch = make_batch(api, h, plain)
    api.post(
        f"/api/migrations/batches/{batch}/mailboxes",
        json={"rows": [{"source_email": f"ana{tag}@{old}"}]},
        headers=h,
    )
    api.post(f"/api/migrations/batches/{batch}/start", json={}, headers=h)
    assert agent.post("/jobs/claim").status_code == 204


def test_no_secret_key(migrated: str, engine: Engine) -> None:
    settings = Settings(environment="test", database_url=migrated)  # type: ignore[arg-type]
    with TestClient(create_app(settings), base_url="https://testserver") as api:
        h = login(api, engine)
        r = api.post(
            "/api/migrations/sources",
            json={
                "name": "k",
                "host": "h.test",
                "auth_mode": "master_plain",
                "master_username": "a",
                "master_password": "b",
            },
            headers=h,
        )
        assert r.status_code == 409
        with Session(engine) as s:
            assert not s.scalar(select(Account).limit(0))
