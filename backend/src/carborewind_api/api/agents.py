"""``/api/agents``: registration and revocation of agents (super-admin)."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .. import audit
from ..authz import Permission
from ..db.models import Agent, AgentStatus, Outcome
from ..pki import token_digest
from .deps import AppSettings, DbSession, Principal, client_ip, require

router = APIRouter(prefix="/api/agents", tags=["agents"])

Admin = Annotated[Principal, Depends(require(Permission.AGENTS_MANAGE))]


class AgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    hostname: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9.-]+$")


class AgentOut(BaseModel):
    id: uuid.UUID
    name: str
    hostname: str
    status: AgentStatus
    cert_not_after: datetime | None
    last_heartbeat_at: datetime | None
    version: str | None

    @classmethod
    def of(cls, a: Agent) -> AgentOut:
        return cls(
            id=a.id,
            name=a.name,
            hostname=a.hostname,
            status=a.status,
            cert_not_after=a.cert_not_after,
            last_heartbeat_at=a.last_heartbeat_at,
            version=a.version,
        )


class AgentCreated(AgentOut):
    enrollment_token: str = Field(description="Shown once. Pass it to `carborewind-agent enroll`.")
    enrollment_expires_at: datetime


async def _audit(
    db: DbSession, admin: Principal, request: Request, action: str, agent: Agent
) -> None:
    await audit.record(
        db,
        action=action,
        outcome=Outcome.SUCCESS,
        actor_id=str(admin.user.id),
        actor_name=admin.user.username,
        target_type="agent",
        target_id=str(agent.id),
        source_ip=client_ip(request),
        details={"name": agent.name, "hostname": agent.hostname},
    )


@router.get("", response_model=list[AgentOut])
async def list_agents(admin: Admin, db: DbSession) -> list[AgentOut]:
    return [AgentOut.of(a) for a in await db.scalars(select(Agent).order_by(Agent.name))]


@router.post("", response_model=AgentCreated, status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: AgentCreate, admin: Admin, db: DbSession, settings: AppSettings, request: Request
) -> AgentCreated:
    """Register an agent and return a one-time enrolment token."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(minutes=settings.agent_enrollment_minutes)
    agent = Agent(
        name=body.name,
        hostname=body.hostname,
        status=AgentStatus.PENDING,
        enrollment_token_hash=token_digest(token),
        enrollment_expires_at=expires,
    )
    db.add(agent)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "agent name already exists") from exc
    await _audit(db, admin, request, "agent.create", agent)
    return AgentCreated(
        **AgentOut.of(agent).model_dump(), enrollment_token=token, enrollment_expires_at=expires
    )


@router.post("/{agent_id}/revoke", response_model=AgentOut)
async def revoke_agent(
    agent_id: uuid.UUID, admin: Admin, db: DbSession, request: Request
) -> AgentOut:
    """Revoke the agent's certificate immediately (fingerprint cleared)."""
    agent = await db.get(Agent, agent_id, with_for_update=True)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    agent.status = AgentStatus.REVOKED
    agent.cert_fingerprint = None
    agent.enrollment_token_hash = None
    await _audit(db, admin, request, "agent.revoke", agent)
    return AgentOut.of(agent)
