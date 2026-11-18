"""``/api/backups`` and ``/api/jobs``: start on-demand backups, follow jobs."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from .. import audit
from ..authz import Permission
from ..db.models import Account, AccountStatus, BackupType, Job, JobKind, JobStatus, Outcome
from .deps import DbSession, Principal, client_ip, deny, require

router = APIRouter(tags=["backups"])

Runner = Annotated[Principal, Depends(require(Permission.BACKUPS_RUN))]
Reader = Annotated[Principal, Depends(require(Permission.BACKUPS_READ))]


class BackupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: uuid.UUID
    backup_type: BackupType = BackupType.INCREMENTAL
    priority: int = 50  # on-demand jobs go before scheduled ones (100)


class JobOut(BaseModel):
    id: uuid.UUID
    kind: JobKind
    status: JobStatus
    account_id: uuid.UUID | None
    attempts: int
    progress: dict[str, Any] | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def of(cls, j: Job) -> JobOut:
        return cls(
            id=j.id,
            kind=j.kind,
            status=j.status,
            account_id=j.account_id,
            attempts=j.attempts,
            progress=j.progress,
            error=j.error,
            created_at=j.created_at,
            started_at=j.started_at,
            finished_at=j.finished_at,
        )


@router.post("/api/backups", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def start_backup(
    body: BackupRequest, principal: Runner, db: DbSession, request: Request
) -> JobOut:
    account = await db.get(Account, body.account_id)
    if account is None or not principal.access.allows(Permission.BACKUPS_RUN, account.domain_id):
        raise await deny(
            db, principal, request, Permission.BACKUPS_RUN, account=str(body.account_id)
        )
    if account.status is AccountStatus.DELETED:
        raise HTTPException(status.HTTP_409_CONFLICT, "account is deleted")
    job = Job(
        kind=JobKind.BACKUP,
        status=JobStatus.QUEUED,
        account_id=account.id,
        created_by=principal.user.id,
        priority=body.priority,
        params={"backup_type": body.backup_type.value},
    )
    db.add(job)
    await db.flush()
    await db.refresh(job)
    await audit.record(
        db,
        action="backup.request",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        target_type="account",
        target_id=account.email,
        domain_id=account.domain_id,
        source_ip=client_ip(request),
        details={"job": str(job.id), "type": body.backup_type.value},
    )
    return JobOut.of(job)


@router.get("/api/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: uuid.UUID, principal: Reader, db: DbSession, request: Request) -> JobOut:
    job = await db.get(Job, job_id)
    account = await db.get(Account, job.account_id) if job and job.account_id else None
    domain = account.domain_id if account else None
    if job is None or not principal.access.allows(Permission.BACKUPS_READ, domain):
        raise await deny(db, principal, request, Permission.BACKUPS_READ, job=str(job_id))
    return JobOut.of(job)
