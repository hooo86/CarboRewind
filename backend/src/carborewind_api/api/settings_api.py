"""``/api/settings/notifications``: SMTP settings, test e-mail and outbox (``settings:manage``).

The SMTP password is write-only: ``password`` omitted or null keeps the stored one, ``""``
clears it, any other value replaces it (encrypted with ``CARBOREWIND_SECRET_KEY``).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy import select

from .. import audit
from ..authz import Permission
from ..db.models import Notification, NotificationStatus, Outcome
from ..notifications import (
    EVENTS,
    TEXTS,
    SecretKeyError,
    SmtpConfig,
    SmtpError,
    encrypt,
    load_config,
    save_config,
    send_mail,
)
from ..scheduling import CronError, zone
from .deps import AppSettings, DbSession, Principal, client_ip, require

router = APIRouter(prefix="/api/settings", tags=["settings"])

Admin = Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))]


class NotificationSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    host: str = Field(default="", max_length=253)
    port: int = Field(default=587, ge=1, le=65535)
    security: Literal["starttls", "ssl", "none"] = "starttls"
    username: str = Field(default="", max_length=320)
    password: str | None = Field(default=None, max_length=1024, repr=False)
    sender: EmailStr | Literal[""] = ""
    recipients: list[EmailStr] = Field(default_factory=list, max_length=50)
    events: dict[str, bool] = Field(default_factory=dict)
    digest_hour: int = Field(default=8, ge=0, le=23)
    timezone: str = Field(default="UTC", max_length=64)
    language: Literal["fr", "en"] = "fr"

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        value = value.strip()
        if any(c in value for c in " /\\@:\r\n"):
            raise ValueError("host is a name or address, without scheme, port or credentials")
        return value

    @field_validator("events")
    @classmethod
    def _events(cls, value: dict[str, bool]) -> dict[str, bool]:
        unknown = set(value) - set(EVENTS)
        if unknown:
            raise ValueError(f"unknown events: {', '.join(sorted(unknown))}")
        return value

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        try:
            zone(value)
        except CronError as exc:
            raise ValueError(str(exc)) from exc
        return value


class NotificationSettingsOut(BaseModel):
    enabled: bool
    host: str
    port: int
    security: str
    username: str
    password_set: bool
    sender: str
    recipients: list[str]
    events: dict[str, bool]
    digest_hour: int
    timezone: str
    language: str
    secret_key_configured: bool


def _out(cfg: SmtpConfig, settings: AppSettings) -> NotificationSettingsOut:
    return NotificationSettingsOut(
        enabled=cfg.enabled,
        host=cfg.host,
        port=cfg.port,
        security=cfg.security,
        username=cfg.username,
        password_set=bool(cfg.password_enc),
        sender=cfg.sender,
        recipients=cfg.recipients,
        events=cfg.events,
        digest_hour=cfg.digest_hour,
        timezone=cfg.timezone,
        language=cfg.language,
        secret_key_configured=settings.secret_key is not None
        and len(settings.secret_key.get_secret_value()) >= 32,
    )


@router.get("/notifications", response_model=NotificationSettingsOut)
async def get_notification_settings(
    principal: Admin, db: DbSession, settings: AppSettings
) -> NotificationSettingsOut:
    return _out(await load_config(db), settings)


@router.put("/notifications", response_model=NotificationSettingsOut)
async def put_notification_settings(
    body: NotificationSettingsIn,
    principal: Admin,
    db: DbSession,
    settings: AppSettings,
    request: Request,
) -> NotificationSettingsOut:
    current = await load_config(db)
    password_enc = current.password_enc
    if body.password == "":
        password_enc = None
    elif body.password is not None:
        try:
            password_enc = encrypt(settings, body.password)
        except SecretKeyError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if body.enabled and (not body.host or not body.sender or not body.recipients):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "server, sender and at least one recipient are required to enable notifications",
        )
    cfg = SmtpConfig(
        enabled=body.enabled,
        host=body.host,
        port=body.port,
        security=body.security,
        username=body.username,
        password_enc=password_enc,
        sender=str(body.sender),
        recipients=[str(r).lower() for r in body.recipients],
        events={**dict.fromkeys(EVENTS, True), **body.events},
        digest_hour=body.digest_hour,
        timezone=body.timezone,
        language=body.language,
    )
    await save_config(db, cfg)
    await audit.record(
        db,
        action="settings.notifications",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        details={
            "enabled": cfg.enabled,
            "host": cfg.host,
            "port": cfg.port,
            "security": cfg.security,
            "recipients": len(cfg.recipients),
            "password_changed": body.password is not None,
        },
    )
    return _out(cfg, settings)


class TestResult(BaseModel):
    ok: bool
    error: str | None = None


class TestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: EmailStr | None = None


@router.post("/notifications/test", response_model=TestResult)
async def test_notification(
    body: TestRequest,
    principal: Admin,
    db: DbSession,
    settings: AppSettings,
    request: Request,
) -> TestResult:
    """Send a test e-mail now with the saved settings (to ``to`` or every recipient)."""
    cfg = await load_config(db)
    recipients = [str(body.to)] if body.to else cfg.recipients
    texts = TEXTS.get(cfg.language, TEXTS["fr"])
    try:
        await asyncio.to_thread(
            send_mail, settings, cfg, recipients, texts["test"], texts["test_body"]
        )
        result = TestResult(ok=True)
    except (SmtpError, SecretKeyError) as exc:
        result = TestResult(ok=False, error=str(exc)[:500])
    await audit.record(
        db,
        action="settings.notifications_test",
        outcome=Outcome.SUCCESS if result.ok else Outcome.FAILURE,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        details={"recipients": len(recipients), "error": result.error},
    )
    return result


class NotificationOut(BaseModel):
    id: int
    event: str
    subject: str
    status: NotificationStatus
    attempts: int
    error: str | None
    created_at: datetime
    sent_at: datetime | None


@router.get("/notifications/outbox", response_model=list[NotificationOut])
async def outbox(principal: Admin, db: DbSession) -> list[NotificationOut]:
    rows = await db.scalars(select(Notification).order_by(Notification.id.desc()).limit(30))
    return [
        NotificationOut(
            id=n.id,
            event=n.event,
            subject=n.subject,
            status=n.status,
            attempts=n.attempts,
            error=n.error,
            created_at=n.created_at,
            sent_at=n.sent_at,
        )
        for n in rows
    ]
