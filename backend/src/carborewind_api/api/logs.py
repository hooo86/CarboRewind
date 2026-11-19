"""``/api/logs``: read CarboRewind's and Carbonio's logs from the interface (super admins).

* ``server``: the API and the scheduler keep their last 5000 lines in Redis.
* Agent sources are read on the Carbonio server by the agent (a ``fetch`` job, a few
  seconds): its own journal, the MTA log, the mailbox log. Only these fixed names are sent to
  the agent, never a path. The text is kept encrypted one hour for the requester.

Every reading is audited.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis

from .. import archiving, audit
from ..authz import Permission
from ..db.models import FetchedContent, Job, JobKind, JobStatus, Outcome
from ..logging import LOG_KEY
from .deps import AppSettings, DbSession, Principal, client_ip, deny, require

router = APIRouter(prefix="/api/logs", tags=["logs"])

Admin = Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))]
AgentSource = Literal["agent", "mail", "mailbox"]


class ServerLogOut(BaseModel):
    process: str
    available: bool
    lines: list[dict[str, Any]]


@router.get("/server", response_model=ServerLogOut)
async def server_log(
    principal: Admin,
    db: DbSession,
    settings: AppSettings,
    request: Request,
    process: Annotated[Literal["api", "scheduler"], Query()] = "api",
    lines: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> ServerLogOut:
    """Last lines of the API or scheduler (most recent last)."""
    if settings.redis_url is None:
        return ServerLogOut(process=process, available=False, lines=[])
    redis = Redis.from_url(settings.redis_url.get_secret_value())
    try:
        raw = await redis.lrange(LOG_KEY.format(process=process), 0, lines - 1)
    finally:
        await redis.aclose()
    out = []
    for item in reversed(raw):
        try:
            out.append(json.loads(item))
        except ValueError:
            out.append(
                {"msg": item.decode("utf-8", "replace") if isinstance(item, bytes) else item}
            )
    await audit.record(
        db,
        action="logs.read",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        details={"source": f"server:{process}", "lines": len(out)},
    )
    return ServerLogOut(process=process, available=True, lines=out)


class AgentLogIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: AgentSource
    lines: int = Field(default=500, ge=1, le=2000)


class AgentLogStarted(BaseModel):
    job_id: uuid.UUID


@router.post("/agent", response_model=AgentLogStarted, status_code=202)
async def ask_agent_log(
    body: AgentLogIn, principal: Admin, db: DbSession, request: Request
) -> AgentLogStarted:
    job = Job(
        kind=JobKind.FETCH,
        status=JobStatus.QUEUED,
        created_by=principal.user.id,
        priority=archiving.FETCH_PRIORITY,
        max_attempts=1,
        params={"log": body.source, "lines": body.lines},
    )
    db.add(job)
    await db.flush()
    await audit.record(
        db,
        action="logs.read",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        details={"source": f"agent:{body.source}", "lines": body.lines, "job": str(job.id)},
    )
    return AgentLogStarted(job_id=job.id)


class AgentLogOut(BaseModel):
    status: JobStatus
    error: str | None = None
    text: str | None = None


@router.get("/agent/{job_id}", response_model=AgentLogOut)
async def agent_log(
    job_id: uuid.UUID,
    principal: Admin,
    db: DbSession,
    settings: AppSettings,
    request: Request,
) -> AgentLogOut:
    job = await db.get(Job, job_id)
    if (
        job is None
        or job.kind is not JobKind.FETCH
        or "log" not in (job.params or {})
        or job.created_by != principal.user.id
    ):
        raise await deny(db, principal, request, Permission.SETTINGS_MANAGE, log=str(job_id))
    if job.status is not JobStatus.SUCCEEDED:
        return AgentLogOut(status=job.status, error=job.error)
    stored = await db.get(FetchedContent, job.id)
    raw = archiving.open_content(settings, stored.data) if stored else None
    if raw is None:
        raise HTTPException(status.HTTP_410_GONE, "expired: read the log again")
    return AgentLogOut(status=job.status, text=raw.decode("utf-8", "replace"))
