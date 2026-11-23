"""``/api/mail``: mail traffic (sending limits, spam) — global administrators.

Counters come from the agent (see :mod:`carborewind_api.mail_traffic`). ``overview`` gives
top senders, spam received, spam senders, hourly totals and alerts for a period;
``limits`` sets the thresholds and the action (alert, or lock the account); an account can
be locked or unlocked by hand (the agent applies it within a minute).
"""

from __future__ import annotations

import ipaddress
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy import desc, func, select

from .. import audit, blocklist, mail_traffic, rdns
from ..authz import Permission
from ..db.models import (
    Account,
    AccountAction,
    AccountStatus,
    Agent,
    AgentStatus,
    IpAction,
    Job,
    JobStatus,
    LoginFailure,
    MailAlert,
    MailStat,
    Outcome,
    Snapshot,
    SnapshotStatus,
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


# Without Carbonio's settings (old agent): a block is assumed to last up to an hour.
DEFAULT_BLOCK = timedelta(hours=1)


def block_window(cfg: dict[str, Any] | None) -> timedelta:
    """How long a block lasts: Carbonio's delay, plus the interval of the task that lifts
    blocks (it runs every N minutes, so a block can outlast the delay by up to N)."""
    minutes = (cfg or {}).get("block_minutes")
    if not minutes:
        return DEFAULT_BLOCK
    return timedelta(minutes=int(minutes) + int((cfg or {}).get("task_minutes") or 0))


class BlockedIp(BaseModel):
    ip: str
    ptr: str | None
    domain: str | None
    failed_mailbox: int
    failed_smtp: int
    suspended: int
    last_suspended_at: datetime | None
    # Still blocked now (last block + Carbonio's delay not over), and until when.
    recently_blocked: bool
    blocked_until: datetime | None = None
    first_seen: datetime
    # Exact time of the last failure when the agent reports it; else the start of the
    # hour it happened in (last_seen_exact False).
    last_seen: datetime
    last_seen_exact: bool = True
    accounts: list[str]
    # In Carbonio's trusted addresses (zimbraHttpThrottleSafeIPs): never blocked again.
    trusted: bool = False
    # A "trust" or "untrust" of this address is waiting for the agent.
    pending: str | None = None


LogStatus = Literal["ok", "denied", "missing", "agent_outdated", "not_reporting"]


class LoginSecurity(BaseModel):
    period: str
    since: datetime
    totals: dict[str, int]
    ips: list[BlockedIp]
    reported: bool
    # Can the agent read mailbox.log? (the reason why nothing shows, if nothing does)
    log_status: LogStatus = "ok"
    log_path: str | None = None


STALE_REPORT = timedelta(minutes=10)


async def _log_status(db: DbSession, now: datetime) -> tuple[LogStatus, str | None]:
    reports = [
        (storage or {}).get("mail") or {}
        for storage in await db.scalars(
            select(Agent.storage).where(Agent.status == AgentStatus.ACTIVE)
        )
    ]
    fresh = [
        r
        for r in reports
        if r.get("reported_at") and datetime.fromisoformat(r["reported_at"]) >= now - STALE_REPORT
    ]
    if not fresh:
        return "not_reporting", None
    logs = [r["mailbox_log"] for r in fresh if isinstance(r.get("mailbox_log"), dict)]
    if not logs:
        return "agent_outdated", None
    readable = next((m for m in logs if m.get("readable")), None)
    if readable is not None:
        return "ok", readable.get("path")
    present = next((m for m in logs if m.get("exists")), None)
    if present is not None:
        return "denied", present.get("path")
    return "missing", logs[0].get("path")


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
            func.max(LoginFailure.last_failed_at).label("last_failed"),
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
    cfg = await _login_filter(db)
    safe = _networks((cfg or {}).get("safe_ips", []))
    window = block_window(cfg)

    def until(last: datetime | None) -> datetime | None:
        return last + window if last and last + window > now else None

    pending = {
        a.ip: a.action
        for a in await db.scalars(select(IpAction).where(IpAction.status == "pending"))
    }
    log_status, log_path = await _log_status(db, now)

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
        totals={
            "ips": int(totals[0]),
            "failed": int(totals[1]),
            "blocked": int(totals[2]),
            "blocked_now": sum(1 for r in rows if until(r.last_suspended)),
        },
        ips=[
            BlockedIp(
                ip=r.ip,
                ptr=names.get(r.ip),
                domain=rdns.organisation(names.get(r.ip)),
                failed_mailbox=int(r.mailbox or 0),
                failed_smtp=int(r.smtp or 0),
                suspended=int(r.suspended or 0),
                last_suspended_at=r.last_suspended,
                recently_blocked=until(r.last_suspended) is not None,
                blocked_until=until(r.last_suspended),
                first_seen=r.first,
                last_seen=r.last_failed or r.last,
                last_seen_exact=r.last_failed is not None,
                accounts=accounts(r.accounts),
                trusted=_covered(r.ip, safe),
                pending=pending.get(r.ip),
            )
            for r in rows
        ],
        reported=reported > 0,
        log_status=log_status,
        log_path=log_path,
    )


def _networks(values: list[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    out = []
    for value in values:
        try:
            out.append(ipaddress.ip_network(value.strip(), strict=False))
        except ValueError:
            continue
    return out


def _covered(ip: str, networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr.version == n.version and addr in n for n in networks)


async def _login_filter(db: DbSession) -> dict[str, Any] | None:
    """The latest report of Carbonio's login filter settings among the active agents."""
    reports = [
        ((storage or {}).get("mail") or {}).get("login_filter")
        for storage in await db.scalars(
            select(Agent.storage).where(Agent.status == AgentStatus.ACTIVE)
        )
    ]
    found = [r for r in reports if isinstance(r, dict)]
    return max(found, key=lambda r: str(r.get("at", ""))) if found else None


class LoginFilterConfig(BaseModel):
    safe_ips: list[str]
    max_failed: int | None
    block_minutes: int | None
    task_minutes: int | None
    at: datetime | None


class IpActionOut(BaseModel):
    id: uuid.UUID
    ip: str
    action: str
    status: str
    error: str | None
    created_at: datetime
    done_at: datetime | None


class LoginFilterOut(BaseModel):
    config: LoginFilterConfig | None
    actions: list[IpActionOut]


@router.get("/login-filter", response_model=LoginFilterOut)
async def login_filter(principal: Admin, db: DbSession) -> LoginFilterOut:
    """Carbonio's invalid login filter (as last read by the agent), trusted addresses and
    the latest trust / untrust requests."""
    cfg = await _login_filter(db)
    actions = await db.scalars(select(IpAction).order_by(IpAction.created_at.desc()).limit(20))
    return LoginFilterOut(
        config=LoginFilterConfig(**{"at": None, **cfg}) if cfg else None,
        actions=[
            IpActionOut(
                id=a.id,
                ip=a.ip,
                action=a.action,
                status=a.status,
                error=a.error,
                created_at=a.created_at,
                done_at=a.done_at,
            )
            for a in actions
        ],
    )


class IpIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ip: str = Field(min_length=2, max_length=64)

    @field_validator("ip")
    @classmethod
    def _ip(cls, value: str) -> str:
        """A single address or a network (CIDR), as Carbonio accepts both."""
        value = value.strip()
        try:
            if "/" in value:
                return str(ipaddress.ip_network(value, strict=False))
            return str(ipaddress.ip_address(value))
        except ValueError as exc:
            raise ValueError("not an IP address or network") from exc


@router.post(
    "/login-filter/{action}", response_model=IpActionOut, status_code=status.HTTP_202_ACCEPTED
)
async def queue_ip_action(
    action: Literal["trust", "untrust"],
    body: IpIn,
    principal: Admin,
    db: DbSession,
    request: Request,
) -> IpActionOut:
    """Add an address to Carbonio's trusted addresses (it is then never blocked again), or
    remove it; the agent applies it within a minute."""
    existing = await db.scalar(
        select(IpAction).where(
            IpAction.ip == body.ip, IpAction.action == action, IpAction.status == "pending"
        )
    )
    item = existing or IpAction(ip=body.ip, action=action, created_by=principal.user.id)
    if existing is None:
        # A pending opposite request is replaced by this one.
        for other in await db.scalars(
            select(IpAction).where(IpAction.ip == body.ip, IpAction.status == "pending")
        ):
            await db.delete(other)
        db.add(item)
        await db.flush()
        await db.refresh(item)
        await audit.record(
            db,
            action=f"mail.ip_{action}",
            outcome=Outcome.SUCCESS,
            actor_id=str(principal.user.id),
            actor_name=principal.user.username,
            target_type="ip",
            target_id=body.ip,
            source_ip=client_ip(request),
        )
    return IpActionOut(
        id=item.id,
        ip=item.ip,
        action=item.action,
        status=item.status,
        error=item.error,
        created_at=item.created_at,
        done_at=item.done_at,
    )


class SpamSourceIp(BaseModel):
    ip: str
    ptr: str | None
    count: int


class SpamSenderOut(BaseModel):
    address: str  # "<>": a bounce (delivery status notification), no sender
    count: int
    recipients: list[str] = Field(default_factory=list)  # local mailboxes that received it


class SpamSource(BaseModel):
    domain: str | None
    count: int
    ips: list[SpamSourceIp]
    sender_domains: list[str]
    # The sender addresses behind these IPs (what can be blocked without blocking the
    # whole provider).
    senders: list[SpamSenderOut] = Field(default_factory=list)


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
        select(SpamSender.sender, SpamSender.count, SpamSender.ips, SpamSender.rcpts)
        .where(SpamSender.hour >= since)
        .limit(50_000)
    )
    per_ip: dict[str, int] = {}
    ip_senders: dict[str, dict[str, int]] = {}
    ip_addresses: dict[str, dict[str, int]] = {}
    sender_domains: dict[str, int] = {}
    rcpts_of: dict[str, list[str]] = {}
    for sender, count, ips, rcpts in rows:
        known = rcpts_of.setdefault(sender.lower(), [])
        for r in rcpts or []:
            if r not in known and len(known) < 10:
                known.append(r)
        sender_domain = sender.rpartition("@")[2].lower() if "@" in sender else "<>"
        sender_domains[sender_domain] = sender_domains.get(sender_domain, 0) + count
        for ip in ips or []:
            per_ip[ip] = per_ip.get(ip, 0) + count
            domains = ip_senders.setdefault(ip, {})
            domains[sender_domain] = domains.get(sender_domain, 0) + count
            addresses = ip_addresses.setdefault(ip, {})
            addresses[sender.lower()] = addresses.get(sender.lower(), 0) + count
    top = sorted(per_ip, key=lambda ip: (-per_ip[ip], ip))[:MAX_SPAM_IPS]
    names = await rdns.resolve(top)
    groups: dict[str | None, SpamSource] = {}
    group_senders: dict[str | None, dict[str, int]] = {}
    for ip in top:
        ptr = names.get(ip)
        domain = rdns.organisation(ptr)
        group = groups.setdefault(
            domain, SpamSource(domain=domain, count=0, ips=[], sender_domains=[])
        )
        group.count += per_ip[ip]
        group.ips.append(SpamSourceIp(ip=ip, ptr=ptr, count=per_ip[ip]))
        tally = group_senders.setdefault(domain, {})
        for address, n in ip_addresses[ip].items():
            tally[address] = tally.get(address, 0) + n
        for d in sorted(ip_senders[ip], key=lambda d: -ip_senders[ip][d]):
            if d not in group.sender_domains and len(group.sender_domains) < 10:
                group.sender_domains.append(d)
    sources = sorted(groups.values(), key=lambda g: (-g.count, g.domain or "~"))[:100]
    for g in sources:
        g.ips = g.ips[:20]
        tally = group_senders.get(g.domain, {})
        g.senders = [
            SpamSenderOut(address=a, count=n, recipients=rcpts_of.get(a, []))
            for a, n in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
        ]
    return SpamSources(
        period=period,
        since=since,
        sources=sources,
        sender_domains=[
            {"domain": d, "count": n}
            for d, n in sorted(sender_domains.items(), key=lambda kv: (-kv[1], kv[0]))[:50]
        ],
    )


class BlockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=3, max_length=320)
    reason: str | None = Field(default=None, max_length=300)
    force: bool = False  # confirm the domain of a public provider


class UnblockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=3, max_length=320)


async def _blocklist_out(db: DbSession) -> dict[str, Any]:
    data = await blocklist.load(db)
    agents = await db.scalars(
        select(Agent).where(Agent.status == AgentStatus.ACTIVE).order_by(Agent.name)
    )
    return {
        "revision": data["revision"],
        "entries": data["entries"],
        "agents": [
            {
                "name": a.name,
                "applied": int((a.storage or {}).get("blocklist_revision") or 0)
                >= data["revision"],
                "status": (a.storage or {}).get("blocklist_status"),
            }
            for a in agents
        ],
    }


@router.get("/blocklist")
async def get_blocklist(principal: Admin, db: DbSession) -> dict[str, Any]:
    """Blocked senders (addresses, domains) and whether the agents applied the list."""
    return await _blocklist_out(db)


@router.post("/blocklist")
async def block_sender(
    body: BlockIn, principal: Admin, db: DbSession, request: Request
) -> dict[str, Any]:
    try:
        await blocklist.add(
            db, body.value, by=principal.user.username, reason=body.reason, force=body.force
        )
    except blocklist.BlocklistError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await audit.record(
        db,
        action="blocklist.add",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        target_type="sender",
        target_id=blocklist.normalise(body.value)[0],
        details={"reason": body.reason, "force": body.force},
    )
    return await _blocklist_out(db)


@router.post("/blocklist/remove")
async def unblock_sender(
    body: UnblockIn, principal: Admin, db: DbSession, request: Request
) -> dict[str, Any]:
    try:
        await blocklist.remove(db, body.value)
    except blocklist.BlocklistError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    await audit.record(
        db,
        action="blocklist.remove",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        target_type="sender",
        target_id=blocklist.normalise(body.value)[0],
    )
    return await _blocklist_out(db)


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


class DeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The mailbox address typed again: a deletion is never one click away.
    confirm: str = Field(max_length=320)
    reason: str | None = Field(default=None, max_length=500)


@router.post("/accounts/{account_id}/delete", response_model=ActionOut)
async def delete_mailbox(
    account_id: uuid.UUID, body: DeleteIn, principal: Admin, db: DbSession, request: Request
) -> ActionOut:
    """Delete the mailbox from Carbonio (the agent applies it within a minute). Refused
    without a successful backup: the account stays restorable from it."""
    account = await db.get(Account, account_id)
    if account is None or account.status is AccountStatus.DELETED:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if body.confirm.strip().lower() != account.email.lower():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "type the mailbox address to confirm"
        )
    backup = await db.scalar(
        select(Snapshot)
        .where(
            Snapshot.account_id == account.id,
            Snapshot.status.in_([SnapshotStatus.COMPLETE, SnapshotStatus.PARTIAL]),
        )
        .order_by(Snapshot.seq.desc())
        .limit(1)
    )
    if backup is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "no backup of this mailbox: back it up first, it could not be restored",
        )
    running = await db.scalar(
        select(Job.id).where(
            Job.account_id == account.id, Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])
        )
    )
    if running is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "a job of this mailbox is queued or running: wait for it"
        )
    queued = await mail_traffic.queue_action(
        db,
        account.id,
        "delete",
        body.reason or f"by {principal.user.username}",
        user_id=principal.user.id,
    )
    await audit.record(
        db,
        action="account.delete_request",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        target_type="account",
        target_id=account.email,
        domain_id=account.domain_id,
        details={
            "reason": body.reason,
            "last_backup": backup.taken_at.isoformat(),
            "backup_seq": backup.seq,
        },
    )
    return ActionOut(id=queued.id if queued else None, action="delete", queued=queued is not None)


class QuotaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_ids: list[uuid.UUID] = Field(min_length=1, max_length=2000)
    quota_bytes: int = Field(ge=0, le=2**50)  # 0: unlimited
    reason: str | None = Field(default=None, max_length=500)


class QuotaOut(BaseModel):
    queued: int
    unchanged: int  # already at that quota, or the same change already pending
    skipped_deleted: int
    not_found: int


@router.post("/accounts/quota", response_model=QuotaOut)
async def set_quota(body: QuotaIn, principal: Admin, db: DbSession, request: Request) -> QuotaOut:
    """Set the mail quota of one or several accounts (the agent applies it within a minute)."""
    ids = list(dict.fromkeys(body.account_ids))
    accounts = list(await db.scalars(select(Account).where(Account.id.in_(ids))))
    out = QuotaOut(queued=0, unchanged=0, skipped_deleted=0, not_found=len(ids) - len(accounts))
    reason = body.reason or f"by {principal.user.username}"
    for account in accounts:
        if account.status == AccountStatus.DELETED:
            out.skipped_deleted += 1
            continue
        if (account.quota_bytes or 0) == body.quota_bytes:
            out.unchanged += 1
            continue
        queued = await mail_traffic.queue_action(
            db, account.id, "quota", reason, user_id=principal.user.id, value=body.quota_bytes
        )
        if queued is None:
            out.unchanged += 1
            continue
        out.queued += 1
        await audit.record(
            db,
            action="account.quota_request",
            outcome=Outcome.SUCCESS,
            actor_id=str(principal.user.id),
            actor_name=principal.user.username,
            source_ip=client_ip(request),
            target_type="account",
            target_id=account.email,
            domain_id=account.domain_id,
            details={
                "quota_bytes": body.quota_bytes,
                "previous_bytes": account.quota_bytes,
                "reason": body.reason,
            },
        )
    return out


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
