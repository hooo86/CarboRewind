"""``/api/monitoring``: health of the backups (global administrators).

``overview``: storage trend and saturation forecast, accounts at risk, copy 2 lag,
integrity checks, failed jobs, agents. ``settings``: thresholds and the integrity schedule.
``verify``: run an integrity check now. Prometheus metrics are at ``/metrics``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select

from .. import audit, monitoring
from ..authz import Permission
from ..db.models import Agent, AgentStatus, Job, JobStatus, Outcome
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
