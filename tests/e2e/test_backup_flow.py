"""End-to-end: Carbonio (simulated) → agent → encrypted repository → API → PostgreSQL.

Covers acceptance criteria of the MVP at the integration level: hot backup of an account,
incremental update, point-in-time catalogue, interrupted job resumed, everything audited.
Requires CARBOREWIND_TEST_DATABASE_URL (a disposable database).
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent" / "tests"))

from fake_carbonio import FakeCarbonio, Mailbox  # noqa: E402

from carborewind_agent.api_client import AgentApi, enroll  # noqa: E402
from carborewind_agent.backup import AccountBackup  # noqa: E402
from carborewind_agent.carbonio.factory import build_client  # noqa: E402
from carborewind_agent.config import AgentSettings  # noqa: E402
from carborewind_agent.migrate import MailboxMigration  # noqa: E402
from carborewind_agent.repository import Repository, generate_key  # noqa: E402
from carborewind_agent.restore import AccountRestore  # noqa: E402
from carborewind_agent.worker import Worker  # noqa: E402
from carborewind_api.audit import GENESIS, chain_hash, event_payload  # noqa: E402
from carborewind_api.config import Settings  # noqa: E402
from carborewind_api.db.models import (  # noqa: E402
    Account,
    AuditEvent,
    AuthSource,
    ItemVersion,
    Role,
    RoleAssignment,
    Snapshot,
    User,
)
from carborewind_api.main import create_app  # noqa: E402
from carborewind_api.pki import init_ca  # noqa: E402
from carborewind_api.security.passwords import hash_password  # noqa: E402

DB_URL = os.environ.get("CARBOREWIND_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="CARBOREWIND_TEST_DATABASE_URL not set")
PASSWORD = "a-long-enough-password"


def eml(subject: str) -> bytes:
    return f"From: bob@example.test\r\nSubject: {subject}\r\n\r\nBody of {subject}\r\n".encode()


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    cfg = Config(str(ROOT / "backend" / "alembic.ini"))
    cfg.attributes["url"] = DB_URL
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    eng = create_engine(str(DB_URL))
    yield eng
    eng.dispose()


class Stack:
    def __init__(self, tmp: Path, engine: Engine) -> None:
        init_ca(tmp / "pki")
        settings = Settings(  # type: ignore[call-arg]
            environment="test", database_url=DB_URL, pki_dir=tmp / "pki",
            agent_proxy_ips=["testclient"], secret_key="e2e-secret-key-" + "x" * 32,
        )
        self.api = TestClient(create_app(settings), base_url="https://testserver").__enter__()
        username = f"root{uuid.uuid4().hex[:6]}"
        with Session(engine) as s, s.begin():
            s.add(User(username=username, auth_source=AuthSource.LOCAL,
                       password_hash=hash_password(PASSWORD),
                       roles=[RoleAssignment(role=Role.SUPER_ADMIN)]))
        login = self.api.post("/api/auth/login", json={"username": username, "password": PASSWORD})
        self.csrf = {"X-CSRF-Token": login.json()["csrf_token"]}

        # Agent enrolment through the real endpoint.
        created = self.api.post("/api/agents", json={"name": f"mx{uuid.uuid4().hex[:6]}",
                                                     "hostname": "mail.example.test"},
                                headers=self.csrf).json()
        secret = tmp / "admin.password"
        secret.write_text("admin-secret-for-tests\n")
        os.chmod(secret, 0o600)
        agent_settings = AgentSettings(  # type: ignore[call-arg]
            api_url="https://testserver", carbonio_admin_password_file=secret,
            tls_cert=tmp / "agent.crt", tls_key=tmp / "agent.key", tls_ca=tmp / "ca.crt",
        )
        enroll(agent_settings, created["enrollment_token"], http=self.api)
        # The reverse proxy would verify the client certificate and forward it.
        proxied = TestClient(self.api.app, base_url="https://testserver", headers={
            "X-Client-Verify": "SUCCESS",
            "X-Client-Cert": quote(agent_settings.tls_cert.read_text()),
        })
        self.carbonio = FakeCarbonio()
        client = build_client(agent_settings, transport=self.carbonio.transport())
        self.repo = Repository.init(tmp / "repo", generate_key())
        self.engine = AccountBackup(client, self.repo, checkpoint_every=2)
        self.client = client
        self.worker = Worker(AgentApi(agent_settings, http=proxied), client, self.engine,
                             AccountRestore(client, self.repo), self.repo,
                             migration=MailboxMigration(client), sleep=lambda _: True)

    def request_backup(self, engine: Engine, email: str) -> str:
        with Session(engine) as s:
            account_id = s.scalar(select(Account.id).where(Account.email == email))
        r = self.api.post("/api/backups", json={"account_id": str(account_id)}, headers=self.csrf)
        assert r.status_code == 202, r.text
        return str(r.json()["id"])

    def job(self, job_id: str) -> dict[str, object]:
        result: dict[str, object] = self.api.get(f"/api/jobs/{job_id}").json()
        return result


def _catalogue(engine: Engine, email: str, seq: int) -> dict[int, str | None]:
    with Session(engine) as s:
        account_id = s.scalar(select(Account.id).where(Account.email == email))
        rows = s.execute(select(ItemVersion.carbonio_item_id, ItemVersion.subject).where(
            ItemVersion.account_id == account_id, ItemVersion.from_seq <= seq,
            (ItemVersion.to_seq.is_(None)) | (ItemVersion.to_seq > seq)))
        return dict(rows.tuples().all())


def _subjects(box: Mailbox) -> dict[int, str]:
    return {i: item.raw.split(b"Subject: ")[1].split(b"\r\n")[0].decode()
            for i, item in box.items.items() if item.kind == "m"}


def test_backup_incremental_resume_and_audit(tmp_path: Path, engine: Engine,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    stack = Stack(tmp_path, engine)
    box = stack.carbonio.add_mailbox(f"alice@{uuid.uuid4().hex[:6]}.test")
    for n in range(5):
        box.add("m", 2, eml(f"Message {n}"))

    # Inventory flows from Carbonio to the API.
    stack.worker.sync_inventory()

    # 1. Full hot backup.
    first = stack.request_backup(engine, box.name)
    assert stack.worker.run_once()
    assert stack.job(first)["status"] == "succeeded"
    state_1 = _subjects(box)
    assert _catalogue(engine, box.name, 1) == state_1

    # 2. Mailbox changes, incremental backup.
    ids = sorted(box.items)
    box.delete(ids[0])
    box.add("m", 2, eml("Nouveau"))
    stack.request_backup(engine, box.name)
    assert stack.worker.run_once()
    state_2 = _subjects(box)
    assert _catalogue(engine, box.name, 2) == state_2
    assert _catalogue(engine, box.name, 1) == state_1  # point in time preserved

    # 3. The agent crashes in the middle of a job: it is re-queued with its checkpoint, and
    #    the next run resumes without re-reading what was already stored.
    for n in range(6):
        box.add("m", 5, eml(f"Lot {n}"))
    job3 = stack.request_backup(engine, box.name)
    real_fetch = stack.engine._client.fetch_raw
    reads = {"n": 0}

    def flaky(name: str, account_id: str, item_id: int) -> bytes:
        reads["n"] += 1
        if reads["n"] == 4:
            raise RuntimeError("agent killed")
        return real_fetch(name, account_id, item_id)

    monkeypatch.setattr(stack.engine._client, "fetch_raw", flaky)
    assert stack.worker.run_once()
    assert stack.job(job3)["status"] == "queued"
    reads["n"] = 100
    assert stack.worker.run_once()
    assert stack.job(job3)["status"] == "succeeded"
    assert reads["n"] - 100 == 6 - 2  # 2 items were checkpointed before the crash
    assert _catalogue(engine, box.name, 3) == _subjects(box)

    # Every stored blob decrypts to the exact Carbonio content.
    with Session(engine) as s:
        snaps = s.scalars(select(Snapshot).join(Account).where(Account.email == box.name)
                          .order_by(Snapshot.seq)).all()
        assert [x.backup_type for x in snaps] == ["full", "incremental", "incremental"]
        manifest = stack.repo.get_manifest(box.id, str(snaps[-1].id))
    for item_id, entry in manifest["items"].items():
        assert stack.repo.get(entry["blob"]) == box.items[int(item_id)].raw

    # Everything was audited, and the hash chain is intact.
    with Session(engine) as s:
        events = s.scalars(select(AuditEvent).order_by(AuditEvent.id)).all()
    actions = {e.action for e in events}
    assert {"agent.enroll", "inventory.sync", "backup.request", "job.claim", "job.fail",
            "backup.complete"} <= actions
    prev = GENESIS
    for event in events:
        assert event.prev_hash == prev
        assert event.hash == chain_hash(event.prev_hash, event_payload(event))
        prev = event.hash


def test_point_in_time_restore_of_deleted_mail(tmp_path: Path, engine: Engine) -> None:
    """Back up, lose mail, browse the old snapshot, dry-run, then restore what was lost."""
    stack = Stack(tmp_path, engine)
    box = stack.carbonio.add_mailbox(f"zoe@{uuid.uuid4().hex[:6]}.test")
    for n in range(4):
        box.add("m", 2, eml(f"Message {n}"), flags="u")
    stack.worker.sync_inventory()
    stack.request_backup(engine, box.name)
    assert stack.worker.run_once()
    before = _subjects(box)

    # Two messages are deleted; a new backup records it.
    lost = sorted(box.items)[:2]
    for item_id in lost:
        box.delete(item_id)
    stack.request_backup(engine, box.name)
    assert stack.worker.run_once()

    with Session(engine) as s:
        account_id = s.scalar(select(Account.id).where(Account.email == box.name))
    snaps = stack.api.get(f"/api/accounts/{account_id}/snapshots").json()["items"]
    first = next(x for x in snaps if x["seq"] == 1)["id"]
    items = stack.api.get(f"/api/accounts/{account_id}/snapshots/{first}/items").json()
    assert sorted(i["subject"] for i in items["items"]) == sorted(before.values())

    def restore(**body: object) -> dict[str, object]:
        r = stack.api.post("/api/restores", json={"account_id": str(account_id),
                                                  "snapshot_id": first, **body},
                           headers=stack.csrf)
        assert r.status_code == 202, r.text
        assert stack.worker.run_once()
        return stack.job(str(r.json()["id"]))

    # Dry run: the plan, nothing written.
    plan = restore(mode="original", dry_run=True)
    assert plan["status"] == "succeeded"
    progress = plan["progress"]
    assert isinstance(progress, dict)
    assert (progress["restored"], progress["present"]) == (2, 2)
    assert len(box.items) == 2

    # Real restore into the original folder: only the two lost messages come back.
    done = restore(mode="original")
    assert done["status"] == "succeeded" and done["error"] is None
    assert sorted(_subjects(box).values()) == sorted(before.values())
    assert all(item.flags == "u" for item in box.items.values())

    # A copy into a new dated folder never touches the originals.
    copy = restore()
    progress = copy["progress"]
    assert isinstance(progress, dict) and progress["restored"] == 4
    assert len(box.items) == 8
    with Session(engine) as s:
        actions = {e.action for e in s.scalars(select(AuditEvent))}
    assert {"restore.request", "restore.dry_run", "restore.complete"} <= actions


def test_scheduled_backups_retention_and_purge(tmp_path: Path, engine: Engine) -> None:
    """A policy drives backups; retention expires old snapshots; the agent frees space."""
    import asyncio
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from carborewind_api.db.models import Blob, Job, Policy
    from carborewind_api.db.session import make_engine
    from carborewind_api.scheduler import tick

    def run_tick(now: datetime) -> None:
        async def go() -> None:
            eng = make_engine(str(DB_URL))
            try:
                async with async_sessionmaker(eng)() as db, db.begin():
                    await tick(db, now)
            finally:
                await eng.dispose()

        asyncio.run(go())

    stack = Stack(tmp_path, engine)
    box = stack.carbonio.add_mailbox(f"yann@{uuid.uuid4().hex[:6]}.test")
    for n in range(3):
        box.add("m", 2, eml(f"Message {n}"))
    stack.worker.sync_inventory()
    with Session(engine) as s:
        account_id = s.scalar(select(Account.id).where(Account.email == box.name))
    r = stack.api.post(
        "/api/policies",
        json={
            "name": f"nightly-{uuid.uuid4().hex[:6]}",
            "target_type": "account",
            "target_id": str(account_id),
            "schedule": "0 1 * * *",
            "timezone": "Africa/Algiers",
            "retention": {"last": 2},
        },
        headers=stack.csrf,
    )
    assert r.status_code == 201, r.text
    policy_id = r.json()["id"]
    # Three nights; mail changes every day.
    night = datetime(2026, 11, 18, 0, 5, tzinfo=UTC)
    with Session(engine) as s, s.begin():
        s.execute(
            update(Policy)
            .where(Policy.id == uuid.UUID(policy_id))
            .values(last_run_at=night - timedelta(days=1))
        )
    for day in range(3):
        now = night + timedelta(days=day)
        run_tick(now)
        assert stack.worker.run_once()  # the scheduled backup
        while stack.worker.run_once():  # a purge job, when retention released something
            pass
        oldest = min(box.items)
        box.delete(oldest)
        box.add("m", 2, eml(f"Jour {day}"))
    # The next hourly pass (midday, no backup due) applies retention to the last night.
    run_tick(night + timedelta(days=2, hours=12))
    assert stack.worker.run_once()  # purge
    assert not stack.worker.run_once()
    with Session(engine) as s:
        snaps = s.scalars(
            select(Snapshot).where(Snapshot.account_id == account_id).order_by(Snapshot.seq)
        ).all()
        assert [(x.seq, x.status) for x in snaps] == [
            (1, "expired"),
            (2, "complete"),
            (3, "complete"),
        ]
        assert snaps[0].purged_at is not None
        jobs = s.scalars(select(Job).where(Job.policy_id == uuid.UUID(policy_id))).all()
        assert len(jobs) == 3 and all(j.status == "succeeded" for j in jobs)
        known_blobs = set(s.scalars(select(Blob.id)))
    # The repository holds exactly the blobs the catalogue knows about for this account's
    # remaining snapshots, and the expired manifest is gone.
    with pytest.raises(Exception, match="not found"):
        stack.repo.get_manifest(box.id, str(snaps[0].id))
    for snap in snaps[1:]:
        for entry in stack.repo.get_manifest(box.id, str(snap.id))["items"].values():
            assert stack.repo.get(entry["blob"])
            assert entry["blob"] in known_blobs
    assert stack.repo.verify().ok
