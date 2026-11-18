"""Audit trail: listing, scoping, CSV export, chain verification and tamper detection."""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from carborewind_api import audit, cli
from carborewind_api.config import Settings
from carborewind_api.db.models import (
    ActorType,
    AuthSource,
    Domain,
    Outcome,
    Role,
    RoleAssignment,
    User,
)
from carborewind_api.main import create_app
from carborewind_api.security.passwords import hash_password

pytestmark = pytest.mark.integration

PASSWORD = "a-long-enough-password"


@pytest.fixture
def make_client(migrated: str) -> Iterator[Callable[[], TestClient]]:
    clients: list[TestClient] = []
    settings = Settings(environment="test", database_url=migrated)  # type: ignore[arg-type]

    def make() -> TestClient:
        c = TestClient(create_app(settings), base_url="https://testserver")
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)


def _login_as(
    make: Callable[[], TestClient], engine: Engine, role: Role, domain: uuid.UUID | None
) -> tuple[TestClient, User]:
    username = f"a{uuid.uuid4().hex[:10]}"
    with Session(engine, expire_on_commit=False) as s, s.begin():
        user = User(
            username=username,
            auth_source=AuthSource.LOCAL,
            password_hash=hash_password(PASSWORD),
            roles=[RoleAssignment(role=role, domain_id=domain)],
        )
        s.add(user)
    client = make()
    response = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200
    return client, user


def _domain(engine: Engine) -> uuid.UUID:
    with Session(engine) as s, s.begin():
        d = Domain(name=f"a{uuid.uuid4().hex[:8]}.test")
        s.add(d)
        s.flush()
        return d.id


async def _record_domain_event(db_url: str, domain_id: uuid.UUID, marker: str) -> None:
    engine = create_async_engine(db_url)
    async with AsyncSession(engine) as db, db.begin():
        await audit.record(
            db,
            action="restore.item",
            outcome=Outcome.SUCCESS,
            actor_type=ActorType.SYSTEM,
            actor_name=marker,
            domain_id=domain_id,
            target_type="item",
            target_id=marker,
            details={"note": '=HYPERLINK("http://evil")'},
        )
    await engine.dispose()


async def test_listing_filters_and_scope(
    make_client: Callable[[], TestClient], engine: Engine, migrated: str
) -> None:
    mine, theirs = _domain(engine), _domain(engine)
    m1, m2 = f"m-{uuid.uuid4().hex[:6]}", f"t-{uuid.uuid4().hex[:6]}"
    await _record_domain_event(migrated, mine, m1)
    await _record_domain_event(migrated, theirs, m2)

    root, _ = _login_as(make_client, engine, Role.SUPER_ADMIN, None)
    page = root.get("/api/audit", params={"actor": m1}).json()
    assert page["total"] == 1
    assert page["items"][0]["action"] == "restore.item"
    assert root.get("/api/audit", params={"action": "restore.*", "actor": m2}).json()["total"] == 1
    assert root.get("/api/audit", params={"action": "restore", "actor": m2}).json()["total"] == 0
    assert root.get("/api/audit", params={"outcome": "denied", "actor": m1}).json()["total"] == 0

    dadmin, dadmin_user = _login_as(make_client, engine, Role.DOMAIN_ADMIN, mine)
    visible = dadmin.get("/api/audit", params={"limit": 200}).json()["items"]
    actors = {e["actor_name"] for e in visible}
    assert m1 in actors
    assert m2 not in actors
    # Their own login event is visible to them although it has no domain.
    assert dadmin_user.username in actors


def test_viewer_and_operator_cannot_read_audit(
    make_client: Callable[[], TestClient], engine: Engine
) -> None:
    for role in (Role.VIEWER, Role.RESTORE_OPERATOR):
        client, _ = _login_as(make_client, engine, role, None)
        assert client.get("/api/audit").status_code == 403


async def test_csv_export_is_safe_and_audited(
    make_client: Callable[[], TestClient], engine: Engine, migrated: str
) -> None:
    domain = _domain(engine)
    marker = f"c-{uuid.uuid4().hex[:6]}"
    await _record_domain_event(migrated, domain, marker)
    client, user = _login_as(make_client, engine, Role.SUPER_ADMIN, None)
    response = client.get("/api/audit/export.csv", params={"actor": marker})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 1
    assert rows[0]["action"] == "restore.item"
    # JSON details start with "{", so no prefix; check the formula guard directly.
    from carborewind_api.api.audit import _csv_safe

    assert _csv_safe("=1+1") == "'=1+1"
    assert _csv_safe("-2") == "'-2"
    assert _csv_safe("normal") == "normal"
    assert _csv_safe(None) == ""

    exported = client.get("/api/audit", params={"action": "audit.export", "actor": user.username})
    assert exported.json()["total"] == 1


def test_verify_and_head(make_client: Callable[[], TestClient], engine: Engine) -> None:
    root, _ = _login_as(make_client, engine, Role.SUPER_ADMIN, None)
    result = root.get("/api/audit/verify").json()
    assert result["ok"] is True
    assert result["checked"] > 0
    head = root.get("/api/audit/head").json()
    assert head["head_id"] >= result["head_id"]
    assert len(head["head_hash"]) == 64

    # Domain admins can read their domain's audit but not verify the global chain.
    dadmin, _ = _login_as(make_client, engine, Role.DOMAIN_ADMIN, _domain(engine))
    assert dadmin.get("/api/audit/verify").status_code == 403
    assert dadmin.get("/api/audit/head").status_code == 403


async def test_tampering_is_detected(migrated: str) -> None:
    """The owner can bypass the trigger; the hash chain still exposes the change."""
    engine = create_async_engine(migrated)
    async with engine.connect() as conn:
        trans = await conn.begin()
        db = AsyncSession(bind=conn)
        await audit.record(
            db, action="test.tamper", outcome=Outcome.SUCCESS, actor_type=ActorType.SYSTEM
        )
        await audit.record(
            db, action="test.tamper", outcome=Outcome.SUCCESS, actor_type=ActorType.SYSTEM
        )
        assert (await audit.verify_chain(db)).ok
        await conn.execute(
            text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only")
        )
        victim = await conn.scalar(
            text("SELECT id FROM audit_events WHERE action = 'test.tamper' ORDER BY id LIMIT 1")
        )
        await conn.execute(
            text("UPDATE audit_events SET outcome = 'failure' WHERE id = :id"), {"id": victim}
        )
        result = await audit.verify_chain(db, batch=7)
        assert not result.ok
        assert result.broken_id == victim
        assert result.reason == "hash mismatch"
        await trans.rollback()  # DDL and UPDATE are transactional: nothing persists
    await engine.dispose()


def test_cli_audit_verify(migrated: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from carborewind_api.config import get_settings

    monkeypatch.setenv("CARBOREWIND_DATABASE_URL", migrated)
    get_settings.cache_clear()
    try:
        assert cli.main(["audit-verify"]) == 0
    finally:
        get_settings.cache_clear()
