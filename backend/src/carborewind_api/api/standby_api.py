"""``/api/standby``: the standby server (clone of the Carbonio VM) for disaster recovery."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from .. import audit, standby
from ..authz import Permission
from ..db.models import Agent, AgentStatus, Job, JobKind, JobStatus, Outcome
from .deps import DbSession, Principal, client_ip, require

router = APIRouter(prefix="/api/standby", tags=["standby"])

Admin = Annotated[Principal, Depends(require(Permission.AGENTS_MANAGE))]


async def _audit(
    db: DbSession, admin: Principal, request: Request, action: str, details: dict[str, Any]
) -> None:
    await audit.record(
        db,
        action=action,
        outcome=Outcome.SUCCESS,
        actor_id=str(admin.user.id),
        actor_name=admin.user.username,
        target_type="standby",
        source_ip=client_ip(request),
        details=details,
    )


@router.get("")
async def overview(admin: Admin, db: DbSession) -> dict[str, Any]:
    return await standby.overview(db, datetime.now(UTC))


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    interval_minutes: int = Field(ge=5, le=1440)


@router.put("/settings")
async def save_settings(
    body: SettingsIn, admin: Admin, db: DbSession, request: Request
) -> dict[str, Any]:
    if body.enabled and await standby.standby_agent(db) is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "no enrolled agent has the standby role yet")
    cfg = standby.StandbyConfig(body.enabled, body.interval_minutes)
    await standby.save(db, cfg)
    await _audit(db, admin, request, "standby.settings", body.model_dump())
    return {"enabled": cfg.enabled, "interval_minutes": cfg.interval_minutes}


class RoleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["primary", "standby"]


@router.put("/agents/{agent_id}/role")
async def set_role(
    agent_id: uuid.UUID, body: RoleIn, admin: Admin, db: DbSession, request: Request
) -> dict[str, str]:
    """Give an agent the primary or the standby role (not for a promotion: see below)."""
    agent = await db.get(Agent, agent_id, with_for_update=True)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    if body.role == standby.STANDBY and agent.role != standby.STANDBY:
        others = await db.scalar(
            select(Agent.id).where(
                Agent.id != agent.id,
                Agent.role == standby.PRIMARY,
                Agent.status == AgentStatus.ACTIVE,
            )
        )
        if others is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "the only primary agent cannot become a standby"
            )
    agent.role = body.role
    await _audit(db, admin, request, "standby.role", {"agent": agent.name, "role": body.role})
    return {"id": str(agent.id), "role": agent.role}


@router.post("/sync-now")
async def sync_now(admin: Admin, db: DbSession, request: Request) -> dict[str, int]:
    """Queue at once the replays due, without waiting for the interval."""
    queued = await standby.enqueue_due(db, datetime.now(UTC), force=True)
    await _audit(db, admin, request, "standby.sync_now", {"queued": queued})
    return {"queued": queued}


class PromoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: uuid.UUID
    confirm: str = Field(max_length=255)  # the agent's name, typed again


@router.post("/promote")
async def promote(body: PromoteIn, admin: Admin, db: DbSession, request: Request) -> dict[str, Any]:
    """Disaster: the standby becomes the primary server.

    The replication stops; the former primary agents take the standby role (if one comes
    back it is not backed up as a second copy of the same accounts), and the replays still
    queued are cancelled.
    """
    agent = await db.get(Agent, body.agent_id, with_for_update=True)
    if agent is None or agent.role != standby.STANDBY:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "standby agent not found")
    if body.confirm.strip() != agent.name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "type the agent's name")
    demoted = list(
        await db.scalars(
            select(Agent.name).where(Agent.role == standby.PRIMARY, Agent.id != agent.id)
        )
    )
    await db.execute(
        update(Agent)
        .where(Agent.role == standby.PRIMARY, Agent.id != agent.id)
        .values(role=standby.STANDBY)
    )
    agent.role = standby.PRIMARY
    cfg = await standby.load(db)
    await standby.save(db, standby.StandbyConfig(False, cfg.interval_minutes))
    cancelled = len(
        (
            await db.execute(
                update(Job)
                .where(Job.kind == JobKind.STANDBY_SYNC, Job.status == JobStatus.QUEUED)
                .values(status=JobStatus.CANCELLED, finished_at=datetime.now(UTC))
                .returning(Job.id)
            )
        ).all()
    )
    await _audit(
        db,
        admin,
        request,
        "standby.promote",
        {"agent": agent.name, "demoted": demoted, "cancelled": cancelled},
    )
    return {"promoted": agent.name, "demoted": demoted}
