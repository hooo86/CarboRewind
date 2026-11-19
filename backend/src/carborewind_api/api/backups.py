"""``/api/backups`` and ``/api/jobs``: start on-demand backups, follow jobs."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from .. import audit
from ..authz import Permission
from ..db.models import Account, AccountStatus, BackupType, Job, JobKind, JobStatus, Outcome
from .deps import DbSession, Principal, client_ip, deny, require
from .schemas import MAX_PAGE, Page

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
    account_email: str | None = None
    backup_type: str | None = None
    # Restore jobs: where from, how, and whether it is only a simulation.
    restore_mode: str | None = None
    dry_run: bool | None = None
    source_email: str | None = None
    snapshot_seq: int | None = None
    attempts: int
    progress: dict[str, Any] | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def of(cls, j: Job, account_email: str | None = None) -> JobOut:
        params = j.params or {}
        restore = j.kind is JobKind.RESTORE
        copies = restore or j.kind is JobKind.MIGRATE
        return cls(
            account_email=account_email,
            backup_type=params.get("backup_type"),
            restore_mode=params.get("mode") if restore else None,
            dry_run=bool(params.get("dry_run")) if copies else None,
            source_email=params.get("source_email") if copies else None,
            snapshot_seq=params.get("seq") if restore else None,
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
    return JobOut.of(job, account.email)


class BulkBackupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_ids: list[uuid.UUID] = Field(min_length=1, max_length=5000)
    backup_type: BackupType = BackupType.INCREMENTAL
    priority: int = 50


class BulkBackupOut(BaseModel):
    queued: int
    skipped_busy: int
    skipped_deleted: int


@router.post(
    "/api/backups/bulk", response_model=BulkBackupOut, status_code=status.HTTP_202_ACCEPTED
)
async def start_bulk_backup(
    body: BulkBackupRequest, principal: Runner, db: DbSession, request: Request
) -> BulkBackupOut:
    """Back up a selection now. All or nothing on permissions; busy or deleted are skipped."""
    ids = set(body.account_ids)
    accounts = list(await db.scalars(select(Account).where(Account.id.in_(ids))))
    if len(accounts) != len(ids) or not all(
        principal.access.allows(Permission.BACKUPS_RUN, a.domain_id) for a in accounts
    ):
        raise await deny(db, principal, request, Permission.BACKUPS_RUN, accounts=str(len(ids)))
    return await queue_backups(
        db, principal, request, accounts, body.backup_type, body.priority, "backup.request_bulk"
    )


async def queue_backups(
    db: DbSession,
    principal: Principal,
    request: Request,
    accounts: list[Account],
    backup_type: BackupType,
    priority: int,
    action: str,
    policy_id: uuid.UUID | None = None,
    **details: Any,
) -> BulkBackupOut:
    """Queue one backup per account now; accounts already queued/running or deleted are
    skipped. Permissions are the caller's job."""
    ids = [a.id for a in accounts]
    busy = set(
        await db.scalars(
            select(Job.account_id).where(
                Job.kind == JobKind.BACKUP,
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                Job.account_id.in_(ids),
            )
        )
    )
    deleted = [a for a in accounts if a.status is AccountStatus.DELETED]
    todo = [a for a in accounts if a.status is not AccountStatus.DELETED and a.id not in busy]
    db.add_all(
        Job(
            kind=JobKind.BACKUP,
            status=JobStatus.QUEUED,
            account_id=a.id,
            policy_id=policy_id,
            created_by=principal.user.id,
            priority=priority,
            params={"backup_type": backup_type.value, "manual": True},
        )
        for a in todo
    )
    await db.flush()
    await audit.record(
        db,
        action=action,
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        details={
            **details,
            "type": backup_type.value,
            "queued": len(todo),
            "accounts": sorted(a.email for a in todo)[:200],
        },
    )
    return BulkBackupOut(
        queued=len(todo),
        skipped_busy=len([a for a in accounts if a.id in busy]),
        skipped_deleted=len(deleted),
    )


class AllBackupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backup_type: BackupType = BackupType.INCREMENTAL
    domain_id: uuid.UUID | None = None


@router.post("/api/backups/all", response_model=BulkBackupOut, status_code=status.HTTP_202_ACCEPTED)
async def start_backup_all(
    body: AllBackupRequest, principal: Runner, db: DbSession, request: Request
) -> BulkBackupOut:
    """Manual backup of every active account the caller may back up (optionally one domain)."""
    query = select(Account).where(Account.status.in_([AccountStatus.ACTIVE, AccountStatus.LOCKED]))
    scope = principal.access.domains(Permission.BACKUPS_RUN)
    if body.domain_id is not None:
        if not principal.access.allows(Permission.BACKUPS_RUN, body.domain_id):
            raise await deny(
                db, principal, request, Permission.BACKUPS_RUN, domain=str(body.domain_id)
            )
        query = query.where(Account.domain_id == body.domain_id)
    elif scope is not None:
        query = query.where(Account.domain_id.in_(scope))
    accounts = list(await db.scalars(query))
    return await queue_backups(
        db,
        principal,
        request,
        accounts,
        body.backup_type,
        50,
        "backup.request_all",
        domain=str(body.domain_id) if body.domain_id else None,
    )


@router.get("/api/jobs", response_model=Page[JobOut])
async def list_jobs(
    principal: Reader,
    db: DbSession,
    status_: Annotated[JobStatus | None, Query(alias="status")] = None,
    account_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[JobOut]:
    """Jobs on the caller's domains, most recent first."""
    query = (
        select(Job, Account.email)
        .join(Account, Account.id == Job.account_id)
        .where(Job.kind != JobKind.FETCH)  # reading a message is not a job worth listing
    )
    scope = principal.access.domains(Permission.BACKUPS_READ)
    if scope is not None:
        query = query.where(Account.domain_id.in_(scope))
    if status_ is not None:
        query = query.where(Job.status == status_)
    if account_id is not None:
        query = query.where(Job.account_id == account_id)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = await db.execute(query.order_by(Job.created_at.desc()).limit(limit).offset(offset))
    return Page(
        items=[JobOut.of(job, email) for job, email in rows.tuples()],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.get("/api/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: uuid.UUID, principal: Reader, db: DbSession, request: Request) -> JobOut:
    job = await db.get(Job, job_id)
    account = await db.get(Account, job.account_id) if job and job.account_id else None
    domain = account.domain_id if account else None
    if job is None or not principal.access.allows(Permission.BACKUPS_READ, domain):
        raise await deny(db, principal, request, Permission.BACKUPS_READ, job=str(job_id))
    return JobOut.of(job, account.email if account else None)
