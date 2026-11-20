"""``/api/mail``: mail traffic (sending limits, spam) — global administrators.

Counters come from the agent (see :mod:`carborewind_api.mail_traffic`). ``overview`` gives
top senders, spam received, spam senders, hourly totals and alerts for a period;
``limits`` sets the thresholds and the action (alert, or lock the account); an account can
be locked or unlocked by hand (the agent applies it within a minute).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import desc, func, select

from .. import audit, mail_traffic, rdns
from ..authz import Permission
from ..db.models import (
    Account,
    AccountAction,
    Agent,
    AgentStatus,
    LoginFailure,
    MailAlert,
    MailStat,
    Outcome,
    SpamSender,
)
from .deps import DbSession, Principal, client_ip, require

router = APIRouter(prefix="/api/mail", tags=["mail traffic"])

Admin = Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))]
PERIODS = {"1h": 0, "24h": 23, "7d": 24 * 7 - 1, "30d": 24 * 30 - 1}


class LimitsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    sent_messages_hour: int = Field(default=0, ge=0, le=1_000_000)
    sent_recipients_hour: int = Field(default=0, ge=0, le=10_000_000)
    sent_messages_day: int = Field(default=0, ge=0, le=10_000_000)
    sent_recipients_day: int = Field(default=0, ge=0, le=100_000_000)
    spam_sent_day: int = Field(default=0, ge=0, le=1_000_000)
    action: Literal["alert", "lock"] = "alert"
    exempt: list[EmailStr] = Field(default_factory=list, max_length=1000)
    max_auto_locks_per_hour: int = Field(default=3, ge=0, le=1000)


@router.get("/limits", response_model=LimitsIn)
async def get_limits(principal: Admin, db: DbSession) -> LimitsIn:
    return LimitsIn(**mail_traffic.asdict(await mail_traffic.load_limits(db)))


@router.put("/limits", response_model=LimitsIn)
async def put_limits(body: LimitsIn, principal: Admin, db: DbSession, request: Request) -> LimitsIn:
    limits = mail_traffic.MailLimits(
        **{**body.model_dump(), "exempt": sorted({str(e).lower() for e in body.exempt})}
    )
    await mail_traffic.save_limits(db, limits)
    await audit.record(
        db,
        action="mail.limits",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        details={k: v for k, v in mail_traffic.asdict(limits).items() if k != "exempt"}
        | {"exempt": len(limits.exempt)},
    )
    return LimitsIn(**mail_traffic.asdict(limits))


class AlertOut(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID | None
    email: str
    metric: str
    value: int
    threshold: int
    window_start: datetime
    action: str
    created_at: datetime
    acknowledged_at: datetime | None
    account_status: str | None


class Overview(BaseModel):
    period: str
    since: datetime
    totals: dict[str, int]
    top_senders: list[dict[str, Any]]
    spam_recipients: list[dict[str, Any]]
    spam_senders: list[dict[str, Any]]
    hourly: list[dict[str, Any]]
    alerts: list[AlertOut]
    pending_actions: list[dict[str, Any]]
    limits: LimitsIn
    agents: list[dict[str, Any]]


@router.get("/overview", response_model=Overview)
async def overview(
    principal: Admin, db: DbSession, period: Literal["1h", "24h", "7d", "30d"] = "24h"
) -> Overview:
    now = datetime.now(UTC)
    since = mail_traffic.hour_start(now) - timedelta(hours=PERIODS[period])
    in_period = MailStat.hour >= since
    sums = (
        func.sum(MailStat.sent_messages).label("messages"),
        func.sum(MailStat.sent_recipients).label("recipients"),
        func.sum(MailStat.spam_sent).label("spam_sent"),
        func.sum(MailStat.spam_received).label("spam_received"),
    )
    totals_row = (await db.execute(select(*sums).where(in_period))).one()
    spam_senders_total = await db.scalar(
        select(func.count(func.distinct(SpamSender.sender))).where(SpamSender.hour >= since)
    )
    senders = await db.execute(
        select(MailStat.email, *sums)
        .where(in_period)
        .group_by(MailStat.email)
        .having(func.sum(MailStat.sent_messages) + func.sum(MailStat.spam_sent) > 0)
        .order_by(desc("messages"), desc("spam_sent"))
        .limit(50)
    )
    sender_rows = senders.all()
    accounts = {
        a.email: a
        for a in await db.scalars(
            select(Account).where(Account.email.in_([r.email for r in sender_rows]))
        )
    }
    spam_rcpts = await db.execute(
        select(MailStat.email, func.sum(MailStat.spam_received).label("spam"))
        .where(in_period)
        .group_by(MailStat.email)
        .having(func.sum(MailStat.spam_received) > 0)
        .order_by(desc("spam"))
        .limit(20)
    )
    spam_from = await db.execute(
        select(
            SpamSender.sender,
            func.sum(SpamSender.count).label("total"),
            func.jsonb_agg(SpamSender.ips).label("ips"),
            func.max(SpamSender.hour).label("last"),
        )
        .where(SpamSender.hour >= since)
        .group_by(SpamSender.sender)
        .order_by(desc("total"))
        .limit(50)
    )
    step = "day" if period in ("7d", "30d") else "hour"
    bucket = func.date_trunc(step, MailStat.hour).label("bucket")
    hourly = await db.execute(
        select(bucket, *sums).where(in_period).group_by(bucket).order_by(bucket)
    )
    alerts = await db.execute(
        select(MailAlert, Account.status)
        .outerjoin(Account, Account.id == MailAlert.account_id)
        .order_by(MailAlert.acknowledged_at.is_not(None), MailAlert.created_at.desc())
        .limit(50)
    )
    pending = await db.execute(
        select(AccountAction, Account.email)
        .join(Account, Account.id == AccountAction.account_id)
        .where(AccountAction.status == "pending")
    )
    agents = [
        {"agent": name, **((report or {}).get("mail") or {})}
        for name, report in await db.execute(
            select(Agent.name, Agent.storage).where(Agent.status == AgentStatus.ACTIVE)
        )
    ]

    def ips(nested: Any) -> list[str]:
        seen: list[str] = []
        for group in nested or []:
            for ip in group or []:
                if ip not in seen:
                    seen.append(ip)
        return seen[:10]

    return Overview(
        period=period,
        since=since,
        totals={
            "sent_messages": int(totals_row.messages or 0),
            "sent_recipients": int(totals_row.recipients or 0),
            "spam_sent": int(totals_row.spam_sent or 0),
            "spam_received": int(totals_row.spam_received or 0),
            "spam_senders": int(spam_senders_total or 0),
        },
        top_senders=[
            {
                "email": r.email,
                "account_id": accounts[r.email].id if r.email in accounts else None,
                "status": accounts[r.email].status.value if r.email in accounts else None,
                "messages": int(r.messages or 0),
                "recipients": int(r.recipients or 0),
                "spam_sent": int(r.spam_sent or 0),
            }
            for r in sender_rows
        ],
        spam_recipients=[{"email": e, "count": int(n)} for e, n in spam_rcpts],
        spam_senders=[
            {"sender": r.sender, "count": int(r.total), "ips": ips(r.ips), "last": r.last}
            for r in spam_from
        ],
        hourly=[
            {
                "at": r.bucket,
                "sent_messages": int(r.messages or 0),
                "sent_recipients": int(r.recipients or 0),
                "spam_received": int(r.spam_received or 0),
                "spam_sent": int(r.spam_sent or 0),
            }
            for r in hourly
        ],
        alerts=[
            AlertOut(
                id=a.id,
                account_id=a.account_id,
                email=a.email,
                metric=a.metric,
                value=a.value,
                threshold=a.threshold,
                window_start=a.window_start,
                action=a.action,
                created_at=a.created_at,
                acknowledged_at=a.acknowledged_at,
                account_status=st.value if st else None,
            )
            for a, st in alerts
        ],
        pending_actions=[
            {"id": str(p.id), "email": email, "action": p.action, "created_at": p.created_at}
            for p, email in pending
        ],
        limits=LimitsIn(**mail_traffic.asdict(await mail_traffic.load_limits(db))),
        agents=agents,
    )


RECENT_BLOCK = timedelta(hours=1)


class BlockedIp(BaseModel):
    ip: str
    ptr: str | None
    domain: str | None
    failed_mailbox: int
    failed_smtp: int
    suspended: int
    last_suspended_at: datetime | None
    recently_blocked: bool
    first_seen: datetime
    last_seen: datetime
    accounts: list[str]


class LoginSecurity(BaseModel):
    period: str
    since: datetime
    totals: dict[str, int]
    ips: list[BlockedIp]
    reported: bool


@router.get("/security", response_model=LoginSecurity)
async def login_security(
    principal: Admin,
    db: DbSession,
    period: Literal["1h", "24h", "7d", "30d"] = "24h",
    only_blocked: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> LoginSecurity:
    """Client IPs with failed logins (webmail, IMAP, POP3, SMTP) and those Carbonio blocked
    after repeated failures, most blocked first."""
    now = datetime.now(UTC)
    since = mail_traffic.hour_start(now) - timedelta(hours=PERIODS[period])
    failed = func.sum(LoginFailure.failed_mailbox) + func.sum(LoginFailure.failed_smtp)
    query = (
        select(
            LoginFailure.ip,
            func.sum(LoginFailure.failed_mailbox).label("mailbox"),
            func.sum(LoginFailure.failed_smtp).label("smtp"),
            func.sum(LoginFailure.suspended).label("suspended"),
            func.max(LoginFailure.last_suspended_at).label("last_suspended"),
            func.min(LoginFailure.hour).label("first"),
            func.max(LoginFailure.hour).label("last"),
            func.jsonb_agg(LoginFailure.accounts).label("accounts"),
        )
        .where(LoginFailure.hour >= since)
        .group_by(LoginFailure.ip)
        .order_by(desc("suspended"), desc(failed), LoginFailure.ip)
        .limit(limit)
    )
    if only_blocked:
        query = query.having(func.sum(LoginFailure.suspended) > 0)
    rows = (await db.execute(query)).all()
    totals = (
        await db.execute(
            select(
                func.count(func.distinct(LoginFailure.ip)),
                func.coalesce(func.sum(LoginFailure.failed_mailbox + LoginFailure.failed_smtp), 0),
                func.count(func.distinct(LoginFailure.ip)).filter(LoginFailure.suspended > 0),
            ).where(LoginFailure.hour >= since)
        )
    ).one()
    names = await rdns.resolve(r.ip for r in rows)
    reported = await db.scalar(select(func.count()).select_from(LoginFailure)) or 0

    def accounts(nested: Any) -> list[str]:
        seen: list[str] = []
        for group in nested or []:
            for a in group or []:
                if a not in seen:
                    seen.append(a)
        return seen[:20]

    return LoginSecurity(
        period=period,
        since=since,
        totals={"ips": int(totals[0]), "failed": int(totals[1]), "blocked": int(totals[2])},
        ips=[
            BlockedIp(
                ip=r.ip,
                ptr=names.get(r.ip),
                domain=rdns.organisation(names.get(r.ip)),
                failed_mailbox=int(r.mailbox or 0),
                failed_smtp=int(r.smtp or 0),
                suspended=int(r.suspended or 0),
                last_suspended_at=r.last_suspended,
                recently_blocked=bool(r.last_suspended and r.last_suspended >= now - RECENT_BLOCK),
                first_seen=r.first,
                last_seen=r.last + timedelta(hours=1),
                accounts=accounts(r.accounts),
            )
            for r in rows
        ],
        reported=reported > 0,
    )


class SpamSourceIp(BaseModel):
    ip: str
    ptr: str | None
    count: int


class SpamSource(BaseModel):
    domain: str | None
    count: int
    ips: list[SpamSourceIp]
    sender_domains: list[str]


class SpamSources(BaseModel):
    period: str
    since: datetime
    sources: list[SpamSource]
    sender_domains: list[dict[str, Any]]


MAX_SPAM_IPS = 300


@router.get("/spam-sources", response_model=SpamSources)
async def spam_sources(
    principal: Admin, db: DbSession, period: Literal["1h", "24h", "7d", "30d"] = "24h"
) -> SpamSources:
    """Where the spam received comes from: the client IPs grouped by the domain of their
    reverse DNS name, and the domains of the sender addresses. A spam is counted for each
    IP its sender was seen with during the hour."""
    now = datetime.now(UTC)
    since = mail_traffic.hour_start(now) - timedelta(hours=PERIODS[period])
    rows = await db.execute(
        select(SpamSender.sender, SpamSender.count, SpamSender.ips)
        .where(SpamSender.hour >= since)
        .limit(50_000)
    )
    per_ip: dict[str, int] = {}
    ip_senders: dict[str, dict[str, int]] = {}
    sender_domains: dict[str, int] = {}
    for sender, count, ips in rows:
        sender_domain = sender.rpartition("@")[2].lower() if "@" in sender else "<>"
        sender_domains[sender_domain] = sender_domains.get(sender_domain, 0) + count
        for ip in ips or []:
            per_ip[ip] = per_ip.get(ip, 0) + count
            domains = ip_senders.setdefault(ip, {})
            domains[sender_domain] = domains.get(sender_domain, 0) + count
    top = sorted(per_ip, key=lambda ip: (-per_ip[ip], ip))[:MAX_SPAM_IPS]
    names = await rdns.resolve(top)
    groups: dict[str | None, SpamSource] = {}
    for ip in top:
        ptr = names.get(ip)
        domain = rdns.organisation(ptr)
        group = groups.setdefault(
            domain, SpamSource(domain=domain, count=0, ips=[], sender_domains=[])
        )
        group.count += per_ip[ip]
        group.ips.append(SpamSourceIp(ip=ip, ptr=ptr, count=per_ip[ip]))
        for d in sorted(ip_senders[ip], key=lambda d: -ip_senders[ip][d]):
            if d not in group.sender_domains and len(group.sender_domains) < 10:
                group.sender_domains.append(d)
    sources = sorted(groups.values(), key=lambda g: (-g.count, g.domain or "~"))[:100]
    for g in sources:
        g.ips = g.ips[:20]
    return SpamSources(
        period=period,
        since=since,
        sources=sources,
        sender_domains=[
            {"domain": d, "count": n}
            for d, n in sorted(sender_domains.items(), key=lambda kv: (-kv[1], kv[0]))[:50]
        ],
    )


@router.post("/alerts/{alert_id}/ack", status_code=status.HTTP_204_NO_CONTENT)
async def acknowledge(alert_id: uuid.UUID, principal: Admin, db: DbSession) -> None:
    alert = await db.get(MailAlert, alert_id)
    if alert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if alert.acknowledged_at is None:
        alert.acknowledged_at, alert.acknowledged_by = datetime.now(UTC), principal.user.id


class LockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


class ActionOut(BaseModel):
    id: uuid.UUID | None
    action: str
    queued: bool


@router.post("/accounts/{account_id}/{action}", response_model=ActionOut)
async def account_action(
    account_id: uuid.UUID,
    action: Literal["lock", "unlock"],
    body: LockIn,
    principal: Admin,
    db: DbSession,
    request: Request,
) -> ActionOut:
    """Lock or unlock an account by hand (the agent applies it within a minute)."""
    account = await db.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    queued = await mail_traffic.queue_action(
        db,
        account.id,
        action,
        body.reason or f"by {principal.user.username}",
        user_id=principal.user.id,
    )
    await audit.record(
        db,
        action=f"account.{action}_request",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        target_type="account",
        target_id=account.email,
        domain_id=account.domain_id,
        details={"reason": body.reason},
    )
    return ActionOut(id=queued.id if queued else None, action=action, queued=queued is not None)
