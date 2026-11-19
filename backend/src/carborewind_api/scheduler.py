"""Scheduler: turns policies into backup jobs, applies retention, requests purges.

Runs as ``carborewind-api scheduler`` (its own container). Every tick is one transaction
guarded by a PostgreSQL advisory lock, so two schedulers never double-book, and job
idempotency keys (``policy:<id>:<slot>:<account>``) make a replayed tick harmless.

Purge safety
------------
Retention only changes the catalogue: expired snapshots are marked ``expired`` and catalogue
rows no kept snapshot needs are deleted. Repository files are removed later by a ``purge``
job executed by the agent, whose blob list is computed **when the agent claims it**: the
agent runs one job at a time, so no backup is writing (and deduplicating against) the
repository at that moment. Blobs named in the checkpoint of an unfinished backup are never
listed, and snapshots that a pending restore will read are never expired.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, exists, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import audit, mail_traffic, monitoring, storage_settings
from .config import Settings
from .db.models import (
    Account,
    AccountStatus,
    ActorType,
    Blob,
    FolderVersion,
    ItemVersion,
    Job,
    JobKind,
    JobStatus,
    Outcome,
    Policy,
    PolicyAccount,
    Snapshot,
    SnapshotStatus,
    TargetType,
)
from .notifications import deliver_pending, periodic_alerts
from .scheduling import Cron, CronError, Retention, snapshots_to_keep, zone

log = logging.getLogger(__name__)

LOCK_ID = 0x0C4B0_5C4E  # pg_advisory_xact_lock key shared by every scheduler
MAX_PURGE_BLOBS = 20_000
MAX_PURGE_MANIFESTS = 2_000
PENDING = (JobStatus.QUEUED, JobStatus.RUNNING)
KEPT = (SnapshotStatus.COMPLETE, SnapshotStatus.PARTIAL)


@dataclass
class TickReport:
    jobs_queued: int = 0
    snapshots_expired: int = 0
    versions_pruned: int = 0
    purge_job: uuid.UUID | None = None
    replicate_job: uuid.UUID | None = None
    verify_jobs: list[uuid.UUID] = field(default_factory=list)
    notifications_queued: int = 0
    notifications_sent: int = 0
    errors: list[str] = field(default_factory=list)


def _accounts_of(policy: Policy) -> Any:
    query = select(Account.id).where(
        Account.status.in_([AccountStatus.ACTIVE, AccountStatus.LOCKED])
    )
    if policy.target_type is TargetType.DOMAIN:
        query = query.where(Account.domain_id == policy.target_id)
    elif policy.target_type is TargetType.ACCOUNT:
        query = query.where(Account.id == policy.target_id)
    elif policy.target_type is TargetType.ACCOUNTS:
        query = query.where(
            Account.id.in_(
                select(PolicyAccount.account_id).where(PolicyAccount.policy_id == policy.id)
            )
        )
    return query


async def _busy_accounts(db: AsyncSession) -> set[uuid.UUID]:
    rows = await db.scalars(
        select(Job.account_id).where(
            Job.kind == JobKind.BACKUP, Job.status.in_(PENDING), Job.account_id.is_not(None)
        )
    )
    return {r for r in rows if r is not None}


async def enqueue_due_backups(db: AsyncSession, now: datetime, report: TickReport) -> None:
    busy = await _busy_accounts(db)
    policies = await db.scalars(
        select(Policy).where(Policy.enabled.is_(True)).order_by(Policy.priority, Policy.name)
    )
    for policy in policies:
        try:
            slot = Cron.parse(policy.schedule).last_slot(
                policy.last_run_at or policy.created_at, now, zone(policy.timezone)
            )
        except CronError as exc:  # validated by the API; a manual DB edit could still break it
            report.errors.append(f"policy {policy.name}: {exc}")
            continue
        if slot is None:
            continue
        slot_utc = slot.astimezone(UTC)
        accounts = [a for a in await db.scalars(_accounts_of(policy)) if a not in busy]
        if accounts:
            rows = [
                {
                    "kind": JobKind.BACKUP,
                    "status": JobStatus.QUEUED,
                    "account_id": account,
                    "policy_id": policy.id,
                    "priority": policy.priority,
                    "params": {"backup_type": policy.backup_type.value},
                    "idempotency_key": f"policy:{policy.id}:{slot_utc:%Y%m%dT%H%M}:{account}",
                }
                for account in accounts
            ]
            result = await db.execute(
                insert(Job)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(Job.id)
            )
            queued = len(result.all())
            report.jobs_queued += queued
            busy.update(accounts)
        else:
            queued = 0
        policy.last_run_at = slot_utc
        await audit.record(
            db,
            action="policy.run",
            outcome=Outcome.SUCCESS,
            actor_type=ActorType.SYSTEM,
            actor_name="scheduler",
            target_type="policy",
            target_id=str(policy.id),
            details={"policy": policy.name, "slot": slot_utc.isoformat(), "jobs": queued},
        )


async def _rules_by_account(db: AsyncSession) -> dict[uuid.UUID, tuple[Retention, str] | None]:
    """Merged retention per covered account; None when one policy keeps everything."""
    rules: dict[uuid.UUID, tuple[Retention, str] | None] = {}
    for policy in await db.scalars(select(Policy).where(Policy.enabled.is_(True))):
        rule = Retention.from_json(policy.retention)
        for account in await db.scalars(_accounts_of(policy)):
            if account in rules and rules[account] is None:
                continue
            if rule.unlimited:
                rules[account] = None
                continue
            current = rules.get(account)
            rules[account] = (
                (current[0].merge(rule), current[1]) if current else (rule, policy.timezone)
            )
    return rules


async def apply_retention(db: AsyncSession, report: TickReport) -> None:
    pending_restores = await db.scalars(
        select(Job.params["snapshot_id"].astext).where(
            Job.kind == JobKind.RESTORE, Job.status.in_(PENDING)
        )
    )
    protected = {uuid.UUID(s) for s in pending_restores if s}
    for account_id, rule in (await _rules_by_account(db)).items():
        if rule is None:
            continue
        retention, tz = rule
        snaps = (
            await db.execute(
                select(Snapshot.id, Snapshot.seq, Snapshot.taken_at).where(
                    Snapshot.account_id == account_id, Snapshot.status.in_(KEPT)
                )
            )
        ).all()
        keep = snapshots_to_keep([(s.seq, s.taken_at) for s in snaps], retention, zone(tz))
        expire = [s.id for s in snaps if s.seq not in keep and s.id not in protected]
        if not expire:
            continue
        await db.execute(
            update(Snapshot).where(Snapshot.id.in_(expire)).values(status=SnapshotStatus.EXPIRED)
        )
        report.snapshots_expired += len(expire)
        report.versions_pruned += await _prune_catalogue(db, account_id)
        await audit.record(
            db,
            action="retention.apply",
            outcome=Outcome.SUCCESS,
            actor_type=ActorType.SYSTEM,
            actor_name="scheduler",
            target_type="account",
            target_id=str(account_id),
            details={"expired": len(expire), "kept": len(snaps) - len(expire)},
        )


async def _prune_catalogue(db: AsyncSession, account_id: uuid.UUID) -> int:
    """Delete catalogue rows that no remaining snapshot of the account needs."""
    pruned = 0
    for model in (ItemVersion, FolderVersion):
        needed = exists().where(
            Snapshot.account_id == model.account_id,
            Snapshot.status.in_(KEPT),
            Snapshot.seq >= model.from_seq,
            or_(model.to_seq.is_(None), Snapshot.seq < model.to_seq),
        )
        result = await db.execute(delete(model).where(model.account_id == account_id, ~needed))
        pruned += int(result.rowcount or 0)  # type: ignore[attr-defined]
    return pruned


def orphan_blobs_query(limit: int) -> Any:
    referenced = or_(
        exists().where(ItemVersion.content_blob_id == Blob.id),
        exists().where(ItemVersion.meta_blob_id == Blob.id),
    )
    return select(Blob.id).where(~referenced).order_by(Blob.id).limit(limit)


async def blobs_in_checkpoints(db: AsyncSession) -> set[str]:
    """Blobs written by unfinished backups: a resumed job trusts them without re-reading."""
    out: set[str] = set()
    checkpoints = await db.scalars(
        select(Job.checkpoint).where(
            Job.kind == JobKind.BACKUP, Job.status.in_(PENDING), Job.checkpoint.is_not(None)
        )
    )
    for checkpoint in checkpoints:
        for entry in (checkpoint or {}).get("done", {}).values():
            out.update(v for k, v in entry.items() if k in ("blob", "meta_blob") and v)
    return out


async def purge_plan(db: AsyncSession) -> dict[str, Any]:
    """What a purge job claimed now may delete (computed at claim time, see module doc)."""
    rows = await db.execute(
        select(Snapshot.id, Account.carbonio_id)
        .join(Account, Account.id == Snapshot.account_id)
        .where(Snapshot.status == SnapshotStatus.EXPIRED, Snapshot.purged_at.is_(None))
        .limit(MAX_PURGE_MANIFESTS)
    )
    manifests = [{"account": carbonio, "snapshot": str(sid)} for sid, carbonio in rows]
    keep = await blobs_in_checkpoints(db)
    blobs = [b for b in await db.scalars(orphan_blobs_query(MAX_PURGE_BLOBS)) if b not in keep]
    return {"manifests": manifests, "blobs": blobs}


async def enqueue_purge(db: AsyncSession, report: TickReport) -> None:
    pending = await db.scalar(
        select(func.count()).where(Job.kind == JobKind.PURGE, Job.status.in_(PENDING))
    )
    if pending:
        return
    expired = await db.scalar(
        select(func.count()).where(
            Snapshot.status == SnapshotStatus.EXPIRED, Snapshot.purged_at.is_(None)
        )
    )
    orphan = await db.scalar(orphan_blobs_query(1))
    if not expired and orphan is None:
        return
    job = Job(kind=JobKind.PURGE, status=JobStatus.QUEUED, priority=200, params={})
    db.add(job)
    await db.flush()
    report.purge_job = job.id


REPLICATE_BACKOFF = timedelta(hours=1)


async def enqueue_replicate(db: AsyncSession, now: datetime, report: TickReport) -> None:
    """Refresh copy 02 when the repository changed since its last refresh."""
    cfg = await storage_settings.load(db)
    if cfg.copy is None:
        return
    pending = await db.scalar(
        select(func.count()).where(Job.kind == JobKind.REPLICATE, Job.status.in_(PENDING))
    )
    if pending:
        return
    last = await db.scalar(
        select(Job).where(Job.kind == JobKind.REPLICATE).order_by(Job.created_at.desc()).limit(1)
    )
    if (
        last is not None
        and last.status is JobStatus.FAILED
        and (last.finished_at or last.created_at) > now - REPLICATE_BACKOFF
    ):
        return  # do not hammer an unreachable NAS; the failure was notified
    done = await db.scalar(
        select(func.max(Job.started_at)).where(
            Job.kind == JobKind.REPLICATE, Job.status == JobStatus.SUCCEEDED
        )
    )
    changed = await db.scalar(
        select(func.max(Job.finished_at)).where(
            Job.kind.in_([JobKind.BACKUP, JobKind.PURGE]), Job.status == JobStatus.SUCCEEDED
        )
    )
    if changed is None or (done is not None and changed <= done):
        return
    job = Job(kind=JobKind.REPLICATE, status=JobStatus.QUEUED, priority=210, max_attempts=2)
    db.add(job)
    await db.flush()
    report.replicate_job = job.id


async def tick(db: AsyncSession, now: datetime, *, retention: bool = True) -> TickReport:
    """One scheduling pass, in the caller's transaction."""
    report = TickReport()
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_ID})
    await enqueue_due_backups(db, now, report)
    report.notifications_queued = await periodic_alerts(db, now)
    await enqueue_replicate(db, now, report)
    report.verify_jobs = await monitoring.enqueue_verify(db, now)
    if retention:
        report.notifications_queued += await monitoring.periodic_alerts(db, now)
        await monitoring.prune(db, now)
        await mail_traffic.prune(db, now)
        await apply_retention(db, report)
        await enqueue_purge(db, report)
    return report


async def run_forever(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings | None = None,
    *,
    interval: float = 60.0,
    retention_every: int = 60,
    stop: asyncio.Event | None = None,
) -> None:
    """Tick every ``interval`` seconds; retention and purge every ``retention_every`` ticks."""
    stop = stop or asyncio.Event()
    count = 0
    while not stop.is_set():
        try:
            async with factory() as db, db.begin():
                report = await tick(db, datetime.now(UTC), retention=count % retention_every == 0)
            if settings is not None:
                # Separate transaction: a slow mail server must not hold the scheduler lock.
                async with factory() as db, db.begin():
                    report.notifications_sent = await deliver_pending(
                        db, settings, datetime.now(UTC)
                    )
            if (
                report.jobs_queued
                or report.snapshots_expired
                or report.purge_job
                or report.notifications_sent
            ):
                log.info(
                    "scheduler: %s job(s) queued, %s snapshot(s) expired, purge %s, "
                    "%s notification(s) sent",
                    report.jobs_queued,
                    report.snapshots_expired,
                    report.purge_job or "-",
                    report.notifications_sent,
                )
            for error in report.errors:
                log.error("scheduler: %s", error)
        except Exception:  # keep scheduling even if one tick fails (DB restart…)
            log.exception("scheduler tick failed")
        count += 1
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
