"""``/api/monitoring``: health of the backups (global administrators).

``overview``: storage trend and saturation forecast, accounts at risk, copy 2 lag,
integrity checks, failed jobs, agents. ``settings``: thresholds and the integrity schedule.
``verify``: run an integrity check now. Prometheus metrics are at ``/metrics``.
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select

from .. import audit, blacklist, monitoring, restore_test
from ..authz import Permission
from ..db.models import Account, AccountStatus, Agent, AgentStatus, Job, JobStatus, Outcome
from ..scheduling import Cron, CronError, zone
from .deps import AppSettings, DbSession, Principal, client_ip, require

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])

Admin = Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))]


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stale_hours: int = Field(default=24, ge=1, le=24 * 60)
    replica_max_lag_hours: int = Field(default=24, ge=1, le=24 * 60)
    integrity_enabled: bool = True
    integrity_cron: str = Field(default="30 3 * * 0", max_length=128)
    integrity_sample: float = Field(default=0.1, gt=0, le=1)
    integrity_timezone: str = Field(default="UTC", max_length=64)
    mailbox_alert_percent: int = Field(default=90, ge=0, le=100)
    mailbox_alert_gb: float = Field(default=0.0, ge=0, le=100_000)
    service_down_reports: int = Field(default=2, ge=1, le=60)
    mail_queue_alert: int = Field(default=200, ge=0, le=1_000_000)

    @field_validator("integrity_cron")
    @classmethod
    def _cron(cls, value: str) -> str:
        try:
            Cron.parse(value)
        except CronError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator("integrity_timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        try:
            zone(value)
        except CronError as exc:
            raise ValueError(str(exc)) from exc
        return value


class Overview(BaseModel):
    settings: SettingsIn
    storage: list[dict[str, Any]]
    at_risk: dict[str, dict[str, Any]]
    replica: dict[str, Any] | None
    integrity: dict[str, Any]
    next_integrity_at: datetime | None
    jobs_failed_24h: dict[str, int]
    agents: list[dict[str, Any]]
    services: list[dict[str, Any]] = Field(default_factory=list)
    metrics_token_configured: bool


def _settings_out(cfg: monitoring.MonitoringSettings) -> SettingsIn:
    return SettingsIn(
        **{k: v for k, v in monitoring.asdict(cfg).items() if k in SettingsIn.model_fields}
    )


@router.get("/overview", response_model=Overview)
async def overview(principal: Admin, db: DbSession, settings: AppSettings) -> Overview:
    cfg = await monitoring.load_settings(db)
    now = datetime.now(UTC)
    failed = dict(
        (kind.value, int(count))
        for kind, count in await db.execute(
            select(Job.kind, func.count())
            .where(Job.status == JobStatus.FAILED, Job.finished_at > now - timedelta(hours=24))
            .group_by(Job.kind)
        )
    )
    active = list(
        await db.scalars(
            select(Agent).where(Agent.status == AgentStatus.ACTIVE).order_by(Agent.name)
        )
    )
    services = [v for a in active if (v := monitoring.services_view(a, now)) is not None]
    agents = [
        {
            "name": a.name,
            "version": a.version,
            "last_heartbeat_at": a.last_heartbeat_at,
            "cert_not_after": a.cert_not_after,
            "mail_reported_at": ((a.storage or {}).get("mail") or {}).get("reported_at"),
        }
        for a in active
    ]
    nxt = None
    if cfg.integrity_enabled:
        try:
            nxt = Cron.parse(cfg.integrity_cron).next_slot(now, zone(cfg.integrity_timezone))
        except CronError:
            nxt = None
    return Overview(
        settings=_settings_out(cfg),
        storage=await monitoring.storage_trend(db),
        at_risk=await monitoring.accounts_at_risk(
            db,
            cfg.stale_hours,
            quota_percent=cfg.mailbox_alert_percent,
            quota_gb=cfg.mailbox_alert_gb,
        ),
        replica=await monitoring.replica_status(db, cfg.replica_max_lag_hours),
        integrity=await monitoring.integrity_status(db),
        next_integrity_at=nxt.astimezone(UTC) if nxt else None,
        jobs_failed_24h=failed,
        agents=agents,
        services=services,
        metrics_token_configured=settings.metrics_token is not None,
    )


@router.put("/settings", response_model=SettingsIn)
async def put_settings(
    body: SettingsIn, principal: Admin, db: DbSession, request: Request
) -> SettingsIn:
    cfg = await monitoring.load_settings(db)
    for key, value in body.model_dump().items():
        setattr(cfg, key, value)
    await monitoring.save_settings(db, cfg)
    await audit.record(
        db,
        action="monitoring.settings",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        details=body.model_dump(),
    )
    return _settings_out(cfg)


class VerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: Literal["primary", "replica"] = "primary"
    sample: float = Field(default=1.0, gt=0, le=1)


class VerifyOut(BaseModel):
    queued: bool
    job_id: str | None


@router.post("/verify", response_model=VerifyOut, status_code=status.HTTP_202_ACCEPTED)
async def verify_now(
    body: VerifyIn, principal: Admin, db: DbSession, request: Request
) -> VerifyOut:
    if body.target == "replica" and (await monitoring.storage_settings.load(db)).copy is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "copy 02 is not configured")
    job = await monitoring.queue_verify(db, body.target, body.sample, principal.user.id)
    await audit.record(
        db,
        action="integrity.verify_request",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        details={**body.model_dump(), "queued": job is not None},
    )
    return VerifyOut(queued=job is not None, job_id=str(job.id) if job else None)


# ------------------------------------------------------------------ blacklists (DNSBL)


class BlacklistSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    ips: list[str] = Field(default_factory=list, max_length=50)
    zones: list[str] = Field(default_factory=lambda: list(blacklist.DEFAULT_ZONES), max_length=30)
    interval_minutes: int = Field(default=60, ge=15, le=24 * 60)

    @field_validator("ips")
    @classmethod
    def _ips(cls, value: list[str]) -> list[str]:
        out = []
        for ip in value:
            try:
                address = ipaddress.ip_address(ip.strip())
            except ValueError as exc:
                raise ValueError(f"invalid address {ip!r}") from exc
            if address.is_private or address.is_loopback:
                raise ValueError(f"{ip} is not a public address: lists only know public ones")
            out.append(str(address))
        return sorted(set(out))

    @field_validator("zones")
    @classmethod
    def _zones(cls, value: list[str]) -> list[str]:
        out = []
        for name in value:
            clean = name.strip().strip(".").lower()
            if not re.fullmatch(r"([a-z0-9-]{1,63}\.)+[a-z]{2,63}", clean):
                raise ValueError(f"invalid list zone {name!r}")
            out.append(clean)
        return list(dict.fromkeys(out))


class BlacklistOut(BaseModel):
    settings: BlacklistSettingsIn
    checked_at: str | None
    results: list[dict[str, Any]]


async def _blacklist_out(db: DbSession) -> BlacklistOut:
    cfg = await blacklist.load_settings(db)
    status_ = await blacklist.load_status(db)
    return BlacklistOut(
        settings=BlacklistSettingsIn(**asdict(cfg)),
        checked_at=status_.get("checked_at"),
        results=status_.get("results", []),
    )


@router.get("/blacklist", response_model=BlacklistOut)
async def get_blacklist(principal: Admin, db: DbSession) -> BlacklistOut:
    return await _blacklist_out(db)


@router.put("/blacklist", response_model=BlacklistOut)
async def put_blacklist(
    body: BlacklistSettingsIn, principal: Admin, db: DbSession, request: Request
) -> BlacklistOut:
    if body.enabled and not body.ips:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "add at least one address")
    await blacklist.save_settings(db, blacklist.BlacklistSettings(**body.model_dump()))
    await audit.record(
        db,
        action="settings.blacklist",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        details=body.model_dump(),
    )
    return await _blacklist_out(db)


@router.post("/blacklist/check", response_model=BlacklistOut)
async def check_blacklist(principal: Admin, db: DbSession) -> BlacklistOut:
    """Check now (a few seconds), whatever the interval."""
    cfg = await blacklist.load_settings(db)
    if not cfg.ips:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "no address to check")
    await blacklist.run_check(db, datetime.now(UTC))
    return await _blacklist_out(db)


# ------------------------------------------------------------------ restore tests


class RestoreTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    schedule: str = Field(default="45 3 * * 6", max_length=128)
    timezone: str = Field(default="UTC", max_length=64)
    target_account_id: uuid.UUID | None = None
    messages: int = Field(default=20, ge=1, le=500)
    cleanup: bool = True

    @field_validator("schedule")
    @classmethod
    def _cron(cls, value: str) -> str:
        try:
            Cron.parse(value)
        except CronError as exc:
            raise ValueError(str(exc)) from exc
        return " ".join(value.split())

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        try:
            zone(value)
        except CronError as exc:
            raise ValueError(str(exc)) from exc
        return value


class RestoreTestOut(BaseModel):
    settings: RestoreTestIn
    target_email: str | None
    history: list[dict[str, Any]]


async def _restore_test_out(db: DbSession) -> RestoreTestOut:
    cfg = await restore_test.load(db)
    target = (
        await db.get(Account, uuid.UUID(cfg.target_account_id)) if cfg.target_account_id else None
    )
    fields = {k: v for k, v in asdict(cfg).items() if k in RestoreTestIn.model_fields}
    return RestoreTestOut(
        settings=RestoreTestIn(**fields),
        target_email=target.email if target else None,
        history=await restore_test.history(db),
    )


@router.get("/restore-tests", response_model=RestoreTestOut)
async def get_restore_tests(principal: Admin, db: DbSession) -> RestoreTestOut:
    return await _restore_test_out(db)


@router.put("/restore-tests", response_model=RestoreTestOut)
async def put_restore_tests(
    body: RestoreTestIn, principal: Admin, db: DbSession, request: Request
) -> RestoreTestOut:
    if body.target_account_id is not None:
        target = await db.get(Account, body.target_account_id)
        if target is None or target.status is AccountStatus.DELETED:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown test mailbox")
    elif body.enabled:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "choose the test mailbox")
    current = await restore_test.load(db)
    cfg = restore_test.RestoreTestSettings(
        **{**body.model_dump(mode="json")},
        last_slot=current.last_slot if current.enabled and body.enabled else None,
    )
    await restore_test.save(db, cfg)
    await audit.record(
        db,
        action="settings.restore_test",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        details=body.model_dump(mode="json"),
    )
    return await _restore_test_out(db)


@router.post("/restore-tests/run", response_model=RestoreTestOut, status_code=202)
async def run_restore_test(principal: Admin, db: DbSession) -> RestoreTestOut:
    cfg = await restore_test.load(db)
    try:
        await restore_test.enqueue(db, cfg, datetime.now(UTC), principal.user.id)
    except restore_test.RestoreTestError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return await _restore_test_out(db)
