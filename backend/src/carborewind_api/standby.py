"""Standby server: keep a clone of the Carbonio VM in line, for a quick disaster recovery.

The clone runs its own agent, set to the *standby* role in the interface. That agent never
backs anything up and its inventory is not merged into the accounts: it only runs
``standby_sync`` jobs, each replaying the latest snapshot of one account into the clone's
Carbonio (see the agent's ``standby.py``). The scheduler queues them every
``interval_minutes`` per account, when the account has a snapshot newer than the one the
clone holds. The delay between the main server and the clone is therefore the backup
frequency plus this interval.

Promotion (disaster): the standby agent takes the *primary* role, the replication stops;
its next inventory finds the same accounts (a clone keeps the zimbraIds), so backups go on
from the clone.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import (
    Account,
    AccountStatus,
    Agent,
    AgentStatus,
    AppSetting,
    Job,
    JobKind,
    JobStatus,
    Snapshot,
    SnapshotStatus,
    StandbyState,
)
from .notifications import notify

KEY = "standby"
PRIMARY, STANDBY = "primary", "standby"
MAX_QUEUED = 200  # jobs queued per tick at most
PENDING = (JobStatus.QUEUED, JobStatus.RUNNING)
KEPT = (SnapshotStatus.COMPLETE, SnapshotStatus.PARTIAL)


@dataclass
class StandbyConfig:
    enabled: bool = False
    interval_minutes: int = 15  # minimum time between two replays of one account

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> StandbyConfig:
        data = data or {}
        try:
            minutes = int(data.get("interval_minutes", 15))
        except (TypeError, ValueError):
            minutes = 15
        return cls(bool(data.get("enabled", False)), max(5, min(1440, minutes)))


async def load(db: AsyncSession) -> StandbyConfig:
    row = await db.get(AppSetting, KEY)
    return StandbyConfig.from_json(row.value if row else None)


async def save(db: AsyncSession, cfg: StandbyConfig) -> None:
    value = asdict(cfg)
    await db.execute(
        insert(AppSetting)
        .values(key=KEY, value=value, updated_at=func.now())
        .on_conflict_do_update(
            index_elements=["key"], set_={"value": value, "updated_at": func.now()}
        )
    )


async def standby_agent(db: AsyncSession) -> Agent | None:
    return await db.scalar(
        select(Agent)
        .where(Agent.role == STANDBY, Agent.status == AgentStatus.ACTIVE)
        .order_by(Agent.created_at)
        .limit(1)
    )


def account_attrs(account: Account) -> dict[str, str]:
    """Attributes the standby needs to create an account it does not have."""
    attrs = {
        "displayName": account.display_name or "",
        "givenName": account.first_name or "",
        "sn": account.last_name or "",
        "zimbraMailQuota": str(account.quota_bytes) if account.quota_bytes else "",
    }
    return {k: v for k, v in attrs.items() if v}


async def enqueue_due(db: AsyncSession, now: datetime, *, force: bool = False) -> int:
    """Queue one replay per account whose newest snapshot the standby does not hold yet."""
    cfg = await load(db)
    if not cfg.enabled or await standby_agent(db) is None:
        return 0
    latest = (
        select(Snapshot.account_id, func.max(Snapshot.seq).label("seq"))
        .where(Snapshot.status.in_(KEPT))
        .group_by(Snapshot.account_id)
        .subquery()
    )
    pending = select(Job.account_id).where(
        Job.kind == JobKind.STANDBY_SYNC, Job.status.in_(PENDING), Job.account_id.is_not(None)
    )
    rows = await db.execute(
        select(Account, Snapshot, StandbyState)
        .join(latest, latest.c.account_id == Account.id)
        .join(Snapshot, (Snapshot.account_id == Account.id) & (Snapshot.seq == latest.c.seq))
        .outerjoin(StandbyState, StandbyState.account_id == Account.id)
        .where(
            Account.status.in_([AccountStatus.ACTIVE, AccountStatus.LOCKED]),
            Account.id.not_in(pending),
        )
        .order_by(StandbyState.applied_at.asc().nulls_first())
        .limit(MAX_QUEUED * 5)
    )
    due = now - timedelta(minutes=cfg.interval_minutes)
    queued = 0
    for account, snapshot, state in rows.tuples():
        if state is not None:
            if state.snapshot_id == snapshot.id and state.status != "failed":
                continue
            if not force and state.applied_at is not None and state.applied_at > due:
                continue
        db.add(
            Job(
                kind=JobKind.STANDBY_SYNC,
                status=JobStatus.QUEUED,
                account_id=account.id,
                priority=300,
                max_attempts=2,
                params={"snapshot_id": str(snapshot.id), "attrs": account_attrs(account)},
            )
        )
        queued += 1
        if queued >= MAX_QUEUED:
            break
    if queued:
        await db.flush()
    return queued


async def record(
    db: AsyncSession,
    job: Job,
    *,
    stats: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Outcome of a replay (success with its counters, or failure)."""
    if job.account_id is None:
        return
    now = datetime.now(UTC)
    values: dict[str, Any] = {"applied_at": now}
    if error is None:
        snapshot = await db.get(Snapshot, uuid.UUID(str(job.params["snapshot_id"])))
        failed = int((stats or {}).get("failed_count") or 0)
        values.update(
            snapshot_id=snapshot.id if snapshot else None,
            snapshot_taken_at=snapshot.taken_at if snapshot else None,
            status="partial" if failed else "ok",
            error=f"{failed} item(s) not replicated" if failed else None,
            stats=stats or {},
        )
    else:
        values.update(status="failed", error=error[:2000])
        account = await db.get(Account, job.account_id)
        await notify(
            db,
            "job_failed",
            dedup_key=f"standby:{now.date().isoformat()}",  # one alert a day, not per account
            kind="standby_sync",
            account=account.email if account else "-",
            error=error,
            job=str(job.id),
        )
    await db.execute(
        insert(StandbyState)
        .values(account_id=job.account_id, **values)
        .on_conflict_do_update(index_elements=["account_id"], set_=values)
    )


async def overview(db: AsyncSession, now: datetime) -> dict[str, Any]:
    """What the interface shows: the agents, the delay, the accounts behind or failing."""
    cfg = await load(db)
    agents = list(await db.scalars(select(Agent).order_by(Agent.name)))
    live = select(Account.id).where(
        Account.status.in_([AccountStatus.ACTIVE, AccountStatus.LOCKED])
    )
    total = await db.scalar(select(func.count()).select_from(live.subquery())) or 0
    counts: dict[str, int] = {
        status_: n
        for status_, n in (
            await db.execute(
                select(StandbyState.status, func.count())
                .where(StandbyState.account_id.in_(live))
                .group_by(StandbyState.status)
            )
        ).tuples()
    }
    # Accounts behind: their newest snapshot is not on the standby yet; the delay is the age
    # of the oldest snapshot still waiting.
    latest = (
        select(Snapshot.account_id, func.max(Snapshot.seq).label("seq"))
        .where(Snapshot.status.in_(KEPT), Snapshot.account_id.in_(live))
        .group_by(Snapshot.account_id)
        .subquery()
    )
    behind_q = (
        select(Snapshot.taken_at)
        .join(latest, (latest.c.account_id == Snapshot.account_id) & (latest.c.seq == Snapshot.seq))
        .outerjoin(StandbyState, StandbyState.account_id == Snapshot.account_id)
        .where((StandbyState.snapshot_id.is_(None)) | (StandbyState.snapshot_id != Snapshot.id))
        .subquery()
    )
    behind = await db.scalar(select(func.count()).select_from(behind_q)) or 0
    oldest = await db.scalar(select(func.min(behind_q.c.taken_at)))
    newest = await db.scalar(select(func.max(StandbyState.applied_at)))
    pending = await db.scalar(
        select(func.count()).where(Job.kind == JobKind.STANDBY_SYNC, Job.status.in_(PENDING))
    )
    rows = await db.execute(
        select(StandbyState, Account.email)
        .join(Account, Account.id == StandbyState.account_id)
        .where(StandbyState.status != "ok", StandbyState.account_id.in_(live))
        .order_by(StandbyState.applied_at.desc())
        .limit(100)
    )
    synced = sum(counts.values())
    return {
        "config": asdict(cfg),
        "agents": [
            {
                "id": str(a.id),
                "name": a.name,
                "hostname": a.hostname,
                "role": a.role,
                "status": a.status.value,
                "last_heartbeat_at": a.last_heartbeat_at.isoformat()
                if a.last_heartbeat_at
                else None,
            }
            for a in agents
        ],
        "accounts": total,
        "synced": counts.get("ok", 0) + counts.get("partial", 0),
        "partial": counts.get("partial", 0),
        "failed": counts.get("failed", 0),
        "never": max(0, total - synced),
        "pending_jobs": pending or 0,
        "behind": behind,
        "oldest_waiting_at": oldest.isoformat() if oldest else None,
        "lag_seconds": max(0, int((now - oldest).total_seconds())) if oldest else 0,
        "last_applied_at": newest.isoformat() if newest else None,
        "problems": [
            {
                "email": email,
                "status": st.status,
                "error": st.error,
                "applied_at": st.applied_at.isoformat() if st.applied_at else None,
            }
            for st, email in rows.tuples()
        ],
    }
