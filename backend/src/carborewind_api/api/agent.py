"""``/api/agent``: endpoints called by agents (enrolment, inventory, job protocol).

Authentication: mutual TLS terminated by the reverse proxy. The proxy verifies the client
certificate against the CarboRewind CA and forwards it (URL-encoded PEM) together with the
verification status; the API accepts these headers **only from** ``agent_proxy_ips`` and maps
the certificate fingerprint to an active agent. Enrolment itself uses a one-time token.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from .. import audit
from ..db.models import (
    Account,
    AccountStatus,
    ActorType,
    Agent,
    AgentStatus,
    BackupType,
    Blob,
    Domain,
    FolderVersion,
    ItemKind,
    ItemVersion,
    Job,
    JobKind,
    JobStatus,
    Outcome,
    Snapshot,
    SnapshotStatus,
)
from ..pki import CertificateAuthority, PkiError, fingerprint_pem, token_digest
from ..scheduler import orphan_blobs_query, purge_plan
from .deps import AppSettings, DbSession, client_ip

router = APIRouter(prefix="/api/agent", tags=["agent protocol"])


# ------------------------------------------------------------------ authentication


async def current_agent(request: Request, db: DbSession, settings: AppSettings) -> Agent:
    peer = request.client.host if request.client else None
    secret = settings.agent_proxy_secret
    via_secret = secret is not None and secrets.compare_digest(
        request.headers.get("X-Proxy-Auth", "").encode(), secret.get_secret_value().encode()
    )
    if not via_secret and peer not in settings.agent_proxy_ips:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "agent endpoints are only reachable via the proxy"
        )
    if request.headers.get(settings.agent_verify_header) != "SUCCESS":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "client certificate required")
    pem = unquote(request.headers.get(settings.agent_cert_header, ""))
    try:
        fp, not_after = fingerprint_pem(pem)
    except PkiError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid client certificate") from exc
    agent = await db.scalar(select(Agent).where(Agent.cert_fingerprint == fp))
    if agent is None or agent.status is not AgentStatus.ACTIVE or not_after < datetime.now(UTC):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown, revoked or expired agent")
    agent.last_heartbeat_at = datetime.now(UTC)
    return agent


CurrentAgent = Annotated[Agent, Depends(current_agent)]


async def _audit(db: DbSession, agent: Agent, action: str, outcome: Outcome, **kw: Any) -> None:
    await audit.record(
        db,
        action=action,
        outcome=outcome,
        actor_type=ActorType.AGENT,
        actor_id=str(agent.id),
        actor_name=agent.name,
        **kw,
    )


# ------------------------------------------------------------------ enrolment


class EnrollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=128, repr=False)
    csr_pem: str = Field(max_length=8192)
    version: str | None = Field(default=None, max_length=64)


class EnrollResponse(BaseModel):
    agent_id: uuid.UUID
    certificate_pem: str
    ca_pem: str
    not_after: datetime


@router.post("/enroll", response_model=EnrollResponse)
async def enroll(
    body: EnrollRequest, request: Request, db: DbSession, settings: AppSettings
) -> EnrollResponse:
    """Exchange a one-time token and a CSR for a client certificate."""
    agent = await db.scalar(
        select(Agent)
        .where(Agent.enrollment_token_hash == token_digest(body.token))
        .with_for_update()
    )
    now = datetime.now(UTC)
    if agent is None or agent.enrollment_expires_at is None or agent.enrollment_expires_at < now:
        await audit.record(
            db,
            action="agent.enroll",
            outcome=Outcome.DENIED,
            actor_type=ActorType.AGENT,
            source_ip=client_ip(request),
            details={"reason": "invalid_or_expired_token"},
        )
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired enrolment token")
    try:
        issued = CertificateAuthority(settings.pki_dir).sign_agent_csr(
            body.csr_pem, str(agent.id), settings.agent_cert_days
        )
    except PkiError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    agent.cert_fingerprint = issued.fingerprint
    agent.cert_not_after = issued.not_after
    agent.status = AgentStatus.ACTIVE
    agent.version = body.version
    agent.enrollment_token_hash = None  # one-time
    agent.enrollment_expires_at = None
    await _audit(
        db,
        agent,
        "agent.enroll",
        Outcome.SUCCESS,
        source_ip=client_ip(request),
        details={"not_after": issued.not_after.isoformat()},
    )
    return EnrollResponse(
        agent_id=agent.id,
        certificate_pem=issued.pem,
        ca_pem=CertificateAuthority(settings.pki_dir).ca_pem,
        not_after=issued.not_after,
    )


# ------------------------------------------------------------------ inventory


class AccountIn(BaseModel):
    carbonio_id: str = Field(max_length=64)
    email: str = Field(max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    status: str = Field(max_length=32)


class InventoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accounts: list[AccountIn] = Field(max_length=100_000)
    # True when the list is the whole server: accounts missing from it are marked deleted.
    complete: bool = False


class InventoryOut(BaseModel):
    domains_created: int
    accounts_created: int
    accounts_updated: int
    accounts_deleted: int


_STATUS_MAP = {
    "active": AccountStatus.ACTIVE,
    "locked": AccountStatus.LOCKED,
    "lockout": AccountStatus.LOCKED,
    "closed": AccountStatus.CLOSED,
}


@router.post("/inventory", response_model=InventoryOut)
async def inventory(body: InventoryIn, agent: CurrentAgent, db: DbSession) -> InventoryOut:
    now = datetime.now(UTC)
    domain_names = {a.email.split("@")[1].lower() for a in body.accounts}
    existing = {
        d.name: d for d in await db.scalars(select(Domain).where(Domain.name.in_(domain_names)))
    }
    created_domains = 0
    for name in domain_names - existing.keys():
        existing[name] = Domain(name=name)
        db.add(existing[name])
        created_domains += 1
    await db.flush()
    known = {
        a.carbonio_id: a
        for a in await db.scalars(
            select(Account).where(Account.carbonio_id.in_([a.carbonio_id for a in body.accounts]))
        )
    }
    created = updated = 0
    for a in body.accounts:
        status_ = _STATUS_MAP.get(a.status.lower(), AccountStatus.ACTIVE)
        row = known.get(a.carbonio_id)
        if row is None:
            db.add(
                Account(
                    domain_id=existing[a.email.split("@")[1].lower()].id,
                    email=a.email.lower(),
                    carbonio_id=a.carbonio_id,
                    status=status_,
                    last_seen_at=now,
                )
            )
            created += 1
        else:
            row.email, row.status, row.last_seen_at, row.deleted_at = (
                a.email.lower(),
                status_,
                now,
                None,
            )
            updated += 1
    deleted = 0
    if body.complete:
        result = await db.execute(
            update(Account)
            .where(
                Account.carbonio_id.not_in([a.carbonio_id for a in body.accounts]),
                Account.status != AccountStatus.DELETED,
            )
            .values(status=AccountStatus.DELETED, deleted_at=now)
        )
        deleted = int(result.rowcount or 0)  # type: ignore[attr-defined]
    await db.flush()
    await _audit(
        db,
        agent,
        "inventory.sync",
        Outcome.SUCCESS,
        details={"accounts": len(body.accounts), "created": created, "deleted": deleted},
    )
    return InventoryOut(
        domains_created=created_domains,
        accounts_created=created,
        accounts_updated=updated,
        accounts_deleted=deleted,
    )


# ------------------------------------------------------------------ job protocol


class PreviousSnapshot(BaseModel):
    snapshot_id: uuid.UUID
    token: str | None


class ClaimedJob(BaseModel):
    job_id: uuid.UUID
    kind: JobKind
    attempt: int
    lease_seconds: int
    params: dict[str, Any]
    checkpoint: dict[str, Any] | None
    account_id: uuid.UUID | None
    carbonio_id: str | None
    account_email: str | None
    snapshot_id: uuid.UUID | None
    seq: int | None
    previous: PreviousSnapshot | None


@router.post(
    "/jobs/claim",
    response_model=ClaimedJob,
    responses={204: {"description": "Nothing to do"}},
)
async def claim(agent: CurrentAgent, db: DbSession, settings: AppSettings) -> ClaimedJob | Response:
    """Lease the next job: first an abandoned one (expired lease, resumed from its
    checkpoint), else the highest-priority queued job."""
    now = datetime.now(UTC)
    job = await db.scalar(
        select(Job)
        .where(
            or_(
                Job.status == JobStatus.QUEUED,
                and_(Job.status == JobStatus.RUNNING, Job.lease_expires_at < now),
            )
        )
        .order_by((Job.status == JobStatus.RUNNING).desc(), Job.priority, Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if job.attempts >= job.max_attempts:
        job.status, job.finished_at = JobStatus.FAILED, now
        job.error = "lease expired too many times"
        job.lease_expires_at = None
        await db.flush()
        # Commit even though we answer "nothing to do": the job must stay failed.
        await db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    resumed = job.status is JobStatus.RUNNING
    job.status = JobStatus.RUNNING
    job.agent_id = agent.id
    job.attempts += 1
    job.started_at = job.started_at or now
    job.lease_expires_at = now + timedelta(seconds=settings.job_lease_seconds)

    account = await db.get(Account, job.account_id) if job.account_id else None
    previous = None
    params = dict(job.params)
    if job.kind is JobKind.BACKUP and account is not None:
        if "snapshot_id" not in params:  # fixed at first claim so a resume reuses them
            last_seq = await db.scalar(
                select(func.max(Snapshot.seq)).where(Snapshot.account_id == account.id)
            )
            params["snapshot_id"] = str(uuid.uuid4())
            params["seq"] = (last_seq or 0) + 1
            job.params = params
        prev = await db.scalar(
            select(Snapshot)
            .where(
                Snapshot.account_id == account.id,
                Snapshot.status.in_([SnapshotStatus.COMPLETE, SnapshotStatus.PARTIAL]),
                Snapshot.seq < params["seq"],
            )
            .order_by(Snapshot.seq.desc())
            .limit(1)
        )
        if prev is not None and params.get("backup_type") != BackupType.FULL.value:
            previous = PreviousSnapshot(snapshot_id=prev.id, token=prev.sync_token)
    if job.kind is JobKind.PURGE and "blobs" not in params:
        # Fixed at first claim (the agent is idle, so nothing deduplicates against them).
        params.update(await purge_plan(db))
        job.params = params
    await db.flush()
    await _audit(
        db,
        agent,
        "job.claim",
        Outcome.SUCCESS,
        target_type="job",
        target_id=str(job.id),
        domain_id=account.domain_id if account else None,
        details={"attempt": job.attempts, "resumed": resumed},
    )
    return ClaimedJob(
        job_id=job.id,
        kind=job.kind,
        attempt=job.attempts,
        lease_seconds=settings.job_lease_seconds,
        params=params,
        checkpoint=job.checkpoint if resumed or job.attempts > 1 else None,
        account_id=account.id if account else None,
        carbonio_id=account.carbonio_id if account else None,
        account_email=account.email if account else None,
        snapshot_id=uuid.UUID(params["snapshot_id"]) if "snapshot_id" in params else None,
        seq=params.get("seq"),
        previous=previous,
    )


async def _own_running_job(db: DbSession, agent: Agent, job_id: uuid.UUID) -> Job:
    job = await db.get(Job, job_id, with_for_update=True)
    if job is None or job.agent_id != agent.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    if job.status is not JobStatus.RUNNING:
        raise HTTPException(status.HTTP_409_CONFLICT, f"job is {job.status.value}")
    return job


class Heartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checkpoint: dict[str, Any] | None = None
    progress: dict[str, Any] | None = None


@router.post("/jobs/{job_id}/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
async def heartbeat(
    job_id: uuid.UUID, body: Heartbeat, agent: CurrentAgent, db: DbSession, settings: AppSettings
) -> None:
    """Extend the lease and store the resume point."""
    job = await _own_running_job(db, agent, job_id)
    job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=settings.job_lease_seconds)
    if body.checkpoint is not None:
        job.checkpoint = body.checkpoint
    if body.progress is not None:
        job.progress = body.progress


class ItemIn(BaseModel):
    kind: ItemKind
    folder: int
    blob: str = Field(pattern=r"^[0-9a-f]{64}$")
    meta_blob: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    meta_size: int = Field(default=0, ge=0)
    subject: str | None = Field(default=None, max_length=2000)
    sender: str | None = Field(default=None, max_length=1000)
    date: datetime | None = None


class FolderIn(BaseModel):
    id: int
    parent: int | None
    path: str = Field(max_length=4096)
    view: str = Field(max_length=32)


class BackupResultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backup_type: BackupType
    manifest_ref: str = Field(max_length=512)
    token: str = Field(max_length=128)
    item_count: int = Field(ge=0)
    size_bytes: int = Field(ge=0)
    new_bytes: int = Field(ge=0)
    errors: dict[int, str] = Field(default_factory=dict)
    added: dict[int, ItemIn] = Field(default_factory=dict)
    removed: list[int] = Field(default_factory=list)
    folders: list[FolderIn] = Field(default_factory=list)
    account_attrs: dict[str, Any] | None = None


_VIEW = {
    "message": ItemKind.MESSAGE,
    "contact": ItemKind.CONTACT,
    "appointment": ItemKind.APPOINTMENT,
    "task": ItemKind.TASK,
    "document": ItemKind.DOCUMENT,
}


@router.post("/jobs/{job_id}/complete", status_code=status.HTTP_204_NO_CONTENT)
async def complete(
    job_id: uuid.UUID, body: BackupResultIn, agent: CurrentAgent, db: DbSession
) -> None:
    """Record the snapshot and apply the catalogue diff, atomically."""
    job = await db.get(Job, job_id, with_for_update=True)
    if job is not None and job.status is JobStatus.SUCCEEDED and job.agent_id == agent.id:
        return  # idempotent: the agent retried after a lost answer
    job = await _own_running_job(db, agent, job_id)
    if job.kind is not JobKind.BACKUP or job.account_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "not a backup job")
    seq = int(job.params["seq"])
    account = await db.get(Account, job.account_id)
    assert account is not None  # noqa: S101 - FK
    snapshot = Snapshot(
        id=uuid.UUID(job.params["snapshot_id"]),
        account_id=account.id,
        seq=seq,
        job_id=job.id,
        backup_type=body.backup_type,
        status=SnapshotStatus.PARTIAL if body.errors else SnapshotStatus.COMPLETE,
        taken_at=datetime.now(UTC),
        manifest_ref=body.manifest_ref,
        sync_token=body.token,
        item_count=body.item_count,
        size_bytes=body.size_bytes,
        new_bytes=body.new_bytes,
        account_attrs=body.account_attrs,
    )
    db.add(snapshot)
    # Blobs referenced by the new versions (no-op for those already known).
    blobs: dict[str, int] = {}
    for item in body.added.values():
        blobs[item.blob] = item.size
        blobs[item.meta_blob] = item.meta_size
    if blobs:
        await db.execute(
            insert(Blob)
            .values([{"id": k, "size": v, "stored_size": v} for k, v in blobs.items()])
            .on_conflict_do_nothing(index_elements=["id"])
        )
    # Close superseded / deleted versions, open the new ones.
    closing = set(body.removed) | set(body.added)
    if closing:
        await db.execute(
            update(ItemVersion)
            .where(
                ItemVersion.account_id == account.id,
                ItemVersion.to_seq.is_(None),
                ItemVersion.carbonio_item_id.in_(closing),
            )
            .values(to_seq=seq)
        )
    db.add_all(
        ItemVersion(
            account_id=account.id,
            carbonio_item_id=item_id,
            carbonio_folder_id=i.folder,
            kind=i.kind,
            content_blob_id=i.blob,
            meta_blob_id=i.meta_blob,
            subject=i.subject,
            sender=i.sender,
            item_date=i.date,
            size=i.size,
            from_seq=seq,
        )
        for item_id, i in body.added.items()
    )
    await _apply_folders(db, account.id, seq, body.folders)
    job.status, job.finished_at, job.lease_expires_at = JobStatus.SUCCEEDED, datetime.now(UTC), None
    job.progress = {"items": body.item_count, "errors": len(body.errors)}
    await db.flush()
    await _audit(
        db,
        agent,
        "backup.complete",
        Outcome.FAILURE if body.errors else Outcome.SUCCESS,
        target_type="account",
        target_id=account.email,
        domain_id=account.domain_id,
        details={
            "job": str(job.id),
            "seq": seq,
            "type": body.backup_type.value,
            "items": body.item_count,
            "added": len(body.added),
            "removed": len(body.removed),
            "errors": len(body.errors),
            "new_bytes": body.new_bytes,
        },
    )


async def _apply_folders(
    db: DbSession, account_id: uuid.UUID, seq: int, folders: list[FolderIn]
) -> None:
    current = {
        f.carbonio_folder_id: f
        for f in await db.scalars(
            select(FolderVersion).where(
                FolderVersion.account_id == account_id, FolderVersion.to_seq.is_(None)
            )
        )
    }
    wanted = {f.id: f for f in folders}
    for fid, row in current.items():
        new = wanted.get(fid)
        if new is None or (new.path, new.parent) != (row.path, row.parent_folder_id):
            row.to_seq = seq
    for fid, f in wanted.items():
        existing = current.get(fid)
        if existing is None or existing.to_seq == seq:
            db.add(
                FolderVersion(
                    account_id=account_id,
                    carbonio_folder_id=fid,
                    parent_folder_id=f.parent,
                    path=f.path,
                    view=_VIEW.get(f.view, ItemKind.MESSAGE),
                    from_seq=seq,
                )
            )


class RestoreResultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool
    mode: str = Field(max_length=32)
    root: str | None = Field(default=None, max_length=4096)
    selected: int = Field(ge=0)
    restored: int = Field(ge=0)
    present: int = Field(ge=0)
    unsupported: int = Field(ge=0)
    failed: dict[int, str] = Field(default_factory=dict)
    by_kind: dict[str, int] = Field(default_factory=dict)
    folders_created: list[str] = Field(default_factory=list)


@router.post("/jobs/{job_id}/restore-complete", status_code=status.HTTP_204_NO_CONTENT)
async def restore_complete(
    job_id: uuid.UUID, body: RestoreResultIn, agent: CurrentAgent, db: DbSession
) -> None:
    """Record the outcome of a restore (or of its dry run) on the job."""
    job = await db.get(Job, job_id, with_for_update=True)
    if job is not None and job.status is JobStatus.SUCCEEDED and job.agent_id == agent.id:
        return  # idempotent
    job = await _own_running_job(db, agent, job_id)
    if job.kind is not JobKind.RESTORE:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "not a restore job")
    summary = body.model_dump(mode="json")
    # Keep the job row small: the full lists are in the agent's log.
    summary["failed"] = dict(list(summary["failed"].items())[:200])
    summary["failed_count"] = len(body.failed)
    summary["folders_created"] = body.folders_created[:200]
    job.progress = summary
    job.status, job.finished_at, job.lease_expires_at = JobStatus.SUCCEEDED, datetime.now(UTC), None
    job.error = f"{len(body.failed)} item(s) could not be restored" if body.failed else None
    account = await db.get(Account, job.account_id) if job.account_id else None
    await _audit(
        db,
        agent,
        "restore.dry_run" if body.dry_run else "restore.complete",
        Outcome.FAILURE if body.failed else Outcome.SUCCESS,
        target_type="account",
        target_id=account.email if account else None,
        domain_id=account.domain_id if account else None,
        details={
            "job": str(job.id),
            "source": job.params.get("source_email"),
            "seq": job.params.get("seq"),
            "mode": body.mode,
            "selected": body.selected,
            "restored": body.restored,
            "present": body.present,
            "unsupported": body.unsupported,
            "failed": len(body.failed),
        },
    )


class PurgeResultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifests_deleted: list[uuid.UUID] = Field(default_factory=list, max_length=100_000)
    blobs_deleted: list[str] = Field(default_factory=list, max_length=100_000)
    freed_bytes: int = Field(default=0, ge=0)


@router.post("/jobs/{job_id}/purge-complete", status_code=status.HTTP_204_NO_CONTENT)
async def purge_complete(
    job_id: uuid.UUID, body: PurgeResultIn, agent: CurrentAgent, db: DbSession
) -> None:
    """Forget what the agent deleted from the repository."""
    job = await db.get(Job, job_id, with_for_update=True)
    if job is not None and job.status is JobStatus.SUCCEEDED and job.agent_id == agent.id:
        return  # idempotent
    job = await _own_running_job(db, agent, job_id)
    if job.kind is not JobKind.PURGE:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "not a purge job")
    planned_blobs = set(job.params.get("blobs", []))
    planned_manifests = {m["snapshot"] for m in job.params.get("manifests", [])}
    blobs = [b for b in body.blobs_deleted if b in planned_blobs]
    manifests = [m for m in body.manifests_deleted if str(m) in planned_manifests]
    now = datetime.now(UTC)
    if manifests:
        await db.execute(
            update(Snapshot)
            .where(Snapshot.id.in_(manifests), Snapshot.status == SnapshotStatus.EXPIRED)
            .values(purged_at=now)
        )
    if blobs:
        # Only rows that are still orphans: anything else would be a bug worth seeing.
        orphans = set(
            await db.scalars(orphan_blobs_query(len(blobs) + 1).where(Blob.id.in_(blobs)))
        )
        await db.execute(delete(Blob).where(Blob.id.in_(orphans)))
        still_used = sorted(set(blobs) - orphans)
    else:
        still_used = []
    job.status, job.finished_at, job.lease_expires_at = JobStatus.SUCCEEDED, now, None
    job.progress = {
        "manifests": len(manifests),
        "blobs": len(blobs),
        "freed_bytes": body.freed_bytes,
    }
    job.error = f"{len(still_used)} deleted blob(s) were referenced again" if still_used else None
    await _audit(
        db,
        agent,
        "purge.complete",
        Outcome.FAILURE if still_used else Outcome.SUCCESS,
        target_type="job",
        target_id=str(job.id),
        details={**job.progress, "still_used": still_used[:20]},
    )


class FailureIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: str = Field(max_length=2000)
    retryable: bool = True


@router.post("/jobs/{job_id}/fail", status_code=status.HTTP_204_NO_CONTENT)
async def fail(job_id: uuid.UUID, body: FailureIn, agent: CurrentAgent, db: DbSession) -> None:
    """Report a failure: re-queued (keeping its checkpoint) if retryable and attempts remain."""
    job = await _own_running_job(db, agent, job_id)
    job.error = body.error
    job.lease_expires_at = None
    if body.retryable and job.attempts < job.max_attempts:
        job.status, job.agent_id = JobStatus.QUEUED, None
    else:
        job.status, job.finished_at = JobStatus.FAILED, datetime.now(UTC)
    account = await db.get(Account, job.account_id) if job.account_id else None
    await _audit(
        db,
        agent,
        "job.fail",
        Outcome.FAILURE,
        target_type="job",
        target_id=str(job.id),
        domain_id=account.domain_id if account else None,
        details={"error": body.error[:500], "requeued": job.status is JobStatus.QUEUED},
    )
