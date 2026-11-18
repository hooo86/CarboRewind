"""Account list with every field, Odoo-like filters, selections (bulk backup, policy)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
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
    PolicyAccount,
    Role,
    RoleAssignment,
    Snapshot,
    SnapshotStatus,
    TargetType,
    User,
)
from carborewind_api.main import create_app
from carborewind_api.security.passwords import hash_password

from .test_scheduler import NOW, run_tick

pytestmark = pytest.mark.integration
PASSWORD = "a-long-enough-password"


@pytest.fixture
def api(migrated: str) -> Iterator[TestClient]:
    settings = Settings(environment="test", database_url=migrated)  # type: ignore[arg-type]
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        yield client


def login(api: TestClient, engine: Engine, role: Role, domain: uuid.UUID | None) -> dict[str, str]:
    name = f"l{uuid.uuid4().hex[:8]}"
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


@pytest.fixture
def world(engine: Engine) -> dict[str, Any]:
    """Domain A: ana (admin, vip, 90% full, backed up), ben (never), old (deleted).
    Domain B: cy. A policy covers domain A."""
    now = datetime.now(UTC)
    tag = uuid.uuid4().hex[:6]
    with Session(engine) as s, s.begin():
        a = Domain(name=f"a{tag}.test")
        b = Domain(name=f"b{tag}.test")
        s.add_all([a, b])
        s.flush()

        def acc(domain: Domain, local: str, **kw: Any) -> Account:
            row = Account(
                domain_id=domain.id,
                email=f"{local}@{domain.name}",
                carbonio_id=uuid.uuid4().hex,
                status=kw.pop("status", AccountStatus.ACTIVE),
                **kw,
            )
            s.add(row)
            return row

        ana = acc(
            a,
            "ana",
            display_name="Ana Lopez",
            first_name="Ana",
            last_name="Lopez",
            aliases=[f"direction@{a.name}"],
            cos="vip",
            quota_bytes=1000,
            used_bytes=900,
            is_admin=True,
            last_login_at=now - timedelta(days=2),
            carbonio_created_at=datetime(2024, 1, 15, tzinfo=UTC),
        )
        ben = acc(a, "ben", cos="default", quota_bytes=0, used_bytes=50)
        old = acc(a, "old", status=AccountStatus.DELETED)
        cy = acc(b, "cy", cos="default", used_bytes=10)
        s.flush()
        s.add(
            Snapshot(
                account_id=ana.id,
                seq=1,
                backup_type=BackupType.FULL,
                status=SnapshotStatus.PARTIAL,
                taken_at=now - timedelta(hours=3),
                manifest_ref="m",
                size_bytes=4242,
            )
        )
        s.add(
            Policy(
                name=f"nightly-{tag}",
                target_type=TargetType.DOMAIN,
                target_id=a.id,
                backup_type=BackupType.INCREMENTAL,
                schedule="0 1 * * *",
                last_run_at=now,
            )
        )
        return {
            "a": a.id,
            "b": b.id,
            "ana": ana.id,
            "ben": ben.id,
            "old": old.id,
            "cy": cy.id,
            "tag": tag,
            "domain_a": a.name,
        }


def emails(api: TestClient, **params: Any) -> list[str]:
    r = api.get("/api/accounts", params=params)
    assert r.status_code == 200, r.text
    return [i["email"].split("@")[0] for i in r.json()["items"]]


def test_list_has_every_field(api: TestClient, engine: Engine, world: dict[str, Any]) -> None:
    login(api, engine, Role.DOMAIN_ADMIN, world["a"])
    items = {i["email"].split("@")[0]: i for i in api.get("/api/accounts").json()["items"]}
    assert set(items) == {"ana", "ben"}  # own domain, deleted hidden
    ana = items["ana"]
    assert ana["display_name"] == "Ana Lopez" and ana["is_admin"] is True
    assert ana["aliases"] == [f"direction@{world['domain_a']}"]
    assert (ana["cos"], ana["quota_bytes"], ana["used_bytes"], ana["usage_pct"]) == (
        "vip",
        1000,
        900,
        90.0,
    )
    assert ana["last_backup_status"] == "partial" and ana["stored_bytes"] == 4242
    assert ana["snapshot_count"] == 1
    assert ana["policies"] == [f"nightly-{world['tag']}"]
    assert ana["domain"] == world["domain_a"]
    assert items["ben"]["usage_pct"] is None  # unlimited quota
    assert items["ben"]["last_backup_at"] is None


def test_filters_on_every_type(api: TestClient, engine: Engine, world: dict[str, Any]) -> None:
    login(api, engine, Role.SUPER_ADMIN, None)
    d = f"domain:eq:{world['domain_a']}"
    assert emails(api, q="lopez", f=[d]) == ["ana"]
    assert emails(api, q="direction", f=[d]) == ["ana"]  # alias
    assert emails(api, f=[d, "display_name:contains:ANA"]) == ["ana"]
    assert emails(api, f=[d, "display_name:not_set"]) == ["ben"]
    assert emails(api, f=[d, "display_name:set"]) == ["ana"]
    assert emails(api, f=[d, "display_name:not_contains:ana"]) == ["ben"]
    assert emails(api, f=[d, "display_name:ne:ana lopez"]) == ["ben"]
    assert emails(api, f=[d, "usage_pct:ge:80"]) == ["ana"]
    assert emails(api, f=[d, "used_bytes:lt:100"]) == ["ben"]
    assert emails(api, f=[d, "used_bytes:eq:50"]) == ["ben"]
    assert emails(api, f=[d, "used_bytes:ne:50"]) == ["ana"]
    assert emails(api, f=[d, "used_bytes:gt:60", "used_bytes:le:900"]) == ["ana"]
    assert emails(api, f=[d, "quota_bytes:not_set"]) == []
    assert emails(api, f=[d, "last_login_at:within_days:7"]) == ["ana"]
    assert emails(api, f=[d, "last_login_at:older_days:1"]) == ["ana"]
    assert emails(api, f=[d, "carbonio_created_at:before:2024-06-01"]) == ["ana"]
    assert emails(api, f=[d, "carbonio_created_at:after:2023-12-31"]) == ["ana"]
    assert emails(api, f=[d, "last_backup_at:not_set"]) == ["ben"]
    assert emails(api, f=[d, "last_backup_status:in:partial"]) == ["ana"]
    assert emails(api, f=[d, "last_backup_status:not_in:complete"]) == ["ana", "ben"]
    assert emails(api, f=[d, "cos:in:vip,default"]) == ["ana", "ben"]
    assert emails(api, f=[d, "is_admin:is:true"]) == ["ana"]
    assert emails(api, f=[d, "has_policy:is:true"]) == ["ana", "ben"]
    assert emails(api, f=[f"domain:eq:b{world['tag']}.test", "has_policy:is:false"]) == ["cy"]
    assert emails(api, f=[d, f"policy:contains:nightly-{world['tag']}"]) == ["ana", "ben"]
    assert emails(api, f=[d, "snapshot_count:ge:1"]) == ["ana"]
    assert emails(api, f=[d, "stored_bytes:gt:0"]) == ["ana"]
    # Deleted accounts appear only when the status filter asks for them.
    assert emails(api, f=[d, "status:in:deleted"]) == ["old"]
    assert emails(api, f=[d, "status:not_in:deleted"]) == ["ana", "ben"]
    # Sorting and paging.
    assert emails(api, f=[d], sort="used_bytes", order="desc") == ["ana", "ben"]
    page = api.get("/api/accounts", params={"f": [d], "limit": 1, "offset": 1}).json()
    assert page["total"] == 2 and len(page["items"]) == 1
    for bad in (
        {"f": ["nope:eq:1"]},
        {"f": ["used_bytes:contains:1"]},
        {"f": ["used_bytes:gt:abc"]},
        {"f": ["is_admin:is:maybe"]},
        {"f": ["cos:in:"]},
        {"f": ["last_login_at:before:yesterday"]},
        {"sort": "password_hash"},
    ):
        assert api.get("/api/accounts", params=bad).status_code == 422, bad


def test_facets_are_scoped(api: TestClient, engine: Engine, world: dict[str, Any]) -> None:
    login(api, engine, Role.VIEWER, world["b"])
    facets = api.get("/api/accounts/facets").json()
    assert [d["name"] for d in facets["domains"]] == [f"b{world['tag']}.test"]
    assert facets["cos"] == ["default"]
    assert facets["fields"]["usage_pct"] == "number"
    assert facets["fields"]["has_policy"] == "boolean"


def test_bulk_backup(api: TestClient, engine: Engine, world: dict[str, Any]) -> None:
    headers = login(api, engine, Role.DOMAIN_ADMIN, world["a"])
    with Session(engine) as s, s.begin():
        s.add(Job(kind=JobKind.BACKUP, status=JobStatus.QUEUED, account_id=world["ben"]))
    body = {"account_ids": [str(world["ana"]), str(world["ben"]), str(world["old"])]}
    r = api.post("/api/backups/bulk", json={**body, "backup_type": "full"}, headers=headers)
    assert r.status_code == 202, r.text
    assert r.json() == {"queued": 1, "skipped_busy": 1, "skipped_deleted": 1}
    with Session(engine) as s:
        job = s.scalar(
            select(Job).where(Job.account_id == world["ana"], Job.kind == JobKind.BACKUP)
        )
        assert job is not None and job.params == {"backup_type": "full", "manual": True}
    # One account outside the caller's domains: nothing is queued.
    out = {"account_ids": [str(world["ana"]), str(world["cy"])]}
    assert api.post("/api/backups/bulk", json=out, headers=headers).status_code == 403
    unknown = {"account_ids": [str(uuid.uuid4())]}
    assert api.post("/api/backups/bulk", json=unknown, headers=headers).status_code == 403


def test_policy_on_a_selection(
    api: TestClient, engine: Engine, migrated: str, world: dict[str, Any]
) -> None:
    headers = login(api, engine, Role.DOMAIN_ADMIN, world["a"])
    body = {
        "name": f"sel-{world['tag']}",
        "target_type": "accounts",
        "account_ids": [str(world["ana"]), str(world["ben"]), str(world["ana"])],
        "schedule": "30 2 * * *",
        "timezone": "UTC",
    }
    r = api.post("/api/policies", json=body, headers=headers)
    assert r.status_code == 201, r.text
    created = r.json()
    assert sorted(created["account_ids"]) == sorted([str(world["ana"]), str(world["ben"])])
    assert created["target_id"] is None
    # Shown on the accounts it covers.
    names = {
        i["email"].split("@")[0]: i["policies"] for i in api.get("/api/accounts").json()["items"]
    }
    assert body["name"] in names["ana"] and body["name"] in names["ben"]

    # The scheduler queues one backup per selected account.
    with Session(engine) as s, s.begin():
        p = s.get(Policy, uuid.UUID(created["id"]))
        assert p is not None
        p.last_run_at = NOW - timedelta(days=1)
    run_tick(migrated, retention=False)
    with Session(engine) as s:
        jobs = s.scalars(select(Job).where(Job.policy_id == uuid.UUID(created["id"])))
        assert {j.account_id for j in jobs} == {world["ana"], world["ben"]}
    # Edit the selection; a domain admin cannot add an account of another domain.
    pid = created["id"]
    r = api.patch(
        f"/api/policies/{pid}", json={"account_ids": [str(world["ben"])]}, headers=headers
    )
    assert r.status_code == 200 and r.json()["account_ids"] == [str(world["ben"])]
    bad = {"account_ids": [str(world["cy"])]}
    assert api.patch(f"/api/policies/{pid}", json=bad, headers=headers).status_code == 403
    assert (
        api.patch(f"/api/policies/{pid}", json={"account_ids": []}, headers=headers).status_code
        == 422
    )
    with Session(engine) as s:
        assert {r.account_id for r in s.scalars(select(PolicyAccount))} >= {world["ben"]}
    for invalid in (
        {**body, "name": "x1", "account_ids": []},
        {**body, "name": "x2", "target_type": "domain", "target_id": str(world["a"])},
        {**body, "name": "x3", "target_id": str(world["a"])},
    ):
        assert api.post("/api/policies", json=invalid, headers=headers).status_code == 422
    other = {**body, "name": "x4", "account_ids": [str(world["ana"]), str(world["cy"])]}
    assert api.post("/api/policies", json=other, headers=headers).status_code == 403
    # Another domain's viewer does not see the selection policy; a patch on a non-selection
    # policy cannot set accounts.
    login(api, engine, Role.VIEWER, world["b"])
    assert body["name"] not in {p["name"] for p in api.get("/api/policies").json()}
    admin = login(api, engine, Role.SUPER_ADMIN, None)
    nightly = next(p for p in api.get("/api/policies").json() if p["name"].startswith("nightly-"))
    assert (
        api.patch(
            f"/api/policies/{nightly['id']}",
            json={"account_ids": [str(world["ana"])]},
            headers=admin,
        ).status_code
        == 422
    )
    deleted = api.delete(f"/api/policies/{pid}", headers=admin)
    assert deleted.status_code == 204
    with Session(engine) as s:
        assert not s.scalars(
            select(PolicyAccount).where(PolicyAccount.policy_id == uuid.UUID(pid))
        ).all()
