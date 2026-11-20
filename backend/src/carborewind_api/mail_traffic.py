"""Mail traffic: sending limits per mailbox, spam received, spam senders.

The agent reads the Carbonio MTA log and sends, every minute, absolute counters per UTC
hour (see ``carborewind_agent.maillog``); they are upserted here, then the **sending
limits** (``app_settings`` row ``mail_limits``) are checked for the senders concerned:

* messages and recipients sent in the current hour, and over the last 24 hours;
* spam sent over the last 24 hours (spam submitted by a local account: stolen password).

A limit reached creates one alert per account, metric and window (hour, or UTC day for the
daily limits), sends the ``mail_limit`` notification and, when the action is ``lock``, queues
a lock of the account that the agent applies within a minute (``zimbraAccountStatus =
locked``: no more webmail, IMAP or SMTP authentication). Exempted addresses (newsletters,
application accounts) are never limited. Unlocking is an explicit action of an
administrator.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field

__all__ = ["asdict"]
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .db.models import (
    Account,
    AccountAction,
    AccountStatus,
    ActorType,
    AppSetting,
    IpAction,
    LoginFailure,
    MailAlert,
    MailStat,
    Outcome,
    SpamSender,
)
from .notifications import notify

LIMITS_KEY = "mail_limits"
METRICS = (
    "sent_messages_hour",
    "sent_recipients_hour",
    "sent_messages_day",
    "sent_recipients_day",
    "spam_sent_day",
)
KEEP_DAYS = 90


@dataclass
class MailLimits:
    enabled: bool = False
    # 0 = no limit for that metric.
    sent_messages_hour: int = 0
    sent_recipients_hour: int = 0
    sent_messages_day: int = 0
    sent_recipients_day: int = 0
    spam_sent_day: int = 0
    action: Literal["alert", "lock"] = "alert"
    exempt: list[str] = field(default_factory=list)
    # Safety: never more automatic locks than this per hour (beyond: alert only), and
    # administrator accounts are never locked automatically.
    max_auto_locks_per_hour: int = 3

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> MailLimits:
        out = cls()
        for key, value in (data or {}).items():
            if hasattr(out, key):
                setattr(out, key, value)
        return out


async def load_limits(db: AsyncSession) -> MailLimits:
    row = await db.get(AppSetting, LIMITS_KEY)
    return MailLimits.from_json(row.value if row else None)


async def save_limits(db: AsyncSession, limits: MailLimits) -> None:
    value = asdict(limits)
    await db.execute(
        insert(AppSetting)
        .values(key=LIMITS_KEY, value=value, updated_at=func.now())
        .on_conflict_do_update(
            index_elements=["key"], set_={"value": value, "updated_at": func.now()}
        )
    )


def hour_start(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


async def record(db: AsyncSession, hours: list[dict[str, Any]]) -> set[str]:
    """Upsert the counters; returns the senders whose sending counters changed."""
    emails: set[str] = set()
    for h in hours:
        emails |= set(h.get("sent", {})) | set(h.get("spam_in", {})) | set(h.get("spam_out", {}))
    ids = (
        {
            e: i
            for e, i in await db.execute(
                select(Account.email, Account.id).where(
                    Account.email.in_(emails), Account.status != AccountStatus.DELETED
                )
            )
        }
        if emails
        else {}
    )
    senders: set[str] = set()
    for h in hours:
        hour = hour_start(datetime.fromisoformat(h["hour"]))
        sent, spam_in, spam_out = h.get("sent", {}), h.get("spam_in", {}), h.get("spam_out", {})
        rows = [
            {
                "hour": hour,
                "email": email,
                "account_id": ids.get(email),
                "sent_messages": int((sent.get(email) or [0, 0])[0]),
                "sent_recipients": int((sent.get(email) or [0, 0])[1]),
                "spam_received": int(spam_in.get(email, 0)),
                "spam_sent": int(spam_out.get(email, 0)),
            }
            for email in set(sent) | set(spam_in) | set(spam_out)
        ]
        senders |= set(sent) | set(spam_out)
        if rows:
            stmt = insert(MailStat).values(rows)
            await db.execute(
                stmt.on_conflict_do_update(
                    index_elements=["hour", "email"],
                    set_={
                        "account_id": stmt.excluded.account_id,
                        "sent_messages": stmt.excluded.sent_messages,
                        "sent_recipients": stmt.excluded.sent_recipients,
                        "spam_received": stmt.excluded.spam_received,
                        "spam_sent": stmt.excluded.spam_sent,
                    },
                )
            )
        spam = h.get("spam_senders", {})
        if spam:
            stmt2 = insert(SpamSender).values(
                [
                    {
                        "hour": hour,
                        "sender": sender[:320],
                        "count": int(v.get("count", 0)),
                        "ips": [str(ip)[:64] for ip in v.get("ips", [])][:10],
                    }
                    for sender, v in spam.items()
                ]
            )
            await db.execute(
                stmt2.on_conflict_do_update(
                    index_elements=["hour", "sender"],
                    set_={"count": stmt2.excluded.count, "ips": stmt2.excluded.ips},
                )
            )
    return senders


async def evaluate(db: AsyncSession, senders: set[str], now: datetime) -> list[MailAlert]:
    """Check the limits for these senders; returns the new alerts."""
    limits = await load_limits(db)
    exempt = {e.lower() for e in limits.exempt}
    candidates = sorted(s for s in senders if s not in exempt)
    if not limits.enabled or not candidates:
        return []
    current = hour_start(now)
    day_from = current - timedelta(hours=23)
    day_window = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = await db.execute(
        select(
            MailStat.email,
            func.sum(MailStat.sent_messages).filter(MailStat.hour == current),
            func.sum(MailStat.sent_recipients).filter(MailStat.hour == current),
            func.sum(MailStat.sent_messages),
            func.sum(MailStat.sent_recipients),
            func.sum(MailStat.spam_sent),
        )
        .where(MailStat.email.in_(candidates), MailStat.hour >= day_from)
        .group_by(MailStat.email)
    )
    accounts = {
        e: i
        for e, i in await db.execute(
            select(Account.email, Account.id).where(
                Account.email.in_(candidates), Account.status != AccountStatus.DELETED
            )
        )
    }
    alerts: list[MailAlert] = []
    for email, msg_h, rcpt_h, msg_d, rcpt_d, spam_d in rows:
        account_id = accounts.get(email)
        values = {
            "sent_messages_hour": (msg_h or 0, current),
            "sent_recipients_hour": (rcpt_h or 0, current),
            "sent_messages_day": (msg_d or 0, day_window),
            "sent_recipients_day": (rcpt_d or 0, day_window),
            "spam_sent_day": (spam_d or 0, day_window),
        }
        for metric, (value, window) in values.items():
            threshold = int(getattr(limits, metric))
            if threshold <= 0 or value < threshold:
                continue
            alert = await _raise(
                db, email, account_id, metric, int(value), threshold, window, limits.action
            )
            if alert is not None:
                alerts.append(alert)
    return alerts


async def _raise(
    db: AsyncSession,
    email: str,
    account_id: uuid.UUID | None,
    metric: str,
    value: int,
    threshold: int,
    window: datetime,
    action: str,
) -> MailAlert | None:
    inserted = await db.execute(
        insert(MailAlert)
        .values(
            email=email,
            account_id=account_id,
            metric=metric,
            value=value,
            threshold=threshold,
            window_start=window,
            action=action,
        )
        .on_conflict_do_nothing(constraint="uq_mail_alerts_window")
        .returning(MailAlert.id)
    )
    alert_id = inserted.scalar_one_or_none()
    if alert_id is None:
        return None  # already raised for this window
    alert = await db.get(MailAlert, alert_id)
    assert alert is not None  # noqa: S101
    locked = False
    if action == "lock" and account_id is not None:
        limits = await load_limits(db)
        account = await db.get(Account, account_id)
        recent = await db.scalar(
            select(func.count())
            .select_from(AccountAction)
            .where(
                AccountAction.action == "lock",
                AccountAction.alert_id.is_not(None),
                AccountAction.created_at > func.now() - timedelta(hours=1),
            )
        )
        if account is not None and account.is_admin:
            alert.action = "alert_admin"  # an administrator is never locked automatically
        elif (recent or 0) >= limits.max_auto_locks_per_hour:
            alert.action = "alert_cap"  # too many automatic locks: a human decides
        else:
            queued = await queue_action(
                db, account_id, "lock", f"{metric}: {value} >= {threshold}", alert_id=alert_id
            )
            locked = queued is not None
    await notify(
        db,
        "mail_limit",
        account=email,
        metric=metric,
        value=value,
        threshold=threshold,
        action="lock" if locked else alert.action,
    )
    await audit.record(
        db,
        action="mail.limit_exceeded",
        outcome=Outcome.FAILURE,
        actor_type=ActorType.SYSTEM,
        actor_name="mail-traffic",
        target_type="account",
        target_id=email,
        details={"metric": metric, "value": value, "threshold": threshold, "lock": locked},
    )
    return alert


async def queue_action(
    db: AsyncSession,
    account_id: uuid.UUID,
    action: Literal["lock", "unlock"],
    reason: str | None,
    *,
    alert_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> AccountAction | None:
    """Queue a lock/unlock for the agent (None if the same one is already pending)."""
    pending = await db.scalar(
        select(AccountAction).where(
            AccountAction.account_id == account_id,
            AccountAction.status == "pending",
        )
    )
    if pending is not None:
        if pending.action == action:
            return None
        pending.status, pending.error = "failed", "superseded"
    row = AccountAction(
        account_id=account_id,
        action=action,
        reason=(reason or "")[:500] or None,
        alert_id=alert_id,
        created_by=user_id,
    )
    db.add(row)
    await db.flush()
    return row


async def pending_actions(db: AsyncSession) -> list[dict[str, Any]]:
    rows = await db.execute(
        select(AccountAction, Account)
        .join(Account, Account.id == AccountAction.account_id)
        .where(AccountAction.status == "pending")
        .order_by(AccountAction.created_at)
        .limit(100)
    )
    return [
        {
            "id": str(a.id),
            "action": a.action,
            "carbonio_id": acc.carbonio_id,
            "email": acc.email,
            "reason": a.reason,
        }
        for a, acc in rows
    ]


async def pending_ip_actions(db: AsyncSession) -> list[dict[str, Any]]:
    rows = await db.scalars(
        select(IpAction)
        .where(IpAction.status == "pending")
        .order_by(IpAction.created_at)
        .limit(100)
    )
    return [{"id": str(a.id), "action": a.action, "ip": a.ip} for a in rows]


# Blocks older than this when first reported (e.g. the first read of an old log) are not
# alerted: the alert is about what happens now.
ALERT_RECENT = timedelta(hours=2)


async def record_logins(
    db: AsyncSession, hours: list[dict[str, Any]], now: datetime | None = None
) -> None:
    """Upsert the failed logins / suspensions per hour and IP (absolute counters), and
    alert (``ip_blocked``) on each new block by Carbonio."""
    now = now or datetime.now(UTC)
    for h in hours:
        hour = hour_start(datetime.fromisoformat(h["hour"]))
        known = {
            ip: last
            for ip, last in await db.execute(
                select(LoginFailure.ip, LoginFailure.last_suspended_at).where(
                    LoginFailure.hour == hour
                )
            )
        }
        rows: list[dict[str, Any]] = []
        for ip, c in (h.get("ips") or {}).items():
            last = c.get("last_suspended")
            rows.append(
                {
                    "hour": hour,
                    "ip": str(ip)[:64],
                    "failed_mailbox": int(c.get("failed_mailbox", 0)),
                    "failed_smtp": int(c.get("failed_smtp", 0)),
                    "suspended": int(c.get("suspended", 0)),
                    "last_suspended_at": datetime.fromisoformat(last) if last else None,
                    "accounts": [str(a)[:320] for a in c.get("accounts", [])][:10],
                }
            )
        for row in rows:
            last = row["last_suspended_at"]
            before = known.get(row["ip"])
            if last and (before is None or last > before) and last >= now - ALERT_RECENT:
                await notify(
                    db,
                    "ip_blocked",
                    dedup_key=f"ip_blocked:{row['ip']}:{last.isoformat()}",
                    ip=row["ip"],
                    at=last.strftime("%Y-%m-%d %H:%M UTC"),
                    failed=row["failed_mailbox"] + row["failed_smtp"],
                    accounts=", ".join(row["accounts"]) or "?",
                )
        for start in range(0, len(rows), 2000):
            stmt = insert(LoginFailure).values(rows[start : start + 2000])
            await db.execute(
                stmt.on_conflict_do_update(
                    index_elements=["hour", "ip"],
                    set_={
                        "failed_mailbox": stmt.excluded.failed_mailbox,
                        "failed_smtp": stmt.excluded.failed_smtp,
                        "suspended": stmt.excluded.suspended,
                        "last_suspended_at": stmt.excluded.last_suspended_at,
                        "accounts": stmt.excluded.accounts,
                    },
                )
            )


async def prune(db: AsyncSession, now: datetime) -> None:
    """Counters older than KEEP_DAYS are dropped (run by the scheduler)."""
    from sqlalchemy import delete

    horizon = now - timedelta(days=KEEP_DAYS)
    await db.execute(delete(MailStat).where(MailStat.hour < horizon))
    await db.execute(delete(SpamSender).where(SpamSender.hour < horizon))
    await db.execute(delete(LoginFailure).where(LoginFailure.hour < horizon))
