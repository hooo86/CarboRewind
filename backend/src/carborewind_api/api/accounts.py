"""``/api/accounts`` and ``/api/dashboard``: backup status for the web interface.

Everything is filtered by the caller's domain scope (``backups:read``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import Select, and_, func, select

from .. import monitoring
from ..authz import Permission
from ..db.models import (
    Account,
    AccountStatus,
    Agent,
    AgentStatus,
    BackupType,
    Domain,
    Job,
    JobStatus,
    Snapshot,
    SnapshotStatus,
)
from .archives import summary_for
from .deps import DbSession, Principal, deny, require
from .schemas import MAX_PAGE, Page

router = APIRouter(tags=["accounts"])

Reader = Annotated[Principal, Depends(require(Permission.BACKUPS_READ))]


class SnapshotOut(BaseModel):
    id: uuid.UUID
    seq: int
    backup_type: BackupType
    status: SnapshotStatus
    taken_at: datetime
    item_count: int
    size_bytes: int
    new_bytes: int


class AccountDetail(BaseModel):
    id: uuid.UUID
    email: str
    status: AccountStatus
    domain_id: uuid.UUID
    domain: str
    last_seen_at: datetime | None
    snapshot_count: int
    last_snapshot: SnapshotOut | None


class Dashboard(BaseModel):
    accounts: int
    protected_24h: int  # accounts with a complete snapshot in the last 24 hours
    never_backed_up: int
    partial_last: int  # accounts whose latest snapshot is partial
    jobs_queued: int
    jobs_running: int
    jobs_failed_24h: int
    stored_bytes: int  # sum of new bytes written (after deduplication)
    agents_active: int | None  # global readers only
    last_agent_heartbeat: datetime | None
    # Repository location and free space per active agent (global readers only).
    storage: list[dict[str, Any]] = Field(default_factory=list)
    # Total size of the live mailboxes (from Carbonio) and their quotas (0/None = unlimited).
    mail_used_bytes: int = 0
    mail_quota_bytes: int = 0
    # Mailboxes over the alert threshold (Monitoring settings).
    mailboxes_nearly_full: int = 0
    mailbox_alert_percent: int = 0
    # Archive: messages kept in the repository, and what left the mailboxes.
    archived_messages: int = 0
    archived_bytes: int = 0
    archived_removed_bytes: int = 0


def snapshot_out(s: Snapshot) -> SnapshotOut:
    return SnapshotOut(
        id=s.id,
        seq=s.seq,
        backup_type=s.backup_type,
        status=s.status,
        taken_at=s.taken_at,
        item_count=s.item_count,
        size_bytes=s.size_bytes,
        new_bytes=s.new_bytes,
    )


def latest_snapshots() -> Select[uuid.UUID, int]:
    """(account_id, seq) of each account's latest usable snapshot."""
    return (
        select(Snapshot.account_id, func.max(Snapshot.seq).label("seq"))
        .where(Snapshot.status != SnapshotStatus.EXPIRED)
        .group_by(Snapshot.account_id)
    )


async def _readable_account(
    db: DbSession, principal: Principal, request: Request, account_id: uuid.UUID
) -> Account:
    account = await db.get(Account, account_id)
    if account is None or not principal.access.allows(Permission.BACKUPS_READ, account.domain_id):
        # Same answer for "unknown" and "forbidden": no existence oracle.
        raise await deny(db, principal, request, Permission.BACKUPS_READ, account=str(account_id))
    return account


@router.get("/api/accounts/{account_id}", response_model=AccountDetail)
async def get_account(
    account_id: uuid.UUID, principal: Reader, db: DbSession, request: Request
) -> AccountDetail:
    account = await _readable_account(db, principal, request, account_id)
    domain = await db.get(Domain, account.domain_id)
    count = await db.scalar(
        select(func.count()).select_from(Snapshot).where(Snapshot.account_id == account.id)
    )
    last = await db.scalar(
        select(Snapshot)
        .where(Snapshot.account_id == account.id, Snapshot.status != SnapshotStatus.EXPIRED)
        .order_by(Snapshot.seq.desc())
        .limit(1)
    )
    return AccountDetail(
        id=account.id,
        email=account.email,
        status=account.status,
        domain_id=account.domain_id,
        domain=domain.name if domain else "",
        last_seen_at=account.last_seen_at,
        snapshot_count=count or 0,
        last_snapshot=snapshot_out(last) if last else None,
    )


@router.get("/api/accounts/{account_id}/snapshots", response_model=Page[SnapshotOut])
async def list_snapshots(
    account_id: uuid.UUID,
    principal: Reader,
    db: DbSession,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[SnapshotOut]:
    """Snapshots of one account, most recent first."""
    account = await _readable_account(db, principal, request, account_id)
    total = await db.scalar(
        select(func.count()).select_from(Snapshot).where(Snapshot.account_id == account.id)
    )
    rows = await db.scalars(
        select(Snapshot)
        .where(Snapshot.account_id == account.id)
        .order_by(Snapshot.seq.desc())
        .limit(limit)
        .offset(offset)
    )
    return Page(items=[snapshot_out(s) for s in rows], total=total or 0, limit=limit, offset=offset)


@router.get("/api/dashboard", response_model=Dashboard)
async def dashboard(principal: Reader, db: DbSession) -> Dashboard:
    """Figures for the home page, restricted to the caller's domains."""
    scope = principal.access.domains(Permission.BACKUPS_READ)
    now = datetime.now(UTC)
    since = now - timedelta(hours=24)

    def scoped(query: Select, column: object) -> Select:  # type: ignore[type-arg]
        return query if scope is None else query.where(column.in_(scope))  # type: ignore[attr-defined]

    live = Account.status != AccountStatus.DELETED
    accounts = await db.scalar(
        scoped(select(func.count()).select_from(Account).where(live), Account.domain_id)
    )
    protected = await db.scalar(
        scoped(
            select(func.count(func.distinct(Snapshot.account_id)))
            .join(Account, Account.id == Snapshot.account_id)
            .where(live, Snapshot.taken_at >= since, Snapshot.status == SnapshotStatus.COMPLETE),
            Account.domain_id,
        )
    )
    with_snapshot = select(Snapshot.account_id).distinct()
    never = await db.scalar(
        scoped(
            select(func.count()).select_from(Account).where(live, Account.id.not_in(with_snapshot)),
            Account.domain_id,
        )
    )
    latest = latest_snapshots().subquery()
    partial = await db.scalar(
        scoped(
            select(func.count())
            .select_from(Snapshot)
            .join(
                latest,
                and_(latest.c.account_id == Snapshot.account_id, latest.c.seq == Snapshot.seq),
            )
            .join(Account, Account.id == Snapshot.account_id)
            .where(live, Snapshot.status == SnapshotStatus.PARTIAL),
            Account.domain_id,
        )
    )

    def jobs(*conditions: object) -> Select:  # type: ignore[type-arg]
        return scoped(
            select(func.count())
            .select_from(Job)
            .join(Account, Account.id == Job.account_id)
            .where(*conditions),  # type: ignore[arg-type]
            Account.domain_id,
        )

    queued = await db.scalar(jobs(Job.status == JobStatus.QUEUED))
    running = await db.scalar(jobs(Job.status == JobStatus.RUNNING))
    failed = await db.scalar(jobs(Job.status == JobStatus.FAILED, Job.finished_at >= since))
    stored = await db.scalar(
        scoped(
            select(func.coalesce(func.sum(Snapshot.new_bytes), 0)).join(
                Account, Account.id == Snapshot.account_id
            ),
            Account.domain_id,
        )
    )
    agents_active = last_heartbeat = None
    storage: list[dict[str, Any]] = []
    if scope is None:
        agents_active = await db.scalar(
            select(func.count()).select_from(Agent).where(Agent.status == AgentStatus.ACTIVE)
        )
        last_heartbeat = await db.scalar(select(func.max(Agent.last_heartbeat_at)))
        storage = [
            {"agent": name, **report}
            for name, report in await db.execute(
                select(Agent.name, Agent.storage).where(
                    Agent.status == AgentStatus.ACTIVE, Agent.storage.is_not(None)
                )
            )
        ]
    mail_used, mail_quota = (
        await db.execute(
            scoped(
                select(
                    func.coalesce(func.sum(Account.used_bytes), 0),
                    func.coalesce(func.sum(Account.quota_bytes), 0),
                ).where(live),
                Account.domain_id,
            )
        )
    ).one()
    cfg = await monitoring.load_settings(db)
    nearly_full = await db.scalar(
        scoped(
            select(func.count())
            .select_from(Account)
            .where(live, monitoring.nearly_full(cfg.mailbox_alert_percent, cfg.mailbox_alert_gb)),
            Account.domain_id,
        )
    )
    archive = await summary_for(db, scope)
    return Dashboard(
        archived_messages=archive.messages,
        archived_bytes=archive.bytes,
        archived_removed_bytes=archive.removed_bytes,
        mail_used_bytes=int(mail_used or 0),
        mail_quota_bytes=int(mail_quota or 0),
        mailboxes_nearly_full=int(nearly_full or 0),
        mailbox_alert_percent=cfg.mailbox_alert_percent,
        accounts=accounts or 0,
        protected_24h=protected or 0,
        never_backed_up=never or 0,
        partial_last=partial or 0,
        jobs_queued=queued or 0,
        jobs_running=running or 0,
        jobs_failed_24h=failed or 0,
        stored_bytes=int(stored or 0),
        agents_active=agents_active,
        last_agent_heartbeat=last_heartbeat,
        storage=storage,
    )
