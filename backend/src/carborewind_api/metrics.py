"""Business metrics for Prometheus (``/metrics``), computed from the database on scrape.

Gauges only, low cardinality (no per-account label). The process metrics of
``prometheus_client`` are served too. Grafana dashboard and alert rules:
``deploy/monitoring/``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import monitoring
from .db.models import (
    Account,
    AccountStatus,
    Agent,
    AgentStatus,
    Job,
    JobKind,
    JobStatus,
    MailAlert,
    MailStat,
    Snapshot,
    SnapshotStatus,
)

FAILED_HELP = "Jobs failed for good in the last 24 h"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class Exposition:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._declared: set[str] = set()

    def gauge(self, name: str, help_text: str, value: float | None, **labels: str) -> None:
        full = f"carborewind_{name}"
        if full not in self._declared:
            self.lines += [f"# HELP {full} {help_text}", f"# TYPE {full} gauge"]
            self._declared.add(full)
        if value is None:
            return
        label = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
        self.lines.append(f"{full}{{{label}}} {value}" if label else f"{full} {value}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


async def collect(db: AsyncSession) -> str:
    now = datetime.now(UTC)
    out = Exposition()
    cfg = await monitoring.load_settings(db)

    for st, count in await db.execute(
        select(Account.status, func.count()).group_by(Account.status)
    ):
        out.gauge("accounts", "Accounts known from Carbonio, by status", count, status=st.value)
    risk = await monitoring.accounts_at_risk(db, cfg.stale_hours, limit=0)
    for key, data in risk.items():
        out.gauge("accounts_at_risk", "Accounts at risk, by reason", data["count"], reason=key)

    oldest = await db.scalar(
        select(func.min(_last_ok()))
        .select_from(Account)
        .where(Account.status.in_([AccountStatus.ACTIVE, AccountStatus.LOCKED]))
    )
    out.gauge(
        "oldest_backup_age_seconds",
        "Age of the oldest last successful backup among live accounts",
        (now - oldest).total_seconds() if oldest else None,
    )
    for kind, job_status, count in await db.execute(
        select(Job.kind, Job.status, func.count())
        .where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
        .group_by(Job.kind, Job.status)
    ):
        out.gauge(
            "jobs", "Queued and running jobs", count, kind=kind.value, status=job_status.value
        )
    for kind, count in await db.execute(
        select(Job.kind, func.count())
        .where(Job.status == JobStatus.FAILED, Job.finished_at > now - timedelta(hours=24))
        .group_by(Job.kind)
    ):
        out.gauge(
            "jobs_failed_24h", "Jobs failed for good in the last 24 h", count, kind=kind.value
        )
    out.gauge("jobs_failed_24h", "Jobs failed for good in the last 24 h", None)

    for series in await monitoring.storage_trend(db):
        labels = {"agent": series["agent"], "target": series["target"]}
        out.gauge("storage_total_bytes", "Size of the storage", series["total_bytes"], **labels)
        out.gauge("storage_free_bytes", "Free space of the storage", series["free_bytes"], **labels)
        out.gauge(
            "storage_days_until_full",
            "Forecast days before the storage is full (absent: not growing)",
            series["days_until_full"],
            **labels,
        )
    replica = await monitoring.replica_status(db, cfg.replica_max_lag_hours)
    out.gauge(
        "replica_lag_seconds",
        "How long copy 2 has been behind the repository (0: up to date)",
        replica["lag_seconds"] if replica else None,
    )
    for target, data in (await monitoring.integrity_status(db)).items():
        if data and data.get("finished_at"):
            progress = data.get("progress") or {}
            out.gauge(
                "integrity_last_check_timestamp_seconds",
                "End of the last integrity check",
                data["finished_at"].timestamp(),
                target=target,
            )
            out.gauge(
                "integrity_corrupted_files",
                "Corrupted files found by the last integrity check",
                int(progress.get("corrupted_count", 0))
                + len(progress.get("manifests_corrupted", [])),
                target=target,
            )
    for agent in await db.scalars(select(Agent).where(Agent.status == AgentStatus.ACTIVE)):
        if agent.last_heartbeat_at:
            out.gauge(
                "agent_last_seen_seconds",
                "Seconds since the agent last contacted the server",
                (now - agent.last_heartbeat_at).total_seconds(),
                agent=agent.name,
            )
        if agent.cert_not_after:
            out.gauge(
                "agent_certificate_expiry_seconds",
                "Seconds before the agent's client certificate expires",
                (agent.cert_not_after - now).total_seconds(),
                agent=agent.name,
            )
    hour = now.replace(minute=0, second=0, microsecond=0)
    sent, spam_in, spam_out = (
        await db.execute(
            select(
                func.coalesce(func.sum(MailStat.sent_messages), 0),
                func.coalesce(func.sum(MailStat.spam_received), 0),
                func.coalesce(func.sum(MailStat.spam_sent), 0),
            ).where(MailStat.hour >= hour)
        )
    ).one()
    out.gauge("mail_sent_current_hour", "Messages sent by local accounts this hour", int(sent))
    out.gauge("mail_spam_received_current_hour", "Spam received this hour", int(spam_in))
    out.gauge("mail_spam_sent_current_hour", "Spam sent by local accounts this hour", int(spam_out))
    open_alerts = await db.scalar(
        select(func.count()).select_from(MailAlert).where(MailAlert.acknowledged_at.is_(None))
    )
    out.gauge("mail_alerts_open", "Sending-limit alerts not acknowledged", int(open_alerts or 0))
    return out.text()


def _last_ok() -> Any:
    from sqlalchemy.orm import aliased

    snap = aliased(Snapshot)
    return (
        select(func.max(snap.taken_at))
        .where(
            snap.account_id == Account.id,
            snap.status.in_([SnapshotStatus.COMPLETE, SnapshotStatus.PARTIAL]),
        )
        .correlate(Account)
        .scalar_subquery()
    )


__all__ = ["JobKind", "collect"]
