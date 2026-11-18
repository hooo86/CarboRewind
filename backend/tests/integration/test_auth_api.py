"""Authentication endpoints against a real PostgreSQL."""

from __future__ import annotations

import io
import itertools
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from carborewind_api import cli
from carborewind_api.api.deps import CSRF_COOKIE, SESSION_COOKIE
from carborewind_api.audit import chain_hash, event_payload
from carborewind_api.config import Settings
from carborewind_api.db.models import (
    AuditEvent,
    AuthSource,
    Role,
    RoleAssignment,
    User,
    UserSession,
)
from carborewind_api.main import create_app
from carborewind_api.security.passwords import hash_password

from ..conftest import Factory, SettingsFactory

pytestmark = pytest.mark.integration

PASSWORD = "a-long-enough-password"


def _settings(db_url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {"environment": "test", "database_url": db_url}
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
def client(migrated: str) -> Iterator[TestClient]:
    with TestClient(create_app(_settings(migrated)), base_url="https://testserver") as c:
        yield c


def _make_user(
    engine: Engine,
    *,
    role: Role | None = Role.SUPER_ADMIN,
    active: bool = True,
    source: AuthSource = AuthSource.LOCAL,
) -> str:
    username = f"user-{uuid.uuid4().hex[:8]}"
    with Session(engine) as s, s.begin():
        user = User(
            username=username,
            auth_source=source,
            password_hash=hash_password(PASSWORD) if source is AuthSource.LOCAL else None,
            is_active=active,
        )
        if role is not None:
            user.roles = [RoleAssignment(role=role)]
        s.add(user)
    return username


def _audit(engine: Engine, username: str) -> list[AuditEvent]:
    with Session(engine) as s:
        return list(
            s.scalars(
                select(AuditEvent).where(AuditEvent.actor_name == username).order_by(AuditEvent.id)
            )
        )


def _login(client: TestClient, username: str, password: str = PASSWORD):  # type: ignore[no-untyped-def]
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_login_me_logout(client: TestClient, engine: Engine) -> None:
    username = _make_user(engine)
    response = _login(client, username.upper())  # usernames are case-insensitive
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["username"] == username
    assert body["user"]["roles"] == [{"role": "super_admin", "domain_id": None}]

    set_cookie = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in set_cookie if c.startswith(SESSION_COOKIE))
    assert "HttpOnly" in session_cookie
    assert "Secure" in session_cookie
    assert "SameSite=strict" in session_cookie
    csrf_cookie = next(c for c in set_cookie if c.startswith(CSRF_COOKIE))
    assert "HttpOnly" not in csrf_cookie

    assert client.get("/api/auth/me").json()["username"] == username

    # Unsafe method without / with a wrong CSRF token is refused.
    assert client.post("/api/auth/logout").status_code == 403
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": "nope"}).status_code == 403

    csrf = body["csrf_token"]
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
    client.cookies.clear()
    assert client.get("/api/auth/me").status_code == 401

    actions = [(e.action, e.outcome) for e in _audit(engine, username)]
    assert actions == [("auth.login", "success"), ("auth.logout", "success")]


def test_session_token_not_stored_in_clear(client: TestClient, engine: Engine) -> None:
    username = _make_user(engine)
    _login(client, username)
    token = client.cookies[SESSION_COOKIE]
    with Session(engine) as s:
        hashes = s.scalars(select(UserSession.token_hash)).all()
    assert token not in hashes
    assert len(token) >= 43  # 32 random bytes


def test_failures_are_generic_and_audited(client: TestClient, engine: Engine) -> None:
    username = _make_user(engine)
    wrong = _login(client, username, "not-the-password")
    unknown = _login(client, f"ghost-{uuid.uuid4().hex[:6]}", "not-the-password")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "invalid credentials"}
    events = _audit(engine, username)
    assert [(e.outcome, e.details["reason"]) for e in events] == [("failure", "bad_credentials")]


def test_lockout_after_repeated_failures(migrated: str, engine: Engine) -> None:
    settings = _settings(migrated, login_max_failures=3)
    username = _make_user(engine)
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        for _ in range(3):
            assert _login(client, username, "not-the-password").status_code == 401
        # Correct password is refused while locked.
        assert _login(client, username).status_code == 401
    with Session(engine) as s:
        user = s.scalar(select(User).where(User.username == username))
        assert user is not None
        assert user.failed_logins == 3
        assert user.locked_until is not None
    assert _audit(engine, username)[-1].details["reason"] == "locked"

    # Once the lock expired, a successful login resets the counter.
    with Session(engine) as s, s.begin():
        s.execute(
            update(User)
            .where(User.username == username)
            .values(locked_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        assert _login(client, username).status_code == 200
    with Session(engine) as s:
        assert s.scalar(select(User.failed_logins).where(User.username == username)) == 0


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [({"role": None}, "no_role"), ({"active": False}, "inactive")],
)
def test_denied_without_role_or_when_inactive(
    client: TestClient, engine: Engine, kwargs: dict[str, object], reason: str
) -> None:
    username = _make_user(engine, **kwargs)  # type: ignore[arg-type]
    assert _login(client, username).status_code == 401
    assert _audit(engine, username)[-1].details["reason"] == reason


def test_login_requires_json(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        content='{"username":"a","password":"b"}',
        headers={"content-type": "text/plain"},
    )
    assert response.status_code in (415, 422)
    response = client.post(
        "/api/auth/login",
        data={"username": "a", "password": "b"},
    )
    assert response.status_code in (415, 422)


def test_login_rejects_unknown_fields(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login", json={"username": "a", "password": "b", "role": "super_admin"}
    )
    assert response.status_code == 422


def test_rate_limit_per_ip(migrated: str) -> None:
    settings = _settings(migrated, login_ip_limit=2)
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        codes = [_login(client, "nobody", "whatever-pass").status_code for _ in range(3)]
    assert codes == [401, 401, 429]


def test_idle_session_expires(client: TestClient, engine: Engine) -> None:
    username = _make_user(engine)
    _login(client, username)
    with Session(engine) as s, s.begin():
        user_id = s.scalar(select(User.id).where(User.username == username))
        s.execute(
            update(UserSession)
            .where(UserSession.user_id == user_id)
            .values(last_seen_at=datetime.now(UTC) - timedelta(hours=2))
        )
    assert client.get("/api/auth/me").status_code == 401


def test_absolute_expiry(client: TestClient, engine: Engine) -> None:
    username = _make_user(engine)
    _login(client, username)
    with Session(engine) as s, s.begin():
        user_id = s.scalar(select(User.id).where(User.username == username))
        s.execute(
            update(UserSession)
            .where(UserSession.user_id == user_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert client.get("/api/auth/me").status_code == 401


def test_ldap_first_login_provisions_user_without_role(
    migrated: str,
    engine: Engine,
    ldap_factory: Factory,
    ldap_settings: SettingsFactory,
) -> None:
    from carborewind_api.auth.ldap import LdapAuthenticator

    app = create_app(_settings(migrated))
    with TestClient(app, base_url="https://testserver") as client:
        app.state.ldap = LdapAuthenticator(ldap_settings(), ldap_factory)
        with Session(engine) as s, s.begin():  # isolate from previous runs
            s.query(User).filter(User.username == "alice").delete()
        assert _login(client, "alice", "alice-password-123").status_code == 401
        with Session(engine) as s, s.begin():
            alice = s.scalar(select(User).where(User.username == "alice"))
            assert alice is not None
            assert alice.auth_source == AuthSource.LDAP
            assert alice.email == "alice@example.test"
            assert alice.password_hash is None
            s.add(RoleAssignment(user_id=alice.id, role=Role.VIEWER))
        assert _login(client, "alice", "alice-password-123").status_code == 200
        assert _login(client, "alice", "wrong").status_code == 401


def test_audit_chain_links_and_verifies(client: TestClient, engine: Engine) -> None:
    username = _make_user(engine)
    _login(client, username, "bad-password-1")
    _login(client, username)
    with Session(engine) as s:
        events = list(s.scalars(select(AuditEvent).order_by(AuditEvent.id)))
    for previous, current in itertools.pairwise(events):
        assert current.prev_hash == previous.hash
    for event in events:
        assert event.hash == chain_hash(event.prev_hash, event_payload(event))


def test_cli_create_user(migrated: str, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    from carborewind_api.config import get_settings

    username = f"Cli-{uuid.uuid4().hex[:6]}"
    monkeypatch.setenv("CARBOREWIND_DATABASE_URL", migrated)
    get_settings.cache_clear()
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert cli.main(["create-user", username, "--role", "restore_operator"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert cli.main(["create-user", f"{username}-2"]) == 2
    get_settings.cache_clear()
    with Session(engine) as s:
        user = s.scalar(select(User).where(User.username == username.lower()))
        assert user is not None
        assert [r.role for r in user.roles] == [Role.RESTORE_OPERATOR]
