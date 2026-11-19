"""``/api/archives``: archive policies, search in the archive, reading and restoring messages.

* Policies, simulations and runs: ``archives:manage`` (global administrators: a policy
  deletes messages from mailboxes).
* Summary: ``backups:read`` (figures only, filtered by domain).
* Search, reading a message: ``content:preview`` on the message's domain; every reading is
  audited.
* Restoring: ``restore:run`` on the source and the target domain.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, select

from .. import archiving, audit
from ..authz import Permission
from ..db.models import (
    Account,
    AccountStatus,
    ArchivedMessage,
    ArchivePolicy,
    Domain,
    FetchedContent,
    Job,
    JobKind,
    JobStatus,
    Outcome,
)
from ..scheduling import Cron, CronError, zone
from .backups import JobOut
from .deps import AppSettings, DbSession, Principal, client_ip, deny, require
from .schemas import MAX_PAGE, Page

router = APIRouter(prefix="/api/archives", tags=["archives"])

Manager = Annotated[Principal, Depends(require(Permission.ARCHIVES_MANAGE))]
Reader = Annotated[Principal, Depends(require(Permission.BACKUPS_READ))]
Previewer = Annotated[Principal, Depends(require(Permission.CONTENT_PREVIEW))]
Restorer = Annotated[Principal, Depends(require(Permission.RESTORE_RUN))]

MAX_RESTORE = 5000
MAX_SCOPE = 10_000


# ------------------------------------------------------------------ policies


class PolicyFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    enabled: bool = False
    mode: Literal["simulate", "copy", "archive"] = "simulate"
    domain_ids: list[uuid.UUID] = Field(default_factory=list, max_length=1000)
    account_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_SCOPE)
    exclude_account_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_SCOPE)
    older_than_days: int = Field(default=365, ge=30, le=36_500)
    exclude_folders: list[str] = Field(default_factory=list, max_length=100)
    keep_flagged: bool = True
    keep_unread: bool = False
    include_admins: bool = False
    max_per_run: int = Field(default=5000, ge=1, le=50_000)
    schedule: str = Field(default="30 1 * * *", max_length=128)
    timezone: str = Field(default="UTC", max_length=64)
    retention_days: int = Field(default=0, ge=0, le=36_500)

    @field_validator("exclude_folders")
    @classmethod
    def _folders(cls, value: list[str]) -> list[str]:
        out = []
        for path in value:
            clean = "/" + "/".join(p for p in path.strip().split("/") if p)
            if clean == "/" or len(clean) > 1024:
                raise ValueError(f"invalid folder path {path!r}")
            out.append(clean)
        return sorted(set(out))

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


class PolicyPatch(PolicyFields):
    name: str = Field(default="", max_length=255)


class PolicyOut(BaseModel):
    id: uuid.UUID
    name: str
    enabled: bool
    mode: str
    effective_mode: str
    simulation_valid: bool
    domain_ids: list[uuid.UUID]
    account_ids: list[uuid.UUID]
    exclude_account_ids: list[uuid.UUID]
    older_than_days: int
    exclude_folders: list[str]
    keep_flagged: bool
    keep_unread: bool
    include_admins: bool
    max_per_run: int
    schedule: str
    timezone: str
    retention_days: int
    cutoff: datetime
    criteria_changed_at: datetime
    simulated_at: datetime | None
    last_run_at: datetime | None
    created_at: datetime
    archived_count: int = 0
    archived_bytes: int = 0

    @classmethod
    def of(cls, p: ArchivePolicy, counts: tuple[int, int] = (0, 0)) -> PolicyOut:
        return cls(
            id=p.id,
            name=p.name,
            enabled=p.enabled,
            mode=p.mode,
            effective_mode=archiving.effective_mode(p),
            simulation_valid=archiving.simulation_valid(p),
            domain_ids=[uuid.UUID(d) for d in p.domain_ids],
            account_ids=[uuid.UUID(a) for a in p.account_ids],
            exclude_account_ids=[uuid.UUID(a) for a in p.exclude_account_ids],
            older_than_days=p.older_than_days,
            exclude_folders=list(p.exclude_folders),
            keep_flagged=p.keep_flagged,
            keep_unread=p.keep_unread,
            include_admins=p.include_admins,
            max_per_run=p.max_per_run,
            schedule=p.schedule,
            timezone=p.timezone,
            retention_days=p.retention_days,
            cutoff=archiving.cutoff(p, datetime.now(UTC)),
            criteria_changed_at=p.criteria_changed_at,
            simulated_at=p.simulated_at,
            last_run_at=p.last_run_at,
            created_at=p.created_at,
            archived_count=counts[0],
            archived_bytes=counts[1],
        )


async def _check_scope(db: DbSession, body: PolicyFields) -> None:
    if body.domain_ids:
        found = await db.scalar(
            select(func.count()).select_from(Domain).where(Domain.id.in_(body.domain_ids))
        )
        if found != len(set(body.domain_ids)):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown domain")
    accounts = set(body.account_ids) | set(body.exclude_account_ids)
    if accounts:
        found = await db.scalar(
            select(func.count()).select_from(Account).where(Account.id.in_(accounts))
        )
        if found != len(accounts):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown account")


def _apply(policy: ArchivePolicy, body: PolicyFields, fields: set[str], now: datetime) -> None:
    values = body.model_dump(mode="json")
    changed = False
    for name in fields:
        value = values[name]
        if getattr(policy, name) != value:
            changed = changed or name in archiving.CRITERIA
            setattr(policy, name, value)
    if changed:
        policy.criteria_changed_at = now


def _require_simulation(policy: ArchivePolicy) -> None:
    if policy.mode == "archive" and not archiving.simulation_valid(policy):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "a simulation of these criteria must finish before messages can be deleted",
        )


async def _counts(db: DbSession) -> dict[uuid.UUID, tuple[int, int]]:
    rows = await db.execute(
        select(
            ArchivedMessage.policy_id,
            func.count(),
            func.coalesce(func.sum(ArchivedMessage.size), 0),
        )
        .where(ArchivedMessage.policy_id.is_not(None))
        .group_by(ArchivedMessage.policy_id)
    )
    return {pid: (int(n), int(size)) for pid, n, size in rows if pid is not None}


async def _audit(
    db: DbSession, principal: Principal, request: Request, action: str, **details: Any
) -> None:
    await audit.record(
        db,
        action=action,
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        target_type="archive_policy",
        target_id=str(details.get("policy_id", "")) or None,
        details={k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in details.items()},
    )


@router.get("/policies", response_model=list[PolicyOut])
async def list_policies(principal: Manager, db: DbSession) -> list[PolicyOut]:
    counts = await _counts(db)
    policies = await db.scalars(select(ArchivePolicy).order_by(ArchivePolicy.name))
    return [PolicyOut.of(p, counts.get(p.id, (0, 0))) for p in policies]


@router.post("/policies", response_model=PolicyOut, status_code=status.HTTP_201_CREATED)
async def create_policy(
    body: PolicyFields, principal: Manager, db: DbSession, request: Request
) -> PolicyOut:
    if await db.scalar(select(ArchivePolicy.id).where(ArchivePolicy.name == body.name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "a policy with this name exists")
    await _check_scope(db, body)
    now = datetime.now(UTC)
    policy = ArchivePolicy(name=body.name, created_at=now, criteria_changed_at=now)
    _apply(policy, body, set(PolicyFields.model_fields), now)
    _require_simulation(policy)
    db.add(policy)
    await db.flush()
    await _audit(
        db,
        principal,
        request,
        "archive.policy.create",
        policy_id=policy.id,
        name=policy.name,
        mode=policy.mode,
    )
    return PolicyOut.of(policy)


@router.patch("/policies/{policy_id}", response_model=PolicyOut)
async def update_policy(
    policy_id: uuid.UUID, body: PolicyPatch, principal: Manager, db: DbSession, request: Request
) -> PolicyOut:
    policy = await db.get(ArchivePolicy, policy_id, with_for_update=True)
    if policy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive policy not found")
    fields = set(body.model_fields_set)
    if "name" in fields:
        if not body.name.strip():
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "name is required")
        clash = await db.scalar(
            select(ArchivePolicy.id).where(
                ArchivePolicy.name == body.name, ArchivePolicy.id != policy.id
            )
        )
        if clash:
            raise HTTPException(status.HTTP_409_CONFLICT, "a policy with this name exists")
    await _check_scope(db, body)
    switching = "mode" in fields and body.mode == "archive" and policy.mode != "archive"
    _apply(policy, body, fields, datetime.now(UTC))
    if switching:
        _require_simulation(policy)
    await _audit(
        db,
        principal,
        request,
        "archive.policy.update",
        policy_id=policy.id,
        fields=sorted(fields),
        mode=policy.mode,
    )
    counts = await _counts(db)
    return PolicyOut.of(policy, counts.get(policy.id, (0, 0)))


@router.delete("/policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(
    policy_id: uuid.UUID, principal: Manager, db: DbSession, request: Request
) -> None:
    """Archived messages stay (kept forever once their policy is gone)."""
    policy = await db.get(ArchivePolicy, policy_id)
    if policy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive policy not found")
    await db.delete(policy)
    await _audit(
        db, principal, request, "archive.policy.delete", policy_id=policy_id, name=policy.name
    )


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    simulate: bool = Field(default=False, description="Count only, whatever the policy's mode")


class RunOut(BaseModel):
    run: str
    mode: str
    jobs: int


@router.post("/policies/{policy_id}/run", response_model=RunOut, status_code=202)
async def run_policy(
    policy_id: uuid.UUID, body: RunIn, principal: Manager, db: DbSession, request: Request
) -> RunOut:
    policy = await db.get(ArchivePolicy, policy_id)
    if policy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive policy not found")
    mode = "simulate" if body.simulate else policy.mode
    if mode == "archive":
        _require_simulation(policy)
    run, count = await archiving.enqueue_run(
        db, policy, mode, datetime.now(UTC), created_by=principal.user.id
    )
    await _audit(
        db,
        principal,
        request,
        "archive.policy.run",
        policy_id=policy.id,
        name=policy.name,
        mode=mode,
        jobs=count,
        run=run,
    )
    return RunOut(run=run, mode=mode, jobs=count)


class RunJobOut(BaseModel):
    id: uuid.UUID
    run: str | None
    mode: str | None
    policy: str | None
    account_email: str | None
    status: JobStatus
    progress: dict[str, Any] | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None


@router.get("/runs", response_model=Page[RunJobOut])
async def list_runs(
    principal: Manager,
    db: DbSession,
    policy_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[RunJobOut]:
    """Archive jobs, most recent first (one per account and run)."""
    query = (
        select(Job, Account.email)
        .join(Account, Account.id == Job.account_id)
        .where(Job.kind == JobKind.ARCHIVE)
    )
    if policy_id is not None:
        query = query.where(Job.params["policy_id"].astext == str(policy_id))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = await db.execute(query.order_by(Job.created_at.desc()).limit(limit).offset(offset))
    items = [
        RunJobOut(
            id=job.id,
            run=job.params.get("run"),
            mode=job.params.get("mode"),
            policy=job.params.get("policy"),
            account_email=email,
            status=job.status,
            progress=job.progress,
            error=job.error,
            created_at=job.created_at,
            finished_at=job.finished_at,
        )
        for job, email in rows.tuples()
    ]
    return Page(items=items, total=total or 0, limit=limit, offset=offset)


# ------------------------------------------------------------------ summary


class SummaryOut(BaseModel):
    messages: int
    bytes: int
    removed_messages: int
    removed_bytes: int
    accounts: int
    last_archived_at: datetime | None
    legal_hold_messages: int = 0
    legal_hold_accounts: int = 0


async def summary_for(db: DbSession, domains: set[uuid.UUID] | None) -> SummaryOut:
    removed = ArchivedMessage.removed_at.is_not(None)
    query = select(
        func.count(),
        func.coalesce(func.sum(ArchivedMessage.size), 0),
        func.count().filter(removed),
        func.coalesce(func.sum(ArchivedMessage.size).filter(removed), 0),
        func.count(func.distinct(ArchivedMessage.account_id)),
        func.max(ArchivedMessage.archived_at),
        func.count().filter(ArchivedMessage.legal_hold.is_(True)),
        func.count(func.distinct(ArchivedMessage.account_id)).filter(
            ArchivedMessage.legal_hold.is_(True)
        ),
    ).select_from(ArchivedMessage)
    if domains is not None:
        query = query.join(Account, Account.id == ArchivedMessage.account_id).where(
            Account.domain_id.in_(domains)
        )
    n, size, rn, rsize, accounts, last, held, held_accounts = (await db.execute(query)).one()
    return SummaryOut(
        messages=n,
        bytes=int(size),
        removed_messages=rn,
        removed_bytes=int(rsize),
        accounts=accounts,
        last_archived_at=last,
        legal_hold_messages=held,
        legal_hold_accounts=held_accounts,
    )


@router.get("/summary", response_model=SummaryOut)
async def summary(principal: Reader, db: DbSession) -> SummaryOut:
    return await summary_for(db, principal.access.domains(Permission.BACKUPS_READ))


# ------------------------------------------------------------------ search


class ArchivedMessageOut(BaseModel):
    id: int
    account_id: uuid.UUID
    account_email: str
    folder_path: str
    subject: str | None
    sender: str | None
    recipients: str | None
    item_date: datetime | None
    size: int
    has_attachments: bool
    archived_at: datetime
    removed_at: datetime | None
    legal_hold: bool = False


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/messages", response_model=Page[ArchivedMessageOut])
async def search_messages(
    principal: Previewer,
    db: DbSession,
    account_id: uuid.UUID | None = None,
    account: Annotated[str | None, Query(max_length=255)] = None,
    q: Annotated[str | None, Query(max_length=255, description="Subject contains")] = None,
    sender: Annotated[str | None, Query(max_length=255)] = None,
    recipient: Annotated[str | None, Query(max_length=255)] = None,
    folder: Annotated[str | None, Query(max_length=1024)] = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    removed: bool | None = None,
    legal_hold: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[ArchivedMessageOut]:
    """Archived messages on the caller's domains, most recent first."""
    query = select(ArchivedMessage, Account.email).join(
        Account, Account.id == ArchivedMessage.account_id
    )
    scope = principal.access.domains(Permission.CONTENT_PREVIEW)
    if scope is not None:
        query = query.where(Account.domain_id.in_(scope))
    if account_id is not None:
        query = query.where(ArchivedMessage.account_id == account_id)
    if account:
        query = query.where(Account.email.ilike(_like(account.strip())))
    if q:
        query = query.where(ArchivedMessage.subject.ilike(_like(q.strip())))
    if sender:
        query = query.where(ArchivedMessage.sender.ilike(_like(sender.strip())))
    if recipient:
        query = query.where(ArchivedMessage.recipients.ilike(_like(recipient.strip())))
    if folder:
        path = "/" + folder.strip().strip("/")
        query = query.where(
            or_(
                func.lower(ArchivedMessage.folder_path) == path.lower(),
                ArchivedMessage.folder_path.ilike(_like(path + "/").removeprefix("%")),
            )
        )
    if date_from is not None:
        query = query.where(ArchivedMessage.item_date >= date_from)
    if date_to is not None:
        query = query.where(ArchivedMessage.item_date < date_to)
    if removed is not None:
        query = query.where(
            ArchivedMessage.removed_at.is_not(None)
            if removed
            else ArchivedMessage.removed_at.is_(None)
        )
    if legal_hold is not None:
        query = query.where(ArchivedMessage.legal_hold.is_(legal_hold))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = await db.execute(
        query.order_by(ArchivedMessage.item_date.desc().nulls_last(), ArchivedMessage.id.desc())
        .limit(limit)
        .offset(offset)
    )
    items = [
        ArchivedMessageOut(
            id=m.id,
            account_id=m.account_id,
            account_email=email,
            folder_path=m.folder_path,
            subject=m.subject,
            sender=m.sender,
            recipients=m.recipients,
            item_date=m.item_date,
            size=m.size,
            has_attachments=m.has_attachments,
            archived_at=m.archived_at,
            removed_at=m.removed_at,
            legal_hold=m.legal_hold,
        )
        for m, email in rows.tuples()
    ]
    return Page(items=items, total=total or 0, limit=limit, offset=offset)


# ------------------------------------------------------------------ reading a message


async def _message(
    db: DbSession, principal: Principal, request: Request, perm: Permission, message_id: int
) -> tuple[ArchivedMessage, Account]:
    message = await db.get(ArchivedMessage, message_id)
    account = await db.get(Account, message.account_id) if message else None
    if message is None or account is None or not principal.access.allows(perm, account.domain_id):
        raise await deny(db, principal, request, perm, archived_message=str(message_id))
    return message, account


class OpenOut(BaseModel):
    job_id: uuid.UUID


@router.post("/messages/{message_id}/open", response_model=OpenOut, status_code=202)
async def open_message(
    message_id: int, principal: Previewer, db: DbSession, request: Request
) -> OpenOut:
    """Ask the agent for the message; poll ``GET /api/archives/open/{job_id}``."""
    message, account = await _message(
        db, principal, request, Permission.CONTENT_PREVIEW, message_id
    )
    job = Job(
        kind=JobKind.FETCH,
        status=JobStatus.QUEUED,
        account_id=account.id,
        created_by=principal.user.id,
        priority=archiving.FETCH_PRIORITY,
        max_attempts=1,
        params={
            "blob": message.content_blob_id,
            "max_bytes": archiving.MAX_FETCH_BYTES,
            "message_id": message.id,
        },
    )
    db.add(job)
    await db.flush()
    await audit.record(
        db,
        action="archive.read",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        target_type="account",
        target_id=account.email,
        domain_id=account.domain_id,
        details={"message": message.id, "job": str(job.id)},
    )
    return OpenOut(job_id=job.id)


class OpenedOut(BaseModel):
    status: JobStatus
    error: str | None = None
    message: dict[str, Any] | None = None


async def _fetched(db: DbSession, principal: Principal, request: Request, job_id: uuid.UUID) -> Job:
    """A fetch job, only for the user who asked for it."""
    job = await db.get(Job, job_id)
    if job is None or job.kind is not JobKind.FETCH or job.created_by != principal.user.id:
        raise await deny(db, principal, request, Permission.CONTENT_PREVIEW, fetch=str(job_id))
    return job


@router.get("/open/{job_id}", response_model=OpenedOut)
async def opened_message(
    job_id: uuid.UUID, principal: Previewer, db: DbSession, request: Request, settings: AppSettings
) -> OpenedOut:
    job = await _fetched(db, principal, request, job_id)
    if job.status is not JobStatus.SUCCEEDED:
        return OpenedOut(status=job.status, error=job.error)
    raw = await _raw(db, settings, job)
    return OpenedOut(status=job.status, message=archiving.parse_message(raw))


async def _raw(db: DbSession, settings: AppSettings, job: Job) -> bytes:
    stored = await db.get(FetchedContent, job.id)
    raw = archiving.open_content(settings, stored.data) if stored else None
    if raw is None:
        raise HTTPException(status.HTTP_410_GONE, "expired: open the message again")
    return raw


@router.get("/open/{job_id}/eml")
async def download_message(
    job_id: uuid.UUID, principal: Previewer, db: DbSession, request: Request, settings: AppSettings
) -> Response:
    job = await _fetched(db, principal, request, job_id)
    if job.status is not JobStatus.SUCCEEDED:
        raise HTTPException(status.HTTP_409_CONFLICT, f"message not ready ({job.status.value})")
    raw = await _raw(db, settings, job)
    message_id = int(job.params.get("message_id", 0))
    await audit.record(
        db,
        action="archive.download",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        target_type="archived_message",
        target_id=str(message_id),
        details={"job": str(job.id), "size": len(raw)},
    )
    return Response(
        content=raw,
        media_type="message/rfc822",
        headers={
            "Content-Disposition": f'attachment; filename="message-{message_id}.eml"',
            "X-Content-Type-Options": "nosniff",
        },
    )


# ------------------------------------------------------------------ by mailbox


class ArchivedMailboxOut(BaseModel):
    account_id: uuid.UUID
    account_email: str
    messages: int
    bytes: int
    removed_messages: int
    removed_bytes: int
    legal_hold: bool
    oldest: datetime | None
    newest: datetime | None
    last_archived_at: datetime | None


@router.get("/mailboxes", response_model=Page[ArchivedMailboxOut])
async def archived_mailboxes(
    principal: Reader,
    db: DbSession,
    q: Annotated[str | None, Query(max_length=255)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[ArchivedMailboxOut]:
    """One line per mailbox that has archived messages (figures only)."""
    removed = ArchivedMessage.removed_at.is_not(None)
    query = (
        select(
            Account.id,
            Account.email,
            func.count(ArchivedMessage.id),
            func.coalesce(func.sum(ArchivedMessage.size), 0),
            func.count().filter(removed),
            func.coalesce(func.sum(ArchivedMessage.size).filter(removed), 0),
            func.bool_or(ArchivedMessage.legal_hold),
            func.min(ArchivedMessage.item_date),
            func.max(ArchivedMessage.item_date),
            func.max(ArchivedMessage.archived_at),
        )
        .join(ArchivedMessage, ArchivedMessage.account_id == Account.id)
        .group_by(Account.id, Account.email)
    )
    scope = principal.access.domains(Permission.BACKUPS_READ)
    if scope is not None:
        query = query.where(Account.domain_id.in_(scope))
    if q:
        query = query.where(Account.email.ilike(_like(q.strip())))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = await db.execute(query.order_by(Account.email).limit(limit).offset(offset))
    items = [
        ArchivedMailboxOut(
            account_id=aid,
            account_email=email,
            messages=n,
            bytes=int(size),
            removed_messages=rn,
            removed_bytes=int(rsize),
            legal_hold=bool(held),
            oldest=oldest,
            newest=newest,
            last_archived_at=last,
        )
        for aid, email, n, size, rn, rsize, held, oldest, newest, last in rows
    ]
    return Page(items=items, total=total or 0, limit=limit, offset=offset)


# ------------------------------------------------------------------ restore


class RestoreIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[int] = Field(min_length=1, max_length=MAX_RESTORE)
    target_account_id: uuid.UUID | None = Field(
        default=None, description="Account to restore into (default: each message's own)"
    )
    mode: Literal["new_folder", "original"] = "new_folder"


class MailboxRestoreIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["new_folder", "original"] = "original"
    target_account_id: uuid.UUID | None = None
    only_removed: bool = Field(
        default=True, description="Only the messages no longer in the mailbox"
    )


@router.post("/accounts/{account_id}/restore", response_model=list[JobOut], status_code=202)
async def restore_mailbox(
    account_id: uuid.UUID,
    body: MailboxRestoreIn,
    principal: Restorer,
    db: DbSession,
    request: Request,
) -> list[JobOut]:
    """Every archived message of a mailbox back into Carbonio (by jobs of 5000)."""
    query = select(ArchivedMessage.id).where(ArchivedMessage.account_id == account_id)
    if body.only_removed:
        query = query.where(ArchivedMessage.removed_at.is_not(None))
    ids = list(await db.scalars(query.order_by(ArchivedMessage.id)))
    if not ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "nothing archived to restore")
    jobs: list[JobOut] = []
    for start in range(0, len(ids), MAX_RESTORE):
        jobs += await restore_messages(
            RestoreIn(
                ids=ids[start : start + MAX_RESTORE],
                target_account_id=body.target_account_id,
                mode=body.mode,
            ),
            principal,
            db,
            request,
        )
    return jobs


@router.post("/restore", response_model=list[JobOut], status_code=202)
async def restore_messages(
    body: RestoreIn, principal: Restorer, db: DbSession, request: Request
) -> list[JobOut]:
    """One restore job per source account; messages go back with their dates and flags."""
    messages = list(
        await db.scalars(select(ArchivedMessage).where(ArchivedMessage.id.in_(set(body.ids))))
    )
    if len(messages) != len(set(body.ids)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archived message not found")
    sources: dict[uuid.UUID, Account] = {}
    for m in messages:
        if m.account_id not in sources:
            account = await db.get(Account, m.account_id)
            assert account is not None  # noqa: S101 - FK
            if not principal.access.allows(Permission.RESTORE_RUN, account.domain_id):
                raise await deny(
                    db, principal, request, Permission.RESTORE_RUN, account=str(account.id)
                )
            sources[account.id] = account
    target: Account | None = None
    if body.target_account_id is not None:
        target = await db.get(Account, body.target_account_id)
        if target is None or not principal.access.allows(Permission.RESTORE_RUN, target.domain_id):
            raise await deny(
                db, principal, request, Permission.RESTORE_RUN, account=str(body.target_account_id)
            )
        if target.status is AccountStatus.DELETED:
            raise HTTPException(status.HTTP_409_CONFLICT, "target account is deleted")
    if body.mode == "original" and target is not None and any(s != target.id for s in sources):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "mode 'original' restores into the source account only",
        )
    now = datetime.now(UTC)
    label = f"Archives restaurées {now:%Y-%m-%d %H.%M}"
    jobs: list[JobOut] = []
    for source_id, source in sources.items():
        into = target or source
        if into.status is AccountStatus.DELETED:
            raise HTTPException(status.HTTP_409_CONFLICT, f"{into.email} is deleted")
        items = [
            {
                "id": m.carbonio_item_id,
                "blob": m.content_blob_id,
                "meta_blob": m.meta_blob_id,
                "folder_path": m.folder_path,
            }
            for m in messages
            if m.account_id == source_id
        ]
        job = Job(
            kind=JobKind.RESTORE,
            status=JobStatus.QUEUED,
            account_id=into.id,
            created_by=principal.user.id,
            priority=40,
            max_attempts=3,
            params={
                "source_account_id": str(source.id),
                "source_carbonio_id": source.carbonio_id,
                "source_email": source.email,
                "mode": body.mode,
                "dry_run": False,
                "label": label,
                "item_ids": sorted(i["id"] for i in items),
                "archive_items": items,
            },
        )
        db.add(job)
        await db.flush()
        await db.refresh(job)
        await audit.record(
            db,
            action="archive.restore",
            outcome=Outcome.SUCCESS,
            actor_id=str(principal.user.id),
            actor_name=principal.user.username,
            source_ip=client_ip(request),
            target_type="account",
            target_id=into.email,
            domain_id=into.domain_id,
            details={
                "job": str(job.id),
                "source": source.email,
                "mode": body.mode,
                "messages": len(items),
            },
        )
        jobs.append(JobOut.of(job, into.email))
    return jobs


# ------------------------------------------------------------------ legal hold


class LegalHoldIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(default="", max_length=500, description="Reason (case, request…)")


class LegalHoldOut(BaseModel):
    account_id: uuid.UUID
    account_email: str
    messages: int
    bytes: int
    last_archived_at: datetime | None
    pending: bool = False


@router.post("/accounts/{account_id}/legal-hold", response_model=JobOut, status_code=202)
async def start_legal_hold(
    account_id: uuid.UUID, body: LegalHoldIn, principal: Manager, db: DbSession, request: Request
) -> JobOut:
    """Copy the whole mailbox into the archive under legal hold (it stays in the mailbox)."""
    account = await db.get(Account, account_id)
    if account is None or account.status is AccountStatus.DELETED:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "account not found")
    busy = await db.scalar(
        select(Job.id).where(
            Job.kind == JobKind.ARCHIVE,
            Job.account_id == account.id,
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        )
    )
    if busy is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "this mailbox is already being archived")
    job = await archiving.enqueue_legal_hold(
        db, account, datetime.now(UTC), principal.user.id, body.note.strip()
    )
    await db.refresh(job)
    await audit.record(
        db,
        action="archive.legal_hold",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        target_type="account",
        target_id=account.email,
        domain_id=account.domain_id,
        details={"job": str(job.id), "note": body.note.strip()},
    )
    return JobOut.of(job, account.email)


@router.delete("/accounts/{account_id}/legal-hold", status_code=status.HTTP_204_NO_CONTENT)
async def release_legal_hold(
    account_id: uuid.UUID, principal: Manager, db: DbSession, request: Request
) -> None:
    """End the hold: the messages stay archived, under their policy's retention again."""
    account = await db.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "account not found")
    released = await archiving.release_legal_hold(db, account)
    await audit.record(
        db,
        action="archive.legal_hold.release",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        target_type="account",
        target_id=account.email,
        domain_id=account.domain_id,
        details={"messages": released},
    )


@router.get("/legal-holds", response_model=list[LegalHoldOut])
async def list_legal_holds(principal: Manager, db: DbSession) -> list[LegalHoldOut]:
    rows = await db.execute(
        select(
            Account.id,
            Account.email,
            func.count(ArchivedMessage.id),
            func.coalesce(func.sum(ArchivedMessage.size), 0),
            func.max(ArchivedMessage.archived_at),
        )
        .join(ArchivedMessage, ArchivedMessage.account_id == Account.id)
        .where(ArchivedMessage.legal_hold.is_(True))
        .group_by(Account.id, Account.email)
        .order_by(Account.email)
    )
    pending = set(
        await db.scalars(
            select(Job.account_id).where(
                Job.kind == JobKind.ARCHIVE,
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                Job.params["legal_hold"].astext == "true",
            )
        )
    )
    out = [
        LegalHoldOut(
            account_id=aid,
            account_email=email,
            messages=n,
            bytes=int(size),
            last_archived_at=last,
            pending=aid in pending,
        )
        for aid, email, n, size, last in rows
    ]
    listed = {o.account_id for o in out}
    for aid in pending - listed:  # first run still going: nothing archived yet
        account = await db.get(Account, aid)
        if account is not None:
            out.append(
                LegalHoldOut(
                    account_id=aid,
                    account_email=account.email,
                    messages=0,
                    bytes=0,
                    last_archived_at=None,
                    pending=True,
                )
            )
    return sorted(out, key=lambda o: o.account_email)
