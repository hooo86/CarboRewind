"""``/api/settings``: SMTP notifications and backup storage (``settings:manage``).

``/notifications``: SMTP settings, test e-mail and outbox. ``/storage``: where backups go
(local disk or NAS share, plus an optional copy 02 on a second share), see
:mod:`carborewind_api.storage_settings`.

The SMTP password is write-only: ``password`` omitted or null keeps the stored one, ``""``
clears it, any other value replaces it (encrypted with ``CARBOREWIND_SECRET_KEY``).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy import select

from .. import agent_tuning, audit, storage_settings
from ..authz import Permission
from ..config import Settings
from ..db.models import (
    Agent,
    AgentStatus,
    Job,
    JobKind,
    JobStatus,
    Notification,
    NotificationStatus,
    Outcome,
)
from ..notifications import (
    EVENTS,
    TEXTS,
    SecretKeyError,
    SmtpConfig,
    SmtpError,
    TelegramConfig,
    TelegramError,
    decrypt,
    encrypt,
    load_config,
    load_telegram,
    save_config,
    save_telegram,
    send_mail,
    send_telegram,
    telegram_call,
)
from ..scheduling import CronError, zone
from ..security import policy as security_policy
from ..storage_settings import SAFE_FIELD, valid_share
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
    fallback_enabled: bool = False
    fallback_host: str = Field(default="", max_length=253)
    fallback_port: int = Field(default=587, ge=1, le=65535)
    fallback_security: Literal["starttls", "ssl", "none"] = "starttls"
    fallback_username: str = Field(default="", max_length=320)
    fallback_password: str | None = Field(default=None, max_length=1024, repr=False)
    fallback_sender: EmailStr | Literal[""] = ""

    @field_validator("host", "fallback_host")
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
    fallback_enabled: bool = False
    fallback_host: str = ""
    fallback_port: int = 587
    fallback_security: str = "starttls"
    fallback_username: str = ""
    fallback_password_set: bool = False
    fallback_sender: str = ""
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
        fallback_enabled=cfg.fallback_enabled,
        fallback_host=cfg.fallback_host,
        fallback_port=cfg.fallback_port,
        fallback_security=cfg.fallback_security,
        fallback_username=cfg.fallback_username,
        fallback_password_set=bool(cfg.fallback_password_enc),
        fallback_sender=cfg.fallback_sender,
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
    fallback_enc = current.fallback_password_enc
    if body.fallback_password == "":
        fallback_enc = None
    elif body.fallback_password is not None:
        try:
            fallback_enc = encrypt(settings, body.fallback_password)
        except SecretKeyError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if body.fallback_enabled and not body.fallback_host:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "the fallback server needs a host"
        )
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
        fallback_enabled=body.fallback_enabled,
        fallback_host=body.fallback_host,
        fallback_port=body.fallback_port,
        fallback_security=body.fallback_security,
        fallback_username=body.fallback_username,
        fallback_password_enc=fallback_enc,
        fallback_sender=str(body.fallback_sender),
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
            "fallback": cfg.fallback_host if cfg.fallback_enabled else None,
            "fallback_password_changed": body.fallback_password is not None,
        },
    )
    return _out(cfg, settings)


class TestResult(BaseModel):
    ok: bool
    error: str | None = None


class TestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: EmailStr | None = None
    server: Literal["main", "fallback"] = "main"


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
    target = cfg
    if body.server == "fallback":
        if not cfg.fallback_host:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "no fallback server")
        target = cfg.fallback()
    try:
        await asyncio.to_thread(
            send_mail, settings, target, recipients, texts["test"], texts["test_body"]
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
        details={"recipients": len(recipients), "server": body.server, "error": result.error},
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
    channel: str = "email"


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
            channel=n.channel,
        )
        for n in rows
    ]


# ------------------------------------------------------------------ storage (NAS)


class ShareIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    share: str = Field(max_length=1024, description="//server/share[/folder]")
    username: str = Field(min_length=1, max_length=256)
    # Write-only: omitted/null keeps the stored password.
    password: str | None = Field(default=None, max_length=1024, repr=False)
    domain: str = Field(default="", max_length=256)
    seal: bool = Field(default=False, description="require SMB 3 transport encryption")

    @field_validator("share")
    @classmethod
    def _share(cls, value: str) -> str:
        value = value.strip().replace("\\", "/")
        if not valid_share(value):
            raise ValueError("expected //server/share[/folder]")
        return value

    @field_validator("username", "domain")
    @classmethod
    def _field(cls, value: str) -> str:
        if not SAFE_FIELD.match(value):
            raise ValueError("invalid characters")
        return value.strip()

    @field_validator("password")
    @classmethod
    def _password(cls, value: str | None) -> str | None:
        if value is not None and any(c in value for c in "\r\n\x00"):
            raise ValueError("invalid characters")
        return value


class PrimaryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["local", "smb"]
    smb: ShareIn | None = None
    # Copy the existing repository to the new place (otherwise start an empty one there:
    # snapshots of the old place are no longer restorable).
    migrate: bool = True


class StorageSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary: PrimaryIn
    copy_02: ShareIn | None = None  # None: no second copy


class ShareOut(BaseModel):
    share: str
    username: str
    domain: str
    seal: bool
    password_set: bool


class ReplicationOut(BaseModel):
    status: JobStatus
    created_at: datetime
    finished_at: datetime | None
    progress: dict[str, Any] | None
    error: str | None


class StorageSettingsOut(BaseModel):
    revision: int
    primary_kind: Literal["local", "smb"] | None  # None: never set here (as installed)
    primary_smb: ShareOut | None
    migrate: bool
    copy_02: ShareOut | None
    # As reported by each active agent: repository, copy 02, last application.
    agents: list[dict[str, Any]]
    applied: bool
    last_replication: ReplicationOut | None
    secret_key_configured: bool


def _share_out(section: dict[str, Any] | None) -> ShareOut | None:
    if not section or section.get("kind", "smb") != "smb":
        return None
    return ShareOut(
        share=section["share"],
        username=section["username"],
        domain=section.get("domain", ""),
        seal=bool(section.get("seal")),
        password_set=bool(section.get("password_enc")),
    )


async def _storage_out(db: DbSession, settings: Settings) -> StorageSettingsOut:
    cfg = await storage_settings.load(db)
    agents = [
        {"agent": name, **(report or {})}
        for name, report in await db.execute(
            select(Agent.name, Agent.storage)
            .where(Agent.status == AgentStatus.ACTIVE)
            .order_by(Agent.name)
        )
    ]
    applied = cfg.revision == 0 or (
        bool(agents)
        and all(int((a.get("apply") or {}).get("revision") or 0) >= cfg.revision for a in agents)
    )
    last = await db.scalar(
        select(Job).where(Job.kind == JobKind.REPLICATE).order_by(Job.created_at.desc()).limit(1)
    )
    return StorageSettingsOut(
        revision=cfg.revision,
        primary_kind=cfg.primary.get("kind") if cfg.primary else None,
        primary_smb=_share_out(cfg.primary),
        migrate=bool((cfg.primary or {}).get("migrate", True)),
        copy_02=_share_out(cfg.copy),
        agents=agents,
        applied=applied,
        last_replication=ReplicationOut(
            status=last.status,
            created_at=last.created_at,
            finished_at=last.finished_at,
            progress=last.progress,
            error=last.error,
        )
        if last
        else None,
        secret_key_configured=settings.secret_key is not None
        and len(settings.secret_key.get_secret_value()) >= 32,
    )


def _share_section(
    body: ShareIn, current: dict[str, Any] | None, settings: Settings, **extra: Any
) -> tuple[dict[str, Any], bool]:
    """New stored section and whether it changed (a new password always counts)."""
    section: dict[str, Any] = {
        **extra,
        "share": body.share,
        "username": body.username,
        "domain": body.domain,
        "seal": body.seal,
    }
    if body.password:
        try:
            section["password_enc"] = encrypt(settings, body.password)
        except SecretKeyError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    elif current and current.get("password_enc"):
        section["password_enc"] = current["password_enc"]
    else:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"password required for {body.share}"
        )
    plain = {k: v for k, v in section.items() if k != "password_enc"}
    before = {k: v for k, v in (current or {}).items() if k != "password_enc"}
    return section, bool(body.password) or plain != before


@router.get("/storage", response_model=StorageSettingsOut)
async def get_storage_settings(
    principal: Admin, db: DbSession, settings: AppSettings
) -> StorageSettingsOut:
    return await _storage_out(db, settings)


@router.put("/storage", response_model=StorageSettingsOut)
async def put_storage_settings(
    body: StorageSettingsIn,
    principal: Admin,
    db: DbSession,
    settings: AppSettings,
    request: Request,
) -> StorageSettingsOut:
    """Choose where backups go; the agent applies it within a minute (mount, restart)."""
    cfg = await storage_settings.load(db)
    if body.primary.kind == "smb":
        if body.primary.smb is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "share settings required")
        current = cfg.primary if (cfg.primary or {}).get("kind") == "smb" else None
        primary, primary_changed = _share_section(
            body.primary.smb, current, settings, kind="smb", migrate=body.primary.migrate
        )
        primary_changed = primary_changed or current is None
    else:
        primary = {"kind": "local", "migrate": body.primary.migrate}
        primary_changed = cfg.primary != primary
    if body.copy_02 is not None:
        if body.primary.smb and body.copy_02.share.rstrip("/").lower() == (
            body.primary.smb.share.rstrip("/").lower()
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "copy 02 must be on another share"
            )
        copy, copy_changed = _share_section(body.copy_02, cfg.copy, settings)
    else:
        copy, copy_changed = None, cfg.copy is not None
    if primary_changed or copy_changed:
        cfg.revision += 1
        if primary_changed:
            cfg.primary, cfg.primary_revision = primary, cfg.revision
        if copy_changed:
            cfg.copy, cfg.copy_revision = copy, cfg.revision
        await storage_settings.save(db, cfg)
    await audit.record(
        db,
        action="settings.storage",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        details={
            "revision": cfg.revision,
            "primary": body.primary.kind,
            "primary_share": body.primary.smb.share if body.primary.smb else None,
            "primary_changed": primary_changed,
            "migrate": body.primary.migrate,
            "copy_02": body.copy_02.share if body.copy_02 else None,
            "copy_changed": copy_changed,
        },
    )
    return await _storage_out(db, settings)


class ReplicateResult(BaseModel):
    job_id: uuid.UUID


@router.post(
    "/storage/replicate", response_model=ReplicateResult, status_code=status.HTTP_202_ACCEPTED
)
async def replicate_now(principal: Admin, db: DbSession, request: Request) -> ReplicateResult:
    """Refresh copy 02 now."""
    cfg = await storage_settings.load(db)
    if cfg.copy is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "copy 02 is not configured")
    pending = await db.scalar(
        select(Job.id).where(
            Job.kind == JobKind.REPLICATE,
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        )
    )
    if pending:
        return ReplicateResult(job_id=pending)
    job = Job(kind=JobKind.REPLICATE, status=JobStatus.QUEUED, priority=60, max_attempts=1)
    db.add(job)
    await db.flush()
    await audit.record(
        db,
        action="storage.replicate_request",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        target_type="job",
        target_id=str(job.id),
    )
    return ReplicateResult(job_id=job.id)


# ------------------------------------------------------------------ security policy


class SecurityPolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    require_2fa: Literal["none", "admins", "all"] = "none"


@router.get("/security", response_model=SecurityPolicyIn)
async def get_security(principal: Admin, db: DbSession) -> SecurityPolicyIn:
    return SecurityPolicyIn(require_2fa=(await security_policy.load(db)).require_2fa)


@router.put("/security", response_model=SecurityPolicyIn)
async def put_security(
    body: SecurityPolicyIn, principal: Admin, db: DbSession, request: Request
) -> SecurityPolicyIn:
    """Who must use two-factor authentication. Users concerned who have not enrolled yet
    are sent to the enrolment at their next request."""
    await security_policy.save(db, security_policy.SecurityPolicy(require_2fa=body.require_2fa))
    await audit.record(
        db,
        action="settings.security",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        details=body.model_dump(),
    )
    return body


# ------------------------------------------------------------------ agent


class AgentTuningIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrent_accounts: int = Field(default=2, ge=1, le=8)
    max_bandwidth_mbps: int = Field(default=0, ge=0, le=100_000)
    max_load_per_cpu: float = Field(default=1.5, ge=0, le=64)
    min_free_gb_local: float = Field(default=20.0, ge=0, le=100_000)
    min_free_percent_local: float = Field(default=15.0, ge=0, le=90)
    min_free_gb_network: float = Field(default=2.0, ge=0, le=100_000)
    service_monitor: bool = True
    service_ports: dict[str, int] = Field(default_factory=dict, max_length=20)
    mail_monitor: bool = True
    autostart_enabled: bool = False
    autostart_minutes: int = Field(default=5, ge=1, le=240)
    mail_test_after_start: bool = True
    mail_test_internal: str = Field(default="", max_length=320)
    mail_test_external: str = Field(default="", max_length=320)
    cert_monitor: bool = True
    cert_name: str = Field(default="", max_length=253)
    cert_alert_days: int = Field(default=7, ge=1, le=90)
    cert_renew_enabled: bool = False
    cert_renew_days: int = Field(default=7, ge=1, le=60)

    @field_validator("cert_name")
    @classmethod
    def _host(cls, value: str) -> str:
        value = value.strip().lower()
        if value and not re.fullmatch(
            r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", value
        ):
            raise ValueError(f"invalid server name {value!r}")
        return value

    @field_validator("mail_test_internal", "mail_test_external")
    @classmethod
    def _address(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+", value):
            raise ValueError(f"invalid e-mail address {value!r}")
        return value

    @field_validator("service_ports")
    @classmethod
    def _ports(cls, value: dict[str, int]) -> dict[str, int]:
        for name, port in value.items():
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name):
                raise ValueError(f"invalid service name {name!r}")
            if not 1 <= int(port) <= 65535:
                raise ValueError(f"invalid port for {name}")
        return value


class AgentTuningOut(AgentTuningIn):
    revision: int
    agents: list[dict[str, Any]] = Field(default_factory=list)


def _tuning_out(cfg: agent_tuning.AgentTuning) -> dict[str, Any]:
    data = asdict(cfg)
    data.pop("ports_defaults", None)  # internal bookkeeping
    data.pop("autostart_paused_until", None)  # shown and changed in Supervision
    return data


@router.get("/agent", response_model=AgentTuningOut)
async def get_agent_tuning(principal: Admin, db: DbSession) -> AgentTuningOut:
    cfg = await agent_tuning.load(db)
    agents = [
        {"name": a.name, "applied": (a.storage or {}).get("tuning")}
        for a in await db.scalars(
            select(Agent).where(Agent.status == AgentStatus.ACTIVE).order_by(Agent.name)
        )
    ]
    return AgentTuningOut(**_tuning_out(cfg), agents=agents)


@router.put("/agent", response_model=AgentTuningOut)
async def put_agent_tuning(
    body: AgentTuningIn, principal: Admin, db: DbSession, request: Request
) -> AgentTuningOut:
    cfg = await agent_tuning.load(db)
    new = agent_tuning.AgentTuning(
        revision=cfg.revision + 1,
        autostart_paused_until=cfg.autostart_paused_until,  # set from Supervision
        **body.model_dump(),
    )
    await agent_tuning.save(db, new)
    await audit.record(
        db,
        action="settings.agent.update",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        target_type="settings",
        target_id="agent",
        details=body.model_dump(),
    )
    return AgentTuningOut(**_tuning_out(new), agents=[])


# ------------------------------------------------------------------ Telegram

CHAT_ID = re.compile(r"^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$")
BOT_TOKEN = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,60}$")


class TelegramIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    token: str | None = Field(
        default=None, max_length=100, repr=False, description="None = keep, '' = clear"
    )
    chat_ids: list[str] = Field(default_factory=list, max_length=10)
    events: dict[str, bool] = Field(default_factory=dict)

    @field_validator("token")
    @classmethod
    def _token(cls, value: str | None) -> str | None:
        if value and not BOT_TOKEN.fullmatch(value.strip()):
            raise ValueError("this is not a bot token (as given by @BotFather)")
        return value.strip() if value else value

    @field_validator("chat_ids")
    @classmethod
    def _chats(cls, value: list[str]) -> list[str]:
        out = []
        for chat in value:
            clean = chat.strip()
            if not CHAT_ID.fullmatch(clean):
                raise ValueError(f"invalid chat {chat!r} (a number such as -1001234567890)")
            out.append(clean)
        return list(dict.fromkeys(out))

    @field_validator("events")
    @classmethod
    def _events(cls, value: dict[str, bool]) -> dict[str, bool]:
        unknown = set(value) - set(EVENTS)
        if unknown:
            raise ValueError(f"unknown events: {', '.join(sorted(unknown))}")
        return value


class TelegramOut(BaseModel):
    enabled: bool
    token_set: bool
    chat_ids: list[str]
    events: dict[str, bool]


def _telegram_out(cfg: TelegramConfig) -> TelegramOut:
    return TelegramOut(
        enabled=cfg.enabled,
        token_set=bool(cfg.token_enc),
        chat_ids=cfg.chat_ids,
        events=cfg.events,
    )


@router.get("/telegram", response_model=TelegramOut)
async def get_telegram(principal: Admin, db: DbSession) -> TelegramOut:
    return _telegram_out(await load_telegram(db))


def _bot_id(settings: Settings, token_enc: str | None) -> str | None:
    """The bot's own id: the part of its token before ':' (a bot cannot message itself)."""
    if not token_enc:
        return None
    try:
        return decrypt(settings, token_enc).split(":", 1)[0] or None
    except SecretKeyError:
        return None


@router.put("/telegram", response_model=TelegramOut)
async def put_telegram(
    body: TelegramIn, principal: Admin, db: DbSession, settings: AppSettings, request: Request
) -> TelegramOut:
    current = await load_telegram(db)
    token_enc = current.token_enc
    if body.token == "":
        token_enc = None
    elif body.token is not None:
        try:
            token_enc = encrypt(settings, body.token)
        except SecretKeyError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    bot_id = _bot_id(settings, token_enc)
    if bot_id and bot_id in body.chat_ids:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{bot_id} is the bot itself (the number before ':' in the token), not a group: "
            "add the bot to a group, write a message there, then use 'Find groups'",
        )
    if body.enabled and (not token_enc or not body.chat_ids):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "a bot token and at least one chat are required to enable Telegram",
        )
    cfg = TelegramConfig(
        enabled=body.enabled,
        token_enc=token_enc,
        chat_ids=body.chat_ids,
        events={**TelegramConfig().events, **body.events},
    )
    await save_telegram(db, cfg)
    await audit.record(
        db,
        action="settings.telegram",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        details={
            "enabled": cfg.enabled,
            "chats": len(cfg.chat_ids),
            "token_changed": body.token is not None,
        },
    )
    return _telegram_out(cfg)


@router.post("/telegram/test", response_model=TestResult)
async def test_telegram(
    principal: Admin, db: DbSession, settings: AppSettings, request: Request
) -> TestResult:
    cfg = await load_telegram(db)
    smtp = await load_config(db)
    texts = TEXTS.get(smtp.language, TEXTS["fr"])
    try:
        await asyncio.to_thread(
            send_telegram,
            settings,
            cfg,
            cfg.chat_ids,
            f"{texts['test']}\n\n{texts['test_body']}",
        )
        result = TestResult(ok=True)
    except (TelegramError, SecretKeyError) as exc:
        result = TestResult(ok=False, error=str(exc)[:500])
    await audit.record(
        db,
        action="settings.telegram_test",
        outcome=Outcome.SUCCESS if result.ok else Outcome.FAILURE,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        details={"chats": len(cfg.chat_ids), "error": result.error},
    )
    return result


class TelegramChat(BaseModel):
    id: str
    title: str
    type: str


@router.get("/telegram/chats", response_model=list[TelegramChat])
async def telegram_chats(
    principal: Admin, db: DbSession, settings: AppSettings
) -> list[TelegramChat]:
    """Groups and people who wrote to the bot lately: to fill the chat ids in."""
    cfg = await load_telegram(db)
    if not cfg.token_enc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "save the bot token first")
    try:
        updates = await asyncio.to_thread(
            telegram_call, settings, cfg.token_enc, "getUpdates", {"limit": 100}
        )
    except (TelegramError, SecretKeyError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    chats: dict[str, TelegramChat] = {}
    for update in updates or []:
        for key in ("message", "channel_post", "my_chat_member", "edited_message"):
            chat = (update.get(key) or {}).get("chat")
            if chat and "id" in chat and not chat.get("is_bot"):
                title = chat.get("title") or " ".join(
                    x for x in (chat.get("first_name"), chat.get("last_name")) if x
                )
                chats[str(chat["id"])] = TelegramChat(
                    id=str(chat["id"]), title=title or str(chat["id"]), type=chat.get("type", "")
                )
    return list(chats.values())
