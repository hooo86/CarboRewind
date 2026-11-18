"""RBAC on the HTTP API: users administration and domain-scoped inventory."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from carborewind_api.config import Settings
from carborewind_api.db.models import (
    Account,
    AccountStatus,
    AuditEvent,
    AuthSource,
    Domain,
    Role,
    RoleAssignment,
    User,
)
from carborewind_api.main import create_app
from carborewind_api.security.passwords import hash_password

pytestmark = pytest.mark.integration

PASSWORD = "a-long-enough-password"
Grants = list[tuple[Role, uuid.UUID | None]]


@pytest.fixture
def app_client(migrated: str) -> Iterator[Callable[[], TestClient]]:
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


@pytest.fixture
def domains(engine: Engine) -> tuple[uuid.UUID, uuid.UUID]:
    """Two fresh domains with accounts (one deleted)."""
    ids = []
    with Session(engine) as s, s.begin():
        for _ in range(2):
            name = f"d{uuid.uuid4().hex[:8]}.test"
            domain = Domain(name=name)
            s.add(domain)
            s.flush()
            for local, status in (("alice", "active"), ("bob", "active"), ("old", "deleted")):
                s.add(
                    Account(
                        domain_id=domain.id,
                        email=f"{local}@{name}",
                        carbonio_id=str(uuid.uuid4()),
                        status=AccountStatus(status),
                    )
                )
            ids.append(domain.id)
    return ids[0], ids[1]


def _user(engine: Engine, grants: Grants) -> tuple[uuid.UUID, str]:
    username = f"u{uuid.uuid4().hex[:10]}"
    with Session(engine) as s, s.begin():
        user = User(
            username=username,
            auth_source=AuthSource.LOCAL,
            password_hash=hash_password(PASSWORD),
            roles=[RoleAssignment(role=r, domain_id=d) for r, d in grants],
        )
        s.add(user)
        s.flush()
        return user.id, username


def _login(client: TestClient, username: str) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def _as(
    make: Callable[[], TestClient], engine: Engine, grants: Grants
) -> tuple[TestClient, dict[str, str], uuid.UUID]:
    client = make()
    user_id, username = _user(engine, grants)
    return client, _login(client, username), user_id


# ------------------------------------------------------------------ users administration

ROLES = [Role.SUPER_ADMIN, Role.DOMAIN_ADMIN, Role.RESTORE_OPERATOR, Role.VIEWER]


@pytest.mark.parametrize("role", ROLES)
def test_only_super_admin_manages_users(
    app_client: Callable[[], TestClient],
    engine: Engine,
    domains: tuple[uuid.UUID, uuid.UUID],
    role: Role,
) -> None:
    domain = None if role is Role.SUPER_ADMIN else domains[0]
    client, csrf, _ = _as(app_client, engine, [(role, domain)])
    expected = 200 if role is Role.SUPER_ADMIN else 403
    assert client.get("/api/users").status_code == expected
    create = client.post(
        "/api/users",
        json={"username": f"n{uuid.uuid4().hex[:8]}", "password": PASSWORD},
        headers=csrf,
    )
    assert create.status_code == (201 if role is Role.SUPER_ADMIN else 403)


def test_denied_access_is_audited(app_client: Callable[[], TestClient], engine: Engine) -> None:
    client, _, user_id = _as(app_client, engine, [(Role.VIEWER, None)])
    assert client.get("/api/users").status_code == 403
    with Session(engine) as s:
        event = s.scalar(
            select(AuditEvent)
            .where(AuditEvent.actor_id == str(user_id), AuditEvent.action == "authz.denied")
            .order_by(AuditEvent.id.desc())
        )
    assert event is not None
    assert event.details["permission"] == "users:manage"
    assert event.details["path"] == "/api/users"


def test_unauthenticated_is_401(app_client: Callable[[], TestClient]) -> None:
    client = app_client()
    assert client.get("/api/users").status_code == 401
    assert client.get("/api/domains").status_code == 401


def test_user_lifecycle(
    app_client: Callable[[], TestClient],
    engine: Engine,
    domains: tuple[uuid.UUID, uuid.UUID],
) -> None:
    admin, csrf, _ = _as(app_client, engine, [(Role.SUPER_ADMIN, None)])
    username = f"Op-{uuid.uuid4().hex[:6]}"
    created = admin.post(
        "/api/users", json={"username": username, "password": PASSWORD}, headers=csrf
    )
    assert created.status_code == 201
    user = created.json()
    assert user["username"] == username.lower()
    assert user["roles"] == []
    uid = user["id"]

    # Duplicate username.
    dup = admin.post("/api/users", json={"username": username, "password": PASSWORD}, headers=csrf)
    assert dup.status_code == 409

    # Without a role the user cannot log in.
    other = app_client()
    assert (
        other.post("/api/auth/login", json={"username": username, "password": PASSWORD}).status_code
        == 401
    )

    # Grant a domain-scoped role; the user can now log in and sees only that domain.
    grant = admin.post(
        f"/api/users/{uid}/roles",
        json={"role": "restore_operator", "domain_id": str(domains[0])},
        headers=csrf,
    )
    assert grant.status_code == 201
    assignment_id = grant.json()["roles"][0]["id"]
    again = admin.post(
        f"/api/users/{uid}/roles",
        json={"role": "restore_operator", "domain_id": str(domains[0])},
        headers=csrf,
    )
    assert again.status_code == 409
    _login(other, username.lower())
    visible = {d["id"] for d in other.get("/api/domains?limit=200").json()["items"]}
    assert visible == {str(domains[0])}

    # Revoking the role kills the user's sessions.
    revoke = admin.delete(f"/api/users/{uid}/roles/{assignment_id}", headers=csrf)
    assert revoke.status_code == 200
    assert other.get("/api/auth/me").status_code == 401

    # Deactivation.
    patched = admin.patch(
        f"/api/users/{uid}", json={"is_active": False, "display_name": "Op"}, headers=csrf
    )
    assert patched.status_code == 200
    assert patched.json()["is_active"] is False
    assert admin.get(f"/api/users/{uid}").json()["display_name"] == "Op"

    actions = []
    with Session(engine) as s:
        actions = list(
            s.scalars(
                select(AuditEvent.action).where(AuditEvent.target_id == uid).order_by(AuditEvent.id)
            )
        )
    assert actions == ["user.create", "user.role.grant", "user.role.revoke", "user.update"]


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"username": "x", "password": "short"}, 422),
        ({"username": "bad name!", "password": PASSWORD}, 422),
        ({"username": "ldapuser", "auth_source": "ldap", "password": PASSWORD}, 422),
        ({"username": "nopass"}, 422),
        ({"username": "y", "password": PASSWORD, "is_admin": True}, 422),
    ],
)
def test_create_user_validation(
    app_client: Callable[[], TestClient], engine: Engine, body: dict[str, object], status: int
) -> None:
    admin, csrf, _ = _as(app_client, engine, [(Role.SUPER_ADMIN, None)])
    assert admin.post("/api/users", json=body, headers=csrf).status_code == status


def test_ldap_user_preregistration(app_client: Callable[[], TestClient], engine: Engine) -> None:
    admin, csrf, _ = _as(app_client, engine, [(Role.SUPER_ADMIN, None)])
    response = admin.post(
        "/api/users",
        json={"username": f"l{uuid.uuid4().hex[:6]}", "auth_source": "ldap"},
        headers=csrf,
    )
    assert response.status_code == 201
    assert response.json()["auth_source"] == "ldap"


def test_super_admin_role_cannot_be_domain_scoped(
    app_client: Callable[[], TestClient],
    engine: Engine,
    domains: tuple[uuid.UUID, uuid.UUID],
) -> None:
    admin, csrf, _ = _as(app_client, engine, [(Role.SUPER_ADMIN, None)])
    uid, _ = _user(engine, [])
    response = admin.post(
        f"/api/users/{uid}/roles",
        json={"role": "super_admin", "domain_id": str(domains[0])},
        headers=csrf,
    )
    assert response.status_code == 422
    unknown = admin.post(
        f"/api/users/{uid}/roles",
        json={"role": "viewer", "domain_id": str(uuid.uuid4())},
        headers=csrf,
    )
    assert unknown.status_code == 422


def test_cannot_deactivate_self(app_client: Callable[[], TestClient], engine: Engine) -> None:
    admin, csrf, admin_id = _as(app_client, engine, [(Role.SUPER_ADMIN, None)])
    response = admin.patch(f"/api/users/{admin_id}", json={"is_active": False}, headers=csrf)
    assert response.status_code == 409


def test_last_super_admin_is_protected(
    app_client: Callable[[], TestClient], engine: Engine
) -> None:
    # Deactivate every other super-admin first so that ours is the last one.
    admin, csrf, admin_id = _as(app_client, engine, [(Role.SUPER_ADMIN, None)])
    with Session(engine) as s, s.begin():
        others = s.scalars(
            select(User)
            .join(RoleAssignment)
            .where(RoleAssignment.role == Role.SUPER_ADMIN, User.id != admin_id)
        ).all()
        for other in others:
            other.is_active = False
    me = admin.get(f"/api/users/{admin_id}").json()
    assignment = me["roles"][0]["id"]
    response = admin.delete(f"/api/users/{admin_id}/roles/{assignment}", headers=csrf)
    assert response.status_code == 409
    assert response.json()["detail"] == "last active super_admin"


def test_unlock_user(app_client: Callable[[], TestClient], engine: Engine) -> None:
    from datetime import UTC, datetime, timedelta

    admin, csrf, _ = _as(app_client, engine, [(Role.SUPER_ADMIN, None)])
    uid, _ = _user(engine, [(Role.VIEWER, None)])
    with Session(engine) as s, s.begin():
        user = s.get(User, uid)
        assert user is not None
        user.failed_logins = 7
        user.locked_until = datetime.now(UTC) + timedelta(hours=1)
    body = admin.patch(f"/api/users/{uid}", json={"unlock": True}, headers=csrf).json()
    assert body["locked_until"] is None


def test_unknown_user_404(app_client: Callable[[], TestClient], engine: Engine) -> None:
    admin, _, _ = _as(app_client, engine, [(Role.SUPER_ADMIN, None)])
    assert admin.get(f"/api/users/{uuid.uuid4()}").status_code == 404


# ------------------------------------------------------------------ domain-scoped inventory


def test_global_reader_sees_all_domains(
    app_client: Callable[[], TestClient],
    engine: Engine,
    domains: tuple[uuid.UUID, uuid.UUID],
) -> None:
    client, _, _ = _as(app_client, engine, [(Role.VIEWER, None)])
    page = client.get("/api/domains?limit=200").json()
    ids = {d["id"] for d in page["items"]}
    assert {str(domains[0]), str(domains[1])} <= ids
    counts = {d["id"]: d["account_count"] for d in page["items"]}
    assert counts[str(domains[0])] == 2  # deleted account not counted


def test_domain_admin_is_confined(
    app_client: Callable[[], TestClient],
    engine: Engine,
    domains: tuple[uuid.UUID, uuid.UUID],
) -> None:
    mine, theirs = domains
    client, _, _user_id = _as(app_client, engine, [(Role.DOMAIN_ADMIN, mine)])
    page = client.get("/api/domains").json()
    assert [d["id"] for d in page["items"]] == [str(mine)]
    assert page["total"] == 1

    accounts = client.get(f"/api/domains/{mine}/accounts").json()
    assert sorted(a["email"].split("@")[0] for a in accounts["items"]) == ["alice", "bob"]
    with_deleted = client.get(f"/api/domains/{mine}/accounts?include_deleted=true").json()
    assert with_deleted["total"] == 3

    assert client.get(f"/api/domains/{theirs}/accounts").status_code == 403
    # Unknown domains are also 403 for scoped users (no existence oracle).
    assert client.get(f"/api/domains/{uuid.uuid4()}/accounts").status_code == 403


def test_account_search_is_literal(
    app_client: Callable[[], TestClient],
    engine: Engine,
    domains: tuple[uuid.UUID, uuid.UUID],
) -> None:
    client, _, _ = _as(app_client, engine, [(Role.SUPER_ADMIN, None)])
    d = domains[0]
    assert client.get(f"/api/domains/{d}/accounts?q=ALI").json()["total"] == 1
    assert client.get(f"/api/domains/{d}/accounts?q=%25").json()["total"] == 0
    assert client.get(f"/api/domains/{d}/accounts?q=_").json()["total"] == 0
    assert client.get(f"/api/domains/{uuid.uuid4()}/accounts").status_code == 404


def test_pagination_bounds(app_client: Callable[[], TestClient], engine: Engine) -> None:
    client, _, _ = _as(app_client, engine, [(Role.SUPER_ADMIN, None)])
    assert client.get("/api/domains?limit=0").status_code == 422
    assert client.get("/api/domains?limit=201").status_code == 422
    assert client.get("/api/users?offset=-1").status_code == 422
