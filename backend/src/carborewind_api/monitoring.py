"""Monitoring: storage trend and saturation forecast, accounts at risk, copy 2 lag,
scheduled integrity checks (``app_settings`` row ``monitoring``).

* **Storage**: every storage report of an agent keeps one sample per hour and copy; the
  forecast is a least-squares line over the last 30 days of used space, extrapolated to the
  total size (``days_until_full``; None when the trend is flat or shrinking).
* **Accounts at risk**: never backed up, no successful backup for ``stale_hours``, last
  snapshot partial, last backup job failed, covered by no policy.
* **Copy 2 lag**: time since the last successful refresh while the repository changed after
  it; above ``replica_max_lag_hours`` the ``replica_lag`` notification is sent (daily).
* **Integrity**: ``verify`` jobs (repository, then copy 2) on the ``integrity_cron``
  schedule, checking ``integrity_sample`` of the blobs (1 = all); corruption is notified.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass

__all__ = ["asdict", "storage_settings"]
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from . import storage_settings
from .db.models import (
    Account,
    AccountStatus,
    Agent,
    AppSetting,
    Job,
    JobKind,
    JobStatus,
    Policy,
    PolicyAccount,
    Snapshot,
    SnapshotStatus,
    StorageSample,
    TargetType,
)
from .notifications import notify
from .scheduling import Cron, CronError, zone

SETTINGS_KEY = "monitoring"
TREND_DAYS = 30
FORECAST_ALERT_DAYS = 14
KEEP_SAMPLES_DAYS = 400
PENDING = (JobStatus.QUEUED, JobStatus.RUNNING)


@dataclass
class MonitoringSettings:
    stale_hours: int = 24
    replica_max_lag_hours: int = 24
    integrity_enabled: bool = True
    integrity_cron: str = "30 3 * * 0"  # Sunday 03:30
    integrity_sample: float = 0.1
    integrity_timezone: str = "UTC"
    integrity_last_slot: str | None = None
    # A mailbox is "nearly full" from this share of its quota (0 = off), or, whatever its
    # quota, from this size in GB (0 = off).
    mailbox_alert_percent: int = 90
    mailbox_alert_gb: float = 0.0
    # Carbonio services: alert once a service is down on this many reports in a row (one a
    # minute); mail queue (deferred + active + incoming + hold) alert from this size (0 = off).
    service_down_reports: int = 2
    mail_queue_alert: int = 200

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> MonitoringSettings:
        out = cls()
        for key, value in (data or {}).items():
            if hasattr(out, key):
                setattr(out, key, value)
        return out


async def load_settings(db: AsyncSession) -> MonitoringSettings:
    row = await db.get(AppSetting, SETTINGS_KEY)
    return MonitoringSettings.from_json(row.value if row else None)


async def save_settings(db: AsyncSession, cfg: MonitoringSettings) -> None:
    value = asdict(cfg)
    await db.execute(
        insert(AppSetting)
        .values(key=SETTINGS_KEY, value=value, updated_at=func.now())
        .on_conflict_do_update(
            index_elements=["key"], set_={"value": value, "updated_at": func.now()}
        )
    )


# ------------------------------------------------------------------ Carbonio services

SERVICES_STALE = timedelta(minutes=5)
QUEUE_ALERT_EVERY_HOURS = 3

# Readable names, in alerts, for the checks that are not Carbonio services.
CHECK_LABELS = {
    "mta:antivirus": "circuit antivirus de Postfix (envoi bloqué : vérifier master.cf)",
    "mta:milter": "milter de Postfix injoignable (messages refusés)",
    "mta:policy": "serveur de politique cbpolicyd injoignable (envois refusés)",
    "guard:master.cf": "garde de master.cf : circuit antivirus absent",
}


def check_label(name: str) -> str:
    return CHECK_LABELS.get(name, name)


async def record_services(
    db: AsyncSession, agent: Agent, report: dict[str, Any], now: datetime
) -> list[str]:
    """Keep the state of each Carbonio service; alert when one goes down or comes back.

    A service counts as down after ``service_down_reports`` reports in a row (a restart
    takes a few seconds and must not page anybody); unknown states (stale records) change
    nothing. Returns the names of the services alerted as down by this report.
    """
    cfg = await load_settings(db)
    previous = (agent.services or {}).get("state", {})
    state: dict[str, dict[str, Any]] = {}
    alerted: list[str] = []
    for svc in report.get("services", []):
        name = str(svc.get("name"))
        old = previous.get(name, {})
        cur = {
            "ok": old.get("ok", True),
            "since": old.get("since"),
            "fails": int(old.get("fails", 0)),
            "alerted": bool(old.get("alerted")),
        }
        ok = svc.get("ok")
        if ok is False:
            cur["fails"] += 1
            cur["since"] = cur["since"] if cur["ok"] is False else now.isoformat()
            cur["ok"] = False
            if cur["fails"] >= max(1, cfg.service_down_reports) and not cur["alerted"]:
                cur["alerted"] = True
                alerted.append(name)
                await notify(
                    db,
                    "service_down",
                    dedup_key=f"service_down:{agent.id}:{name}:{cur['since']}",
                    service=check_label(name),
                    agent=agent.name,
                    since=cur["since"],
                )
        elif ok is True:
            if cur["alerted"]:
                await notify(
                    db,
                    "service_up",
                    dedup_key=f"service_up:{agent.id}:{name}:{cur['since']}",
                    service=check_label(name),
                    agent=agent.name,
                    since=cur["since"],
                )
            cur = {
                "ok": True,
                "since": now.isoformat() if cur["ok"] is False else cur["since"],
                "fails": 0,
                "alerted": False,
            }
        state[name] = cur
    raw_queue = report.get("queue")
    queue: dict[str, int] = raw_queue if isinstance(raw_queue, dict) else {}
    total = sum(int(v) for v in queue.values()) if isinstance(raw_queue, dict) else None
    if total is not None and cfg.mail_queue_alert and total >= cfg.mail_queue_alert:
        window = now.hour // QUEUE_ALERT_EVERY_HOURS
        await notify(
            db,
            "mail_queue",
            dedup_key=f"mail_queue:{agent.id}:{now:%Y%m%d}:{window}",
            count=total,
            limit=cfg.mail_queue_alert,
            agent=agent.name,
            detail=", ".join(f"{k}: {v}" for k, v in sorted(queue.items())),
        )
    await _autostart_events(db, agent, report, (agent.services or {}))
    agent.services = {
        "checked_at": report.get("checked_at") or now.isoformat(),
        "received_at": now.isoformat(),
        "services": [
            {**svc, "since": state.get(str(svc.get("name")), {}).get("since")}
            for svc in report.get("services", [])
        ],
        "queue": raw_queue,
        "queue_total": total,
        "queue_items": report.get("queue_items"),
        "autostart": report.get("autostart"),
        "mail_test": report.get("mail_test") or (agent.services or {}).get("mail_test"),
        "errors": report.get("errors", [])[:5],
        "state": state,
    }
    return alerted


def _test_lines(test: dict[str, Any]) -> str:
    out = []
    for kind, r in sorted((test.get("results") or {}).items()):
        detail = f" — {r['detail']}" if r.get("detail") else ""
        out.append(f"{kind}: {r.get('address')} → {r.get('status')}{detail}")
    if test.get("error"):
        out.append(str(test["error"]))
    return "\n".join(out) or "—"


async def _autostart_events(
    db: AsyncSession, agent: Agent, report: dict[str, Any], previous: dict[str, Any]
) -> None:
    """Alerts for the automatic start of Carbonio and the sending test, once each."""
    auto = report.get("autostart") or {}
    before = previous.get("autostart") or {}
    asked = auto.get("last_request") or {}
    if asked.get("id") and asked.get("id") != (before.get("last_request") or {}).get("id"):
        await notify(
            db,
            "carbonio_autostart",
            dedup_key=f"carbonio_autostart:{asked['id']}",
            agent=agent.name,
            down=", ".join(asked.get("down") or []) or "—",
            minutes=auto.get("minutes", "?"),
        )
    result = auto.get("last_result") or {}
    if (
        result.get("id")
        and result.get("ok") is False
        and result.get("id") != (before.get("last_result") or {}).get("id")
    ):
        await notify(
            db,
            "carbonio_autostart_failed",
            dedup_key=f"carbonio_autostart_failed:{result['id']}",
            agent=agent.name,
            error=str(result.get("error") or result.get("output") or "")[-1500:] or "—",
        )
    test = report.get("mail_test") or {}
    was = previous.get("mail_test") or {}
    if (
        test.get("id")
        and test.get("done")
        and not (was.get("id") == test["id"] and was.get("done"))
    ):
        if not test.get("ok"):
            await notify(
                db,
                "mail_test_failed",
                dedup_key=f"mail_test_failed:{test['id']}",
                agent=agent.name,
                detail=_test_lines(test),
            )
        elif test.get("reason") == "after_start":
            await notify(
                db,
                "mail_test_ok",
                dedup_key=f"mail_test_ok:{test['id']}",
                agent=agent.name,
                detail=_test_lines(test),
            )


def services_view(agent: Agent, now: datetime) -> dict[str, Any] | None:
    """What the interface shows: services (down first), queue, freshness."""
    data = agent.services
    if not data:
        return None
    received = data.get("received_at")
    stale = True
    if received:
        stale = now - datetime.fromisoformat(received) > SERVICES_STALE
    services = sorted(
        data.get("services", []),
        key=lambda s: (s.get("ok") is not False, s.get("source") != "carbonio", s.get("name")),
    )
    return {
        "agent": agent.name,
        "agent_id": str(agent.id),
        "checked_at": data.get("checked_at"),
        "stale": stale,
        "services": services,
        "down": [s["name"] for s in services if s.get("ok") is False],
        "queue": data.get("queue"),
        "queue_total": data.get("queue_total"),
        "errors": data.get("errors", []),
        "autostart": data.get("autostart"),
        "mail_test": data.get("mail_test"),
        "mail_test_request": (agent.storage or {}).get("mail_test_request"),
    }


# ------------------------------------------------------------------ storage


async def record_storage(db: AsyncSession, agent: Agent, report: dict[str, Any]) -> None:
    hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    rows = []
    for target, place in (
        ("primary", report),
        ("replica", report.get("replica")),
        ("server", report.get("mail_store")),
    ):
        if place and place.get("total_bytes") and place.get("free_bytes") is not None:
            rows.append(
                {
                    "hour": hour,
                    "agent_id": agent.id,
                    "target": target,
                    "total_bytes": int(place["total_bytes"]),
                    "free_bytes": int(place["free_bytes"]),
                }
            )
    if rows:
        stmt = insert(StorageSample).values(rows)
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=["hour", "agent_id", "target"],
                set_={
                    "total_bytes": stmt.excluded.total_bytes,
                    "free_bytes": stmt.excluded.free_bytes,
                },
            )
        )


def forecast(points: list[tuple[datetime, int, int]]) -> dict[str, Any]:
    """Least squares on used bytes; ``points`` = (time, total, free), oldest first."""
    if not points:
        return {"days_until_full": None, "growth_per_day": None}
    t0 = points[0][0]
    xs = [(t - t0).total_seconds() / 86400 for t, _, _ in points]
    ys = [float(total - free) for _, total, free in points]
    last_total, last_used = points[-1][1], ys[-1]
    if len(points) < 2 or max(xs) - min(xs) < 1 / 24:
        return {"days_until_full": None, "growth_per_day": None}
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    var = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / var if var else 0.0
    days = (last_total - last_used) / slope if slope > 0 else None
    return {
        "days_until_full": round(days, 1) if days is not None else None,
        "growth_per_day": int(slope),
    }


async def storage_trend(db: AsyncSession, days: int = TREND_DAYS) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (
        await db.execute(
            select(Agent.name, StorageSample)
            .join(Agent, Agent.id == StorageSample.agent_id)
            .where(StorageSample.hour >= since)
            .order_by(Agent.name, StorageSample.target, StorageSample.hour)
        )
    ).all()
    series: dict[tuple[str, str], list[StorageSample]] = {}
    for name, sample in rows:
        series.setdefault((name, sample.target), []).append(sample)
    out = []
    for (name, target), samples in series.items():
        points = [(s.hour, s.total_bytes, s.free_bytes) for s in samples]
        last = samples[-1]
        out.append(
            {
                "agent": name,
                "target": target,
                "total_bytes": last.total_bytes,
                "free_bytes": last.free_bytes,
                "used_bytes": last.total_bytes - last.free_bytes,
                **forecast(points),
                "points": [
                    {"at": s.hour, "used_bytes": s.total_bytes - s.free_bytes} for s in samples
                ],
            }
        )
    return out


# ------------------------------------------------------------------ accounts at risk


def _latest_backup_job() -> Any:
    job = aliased(Job)
    return (
        select(job.status)
        .where(
            job.account_id == Account.id,
            job.kind == JobKind.BACKUP,
            job.status.in_([JobStatus.SUCCEEDED, JobStatus.FAILED]),
        )
        .order_by(job.created_at.desc())
        .limit(1)
        .correlate(Account)
        .scalar_subquery()
    )


def _covered() -> Any:
    return exists(
        select(Policy.id).where(
            Policy.enabled.is_(True),
            or_(
                Policy.target_type == TargetType.GLOBAL,
                and_(
                    Policy.target_type == TargetType.DOMAIN, Policy.target_id == Account.domain_id
                ),
                and_(Policy.target_type == TargetType.ACCOUNT, Policy.target_id == Account.id),
                and_(
                    Policy.target_type == TargetType.ACCOUNTS,
                    exists(
                        select(PolicyAccount.account_id).where(
                            PolicyAccount.policy_id == Policy.id,
                            PolicyAccount.account_id == Account.id,
                        )
                    ),
                ),
            ),
        )
    )


def nearly_full(percent: int, gb: float) -> Any:
    """Condition on Account: over ``percent`` of its quota, or over ``gb`` GB."""
    from sqlalchemy import Float, cast, false

    conditions = []
    if percent > 0:
        conditions.append(
            and_(
                Account.quota_bytes > 0,
                Account.used_bytes.is_not(None),
                cast(Account.used_bytes, Float) * 100.0 / cast(Account.quota_bytes, Float)
                >= percent,
            )
        )
    if gb > 0:
        conditions.append(Account.used_bytes >= int(gb * 1024**3))
    return or_(*conditions) if conditions else false()


async def accounts_at_risk(
    db: AsyncSession,
    stale_hours: int,
    limit: int = 20,
    quota_percent: int = 0,
    quota_gb: float = 0.0,
) -> dict[str, dict[str, Any]]:
    snap = aliased(Snapshot)
    last_ok = (
        select(func.max(snap.taken_at))
        .where(
            snap.account_id == Account.id,
            snap.status.in_([SnapshotStatus.COMPLETE, SnapshotStatus.PARTIAL]),
        )
        .correlate(Account)
        .scalar_subquery()
    )
    last_status = (
        select(snap.status)
        .where(snap.account_id == Account.id, snap.status != SnapshotStatus.EXPIRED)
        .order_by(snap.seq.desc())
        .limit(1)
        .correlate(Account)
        .scalar_subquery()
    )
    live = Account.status.in_([AccountStatus.ACTIVE, AccountStatus.LOCKED])
    horizon = datetime.now(UTC) - timedelta(hours=stale_hours)
    categories = {
        "never": (last_ok.is_(None), ["last_backup_at:not_set"]),
        "stale": (
            and_(last_ok.is_not(None), last_ok < horizon),
            [f"last_backup_at:older_days:{max(1, round(stale_hours / 24))}"],
        ),
        "partial": (last_status == SnapshotStatus.PARTIAL, ["last_backup_status:in:partial"]),
        "failed": (_latest_backup_job() == JobStatus.FAILED, []),
        "no_policy": (~_covered(), ["has_policy:is:false"]),
        "quota": (
            nearly_full(quota_percent, quota_gb),
            [f"usage_pct:ge:{quota_percent}"] if quota_percent and not quota_gb else [],
        ),
    }
    out: dict[str, dict[str, Any]] = {}
    for key, (condition, filters) in categories.items():
        query = select(Account.id, Account.email, last_ok.label("last")).where(live, condition)
        total = await db.scalar(select(func.count()).select_from(query.subquery()))
        rows = await db.execute(query.order_by(Account.email).limit(limit))
        out[key] = {
            "count": int(total or 0),
            "filters": filters,
            "accounts": [{"id": i, "email": e, "last_backup_at": last} for i, e, last in rows],
        }
    return out


# ------------------------------------------------------------------ copy 2 lag


async def replica_status(db: AsyncSession, max_lag_hours: int) -> dict[str, Any] | None:
    cfg = await storage_settings.load(db)
    if cfg.copy is None:
        return None
    last_ok = await db.scalar(
        select(func.max(Job.finished_at)).where(
            Job.kind == JobKind.REPLICATE, Job.status == JobStatus.SUCCEEDED
        )
    )
    last_ok_start = await db.scalar(
        select(func.max(Job.started_at)).where(
            Job.kind == JobKind.REPLICATE, Job.status == JobStatus.SUCCEEDED
        )
    )
    changed = await db.scalar(
        select(func.max(Job.finished_at)).where(
            Job.kind.in_([JobKind.BACKUP, JobKind.PURGE]), Job.status == JobStatus.SUCCEEDED
        )
    )
    last_failed = await db.scalar(
        select(Job).where(Job.kind == JobKind.REPLICATE).order_by(Job.created_at.desc()).limit(1)
    )
    now = datetime.now(UTC)
    behind = changed is not None and (last_ok_start is None or changed > last_ok_start)
    if last_ok is None and behind:  # never refreshed: late since the first change to copy
        last_ok_ref = await db.scalar(
            select(func.min(Job.finished_at)).where(
                Job.kind.in_([JobKind.BACKUP, JobKind.PURGE]), Job.status == JobStatus.SUCCEEDED
            )
        )
    else:
        last_ok_ref = last_ok
    lag = (now - (last_ok_ref or now)).total_seconds() if behind else 0.0
    return {
        "last_success_at": last_ok,
        "behind": behind,
        "lag_seconds": int(lag),
        "max_lag_hours": max_lag_hours,
        "late": behind and lag > max_lag_hours * 3600,
        "last_status": last_failed.status.value if last_failed else None,
        "last_error": last_failed.error if last_failed else None,
    }


# ------------------------------------------------------------------ integrity


async def integrity_status(db: AsyncSession) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for target in ("primary", "replica"):
        job = await db.scalar(
            select(Job)
            .where(Job.kind == JobKind.VERIFY, Job.params["target"].astext == target)
            .order_by(Job.created_at.desc())
            .limit(1)
        )
        out[target] = (
            {
                "job_id": job.id,
                "status": job.status.value,
                "created_at": job.created_at,
                "finished_at": job.finished_at,
                "sample": job.params.get("sample"),
                "progress": job.progress,
                "error": job.error,
            }
            if job
            else None
        )
    return out


async def queue_verify(
    db: AsyncSession, target: str, sample: float, user: uuid.UUID | None = None
) -> Job | None:
    pending = await db.scalar(
        select(Job.id).where(
            Job.kind == JobKind.VERIFY,
            Job.status.in_(PENDING),
            Job.params["target"].astext == target,
        )
    )
    if pending:
        return None
    job = Job(
        kind=JobKind.VERIFY,
        status=JobStatus.QUEUED,
        priority=220,  # after backups, purge and copy 2
        max_attempts=1,
        created_by=user,
        params={"target": target, "sample": sample},
    )
    db.add(job)
    await db.flush()
    return job


async def enqueue_verify(db: AsyncSession, now: datetime) -> list[uuid.UUID]:
    cfg = await load_settings(db)
    if not cfg.integrity_enabled:
        return []
    try:
        cron = Cron.parse(cfg.integrity_cron)
    except CronError:
        return []
    if not cfg.integrity_last_slot:  # first run: remember where we start
        cfg.integrity_last_slot = now.isoformat()
        await save_settings(db, cfg)
        return []
    last = datetime.fromisoformat(cfg.integrity_last_slot)
    slot = cron.last_slot(last, now, zone(cfg.integrity_timezone))
    if slot is None:
        return []
    cfg.integrity_last_slot = slot.astimezone(UTC).isoformat()
    await save_settings(db, cfg)
    queued = []
    targets = ["primary"] + (["replica"] if (await storage_settings.load(db)).copy else [])
    for target in targets:
        job = await queue_verify(db, target, cfg.integrity_sample)
        if job is not None:
            queued.append(job.id)
    return queued


# ------------------------------------------------------------------ alerts


async def periodic_alerts(db: AsyncSession, now: datetime) -> int:
    """Copy 2 late, storage full soon, mailboxes nearly full (once a day each)."""
    cfg = await load_settings(db)
    queued = 0
    day = f"{now:%Y-%m-%d}"
    if cfg.mailbox_alert_percent or cfg.mailbox_alert_gb:
        rows = (
            await db.execute(
                select(Account.email, Account.used_bytes, Account.quota_bytes)
                .where(
                    Account.status.in_([AccountStatus.ACTIVE, AccountStatus.LOCKED]),
                    nearly_full(cfg.mailbox_alert_percent, cfg.mailbox_alert_gb),
                )
                .order_by(Account.used_bytes.desc())
            )
        ).all()
        if rows:
            lines = [
                f"- {email} : {(used or 0) / 1024**3:.1f} GB"
                + (f" / {quota / 1024**3:.1f} GB ({(used or 0) * 100 // quota} %)" if quota else "")
                for email, used, quota in rows[:200]
            ]
            if len(rows) > 200:
                lines.append(f"… (+{len(rows) - 200})")
            queued += await notify(
                db,
                "mailbox_quota",
                dedup_key=f"mailbox_quota:{day}",
                count=len(rows),
                list="\n".join(lines),
            )
    replica = await replica_status(db, cfg.replica_max_lag_hours)
    if replica and replica["late"]:
        queued += await notify(
            db,
            "replica_lag",
            dedup_key=f"replica_lag:{day}",
            hours=replica["lag_seconds"] // 3600,
            limit=cfg.replica_max_lag_hours,
        )
    for series in await storage_trend(db):
        days = series["days_until_full"]
        if days is not None and days < FORECAST_ALERT_DAYS:
            queued += await notify(
                db,
                "storage_full",
                dedup_key=f"storage_full:{series['agent']}:{series['target']}:{day}",
                agent=series["agent"],
                target=series["target"],
                days=days,
            )
    return queued


async def prune(db: AsyncSession, now: datetime) -> None:
    from sqlalchemy import delete

    await db.execute(
        delete(StorageSample).where(StorageSample.hour < now - timedelta(days=KEEP_SAMPLES_DAYS))
    )
