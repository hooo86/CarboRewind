"""Two-factor authentication (TOTP), recovery codes, security policy, account management."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from carborewind_api.config import Settings
from carborewind_api.db.models import (
    AppSetting,
    AuditEvent,
    AuthSource,
    Role,
    RoleAssignment,
    User,
)
from carborewind_api.main import create_app
from carborewind_api.security import totp
from carborewind_api.security.passwords import hash_password

pytestmark = pytest.mark.integration
PASSWORD = "a-long-enough-password"


def test_totp_rfc6238_vectors() -> None:
    # RFC 6238 appendix B, SHA-1 secret "12345678901234567890", 8 digits -> last 6 here.
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert totp.code_at(secret, 59 // 30) == "287082"
    assert totp.code_at(secret, 1111111109 // 30) == "081804"
    now = 1111111109.0
    step = totp.verify(secret, "081804", None, now)
    assert step == 1111111109 // 30
    assert totp.verify(secret, "081804", step, now) is None  # never replayed
    assert totp.verify(secret, "000000", None, now) is None
    assert totp.verify(secret, "12", None, now) is None
    assert totp.verify(secret, totp.code_at(secret, step + 1), None, now) == step + 1  # drift
    uri = totp.provisioning_uri("ABC", "ana@lnhc.dz")
    assert uri.startswith("otpauth://totp/CarboRewind%3Aana%40lnhc.dz?secret=ABC&issuer=")
    assert totp.qr_svg(uri).lstrip().startswith("<?xml") or "<svg" in totp.qr_svg(uri)
    codes = totp.new_recovery_codes()
    assert len(codes) == 10 and len(set(codes)) == 10 and all(len(c) == 9 for c in codes)
    assert totp.hash_recovery(" AbCd-EfGh ") == totp.hash_recovery("abcd-efgh")


@pytest.fixture
def api(migrated: str, engine: Engine) -> Iterator[TestClient]:
    with Session(engine) as s, s.begin():
        s.execute(delete(AppSetting))
    settings = Settings(  # type: ignore[call-arg]
        environment="test", database_url=migrated, secret_key="z" * 40
    )
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        yield client
    with Session(engine) as s, s.begin():
        s.execute(delete(AppSetting))


def make_user(engine: Engine, role: Role = Role.SUPER_ADMIN, **kw: object) -> str:
    name = f"t{uuid.uuid4().hex[:8]}"
    with Session(engine) as s, s.begin():
        s.add(
            User(
                username=name,
                auth_source=AuthSource.LOCAL,
                password_hash=hash_password(PASSWORD),
                roles=[RoleAssignment(role=role)],
                **kw,
            )
        )
    return name


def login(api: TestClient, name: str, otp: str | None = None) -> tuple[int, dict[str, object]]:
    api.cookies.clear()
    body: dict[str, object] = {"username": name, "password": PASSWORD}
    if otp is not None:
        body["otp"] = otp
    r = api.post("/api/auth/login", json=body)
    return r.status_code, r.json()


def current_code(secret: str) -> str:
    return totp.code_at(secret, totp.current_step())


def test_enrol_login_recovery_and_disable(api: TestClient, engine: Engine) -> None:
    name = make_user(engine)
    status, session = login(api, name)
    assert status == 200 and session["totp_enrollment_required"] is False
    h = {"X-CSRF-Token": str(session["csrf_token"])}
    setup = api.post("/api/auth/2fa/setup", headers=h).json()
    assert "<svg" in setup["qr_svg"] and setup["uri"].startswith("otpauth://totp/")
    secret = setup["secret"]
    with Session(engine) as s:
        stored = s.scalar(select(User).where(User.username == name))
        assert (
            stored is not None and stored.totp_secret_enc and secret not in stored.totp_secret_enc
        )
    assert api.post("/api/auth/2fa/enable", json={"code": "000000"}, headers=h).status_code == 422
    assert (
        api.post("/api/auth/2fa/enable", json={"code": "abcd-efgh"}, headers=h).status_code == 422
    )
    r = api.post("/api/auth/2fa/enable", json={"code": current_code(secret)}, headers=h)
    assert r.status_code == 200
    codes = r.json()["recovery_codes"]
    assert len(codes) == 10
    assert api.post("/api/auth/2fa/setup", headers=h).status_code == 409
    assert api.get("/api/auth/me").json()["recovery_codes_left"] == 10

    # Password alone is no longer enough; a wrong code counts as a failure.
    status, body = login(api, name)
    assert status == 401 and body["detail"] == "otp_required"
    status, body = login(api, name, "123456")
    assert status == 401 and body["detail"] == "invalid credentials"
    # The code already used for the enrolment cannot be replayed: wait for the next step.
    next_code = totp.code_at(secret, totp.current_step() + 1)
    status, session = login(api, name, next_code)
    assert status == 200
    status, _ = login(api, name, next_code)
    assert status == 401  # replay refused
    # A recovery code works once.
    status, session = login(api, name, codes[0].upper())
    assert status == 200
    status, _ = login(api, name, codes[0])
    assert status == 401
    status, session = login(api, name, codes[3])
    h = {"X-CSRF-Token": str(session["csrf_token"])}
    assert api.get("/api/auth/me").json()["recovery_codes_left"] == 8
    # New recovery codes, then disable with a recovery code.
    assert (
        api.post("/api/auth/2fa/recovery-codes", json={"code": "999999"}, headers=h).status_code
        == 422
    )
    fresh = api.post("/api/auth/2fa/recovery-codes", json={"code": codes[1]}, headers=h)
    assert fresh.status_code == 200
    new_codes = fresh.json()["recovery_codes"]
    assert set(new_codes).isdisjoint(codes)
    with Session(engine) as s:
        events = [
            a.action for a in s.scalars(select(AuditEvent).where(AuditEvent.actor_name == name))
        ]
        assert "user.2fa_enable" in events
    assert api.post("/api/auth/2fa/disable", json={"code": "999999"}, headers=h).status_code == 422
    assert api.post("/api/auth/2fa/disable", json={"code": codes[2]}, headers=h).status_code == 422
    assert (
        api.post("/api/auth/2fa/disable", json={"code": new_codes[0]}, headers=h).status_code == 204
    )
    status, _ = login(api, name)
    assert status == 200


def test_policy_forces_enrolment(api: TestClient, engine: Engine) -> None:
    admin = make_user(engine)
    _, session = login(api, admin)
    h = {"X-CSRF-Token": str(session["csrf_token"])}
    assert (
        api.put("/api/settings/security", json={"require_2fa": "bogus"}, headers=h).status_code
        == 422
    )
    assert (
        api.put("/api/settings/security", json={"require_2fa": "admins"}, headers=h).status_code
        == 200
    )
    # The admin itself must now enrol: everything but /api/auth/* is refused.
    r = api.get("/api/dashboard")
    assert r.status_code == 403 and r.json()["detail"] == "2fa_enrollment_required"
    me = api.get("/api/auth/me").json()
    assert me["totp_enrollment_required"] is True
    secret = api.post("/api/auth/2fa/setup", headers=h).json()["secret"]
    api.post("/api/auth/2fa/enable", json={"code": current_code(secret)}, headers=h)
    assert api.get("/api/dashboard").status_code == 200
    assert api.get("/api/settings/security").json() == {"require_2fa": "admins"}
    assert api.post("/api/auth/2fa/disable", json={"code": "x"}, headers=h).status_code == 422
    # Viewers are not concerned by "admins"…
    viewer = make_user(engine, Role.VIEWER)
    status, session = login(api, viewer)
    assert status == 200 and session["totp_enrollment_required"] is False
    assert api.get("/api/dashboard").status_code == 200
    # …but they are by "all".
    _, admin_session = login(api, admin, totp.code_at(secret, totp.current_step() + 1))
    api.put(
        "/api/settings/security",
        json={"require_2fa": "all"},
        headers={"X-CSRF-Token": str(admin_session["csrf_token"])},
    )
    status, session = login(api, viewer)
    assert session["totp_enrollment_required"] is True
    assert api.get("/api/jobs").status_code == 403


def test_account_management(api: TestClient, engine: Engine) -> None:
    admin = make_user(engine)
    _, session = login(api, admin)
    h = {"X-CSRF-Token": str(session["csrf_token"])}
    me = api.get("/api/auth/me").json()
    created = api.post(
        "/api/users",
        json={"username": "Operateur1", "password": "x" * 16, "display_name": "Opé"},
        headers=h,
    )
    assert created.status_code == 201, created.text
    user = created.json()
    assert user["username"] == "operateur1" and user["totp_enabled"] is False
    uid = user["id"]
    r = api.post(f"/api/users/{uid}/roles", json={"role": "restore_operator"}, headers=h)
    assert r.status_code == 201
    # Admin resets the password: the old one stops working.
    assert api.patch(f"/api/users/{uid}", json={"password": "short"}, headers=h).status_code == 422
    assert api.patch(f"/api/users/{uid}", json={"password": "y" * 16}, headers=h).status_code == 200
    with Session(engine) as s:
        target = s.get(User, uuid.UUID(uid))
        assert target is not None and target.password_changed_at is not None
    # Admin resets a user's 2FA (lost phone).
    with Session(engine) as s, s.begin():
        target = s.get(User, uuid.UUID(uid))
        assert target is not None
        target.totp_enabled, target.totp_secret_enc, target.recovery_codes = True, "x", ["y"]
    r = api.patch(f"/api/users/{uid}", json={"reset_2fa": True}, headers=h)
    assert r.json()["totp_enabled"] is False
    # Own password change.
    bad = {"current_password": "wrong", "new_password": "z" * 16}
    assert api.post("/api/auth/password", json=bad, headers=h).status_code == 403
    weak = {"current_password": PASSWORD, "new_password": "1"}
    assert api.post("/api/auth/password", json=weak, headers=h).status_code == 422
    good = {"current_password": PASSWORD, "new_password": "z" * 16}
    assert api.post("/api/auth/password", json=good, headers=h).status_code == 204
    assert api.get("/api/auth/me").status_code == 200  # this session survives
    # Delete: not yourself; the others yes.
    assert api.delete(f"/api/users/{me['id']}", headers=h).status_code == 409
    assert api.delete(f"/api/users/{uid}", headers=h).status_code == 204
    assert api.get(f"/api/users/{uid}").status_code == 404
    # LDAP accounts: no password here.
    ldap = api.post(
        "/api/users", json={"username": "jdoe", "auth_source": "ldap"}, headers=h
    ).json()
    r = api.patch(f"/api/users/{ldap['id']}", json={"password": "y" * 16}, headers=h)
    assert r.status_code == 409
