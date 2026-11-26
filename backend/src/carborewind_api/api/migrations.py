"""``/api/migrations``: copy mailboxes from a remote IMAP server into Carbonio.

* **Sources**: IMAP servers (host, port, TLS, authentication mode). With a master user the
  administrator's password is stored once; otherwise each mailbox carries its own.
* **Batches**: a list of source mailboxes, each matched to a Carbonio account (see
  :mod:`carborewind_api.migration_tools`), filled by hand or from an Excel/CSV file, with
  options (root folder, excluded folders, date range).
* **Runs**: ``start`` queues one ``migrate`` job per mailbox for the agent; running it again
  copies only what is new. ``dry_run`` connects and counts without writing (a connection
  test). ``cancel`` stops queued and running jobs.

Every password is write-only: encrypted with ``CARBOREWIND_SECRET_KEY`` (Fernet), never
returned, never put in job parameters or the audit trail; it reaches the agent only in the
answer to its (mutual TLS) job claim. ``forget-passwords`` erases them once the migration is
over. Everything here requires ``migrations:manage`` (global administrators).
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from .. import audit
from ..authz import Permission
from ..config import Settings
from ..db.models import (
    Account,
    AccountStatus,
    ImapSecurity,
    Job,
    JobKind,
    JobStatus,
    MigrationAuth,
    MigrationBatch,
    MigrationMailbox,
    MigrationSource,
    Outcome,
)
from ..migration_tools import (
    MAX_FILE_BYTES,
    MAX_ROWS,
    ROOT_RE,
    ImportFileError,
    Matcher,
    Row,
    Target,
    make_row,
    parse_file,
    template_csv,
    template_xlsx,
)
from ..notifications import SecretKeyError, encrypt
from .deps import AppSettings, DbSession, Principal, client_ip, require

router = APIRouter(prefix="/api/migrations", tags=["migrations"])

Admin = Annotated[Principal, Depends(require(Permission.MIGRATIONS_MANAGE))]

HOST_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
ACTIVE = (JobStatus.QUEUED, JobStatus.RUNNING)
MAX_EXCLUDES = 100


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
        target_type="migration",
        target_id=str(details.pop("target", "")) or None,
        details=details,
    )


def _encrypt(settings: Settings, value: str) -> str:
    try:
        return encrypt(settings, value)
    except SecretKeyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


# ------------------------------------------------------------------ sources


class SourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    host: str = Field(max_length=253)
    port: int = Field(default=993, ge=1, le=65535)
    security: ImapSecurity = ImapSecurity.SSL
    verify_tls: bool = True
    auth_mode: MigrationAuth = MigrationAuth.PASSWORD
    master_username: str | None = Field(default=None, max_length=320)
    # Write-only: omitted/null keeps the stored one, "" clears it.
    master_password: str | None = Field(default=None, max_length=1024, repr=False)
    master_separator: str = Field(default="*", min_length=1, max_length=8)

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        value = value.strip().lower()
        try:
            ipaddress.ip_address(value)
        except ValueError:
            if not HOST_RE.match(value):
                raise ValueError("host is a DNS name or an IP address") from None
        return value

    @field_validator("master_username", "master_separator")
    @classmethod
    def _single_line(cls, value: str | None) -> str | None:
        if value is not None and any(c in value for c in "\r\n\x00 "):
            raise ValueError("invalid characters")
        return value or None

    @field_validator("master_password")
    @classmethod
    def _password(cls, value: str | None) -> str | None:
        if value and any(c in value for c in "\r\n\x00"):
            raise ValueError("invalid characters")
        return value


class SourceOut(BaseModel):
    id: uuid.UUID
    name: str
    host: str
    port: int
    security: ImapSecurity
    verify_tls: bool
    auth_mode: MigrationAuth
    master_username: str | None
    master_password_set: bool
    master_separator: str
    batches: int = 0

    @classmethod
    def of(cls, s: MigrationSource, batches: int = 0) -> SourceOut:
        return cls(
            id=s.id,
            name=s.name,
            host=s.host,
            port=s.port,
            security=s.security,
            verify_tls=s.verify_tls,
            auth_mode=s.auth_mode,
            master_username=s.master_username,
            master_password_set=bool(s.master_password_enc),
            master_separator=s.master_separator,
            batches=batches,
        )


def _apply_source(row: MigrationSource, body: SourceIn, settings: Settings) -> None:
    row.name, row.host, row.port = body.name.strip(), body.host, body.port
    row.security, row.verify_tls, row.auth_mode = body.security, body.verify_tls, body.auth_mode
    row.master_separator = body.master_separator
    if body.auth_mode is MigrationAuth.PASSWORD:
        row.master_username, row.master_password_enc = None, None
        return
    row.master_username = body.master_username
    if body.master_password == "":
        row.master_password_enc = None
    elif body.master_password is not None:
        row.master_password_enc = _encrypt(settings, body.master_password)
    if not row.master_username or not row.master_password_enc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "a master user and its password are required for this authentication mode",
        )


@router.get("/sources", response_model=list[SourceOut])
async def list_sources(principal: Admin, db: DbSession) -> list[SourceOut]:
    counts = dict(
        (
            await db.execute(
                select(MigrationBatch.source_id, func.count()).group_by(MigrationBatch.source_id)
            )
        ).all()
    )
    rows = await db.scalars(select(MigrationSource).order_by(MigrationSource.name))
    return [SourceOut.of(s, counts.get(s.id, 0)) for s in rows]


@router.post("/sources", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
async def create_source(
    body: SourceIn, principal: Admin, db: DbSession, settings: AppSettings, request: Request
) -> SourceOut:
    row = MigrationSource()
    _apply_source(row, body, settings)
    db.add(row)
    await _flush_unique(db, "a source with this name already exists")
    await _audit(db, principal, request, "migration.source_create", target=row.id, host=row.host)
    return SourceOut.of(row)


@router.put("/sources/{source_id}", response_model=SourceOut)
async def update_source(
    source_id: uuid.UUID,
    body: SourceIn,
    principal: Admin,
    db: DbSession,
    settings: AppSettings,
    request: Request,
) -> SourceOut:
    row = await _get(db, MigrationSource, source_id)
    _apply_source(row, body, settings)
    await _flush_unique(db, "a source with this name already exists")
    await _audit(
        db,
        principal,
        request,
        "migration.source_update",
        target=row.id,
        host=row.host,
        password_changed=body.master_password is not None,
    )
    return SourceOut.of(row)


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(
    source_id: uuid.UUID, principal: Admin, db: DbSession, request: Request
) -> None:
    row = await _get(db, MigrationSource, source_id)
    if await db.scalar(select(MigrationBatch.id).where(MigrationBatch.source_id == row.id)):
        raise HTTPException(status.HTTP_409_CONFLICT, "batches still use this source")
    await db.delete(row)
    await _audit(db, principal, request, "migration.source_delete", target=source_id)


# ------------------------------------------------------------------ batches


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str | None = Field(default=None, max_length=128)
    exclude_folders: list[str] = Field(default_factory=list, max_length=MAX_EXCLUDES)
    since: date | None = None
    before: date | None = None

    @field_validator("root")
    @classmethod
    def _root(cls, value: str | None) -> str | None:
        value = (value or "").strip().strip("/") or None
        if value and not ROOT_RE.match(value):
            raise ValueError("invalid folder name")
        return value

    @field_validator("exclude_folders")
    @classmethod
    def _excludes(cls, value: list[str]) -> list[str]:
        out = [v.strip() for v in value if v.strip()]
        if any(len(v) > 255 or any(c in v for c in "\r\n\x00") for v in out):
            raise ValueError("invalid folder name")
        return out

    @model_validator(mode="after")
    def _range(self) -> Options:
        if self.since and self.before and self.since >= self.before:
            raise ValueError("since must be before 'before'")
        return self


class BatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    source_id: uuid.UUID
    options: Options = Field(default_factory=Options)


class BatchOut(BaseModel):
    id: uuid.UUID
    name: str
    source_id: uuid.UUID
    source_name: str
    auth_mode: MigrationAuth
    options: dict[str, Any]
    created_at: datetime
    counts: dict[str, int]


class MailboxOut(BaseModel):
    id: uuid.UUID
    source_email: str
    source_login: str | None
    password_set: bool
    target_account_id: uuid.UUID | None
    target_email: str | None
    target_status: AccountStatus | None
    match: str | None
    root: str | None
    status: str  # new, queued, running, succeeded, failed, cancelled
    dry_run: bool | None
    progress: dict[str, Any] | None
    stats: dict[str, Any] | None
    error: str | None
    last_run_at: datetime | None


class BatchDetail(BatchOut):
    mailboxes: list[MailboxOut]


async def _get(db: DbSession, model: Any, row_id: uuid.UUID) -> Any:
    row = await db.get(model, row_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return row


async def _flush_unique(db: DbSession, message: str) -> None:
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, message) from exc


async def _mailbox_rows(
    db: DbSession, batch_ids: list[uuid.UUID]
) -> list[tuple[MigrationMailbox, Account | None, Job | None]]:
    rows = await db.execute(
        select(MigrationMailbox, Account, Job)
        .outerjoin(Account, Account.id == MigrationMailbox.target_account_id)
        .outerjoin(Job, Job.id == MigrationMailbox.last_job_id)
        .where(MigrationMailbox.batch_id.in_(batch_ids))
        .order_by(MigrationMailbox.source_email, Account.email)
    )
    return [(m, a, j) for m, a, j in rows]


def _status(job: Job | None) -> str:
    return job.status.value if job is not None else "new"


def _counts(rows: list[tuple[MigrationMailbox, Account | None, Job | None]]) -> dict[str, int]:
    counts = dict.fromkeys(
        ("mailboxes", "matched", "unmatched", "new", "queued", "running", "succeeded", "failed"),
        0,
    )
    for m, account, job in rows:
        counts["mailboxes"] += 1
        counts["matched" if account is not None else "unmatched"] += 1
        status_ = _status(job)
        if job is not None and job.params.get("dry_run"):
            status_ = "new" if status_ in ("succeeded", "cancelled") else status_
        counts[status_] = counts.get(status_, 0) + 1
        counts["copied"] = counts.get("copied", 0) + int((m.stats or {}).get("total_copied", 0))
    return counts


def _batch_out(
    batch: MigrationBatch,
    source: MigrationSource,
    rows: list[tuple[MigrationMailbox, Account | None, Job | None]],
) -> BatchOut:
    return BatchOut(
        id=batch.id,
        name=batch.name,
        source_id=source.id,
        source_name=source.name,
        auth_mode=source.auth_mode,
        options=batch.options,
        created_at=batch.created_at,
        counts=_counts(rows),
    )


def _mailbox_out(m: MigrationMailbox, account: Account | None, job: Job | None) -> MailboxOut:
    return MailboxOut(
        id=m.id,
        source_email=m.source_email,
        source_login=m.source_login,
        password_set=bool(m.password_enc),
        target_account_id=m.target_account_id,
        target_email=account.email if account else None,
        target_status=account.status if account else None,
        match=m.match,
        root=m.root,
        status=_status(job),
        dry_run=bool(job.params.get("dry_run")) if job else None,
        progress=job.progress if job else None,
        stats=m.stats,
        error=job.error if job else None,
        last_run_at=m.last_run_at,
    )


@router.get("/batches", response_model=list[BatchOut])
async def list_batches(principal: Admin, db: DbSession) -> list[BatchOut]:
    pairs = (
        await db.execute(
            select(MigrationBatch, MigrationSource)
            .join(MigrationSource, MigrationSource.id == MigrationBatch.source_id)
            .order_by(MigrationBatch.created_at.desc())
        )
    ).all()
    pairs_list = list(pairs)
    rows = await _mailbox_rows(db, [b.id for b, _ in pairs_list])
    by_batch: dict[uuid.UUID, list[tuple[MigrationMailbox, Account | None, Job | None]]] = {}
    for row in rows:
        by_batch.setdefault(row[0].batch_id, []).append(row)
    return [_batch_out(b, s, by_batch.get(b.id, [])) for b, s in pairs_list]


@router.post("/batches", response_model=BatchOut, status_code=status.HTTP_201_CREATED)
async def create_batch(
    body: BatchIn, principal: Admin, db: DbSession, request: Request
) -> BatchOut:
    source = await _get(db, MigrationSource, body.source_id)
    batch = MigrationBatch(
        name=body.name.strip(),
        source_id=source.id,
        options=body.options.model_dump(mode="json"),
        created_by=principal.user.id,
    )
    db.add(batch)
    await _flush_unique(db, "a batch with this name already exists")
    await db.refresh(batch)
    await _audit(db, principal, request, "migration.batch_create", target=batch.id, name=batch.name)
    return _batch_out(batch, source, [])


async def _batch(db: DbSession, batch_id: uuid.UUID) -> tuple[MigrationBatch, MigrationSource]:
    batch = await _get(db, MigrationBatch, batch_id)
    source = await db.get(MigrationSource, batch.source_id)
    assert source is not None  # noqa: S101 - FK RESTRICT
    return batch, source


@router.get("/batches/{batch_id}", response_model=BatchDetail)
async def get_batch(batch_id: uuid.UUID, principal: Admin, db: DbSession) -> BatchDetail:
    batch, source = await _batch(db, batch_id)
    rows = await _mailbox_rows(db, [batch.id])
    out = _batch_out(batch, source, rows)
    return BatchDetail(**out.model_dump(), mailboxes=[_mailbox_out(*r) for r in rows])


@router.put("/batches/{batch_id}", response_model=BatchOut)
async def update_batch(
    batch_id: uuid.UUID, body: BatchIn, principal: Admin, db: DbSession, request: Request
) -> BatchOut:
    batch, _ = await _batch(db, batch_id)
    source = await _get(db, MigrationSource, body.source_id)
    batch.name, batch.source_id = body.name.strip(), source.id
    batch.options = body.options.model_dump(mode="json")
    await _flush_unique(db, "a batch with this name already exists")
    await _audit(db, principal, request, "migration.batch_update", target=batch.id)
    return _batch_out(batch, source, await _mailbox_rows(db, [batch.id]))


async def _active_jobs(db: DbSession, batch_id: uuid.UUID) -> list[Job]:
    return list(
        await db.scalars(
            select(Job).where(
                Job.kind == JobKind.MIGRATE,
                Job.status.in_(ACTIVE),
                Job.params["batch_id"].astext == str(batch_id),
            )
        )
    )


@router.delete("/batches/{batch_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_batch(
    batch_id: uuid.UUID, principal: Admin, db: DbSession, request: Request
) -> None:
    batch, _ = await _batch(db, batch_id)
    if any(j.status is JobStatus.RUNNING for j in await _active_jobs(db, batch.id)):
        raise HTTPException(status.HTTP_409_CONFLICT, "cancel the running copies first")
    await _cancel(db, batch.id)
    await db.delete(batch)
    await _audit(db, principal, request, "migration.batch_delete", target=batch_id)


# ------------------------------------------------------------------ mailboxes


class RowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_email: str = Field(max_length=320)
    source_login: str | None = Field(default=None, max_length=320)
    password: str | None = Field(default=None, max_length=1024, repr=False)
    target_email: str | None = Field(default=None, max_length=320)
    root: str | None = Field(default=None, max_length=128)


class RowsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[RowIn] = Field(max_length=MAX_ROWS)
    target_domain: str | None = Field(default=None, max_length=253)


class ImportReport(BaseModel):
    added: int
    updated: int
    matched: int
    unmatched: list[dict[str, Any]]
    errors: list[dict[str, Any]]


async def _matcher(db: DbSession) -> Matcher:
    accounts = await db.execute(
        select(Account.id, Account.email, Account.aliases).where(
            Account.status != AccountStatus.DELETED
        )
    )
    return Matcher(Target(i, e, tuple(a or ())) for i, e, a in accounts)


async def _add_rows(
    db: DbSession,
    settings: Settings,
    batch: MigrationBatch,
    rows: list[Row],
    errors: list[dict[str, Any]],
    target_domain: str | None,
) -> ImportReport:
    matcher = await _matcher(db)
    existing = {
        (m.source_email, m.target_account_id): m
        for m in await db.scalars(
            select(MigrationMailbox).where(MigrationMailbox.batch_id == batch.id)
        )
    }
    added = updated = matched = 0
    unmatched: list[dict[str, Any]] = []
    for row in rows:
        result = matcher.match(row.source_email, row.target_email, target_domain)
        if result.error:
            errors.append({"line": row.line, "message": result.error})
            continue
        target_id = result.target.id if result.target else None
        mailbox = existing.get((row.source_email, target_id))
        if mailbox is None and target_id is None:
            # Already in the batch with a destination (chosen by hand or with a domain):
            # an import without a better answer keeps it.
            known = [m for (src, _), m in existing.items() if src == row.source_email]
            if known:
                mailbox, target_id = known[0], known[0].target_account_id
                result.target, result.how = None, known[0].match
        if mailbox is None and target_id is not None:
            # A previously unmatched entry for this source now gets its target.
            mailbox = existing.pop((row.source_email, None), None)
        if mailbox is None:
            mailbox = MigrationMailbox(batch_id=batch.id, source_email=row.source_email)
            db.add(mailbox)
            added += 1
        else:
            updated += 1
        existing[(row.source_email, target_id)] = mailbox
        mailbox.target_account_id, mailbox.match = target_id, result.how
        if row.source_login:
            mailbox.source_login = row.source_login
        if row.password:
            mailbox.password_enc = _encrypt(settings, row.password)
        if row.root:
            mailbox.root = row.root
        if target_id is not None:
            matched += 1
        else:
            unmatched.append(
                {"line": row.line, "source": row.source_email, "candidates": result.candidates}
            )
    await db.flush()
    return ImportReport(
        added=added, updated=updated, matched=matched, unmatched=unmatched, errors=errors
    )


def _domain(value: str | None) -> str | None:
    value = (value or "").strip().lower().lstrip("@") or None
    if value and not HOST_RE.match(value):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid destination domain")
    return value


@router.post("/batches/{batch_id}/mailboxes", response_model=ImportReport)
async def add_mailboxes(
    batch_id: uuid.UUID,
    body: RowsIn,
    principal: Admin,
    db: DbSession,
    settings: AppSettings,
    request: Request,
) -> ImportReport:
    """Add mailboxes typed by hand; the destination is matched when not given."""
    batch, _ = await _batch(db, batch_id)
    rows: list[Row] = []
    errors: list[dict[str, Any]] = []
    for line, r in enumerate(body.rows, start=1):
        row, error = make_row(line, r.model_dump())
        if error:
            errors.append({"line": line, "message": error})
        elif row:
            rows.append(row)
    report = await _add_rows(db, settings, batch, rows, errors, _domain(body.target_domain))
    await _audit(
        db,
        principal,
        request,
        "migration.mailboxes_add",
        target=batch.id,
        added=report.added,
        updated=report.updated,
        errors=len(report.errors),
    )
    return report


@router.post(
    "/batches/{batch_id}/import",
    response_model=ImportReport,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
async def import_file(
    batch_id: uuid.UUID,
    principal: Admin,
    db: DbSession,
    settings: AppSettings,
    request: Request,
    target_domain: Annotated[str | None, Query(max_length=253)] = None,
) -> ImportReport:
    """Import an ``.xlsx`` or ``.csv`` file sent as the raw request body (5 MB max)."""
    batch, _ = await _batch(db, batch_id)
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file too large")
    try:
        parsed = parse_file(bytes(data))
    except ImportFileError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    errors = [{"line": e.line, "message": e.message} for e in parsed.errors]
    report = await _add_rows(db, settings, batch, parsed.rows, errors, _domain(target_domain))
    await _audit(
        db,
        principal,
        request,
        "migration.import",
        target=batch.id,
        rows=len(parsed.rows),
        added=report.added,
        updated=report.updated,
        errors=len(report.errors),
    )
    return report


class MailboxPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_email: str | None = Field(default=None, max_length=320)
    clear_target: bool = False
    source_login: str | None = Field(default=None, max_length=320)
    password: str | None = Field(default=None, max_length=1024, repr=False)
    root: str | None = Field(default=None, max_length=128)


async def _mailbox(db: DbSession, mailbox_id: uuid.UUID) -> MigrationMailbox:
    mailbox: MigrationMailbox = await _get(db, MigrationMailbox, mailbox_id)
    return mailbox


async def _busy(db: DbSession, mailbox: MigrationMailbox) -> bool:
    if mailbox.last_job_id is None:
        return False
    job = await db.get(Job, mailbox.last_job_id)
    return job is not None and job.status in ACTIVE


@router.patch("/mailboxes/{mailbox_id}", response_model=MailboxOut)
async def update_mailbox(
    mailbox_id: uuid.UUID,
    body: MailboxPatch,
    principal: Admin,
    db: DbSession,
    settings: AppSettings,
    request: Request,
) -> MailboxOut:
    mailbox = await _mailbox(db, mailbox_id)
    if await _busy(db, mailbox):
        raise HTTPException(status.HTTP_409_CONFLICT, "a copy of this mailbox is in progress")
    fields = body.model_dump(exclude_unset=True)
    row, error = make_row(
        0,
        {
            "source_email": mailbox.source_email,
            "target_email": body.target_email,
            "source_login": body.source_login,
            "password": body.password,
            "root": body.root,
        },
    )
    if error or row is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, error)
    if body.clear_target:
        mailbox.target_account_id, mailbox.match = None, None
    elif row.target_email:
        target = (await _matcher(db)).resolve(row.target_email)
        if target is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"destination {row.target_email} not found in Carbonio",
            )
        if target.id != mailbox.target_account_id:
            mailbox.state = {}  # another mailbox: start from scratch
        mailbox.target_account_id, mailbox.match = target.id, "manual"
    if "source_login" in fields:
        mailbox.source_login = row.source_login
    if "password" in fields:
        mailbox.password_enc = _encrypt(settings, row.password) if row.password else None
    if "root" in fields:
        mailbox.root = row.root
    await _flush_unique(db, "this source is already copied into that account in this batch")
    await _audit(
        db,
        principal,
        request,
        "migration.mailbox_update",
        target=mailbox.id,
        fields=sorted(k for k in fields if k != "password"),
        password_changed="password" in fields,
    )
    rows = [r for r in await _mailbox_rows(db, [mailbox.batch_id]) if r[0].id == mailbox.id]
    return _mailbox_out(*rows[0])


@router.delete("/mailboxes/{mailbox_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mailbox(
    mailbox_id: uuid.UUID, principal: Admin, db: DbSession, request: Request
) -> None:
    mailbox = await _mailbox(db, mailbox_id)
    if await _busy(db, mailbox):
        raise HTTPException(status.HTTP_409_CONFLICT, "a copy of this mailbox is in progress")
    await db.delete(mailbox)
    await _audit(
        db,
        principal,
        request,
        "migration.mailbox_delete",
        target=mailbox_id,
        source=mailbox.source_email,
    )


class MatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_domain: str | None = Field(default=None, max_length=253)
    # False: only mailboxes without a destination; True: also redo automatic matches.
    all: bool = False


@router.post("/batches/{batch_id}/match", response_model=ImportReport)
async def rematch(
    batch_id: uuid.UUID, body: MatchRequest, principal: Admin, db: DbSession, request: Request
) -> ImportReport:
    """Match again (after new accounts appeared in Carbonio, or with a domain)."""
    batch, _ = await _batch(db, batch_id)
    domain = _domain(body.target_domain)
    matcher = await _matcher(db)
    mailboxes = list(
        await db.scalars(select(MigrationMailbox).where(MigrationMailbox.batch_id == batch.id))
    )
    taken = {(m.source_email, m.target_account_id) for m in mailboxes}
    matched = updated = 0
    unmatched: list[dict[str, Any]] = []
    for m in mailboxes:
        if m.match == "manual" or (m.target_account_id is not None and not body.all):
            continue
        if await _busy(db, m):
            continue
        result = matcher.match(m.source_email, None, domain)
        new_id = result.target.id if result.target else None
        if new_id != m.target_account_id and (m.source_email, new_id) in taken:
            continue  # that pair already exists in the batch
        if new_id != m.target_account_id:
            taken.discard((m.source_email, m.target_account_id))
            taken.add((m.source_email, new_id))
            m.target_account_id, m.match, m.state = new_id, result.how, {}
            updated += 1
        if result.target:
            matched += 1
        else:
            unmatched.append({"line": 0, "source": m.source_email, "candidates": result.candidates})
    await db.flush()
    await _audit(db, principal, request, "migration.match", target=batch.id, updated=updated)
    return ImportReport(added=0, updated=updated, matched=matched, unmatched=unmatched, errors=[])


# ------------------------------------------------------------------ runs


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mailbox_ids: list[uuid.UUID] | None = Field(default=None, max_length=MAX_ROWS)
    dry_run: bool = False


class StartResult(BaseModel):
    queued: int
    skipped: list[dict[str, str]]


@router.post("/batches/{batch_id}/start", response_model=StartResult)
async def start(
    batch_id: uuid.UUID, body: StartRequest, principal: Admin, db: DbSession, request: Request
) -> StartResult:
    """Queue one copy (or connection test with ``dry_run``) per mailbox."""
    batch, source = await _batch(db, batch_id)
    rows = await _mailbox_rows(db, [batch.id])
    wanted = set(body.mailbox_ids) if body.mailbox_ids is not None else None
    queued = 0
    skipped: list[dict[str, str]] = []
    for mailbox, account, job in rows:
        if wanted is not None and mailbox.id not in wanted:
            continue
        reason = None
        if account is None:
            reason = "no destination"
        elif account.status is AccountStatus.DELETED:
            reason = "destination deleted"
        elif source.auth_mode is MigrationAuth.PASSWORD and not mailbox.password_enc:
            reason = "no password"
        elif job is not None and job.status in ACTIVE:
            reason = "already queued or running"
        if reason or account is None:
            skipped.append(
                {"id": str(mailbox.id), "source": mailbox.source_email, "reason": reason or ""}
            )
            continue
        new_job = Job(
            kind=JobKind.MIGRATE,
            status=JobStatus.QUEUED,
            account_id=account.id,
            created_by=principal.user.id,
            priority=60 if body.dry_run else 120,  # tests before copies, after backups
            max_attempts=1 if body.dry_run else 5,
            params={
                "batch_id": str(batch.id),
                "mailbox_id": str(mailbox.id),
                "source_email": mailbox.source_email,
                "dry_run": body.dry_run,
            },
        )
        db.add(new_job)
        await db.flush()
        mailbox.last_job_id = new_job.id
        queued += 1
    await _audit(
        db,
        principal,
        request,
        "migration.start",
        target=batch.id,
        queued=queued,
        skipped=len(skipped),
        dry_run=body.dry_run,
    )
    return StartResult(queued=queued, skipped=skipped)


async def _cancel(db: DbSession, batch_id: uuid.UUID) -> int:
    jobs = await _active_jobs(db, batch_id)
    if jobs:
        await db.execute(
            update(Job)
            .where(Job.id.in_([j.id for j in jobs]))
            .values(status=JobStatus.CANCELLED, finished_at=func.now(), lease_expires_at=None)
        )
    return len(jobs)


class CancelResult(BaseModel):
    cancelled: int


@router.post("/batches/{batch_id}/cancel", response_model=CancelResult)
async def cancel(
    batch_id: uuid.UUID, principal: Admin, db: DbSession, request: Request
) -> CancelResult:
    """Cancel queued copies; running ones stop at the next message (state is kept)."""
    batch, _ = await _batch(db, batch_id)
    count = await _cancel(db, batch.id)
    await _audit(db, principal, request, "migration.cancel", target=batch.id, jobs=count)
    return CancelResult(cancelled=count)


@router.post("/batches/{batch_id}/forget-passwords", status_code=status.HTTP_204_NO_CONTENT)
async def forget_passwords(
    batch_id: uuid.UUID, principal: Admin, db: DbSession, request: Request
) -> None:
    batch, _ = await _batch(db, batch_id)
    result = await db.execute(
        update(MigrationMailbox)
        .where(MigrationMailbox.batch_id == batch.id, MigrationMailbox.password_enc.is_not(None))
        .values(password_enc=None)
    )
    await _audit(
        db,
        principal,
        request,
        "migration.forget_passwords",
        target=batch.id,
        mailboxes=int(result.rowcount or 0),  # type: ignore[attr-defined]
    )


@router.get(
    "/template",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}, "application/vnd.ms-excel": {}}}},
)
async def template(
    principal: Admin,
    format: Literal["xlsx", "csv"] = "xlsx",
) -> Response:
    """Blank list to fill in (Excel or CSV)."""
    if format == "csv":
        return Response(
            template_csv(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="carborewind-migration.csv"'},
        )
    return Response(
        template_xlsx(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="carborewind-migration.xlsx"'},
    )
