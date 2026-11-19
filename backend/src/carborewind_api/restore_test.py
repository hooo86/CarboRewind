"""Automatic restore test: proof, on a schedule, that a real restore works end to end.

On each run (cron, or "Run now"): an active account is drawn at random among those with a
backup, some messages of its latest backup are drawn at random, and a real restore job copies
them into a **test mailbox** (never the source), under a dated folder. The agent reads the
blobs back from the repository, decrypts them and writes them through Carbonio, exactly as
for a user's restore; with ``cleanup`` it deletes the test folder afterwards.

A run passes when every selected message was restored. A failure (or a job that fails) is
notified (``restore_test_failed``); the history is in Monitoring.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .db.models import (
    Account,
    AccountStatus,
    ActorType,
    AppSetting,
    ItemKind,
    ItemVersion,
    Job,
    JobKind,
    JobStatus,
    Outcome,
    Snapshot,
    SnapshotStatus,
)
from .notifications import notify
from .scheduling import Cron, CronError, zone

KEY = "restore_test"
PENDING = (JobStatus.QUEUED, JobStatus.RUNNING)
KEPT = (SnapshotStatus.COMPLETE, SnapshotStatus.PARTIAL)


@dataclass
class RestoreTestSettings:
    enabled: bool = False
    schedule: str = "45 3 * * 6"  # Saturday 03:45
    timezone: str = "UTC"
    target_account_id: str | None = None
    messages: int = 20
    cleanup: bool = True
    last_slot: str | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> RestoreTestSettings:
        out = cls()
        for key, value in (data or {}).items():
            if hasattr(out, key):
                setattr(out, key, value)
        return out


async def load(db: AsyncSession) -> RestoreTestSettings:
    row = await db.get(AppSetting, KEY)
    return RestoreTestSettings.from_json(row.value if row else None)


async def save(db: AsyncSession, cfg: RestoreTestSettings) -> None:
    value = asdict(cfg)
    await db.execute(
        insert(AppSetting)
        .values(key=KEY, value=value, updated_at=func.now())
        .on_conflict_do_update(
            index_elements=["key"], set_={"value": value, "updated_at": func.now()}
        )
    )


class RestoreTestError(Exception):
    pass


async def enqueue(
    db: AsyncSession, cfg: RestoreTestSettings, now: datetime, created_by: uuid.UUID | None
) -> Job:
    """Draw an account and messages, queue the restore into the test mailbox."""
    if not cfg.target_account_id:
        raise RestoreTestError("choose the test mailbox first")
    target = await db.get(Account, uuid.UUID(cfg.target_account_id))
    if target is None or target.status is AccountStatus.DELETED:
        raise RestoreTestError("the test mailbox no longer exists")
    busy = await db.scalar(
        select(Job.id).where(
            Job.kind == JobKind.RESTORE,
            Job.status.in_(PENDING),
            Job.params["test"].astext == "true",
        )
    )
    if busy is not None:
        raise RestoreTestError("a restore test is already running")
    latest = (
        select(Snapshot.account_id, func.max(Snapshot.seq).label("seq"))
        .where(Snapshot.status.in_(KEPT))
        .group_by(Snapshot.account_id)
        .subquery()
    )
    row = (
        await db.execute(
            select(Account, Snapshot)
            .join(latest, latest.c.account_id == Account.id)
            .join(
                Snapshot,
                (Snapshot.account_id == Account.id) & (Snapshot.seq == latest.c.seq),
            )
            .where(
                Account.status == AccountStatus.ACTIVE,
                Account.id != target.id,
                Snapshot.item_count > 0,
            )
            .order_by(func.random())
            .limit(1)
        )
    ).first()
    if row is None:
        raise RestoreTestError("no account has a backup with messages yet")
    source, snap = row
    items = list(
        await db.scalars(
            select(ItemVersion.carbonio_item_id)
            .where(
                ItemVersion.account_id == source.id,
                ItemVersion.kind == ItemKind.MESSAGE,
                ItemVersion.from_seq <= snap.seq,
                (ItemVersion.to_seq.is_(None)) | (ItemVersion.to_seq > snap.seq),
            )
            .order_by(func.random())
            .limit(cfg.messages)
        )
    )
    if not items:
        raise RestoreTestError(f"the latest backup of {source.email} holds no message")
    job = Job(
        kind=JobKind.RESTORE,
        status=JobStatus.QUEUED,
        account_id=target.id,
        created_by=created_by,
        priority=150,
        max_attempts=2,
        params={
            "source_account_id": str(source.id),
            "source_carbonio_id": source.carbonio_id,
            "source_email": source.email,
            "snapshot_id": str(snap.id),
            "seq": snap.seq,
            "mode": "new_folder",
            "dry_run": False,
            "label": f"Test de restauration {now:%Y-%m-%d %H.%M}",
            "folder_ids": [],
            "item_ids": sorted(items),
            "test": True,
            "cleanup": cfg.cleanup,
        },
    )
    db.add(job)
    await db.flush()
    await audit.record(
        db,
        action="restore_test.start",
        outcome=Outcome.SUCCESS,
        actor_type=ActorType.SYSTEM if created_by is None else ActorType.USER,
        actor_id=str(created_by) if created_by else None,
        actor_name="scheduler" if created_by is None else None,
        target_type="account",
        target_id=target.email,
        domain_id=target.domain_id,
        details={"job": str(job.id), "source": source.email, "seq": snap.seq, "items": len(items)},
    )
    return job


async def enqueue_due(db: AsyncSession, now: datetime) -> uuid.UUID | None:
    """From the scheduler: one test per cron slot."""
    cfg = await load(db)
    if not cfg.enabled or not cfg.target_account_id:
        return None
    try:
        cron = Cron.parse(cfg.schedule)
        tz = zone(cfg.timezone)
    except CronError:
        return None
    since = datetime.fromisoformat(cfg.last_slot) if cfg.last_slot else now
    slot = cron.last_slot(since, now, tz)
    if cfg.last_slot is None:  # first run after enabling: wait for the next slot
        cfg.last_slot = now.isoformat()
        await save(db, cfg)
        return None
    if slot is None:
        return None
    cfg.last_slot = slot.astimezone(UTC).isoformat()
    await save(db, cfg)
    try:
        job = await enqueue(db, cfg, now, None)
    except RestoreTestError as exc:
        await notify(
            db,
            "restore_test_failed",
            dedup_key=f"restore_test:{cfg.last_slot}",
            source="-",
            seq="-",
            target="-",
            restored=0,
            selected=0,
            failed=0,
            error=str(exc),
        )
        return None
    return job.id


async def record_result(db: AsyncSession, job: Job, result: dict[str, Any]) -> bool:
    """After restore-complete (or a final failure) of a test job: notify a failure."""
    selected = int(result.get("selected", 0))
    restored = int(result.get("restored", 0)) + int(result.get("present", 0))
    failed = len(result.get("failed") or {}) or int(result.get("failed_count", 0))
    error = str(result.get("error") or "")
    ok = not error and selected > 0 and failed == 0 and restored == selected
    target = await db.get(Account, job.account_id) if job.account_id else None
    if not ok:
        await notify(
            db,
            "restore_test_failed",
            dedup_key=f"restore_test_job:{job.id}",
            source=job.params.get("source_email", "?"),
            seq=job.params.get("seq", "?"),
            target=target.email if target else "?",
            restored=restored,
            selected=selected,
            failed=failed,
            error=error,
        )
    job.progress = {**(job.progress or {}), "test_ok": ok}
    await audit.record(
        db,
        action="restore_test.result",
        outcome=Outcome.SUCCESS if ok else Outcome.FAILURE,
        actor_type=ActorType.SYSTEM,
        actor_name="restore-test",
        target_type="job",
        target_id=str(job.id),
        details={"selected": selected, "restored": restored, "failed": failed, "error": error},
    )
    return ok


async def history(db: AsyncSession, limit: int = 20) -> list[dict[str, Any]]:
    rows = await db.execute(
        select(Job, Account.email)
        .join(Account, Account.id == Job.account_id)
        .where(Job.kind == JobKind.RESTORE, Job.params["test"].astext == "true")
        .order_by(Job.created_at.desc())
        .limit(limit)
    )
    out = []
    for job, target in rows.tuples():
        progress = job.progress or {}
        out.append(
            {
                "job_id": str(job.id),
                "status": job.status.value,
                "source": job.params.get("source_email"),
                "seq": job.params.get("seq"),
                "target": target,
                "selected": progress.get("selected", len(job.params.get("item_ids", []))),
                "restored": progress.get("restored"),
                "failed": progress.get("failed_count"),
                "ok": progress.get("test_ok"),
                "cleaned": progress.get("cleaned"),
                "error": job.error,
                "created_at": job.created_at.isoformat(),
                "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            }
        )
    return out
