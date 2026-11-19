"""SMTP notifications: settings API, encryption, outbox delivery, periodic alerts."""

from __future__ import annotations

import asyncio
import smtplib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from carborewind_api.config import Settings
from carborewind_api.db.models import (
    Account,
    AccountStatus,
    Agent,
    AgentStatus,
    AppSetting,
    AuditEvent,
    AuthSource,
    Domain,
    Notification,
    Role,
    RoleAssignment,
    User,
)
from carborewind_api.db.session import make_engine
from carborewind_api.main import create_app
from carborewind_api.notifications import (
    SecretKeyError,
    SmtpConfig,
    decrypt,
    deliver_pending,
    encrypt,
    notify,
    periodic_alerts,
)
from carborewind_api.security.passwords import hash_password

pytestmark = pytest.mark.integration
PASSWORD = "a-long-enough-password"
KEY = "k" * 40


class FakeSMTP:
    """Stands in for smtplib.SMTP / SMTP_SSL; records what would be sent."""

    sent: ClassVar[list[EmailMessage]] = []
    events: ClassVar[list[tuple[str, Any]]] = []
    fail_with: ClassVar[Exception | None] = None
    down: ClassVar[set[str]] = set()  # hosts that refuse connections

    def __init__(self, host: str, port: int, timeout: float = 0, context: Any = None) -> None:
        FakeSMTP.events.append(("connect", (host, port, "ssl" if context else "plain")))
        if host in FakeSMTP.down:
            raise ConnectionRefusedError(111, "Connection refused")
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        FakeSMTP.events.append(("quit", None))

    def ehlo(self) -> None:
        pass

    def starttls(self, context: Any = None) -> None:
        FakeSMTP.events.append(("starttls", None))

    def login(self, user: str, password: str) -> None:
        if password != "smtp-secret":
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        FakeSMTP.events.append(("login", user))

    def send_message(self, msg: EmailMessage) -> None:
        FakeSMTP.sent.append(msg)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FakeSMTP]]:
    FakeSMTP.sent, FakeSMTP.events, FakeSMTP.fail_with = [], [], None
    FakeSMTP.down = set()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    yield FakeSMTP


@pytest.fixture
def clean(engine: Engine) -> Iterator[None]:
    with Session(engine) as s, s.begin():
        s.execute(delete(Notification))
        s.execute(delete(AppSetting))
    yield
    with Session(engine) as s, s.begin():
        s.execute(delete(AppSetting))


def make_api(url: str, secret: str | None) -> TestClient:
    settings = Settings(environment="test", database_url=url, secret_key=secret)  # type: ignore[arg-type]
    return TestClient(create_app(settings), base_url="https://testserver")


def login(api: TestClient, engine: Engine, role: Role) -> dict[str, str]:
    name = f"n{uuid.uuid4().hex[:8]}"
    with Session(engine) as s, s.begin():
        s.add(
            User(
                username=name,
                auth_source=AuthSource.LOCAL,
                password_hash=hash_password(PASSWORD),
                roles=[RoleAssignment(role=role)],
            )
        )
    r = api.post("/api/auth/login", json={"username": name, "password": PASSWORD})
    return {"X-CSRF-Token": r.json()["csrf_token"]}


SETTINGS_BODY = {
    "enabled": True,
    "host": "smtp.example.test",
    "port": 587,
    "security": "starttls",
    "username": "alerts@example.com",
    "password": "smtp-secret",
    "sender": "carborewind@example.com",
    "recipients": ["Admin@Example.com", "ops@example.com"],
    "events": {"daily_digest": False},
    "digest_hour": 7,
    "timezone": "Africa/Algiers",
    "language": "en",
}


def test_encryption_round_trip() -> None:
    settings = Settings(secret_key=KEY)  # type: ignore[call-arg]
    token = encrypt(settings, "smtp-secret")
    assert "smtp-secret" not in token
    assert decrypt(settings, token) == "smtp-secret"
    with pytest.raises(SecretKeyError):
        decrypt(Settings(secret_key="x" * 40), token)  # type: ignore[call-arg]
    with pytest.raises(SecretKeyError):
        encrypt(Settings(secret_key="short"), "x")  # type: ignore[call-arg]
    with pytest.raises(SecretKeyError):
        encrypt(Settings(), "x")


def test_settings_api(migrated: str, engine: Engine, clean: None) -> None:
    with make_api(migrated, None) as api:
        headers = login(api, engine, Role.SUPER_ADMIN)
        defaults = api.get("/api/settings/notifications").json()
        assert defaults["enabled"] is False and defaults["secret_key_configured"] is False
        # No secret key: a password cannot be stored.
        r = api.put("/api/settings/notifications", json=SETTINGS_BODY, headers=headers)
        assert r.status_code == 409
    with make_api(migrated, KEY) as api:
        headers = login(api, engine, Role.SUPER_ADMIN)
        r = api.put("/api/settings/notifications", json=SETTINGS_BODY, headers=headers)
        assert r.status_code == 200, r.text
        out = r.json()
        assert "password" not in out and out["password_set"] is True
        assert out["recipients"] == ["admin@example.com", "ops@example.com"]
        assert out["events"]["daily_digest"] is False and out["events"]["job_failed"] is True
        with Session(engine) as s:
            stored = s.get(AppSetting, "smtp")
            assert stored is not None and "smtp-secret" not in str(stored.value)
        # Omitted password keeps it; "" clears it.
        keep = {**SETTINGS_BODY, "password": None, "port": 2525}
        r = api.put("/api/settings/notifications", json=keep, headers=headers)
        assert r.json()["password_set"] is True and r.json()["port"] == 2525
        clear = {**SETTINGS_BODY, "password": ""}
        assert (
            api.put("/api/settings/notifications", json=clear, headers=headers).json()[
                "password_set"
            ]
            is False
        )
        for bad in (
            {"enabled": True, "recipients": []},
            {"host": "smtp://evil"},
            {"events": {"coffee": True}},
            {"timezone": "Mars/Olympus"},
            {"recipients": ["not-an-address"]},
            {"port": 0},
        ):
            r = api.put(
                "/api/settings/notifications", json={**SETTINGS_BODY, **bad}, headers=headers
            )
            assert r.status_code == 422, bad
        # Only settings managers.
        viewer = login(api, engine, Role.DOMAIN_ADMIN)
        assert api.get("/api/settings/notifications").status_code == 403
        assert (
            api.put("/api/settings/notifications", json=SETTINGS_BODY, headers=viewer).status_code
            == 403
        )
    with Session(engine) as s:
        assert s.scalar(select(AuditEvent).where(AuditEvent.action == "settings.notifications"))


def test_test_email(migrated: str, engine: Engine, clean: None, fake_smtp: type[FakeSMTP]) -> None:
    with make_api(migrated, KEY) as api:
        headers = login(api, engine, Role.SUPER_ADMIN)
        api.put("/api/settings/notifications", json=SETTINGS_BODY, headers=headers)
        r = api.post("/api/settings/notifications/test", json={}, headers=headers)
        assert r.json() == {"ok": True, "error": None}
        msg = fake_smtp.sent[-1]
        assert msg["Subject"] == "[CarboRewind] Test e-mail"
        assert msg["To"] == "admin@example.com, ops@example.com"
        assert ("starttls", None) in fake_smtp.events
        assert ("login", "alerts@example.com") in fake_smtp.events
        # Implicit TLS, one recipient, and an error reported back.
        api.put(
            "/api/settings/notifications",
            json={**SETTINGS_BODY, "security": "ssl", "port": 465, "password": "wrong"},
            headers=headers,
        )
        r = api.post(
            "/api/settings/notifications/test", json={"to": "me@example.com"}, headers=headers
        )
        assert r.json()["ok"] is False and "SMTPAuthenticationError" in r.json()["error"]
        assert ("connect", ("smtp.example.test", 465, "ssl")) in fake_smtp.events
        fake_smtp.fail_with = ConnectionRefusedError("refused")
        r = api.post("/api/settings/notifications/test", json={}, headers=headers)
        assert "refused" in r.json()["error"]
        fake_smtp.fail_with = None
        api.put(
            "/api/settings/notifications",
            json={
                **SETTINGS_BODY,
                "enabled": False,
                "security": "none",
                "username": "",
                "sender": "",
            },
            headers=headers,
        )
        r = api.post("/api/settings/notifications/test", json={}, headers=headers)
        assert "sender" in r.json()["error"]


def _run(url: str, fn: Any, *args: Any) -> Any:
    async def go() -> Any:
        engine = make_engine(url)
        try:
            async with async_sessionmaker(engine)() as db, db.begin():
                return await fn(db, *args)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _enable(engine: Engine, **overrides: Any) -> None:
    settings = Settings(secret_key=KEY)  # type: ignore[call-arg]
    cfg = SmtpConfig(
        enabled=True,
        host="smtp.example.test",
        port=25,
        security="none",
        sender="cr@example.test",
        recipients=["admin@example.test"],
        password_enc=encrypt(settings, "smtp-secret"),
        username="alerts",
        language="fr",
        timezone="UTC",
        digest_hour=6,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    with Session(engine) as s, s.begin():
        s.merge(AppSetting(key="smtp", value=cfg.to_json()))


def test_outbox_delivery_and_retries(
    migrated: str, engine: Engine, clean: None, fake_smtp: type[FakeSMTP]
) -> None:
    settings = Settings(secret_key=KEY)  # type: ignore[call-arg]
    now = datetime.now(UTC) + timedelta(minutes=1)  # after the rows' database timestamps
    # Disabled: nothing is queued.
    assert _run(migrated, notify, "job_failed") is False
    _enable(engine, events={"job_failed": False})
    assert _run(migrated, notify, "job_failed") is False
    _enable(engine)
    queued = _run(
        migrated,
        lambda db: notify(
            db, "job_failed", kind="backup", account="a@x.test", error="boom\r\nBcc: x", job="j1"
        ),
    )
    assert queued is True
    assert _run(migrated, lambda db: deliver_pending(db, settings, now)) == 1
    msg = fake_smtp.sent[-1]
    assert msg["Subject"] == "[CarboRewind] Échec : backup de a@x.test"
    assert "Bcc" not in msg and "boom Bcc: x" in msg.get_content()  # no header injection
    # Failure: retried with backoff, then given up.
    _run(migrated, lambda db: notify(db, "backup_partial", account="b@x.test", seq=3, errors=2))
    fake_smtp.fail_with = TimeoutError("timed out")
    later = now
    for _ in range(6):
        _run(migrated, lambda db, t=later: deliver_pending(db, settings, t))
        later += timedelta(hours=2)
    with Session(engine) as s:
        note = s.scalar(select(Notification).where(Notification.event == "backup_partial"))
        assert note is not None and note.status == "failed" and note.attempts == 6
        assert "timed out" in (note.error or "")
    # Disabled after queuing: kept pending with an error.
    fake_smtp.fail_with = None
    _run(migrated, lambda db: notify(db, "restore_failed", account="c", failed=1, restored=2))
    _enable(engine, enabled=False)
    _run(migrated, lambda db: deliver_pending(db, settings, now))
    with Session(engine) as s:
        note = s.scalar(select(Notification).where(Notification.event == "restore_failed"))
        assert note is not None and note.status == "pending" and "disabled" in (note.error or "")
    with make_api(migrated, KEY) as api:
        login(api, engine, Role.SUPER_ADMIN)
        events = [n["event"] for n in api.get("/api/settings/notifications/outbox").json()]
        assert events[:3] == ["restore_failed", "backup_partial", "job_failed"]


def test_periodic_alerts(migrated: str, engine: Engine, clean: None) -> None:
    now = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
    tag = uuid.uuid4().hex[:6]
    with Session(engine) as s, s.begin():
        s.execute(update(Agent).values(status=AgentStatus.REVOKED))
        s.execute(update(Account).values(status=AccountStatus.CLOSED))
        d = Domain(name=f"n{tag}.test")
        s.add(d)
        s.flush()
        s.add(
            Account(
                domain_id=d.id, email=f"z@{d.name}", carbonio_id=tag, status=AccountStatus.ACTIVE
            )
        )
        s.add(
            Agent(
                name=f"mx-{tag}",
                hostname="mail.example.test",
                status=AgentStatus.ACTIVE,
                last_heartbeat_at=now - timedelta(hours=2),
            )
        )
    assert _run(migrated, periodic_alerts, now) == 0  # disabled
    _enable(engine)
    assert _run(migrated, periodic_alerts, now) == 2
    assert _run(migrated, periodic_alerts, now) == 0  # sent once (dedup)
    with Session(engine) as s:
        notes = {n.event: n for n in s.scalars(select(Notification))}
        assert f"mx-{tag}" in notes["agent_offline"].subject
        assert f"z@n{tag}.test (jamais)" in notes["daily_digest"].body
    # Before the digest hour, only agents are checked.
    with Session(engine) as s, s.begin():
        s.execute(delete(Notification))
    _enable(engine, digest_hour=23, events={"agent_offline": False})
    assert _run(migrated, periodic_alerts, now) == 0


def test_fallback_smtp_server(
    migrated: str, engine: Engine, clean: None, fake_smtp: type[FakeSMTP]
) -> None:
    settings = Settings(secret_key=KEY)  # type: ignore[call-arg]
    now = datetime.now(UTC) + timedelta(minutes=1)
    _enable(
        engine,
        fallback_enabled=True,
        fallback_host="relay.backup.test",
        fallback_port=465,
        fallback_security="ssl",
        fallback_username="relay",
        fallback_password_enc=encrypt(settings, "smtp-secret"),
        fallback_sender="noreply@backup.example.com",
    )
    fake_smtp.down = {"smtp.example.test"}
    _run(
        migrated,
        lambda db: notify(db, "job_failed", kind="backup", account="a", error="e", job="j"),
    )
    assert _run(migrated, lambda db: deliver_pending(db, settings, now)) == 1
    msg = fake_smtp.sent[-1]
    assert msg["From"] == "noreply@backup.example.com"
    assert ("connect", ("relay.backup.test", 465, "ssl")) in fake_smtp.events
    assert ("login", "relay") in fake_smtp.events
    with Session(engine) as s:
        note = s.scalar(select(Notification).where(Notification.event == "job_failed"))
        assert note is not None and note.status == "sent"
        assert "fallback" in (note.error or "") and "refused" in (note.error or "")
    # Both down: retried later, with both errors.
    fake_smtp.down = {"smtp.example.test", "relay.backup.test"}
    _run(migrated, lambda db: notify(db, "backup_partial", account="b", seq=1, errors=1))
    _run(migrated, lambda db: deliver_pending(db, settings, now))
    with Session(engine) as s:
        note = s.scalar(select(Notification).where(Notification.event == "backup_partial"))
        assert note is not None and note.status == "pending"
        assert "main server" in (note.error or "") and "fallback server" in (note.error or "")

    # Settings API: password kept, never returned; test e-mail through the fallback.
    fake_smtp.down = set()
    with make_api(migrated, KEY) as api:
        h = login(api, engine, Role.SUPER_ADMIN)
        out = api.get("/api/settings/notifications", headers=h).json()
        assert out["fallback_enabled"] and out["fallback_password_set"]
        assert "fallback_password_enc" not in out
        body = {
            k: out[k]
            for k in (
                "enabled",
                "host",
                "port",
                "security",
                "username",
                "sender",
                "recipients",
                "events",
                "digest_hour",
                "timezone",
                "language",
                "fallback_enabled",
                "fallback_host",
                "fallback_port",
                "fallback_security",
                "fallback_username",
                "fallback_sender",
            )
        }
        body.update(sender="cr@example.com", recipients=["admin@example.com"])
        r = api.put("/api/settings/notifications", json=body, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["fallback_password_set"] is True
        bad = api.put("/api/settings/notifications", json={**body, "fallback_host": ""}, headers=h)
        assert bad.status_code == 422
        r = api.post("/api/settings/notifications/test", json={"server": "fallback"}, headers=h)
        assert r.json() == {"ok": True, "error": None}
        assert fake_smtp.sent[-1]["From"] == "noreply@backup.example.com"
