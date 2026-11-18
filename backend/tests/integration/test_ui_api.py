"""Read endpoints used by the web interface: account status, snapshots, jobs, dashboard."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
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
    Role,
    RoleAssignment,
    Snapshot,
    SnapshotStatus,
    User,
)
from carborewind_api.main import create_app
from carborewind_api.security.passwords import hash_password

pytestmark = pytest.mark.integration

PASSWORD = "a-long-enough-password"


@pytest.fixture
def api(migrated: str) -> Iterator[TestClient]:
    settings = Settings(environment="test", database_url=migrated)  # type: ignore[arg-type]
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        yield client


@pytest.fixture
def world(engine: Engine) -> dict[str, uuid.UUID]:
    """Two domains; in the first: alice (2 snapshots, last partial), bob (never), and jobs."""
    now = datetime.now(UTC)
    ids: dict[str, uuid.UUID] = {}
    with Session(engine) as s, s.begin():
        d1 = Domain(name=f"u{uuid.uuid4().hex[:6]}.test")
        d2 = Domain(name=f"v{uuid.uuid4().hex[:6]}.test")
        s.add_all([d1, d2])
        s.flush()
        alice = Account(
            domain_id=d1.id,
            email=f"alice@{d1.name}",
            carbonio_id=uuid.uuid4().hex,
            status=AccountStatus.ACTIVE,
        )
        bob = Account(
            domain_id=d1.id,
            email=f"bob@{d1.name}",
            carbonio_id=uuid.uuid4().hex,
            status=AccountStatus.ACTIVE,
        )
        carol = Account(
            domain_id=d2.id,
            email=f"carol@{d2.name}",
            carbonio_id=uuid.uuid4().hex,
            status=AccountStatus.ACTIVE,
        )
        s.add_all([alice, bob, carol])
        s.flush()
        for seq, kind, state, when, size in (
            (1, BackupType.FULL, SnapshotStatus.COMPLETE, now - timedelta(days=2), 1000),
            (2, BackupType.INCREMENTAL, SnapshotStatus.PARTIAL, now - timedelta(hours=1), 50),
        ):
            s.add(
                Snapshot(
                    account_id=alice.id,
                    seq=seq,
                    backup_type=kind,
                    status=state,
                    taken_at=when,
                    manifest_ref=f"m/{seq}",
                    item_count=10 * seq,
                    size_bytes=size * 10,
                    new_bytes=size,
                )
            )
        s.add(
            Snapshot(
                account_id=carol.id,
                seq=1,
                backup_type=BackupType.FULL,
                status=SnapshotStatus.COMPLETE,
                taken_at=now,
                manifest_ref="m/c",
                item_count=3,
                size_bytes=300,
                new_bytes=300,
            )
        )
        s.add_all(
            [
                Job(
                    kind=JobKind.BACKUP,
                    status=JobStatus.QUEUED,
                    account_id=bob.id,
                    params={"backup_type": "full"},
                ),
                Job(
                    kind=JobKind.BACKUP,
                    status=JobStatus.FAILED,
                    account_id=alice.id,
                    finished_at=now,
                    error="carbonio busy",
                    params={"backup_type": "incremental"},
                ),
                Job(kind=JobKind.BACKUP, status=JobStatus.QUEUED, account_id=carol.id),
            ]
        )
        ids.update(d1=d1.id, d2=d2.id, alice=alice.id, bob=bob.id, carol=carol.id)
    return ids


def _login(api: TestClient, engine: Engine, role: Role, domain: uuid.UUID | None) -> None:
    name = f"ui{uuid.uuid4().hex[:8]}"
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
    assert (
        api.post("/api/auth/login", json={"username": name, "password": PASSWORD}).status_code
        == 200
    )


def test_account_list_shows_last_backup(
    api: TestClient, engine: Engine, world: dict[str, uuid.UUID]
) -> None:
    _login(api, engine, Role.VIEWER, world["d1"])
    items = {
        a["email"].split("@")[0]: a
        for a in api.get(f"/api/domains/{world['d1']}/accounts").json()["items"]
    }
    assert items["alice"]["last_backup_status"] == "partial"
    assert items["alice"]["last_backup_type"] == "incremental"
    assert items["alice"]["last_backup_at"] is not None
    assert items["bob"]["last_backup_at"] is None


def test_account_detail_and_snapshots(
    api: TestClient, engine: Engine, world: dict[str, uuid.UUID]
) -> None:
    _login(api, engine, Role.VIEWER, world["d1"])
    detail = api.get(f"/api/accounts/{world['alice']}").json()
    assert detail["snapshot_count"] == 2
    assert detail["last_snapshot"]["seq"] == 2
    assert detail["domain"].endswith(".test")
    snaps = api.get(f"/api/accounts/{world['alice']}/snapshots").json()
    assert [s["seq"] for s in snaps["items"]] == [2, 1]
    assert snaps["items"][1]["new_bytes"] == 1000
    never = api.get(f"/api/accounts/{world['bob']}").json()
    assert never["snapshot_count"] == 0 and never["last_snapshot"] is None
    # Other domain or unknown account: identical refusal.
    assert api.get(f"/api/accounts/{world['carol']}").status_code == 403
    assert api.get(f"/api/accounts/{uuid.uuid4()}/snapshots").status_code == 403


def test_jobs_list_is_scoped_and_filterable(
    api: TestClient, engine: Engine, world: dict[str, uuid.UUID]
) -> None:
    _login(api, engine, Role.RESTORE_OPERATOR, world["d1"])
    page = api.get("/api/jobs", params={"limit": 200}).json()
    emails = {j["account_email"] for j in page["items"]}
    assert any(e.startswith("alice@") for e in emails)
    assert not any(e.startswith("carol@") for e in emails)
    failed = api.get(
        "/api/jobs", params={"status": "failed", "account_id": str(world["alice"])}
    ).json()
    assert failed["total"] == 1
    assert failed["items"][0]["error"] == "carbonio busy"
    assert failed["items"][0]["backup_type"] == "incremental"


def test_dashboard_scoped_and_global(
    api: TestClient, engine: Engine, world: dict[str, uuid.UUID]
) -> None:
    _login(api, engine, Role.DOMAIN_ADMIN, world["d1"])
    d = api.get("/api/dashboard").json()
    assert d["accounts"] == 2
    assert d["protected_24h"] == 0  # alice's recent snapshot is partial
    assert d["never_backed_up"] == 1  # bob
    assert d["partial_last"] == 1
    assert d["jobs_queued"] == 1
    assert d["jobs_failed_24h"] == 1
    assert d["stored_bytes"] == 1050
    assert d["agents_active"] is None  # agents are a global concern

    _login(api, engine, Role.SUPER_ADMIN, None)
    g = api.get("/api/dashboard").json()
    assert g["accounts"] >= 3
    assert g["protected_24h"] >= 1  # carol
    assert g["agents_active"] is not None


def test_backup_request_returns_account_email(
    api: TestClient, engine: Engine, world: dict[str, uuid.UUID]
) -> None:
    _login(api, engine, Role.DOMAIN_ADMIN, world["d1"])
    csrf = api.cookies.get("__Host-carborewind_csrf")
    r = api.post(
        "/api/backups",
        json={"account_id": str(world["bob"]), "backup_type": "full"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert r.status_code == 202, r.text
    assert r.json()["account_email"].startswith("bob@")
    assert r.json()["backup_type"] == "full"
