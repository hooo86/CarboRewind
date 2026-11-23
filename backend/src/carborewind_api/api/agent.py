"""``/api/agent``: endpoints called by agents (enrolment, inventory, job protocol).

Authentication: mutual TLS terminated by the reverse proxy. The proxy verifies the client
certificate against the CarboRewind CA and forwards it (URL-encoded PEM) together with the
verification status; the API accepts these headers **only from** ``agent_proxy_ips`` and maps
the certificate fingerprint to an active agent. Enrolment itself uses a one-time token.
"""

from __future__ import annotations

import base64
import json
import secrets
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from .. import (
    agent_tuning,
    archiving,
    audit,
    mail_queue,
    mail_traffic,
    monitoring,
    restore_test,
    standby,
    storage_settings,
)
from ..db.models import (
    Account,
    AccountAction,
    AccountStatus,
    ActorType,
    Agent,
    AgentStatus,
    BackupType,
    Blob,
    Domain,
    FetchedContent,
    FolderVersion,
    IpAction,
    ItemKind,
    ItemVersion,
    Job,
    JobKind,
    JobStatus,
    MigrationMailbox,
    Outcome,
    Snapshot,
    SnapshotStatus,
)
from ..migration_jobs import MigrationSpecError, job_spec, notify_batch_done, record_result
from ..notifications import SecretKeyError, notify
from ..pki import CertificateAuthority, PkiError, fingerprint_pem, token_digest
from ..scheduler import orphan_blobs_query, purge_plan
from .deps import AppSettings, DbSession, client_ip

router = APIRouter(prefix="/api/agent", tags=["agent protocol"])

# Profile fields an agent may send (see carborewind_agent.worker.PROFILE_ATTRS).
PROFILE_FIELDS = (
    "title",
    "company",
    "department",
    "phone",
    "mobile",
    "home_phone",
    "street",
    "postal_code",
    "city",
    "state",
    "country",
    "description",
    "notes",
    "employee_number",
)


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
    # Details (optional: older agents send only the three fields above).
    display_name: str | None = Field(default=None, max_length=255)
    first_name: str | None = Field(default=None, max_length=255)
    last_name: str | None = Field(default=None, max_length=255)
    aliases: list[str] = Field(default_factory=list, max_length=1000)
    cos: str | None = Field(default=None, max_length=255)
    quota_bytes: int | None = Field(default=None, ge=0)
    used_bytes: int | None = Field(default=None, ge=0)
    is_admin: bool = False
    last_login_at: datetime | None = None
    carbonio_created_at: datetime | None = None
    profile: dict[str, str] | None = None

    @field_validator("profile")
    @classmethod
    def _profile(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        return {k: v[:1000] for k, v in value.items() if k in PROFILE_FIELDS and v}

    def details(self) -> dict[str, Any]:
        out = self.model_dump(
            include={
                "display_name",
                "first_name",
                "last_name",
                "cos",
                "quota_bytes",
                "is_admin",
                "last_login_at",
                "carbonio_created_at",
            }
        )
        out["aliases"] = [a.lower() for a in self.aliases]
        if "used_bytes" in self.model_fields_set:  # absent = usage unknown, keep the last value
            out["used_bytes"] = self.used_bytes
        if self.profile is not None:  # older agents do not send it
            out["profile"] = self.profile
        return out


class PlaceIn(BaseModel):
    path: str = Field(max_length=4096)
    kind: Literal["local", "network"]
    fstype: str | None = Field(default=None, max_length=64)
    source: str | None = Field(default=None, max_length=1024)
    total_bytes: int | None = Field(default=None, ge=0)
    free_bytes: int | None = Field(default=None, ge=0)
    error: str | None = Field(default=None, max_length=200)


class ApplyIn(BaseModel):
    """Outcome of the last storage settings applied by the root helper."""

    revision: int | None = None
    ok: bool | None = None
    error: str | None = Field(default=None, max_length=500)
    at: str | None = Field(default=None, max_length=64)
    changed: list[str] | None = Field(default=None, max_length=10)


class StorageIn(PlaceIn):
    replica: PlaceIn | None = None  # copy 02
    apply: ApplyIn | None = None
    mail_store: PlaceIn | None = None  # Carbonio's data disk (/opt/zextras)


class InventoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accounts: list[AccountIn] = Field(max_length=100_000)
    # True when the list is the whole server: accounts missing from it are marked deleted.
    complete: bool = False
    storage: StorageIn | None = None


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
    if body.storage is not None:
        agent.storage = {
            **(agent.storage or {}),
            **body.storage.model_dump(),
            "reported_at": now.isoformat(),
        }
        await monitoring.record_storage(db, agent, body.storage.model_dump())
    if agent.role == standby.STANDBY:
        # The clone has the same accounts (same ids): only their number is kept.
        agent.storage = {
            **(agent.storage or {}),
            "standby_accounts": len(body.accounts),
            "standby_inventory_at": now.isoformat(),
        }
        return InventoryOut(
            domains_created=0, accounts_created=0, accounts_updated=0, accounts_deleted=0
        )
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
                    **a.details(),
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
            for key, value in a.details().items():
                setattr(row, key, value)
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


# ------------------------------------------------------------------ storage settings


@router.get(
    "/storage",
    response_model=None,
    responses={204: {"description": "Never configured from the interface"}},
)
async def storage_config(
    agent: CurrentAgent, db: DbSession, settings: AppSettings
) -> dict[str, Any] | Response:
    """Storage settings with the share passwords decrypted (mutual TLS only)."""
    cfg = await storage_settings.load(db)
    if cfg.revision == 0:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    try:
        return cfg.for_agent(settings)
    except SecretKeyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get(
    "/config",
    response_model=None,
    responses={204: {"description": "Never set in the interface: the agent keeps agent.env"}},
)
async def agent_config(agent: CurrentAgent, db: DbSession) -> dict[str, Any] | Response:
    """Tuning chosen in Settings → Agent; the agent applies it at once."""
    cfg = await agent_tuning.load(db)
    if cfg.revision == 0:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    seen = (agent.storage or {}).get("tuning") or {}
    if seen.get("revision") != cfg.revision:
        agent.storage = {
            **(agent.storage or {}),
            "tuning": {"revision": cfg.revision, "at": datetime.now(UTC).isoformat()},
        }
    return asdict(cfg)


@router.post("/storage-status", status_code=status.HTTP_204_NO_CONTENT)
async def storage_status(body: StorageIn, agent: CurrentAgent, db: DbSession) -> None:
    """Where the repository and copy 02 are now, and how the last settings were applied."""
    previous = (agent.storage or {}).get("apply") or {}
    agent.storage = {
        **(agent.storage or {}),
        **body.model_dump(),
        "reported_at": datetime.now(UTC).isoformat(),
    }
    await monitoring.record_storage(db, agent, body.model_dump())
    if body.apply and body.apply.revision != previous.get("revision"):
        await _audit(
            db,
            agent,
            "storage.apply",
            Outcome.SUCCESS if body.apply.ok else Outcome.FAILURE,
            details=body.apply.model_dump(),
        )
        if body.apply.ok is False:
            await notify(
                db,
                "job_failed",
                kind="storage",
                account=agent.name,
                error=body.apply.error or "?",
                job=f"storage settings #{body.apply.revision}",
            )


# ------------------------------------------------------------------ mail traffic


class MailHourIn(BaseModel):
    hour: datetime
    sent: dict[str, list[int]] = Field(default_factory=dict, max_length=100_000)
    spam_in: dict[str, int] = Field(default_factory=dict, max_length=100_000)
    spam_out: dict[str, int] = Field(default_factory=dict, max_length=100_000)
    spam_senders: dict[str, dict[str, Any]] = Field(default_factory=dict, max_length=1000)

    @field_validator("sent")
    @classmethod
    def _pairs(cls, value: dict[str, list[int]]) -> dict[str, list[int]]:
        if any(len(v) != 2 or min(v) < 0 for v in value.values()):
            raise ValueError("sent counters are [messages, recipients]")
        return {k.lower()[:320]: v for k, v in value.items()}


class LoginCountsIn(BaseModel):
    failed_mailbox: int = Field(default=0, ge=0)
    failed_smtp: int = Field(default=0, ge=0)
    suspended: int = Field(default=0, ge=0)
    last_suspended: datetime | None = None
    last_failed: datetime | None = None
    accounts: list[str] = Field(default_factory=list, max_length=10)


class LoginHourIn(BaseModel):
    hour: datetime
    ips: dict[str, LoginCountsIn] = Field(default_factory=dict, max_length=5000)


class MailboxLogIn(BaseModel):
    path: str = Field(max_length=4096)
    exists: bool
    readable: bool


class LoginFilterIn(BaseModel):
    """Carbonio's invalid login filter settings and trusted addresses (global config)."""

    safe_ips: list[str] = Field(default_factory=list, max_length=1000)
    max_failed: int | None = None
    block_minutes: int | None = None
    task_minutes: int | None = None


class MailStatsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: list[MailHourIn] = Field(default_factory=list, max_length=72)
    log: str | None = Field(default=None, max_length=4096)
    # Failed logins and IPs blocked by Carbonio (agents from this version on).
    security: list[LoginHourIn] = Field(default_factory=list, max_length=72)
    mailbox_log: MailboxLogIn | None = None
    login_filter: LoginFilterIn | None = None


class ServiceIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=255)
    ok: bool | None
    source: Literal["carbonio", "port", "mta", "guard"]
    port: int | None = Field(default=None, ge=1, le=65535)
    checked: int | None = None
    detail: str | None = Field(default=None, max_length=300)


class QueueItemIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=40)
    sender: str = Field(default="", max_length=320, alias="from")
    to: str = Field(default="", max_length=1000)
    time: int | None = None
    reason: str = Field(default="", max_length=500)
    origip: str = Field(default="", max_length=64)


class QueueReasonIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str = Field(default="", max_length=300)
    count: int = Field(default=0, ge=0)


class QueueDetailIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    server: str = Field(min_length=1, max_length=255)
    queue: str = Field(min_length=1, max_length=20)
    total: int = Field(default=0, ge=0)
    items: list[QueueItemIn] = Field(default_factory=list, max_length=100)
    reasons: list[QueueReasonIn] = Field(default_factory=list, max_length=20)


class ServicesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checked_at: datetime | None = None
    services: list[ServiceIn] = Field(default_factory=list, max_length=500)
    queue: dict[str, int] | None = Field(default=None, max_length=20)
    queue_items: list[QueueDetailIn] | None = Field(default=None, max_length=10)
    # Automatic start of Carbonio and sending test (agents from this version on).
    autostart: dict[str, Any] | None = None
    mail_test: dict[str, Any] | None = None
    certificate: dict[str, Any] | None = None
    errors: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("autostart", "mail_test", "certificate")
    @classmethod
    def _small(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and len(json.dumps(value, default=str)) > 20_000:
            raise ValueError("report too large")
        return value


@router.post("/services")
async def services(body: ServicesIn, agent: CurrentAgent, db: DbSession) -> dict[str, Any]:
    """State of the Carbonio services and of the mail queue, every minute; the answer
    lists the queue actions asked in the interface."""
    now = datetime.now(UTC)
    report = body.model_dump(mode="json", by_alias=True)
    report["errors"] = [e[:300] for e in body.errors]
    await monitoring.record_services(db, agent, report, now)
    # Lock the row: an administrator may be adding a queue action at the same moment.
    await db.refresh(agent, attribute_names=["storage"], with_for_update=True)
    answer: dict[str, Any] = {"queue_actions": mail_queue.deliver(agent, now)}
    renew = (agent.storage or {}).get("cert_renew_request")
    if renew and not renew.get("sent_at"):
        answer["cert_renew"] = {"id": renew["id"]}
        agent.storage = {
            **(agent.storage or {}),
            "cert_renew_request": {**renew, "sent_at": now.isoformat()},
        }
    test = (agent.storage or {}).get("mail_test_request")
    if test and not test.get("sent_at"):
        answer["mail_test"] = {"id": test["id"]}
        agent.storage = {
            **(agent.storage or {}),
            "mail_test_request": {**test, "sent_at": now.isoformat()},
        }
    return answer


@router.post("/mail-stats")
async def mail_stats(body: MailStatsIn, agent: CurrentAgent, db: DbSession) -> dict[str, Any]:
    """Hourly mail counters from the MTA log; answers with the account actions to apply."""
    now = datetime.now(UTC)
    if agent.role == standby.STANDBY:
        return {"actions": [], "ip_actions": []}  # the clone carries no user traffic
    hours = [h.model_dump(mode="json") for h in body.hours]
    senders = await mail_traffic.record(db, hours)
    if body.security:
        await mail_traffic.record_logins(db, [h.model_dump(mode="json") for h in body.security])
    await mail_traffic.evaluate(db, senders, now)
    previous = ((agent.storage or {}).get("mail") or {}).get("login_filter")
    login_filter = (
        {**body.login_filter.model_dump(), "at": now.isoformat()}
        if body.login_filter
        else previous  # read every few minutes only: keep the last one
    )
    agent.storage = {
        **(agent.storage or {}),
        "mail": {
            "log": body.log,
            "reported_at": now.isoformat(),
            **({"mailbox_log": body.mailbox_log.model_dump()} if body.mailbox_log else {}),
            **({"login_filter": login_filter} if login_filter else {}),
        },
    }
    return {
        "actions": await mail_traffic.pending_actions(db),
        "ip_actions": await mail_traffic.pending_ip_actions(db),
    }


class ActionDoneIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: str | None = Field(default=None, max_length=500)


@router.post("/queue-actions/{action_id}/done", status_code=status.HTTP_204_NO_CONTENT)
async def queue_action_done(
    action_id: str, body: ActionDoneIn, agent: CurrentAgent, db: DbSession
) -> None:
    """The agent applied the queue action (or could not)."""
    await mail_queue.done(db, agent, action_id, body.error, datetime.now(UTC))


@router.post("/ip-actions/{action_id}/done", status_code=status.HTTP_204_NO_CONTENT)
async def ip_action_done(
    action_id: uuid.UUID, body: ActionDoneIn, agent: CurrentAgent, db: DbSession
) -> None:
    """The agent added / removed the trusted address (or could not)."""
    action = await db.get(IpAction, action_id, with_for_update=True)
    if action is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "action not found")
    if action.status != "pending":
        return  # idempotent
    action.done_at = datetime.now(UTC)
    action.status, action.error = ("failed", body.error) if body.error else ("done", None)
    # Mirror the change at once so the interface shows it before the next report.
    mail = dict((agent.storage or {}).get("mail") or {})
    lf = dict(mail.get("login_filter") or {})
    if not body.error and lf:
        ips = [ip for ip in lf.get("safe_ips", []) if ip != action.ip]
        lf["safe_ips"] = [*ips, action.ip] if action.action == "trust" else ips
        agent.storage = {**(agent.storage or {}), "mail": {**mail, "login_filter": lf}}


@router.post("/account-actions/{action_id}/done", status_code=status.HTTP_204_NO_CONTENT)
async def account_action_done(
    action_id: uuid.UUID, body: ActionDoneIn, agent: CurrentAgent, db: DbSession
) -> None:
    """The agent locked/unlocked the account (or could not)."""
    action = await db.get(AccountAction, action_id, with_for_update=True)
    if action is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "action not found")
    if action.status != "pending":
        return  # idempotent
    account = await db.get(Account, action.account_id)
    action.done_at = datetime.now(UTC)
    action.status, action.error = ("failed", body.error) if body.error else ("done", None)
    if account is not None and not body.error:
        if action.action == "quota":
            account.quota_bytes = action.value or None  # 0: unlimited
        else:
            account.status = {
                "lock": AccountStatus.LOCKED,
                "unlock": AccountStatus.ACTIVE,
                "delete": AccountStatus.DELETED,  # still restorable from its backups
            }[action.action]
    if body.error:
        await notify(
            db,
            "job_failed",
            kind=f"account {action.action}",
            account=account.email if account else "?",
            error=body.error,
            job=str(action.id),
        )
    await _audit(
        db,
        agent,
        f"account.{action.action}",
        Outcome.FAILURE if body.error else Outcome.SUCCESS,
        target_type="account",
        target_id=account.email if account else None,
        domain_id=account.domain_id if account else None,
        details={
            "reason": action.reason,
            "error": body.error,
            "action": str(action.id),
            **({"quota_bytes": action.value} if action.action == "quota" else {}),
        },
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
    # migrate jobs: IMAP source (with its credentials), options and incremental state.
    migration: dict[str, Any] | None = None


@router.post(
    "/jobs/claim",
    response_model=ClaimedJob,
    responses={204: {"description": "Nothing to do"}},
)
async def claim(agent: CurrentAgent, db: DbSession, settings: AppSettings) -> ClaimedJob | Response:
    """Lease the next job: first an abandoned one (expired lease, resumed from its
    checkpoint), else the highest-priority queued job."""
    now = datetime.now(UTC)
    # The standby agent replays snapshots into its Carbonio and does nothing else; the
    # others never take those jobs.
    for_role = (
        Job.kind == JobKind.STANDBY_SYNC
        if agent.role == standby.STANDBY
        else Job.kind != JobKind.STANDBY_SYNC
    )
    job = await db.scalar(
        select(Job)
        .where(
            for_role,
            or_(
                Job.status == JobStatus.QUEUED,
                and_(Job.status == JobStatus.RUNNING, Job.lease_expires_at < now),
            ),
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
        await _notify_failure(db, job)
        await notify_batch_done(db, job)
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
    migration = None
    if job.kind is JobKind.MIGRATE:
        try:
            migration = await job_spec(db, settings, job)
        except MigrationSpecError as exc:
            job.status, job.finished_at, job.lease_expires_at = JobStatus.FAILED, now, None
            job.error = str(exc)
            await _notify_failure(db, job)
            await notify_batch_done(db, job)
            await db.flush()
            await db.commit()
            return Response(status_code=status.HTTP_204_NO_CONTENT)
    if job.kind is JobKind.ARCHIVE and account is not None and params.get("mode") == "copy":
        # Sent, not stored: messages already copied are not read again.
        params["skip_ids"] = await archiving.skip_ids(db, account.id)
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
        migration=migration,
    )


async def _own_running_job(db: DbSession, agent: Agent, job_id: uuid.UUID) -> Job:
    job = await db.get(Job, job_id, with_for_update=True)
    if job is None or job.agent_id != agent.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    if job.status is not JobStatus.RUNNING:
        raise HTTPException(status.HTTP_409_CONFLICT, f"job is {job.status.value}")
    return job


def _json_no_nul(value: Any) -> Any:
    """JSONB rejects \\u0000: drop it from every string (subjects saved in checkpoints)."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {_json_no_nul(k): _json_no_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_no_nul(v) for v in value]
    return value


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
        job.checkpoint = _json_no_nul(body.checkpoint)
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

    @field_validator("subject", "sender")
    @classmethod
    def _strip_nul(cls, value: str | None) -> str | None:
        return _no_nul(value)


class FolderIn(BaseModel):
    id: int
    parent: int | None
    path: str = Field(max_length=4096)
    view: str = Field(max_length=32)

    @field_validator("path")
    @classmethod
    def _strip_nul(cls, value: str) -> str:
        return value.replace("\x00", "")


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


# Rows per bulk statement (3 parameters each for blobs: well under PostgreSQL's 65 535).
_BATCH = 5000


def _no_nul(value: str | None) -> str | None:
    """PostgreSQL text cannot hold NUL characters, which some messages carry in headers."""
    return value.replace("\x00", "") if value else value


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
    # In batches: PostgreSQL takes at most 65 535 parameters per statement, and a full
    # backup of a large mailbox sends tens of thousands of items at once.
    rows = [{"id": k, "size": v, "stored_size": v} for k, v in blobs.items()]
    for start in range(0, len(rows), _BATCH):
        await db.execute(
            insert(Blob)
            .values(rows[start : start + _BATCH])
            .on_conflict_do_nothing(index_elements=["id"])
        )
    # Close superseded / deleted versions, open the new ones.
    closing = sorted(set(body.removed) | set(body.added))
    for start in range(0, len(closing), _BATCH):
        await db.execute(
            update(ItemVersion)
            .where(
                ItemVersion.account_id == account.id,
                ItemVersion.to_seq.is_(None),
                ItemVersion.carbonio_item_id.in_(closing[start : start + _BATCH]),
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
    if body.errors:
        await notify(
            db,
            "backup_partial",
            account=account.email,
            seq=seq,
            errors=len(body.errors),
        )
    job.status, job.finished_at, job.lease_expires_at = JobStatus.SUCCEEDED, datetime.now(UTC), None
    job.progress = {
        "items": body.item_count,
        "errors": len(body.errors),
        # Read from Carbonio by this run (changed items), for the throughput.
        "read_items": len(body.added),
        "read_bytes": sum(i.size for i in body.added.values()),
        "new_bytes": body.new_bytes,
        "size_bytes": body.size_bytes,
    }
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
    cleaned: bool = False  # restore test: the test folder was deleted afterwards


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
    if job.params.get("test"):
        await restore_test.record_result(db, job, summary)
    elif body.failed and not body.dry_run:
        await notify(
            db,
            "restore_failed",
            account=account.email if account else "?",
            failed=len(body.failed),
            restored=body.restored,
        )
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
    archives_deleted: list[str] = Field(default_factory=list, max_length=100_000)
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
    planned_archives = {a["archive"] for a in job.params.get("archives", [])}
    await archiving.mark_archives_purged(
        db, [a for a in body.archives_deleted if a in planned_archives]
    )
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


class MigrationFolderIn(BaseModel):
    source: str = Field(max_length=1024)
    target: str = Field(max_length=4096)
    total: int = Field(default=0, ge=0)
    new: int = Field(default=0, ge=0)
    copied: int = Field(default=0, ge=0)
    duplicates: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    error: str | None = Field(default=None, max_length=300)


class MigrationResultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool
    cancelled: bool = False
    copied: int = Field(ge=0)
    duplicates: int = Field(default=0, ge=0)
    bytes: int = Field(default=0, ge=0)
    failed: dict[str, str] = Field(default_factory=dict, max_length=500)
    failed_count: int = Field(default=0, ge=0)
    folders: list[MigrationFolderIn] = Field(default_factory=list, max_length=20_000)
    folders_created: list[str] = Field(default_factory=list, max_length=500)
    state: dict[str, Any] = Field(default_factory=dict)


@router.post("/jobs/{job_id}/migrate-complete", status_code=status.HTTP_204_NO_CONTENT)
async def migrate_complete(
    job_id: uuid.UUID, body: MigrationResultIn, agent: CurrentAgent, db: DbSession
) -> None:
    """Record a mailbox copy: its incremental state first, so that the next run (even after
    a cancellation) starts where this one stopped."""
    job = await db.get(Job, job_id, with_for_update=True)
    if job is None or job.agent_id != agent.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    if job.kind is not JobKind.MIGRATE:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "not a migration job")
    if job.status is JobStatus.SUCCEEDED:
        return  # idempotent
    if job.status not in (JobStatus.RUNNING, JobStatus.CANCELLED):
        raise HTTPException(status.HTTP_409_CONFLICT, f"job is {job.status.value}")
    now = datetime.now(UTC)
    mailbox = await db.get(MigrationMailbox, uuid.UUID(str(job.params.get("mailbox_id"))))
    summary = body.model_dump(mode="json", exclude={"state"})
    summary["folders"] = summary["folders"][:300]
    if mailbox is not None:
        record_result(mailbox, body.model_dump(mode="json"), now)
    job.progress = summary
    job.error = f"{body.failed_count} message(s) not copied" if body.failed_count else None
    if job.status is JobStatus.RUNNING:
        job.status, job.finished_at, job.lease_expires_at = JobStatus.SUCCEEDED, now, None
    account = await db.get(Account, job.account_id) if job.account_id else None
    await _audit(
        db,
        agent,
        "migration.dry_run" if body.dry_run else "migration.complete",
        Outcome.FAILURE if body.failed_count else Outcome.SUCCESS,
        target_type="account",
        target_id=account.email if account else None,
        domain_id=account.domain_id if account else None,
        details={
            "job": str(job.id),
            "source": job.params.get("source_email"),
            "copied": body.copied,
            "duplicates": body.duplicates,
            "failed": body.failed_count,
            "bytes": body.bytes,
            "cancelled": body.cancelled,
        },
    )
    await notify_batch_done(db, job)


class ReplicaResultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: int = Field(ge=0)
    copied: int = Field(ge=0)
    copied_bytes: int = Field(ge=0)
    deleted: int = Field(ge=0)
    errors: list[str] = Field(default_factory=list, max_length=50)
    error_count: int = Field(default=0, ge=0)


@router.post("/jobs/{job_id}/replicate-complete", status_code=status.HTTP_204_NO_CONTENT)
async def replicate_complete(
    job_id: uuid.UUID, body: ReplicaResultIn, agent: CurrentAgent, db: DbSession
) -> None:
    """Record a refresh of copy 02."""
    job = await db.get(Job, job_id, with_for_update=True)
    if job is not None and job.status is JobStatus.SUCCEEDED and job.agent_id == agent.id:
        return  # idempotent
    job = await _own_running_job(db, agent, job_id)
    if job.kind is not JobKind.REPLICATE:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "not a replicate job")
    job.progress = body.model_dump()
    job.error = f"{body.error_count} file(s) not copied" if body.error_count else None
    job.status, job.finished_at, job.lease_expires_at = JobStatus.SUCCEEDED, datetime.now(UTC), None
    if body.error_count:
        await notify(
            db,
            "job_failed",
            kind="replicate",
            account=agent.name,
            error=job.error,
            job=str(job.id),
        )
    await _audit(
        db,
        agent,
        "replicate.complete",
        Outcome.FAILURE if body.error_count else Outcome.SUCCESS,
        target_type="job",
        target_id=str(job.id),
        details={k: v for k, v in body.model_dump().items() if k != "errors"},
    )


class StandbyResultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: uuid.UUID
    account_created: bool = False
    items: int = Field(default=0, ge=0)
    added: int = Field(default=0, ge=0)
    deleted: int = Field(default=0, ge=0)
    moved: int = Field(default=0, ge=0)
    updated: int = Field(default=0, ge=0)
    reimported_folders: int = Field(default=0, ge=0)
    folders_created: int = Field(default=0, ge=0)
    folders_deleted: int = Field(default=0, ge=0)
    failed: dict[str, str] = Field(default_factory=dict, max_length=200)
    failed_count: int = Field(default=0, ge=0)


@router.post("/jobs/{job_id}/standby-complete", status_code=status.HTTP_204_NO_CONTENT)
async def standby_complete(
    job_id: uuid.UUID, body: StandbyResultIn, agent: CurrentAgent, db: DbSession
) -> None:
    """The standby Carbonio now holds this snapshot of the account."""
    job = await db.get(Job, job_id, with_for_update=True)
    if job is not None and job.status is JobStatus.SUCCEEDED and job.agent_id == agent.id:
        return  # idempotent
    job = await _own_running_job(db, agent, job_id)
    if job.kind is not JobKind.STANDBY_SYNC:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "not a standby job")
    if str(body.snapshot_id) != str(job.params.get("snapshot_id")):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "not the job's snapshot")
    stats = body.model_dump(mode="json", exclude={"snapshot_id"})
    job.progress = {k: v for k, v in stats.items() if k != "failed"}
    job.error = f"{body.failed_count} item(s) not replicated" if body.failed_count else None
    job.status, job.finished_at, job.lease_expires_at = JobStatus.SUCCEEDED, datetime.now(UTC), None
    await standby.record(db, job, stats=stats)
    if body.account_created:
        account = await db.get(Account, job.account_id) if job.account_id else None
        await _audit(
            db,
            agent,
            "standby.account_created",
            Outcome.SUCCESS,
            target_type="account",
            target_id=account.email if account else None,
            domain_id=account.domain_id if account else None,
            details={"job": str(job.id)},
        )


class VerifyResultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: Literal["primary", "replica"]
    sample: float = Field(gt=0, le=1)
    checked: int = Field(ge=0)
    corrupted: list[str] = Field(default_factory=list, max_length=100)
    corrupted_count: int = Field(default=0, ge=0)
    manifests_checked: int = Field(default=0, ge=0)
    manifests_corrupted: list[str] = Field(default_factory=list, max_length=100)


@router.post("/jobs/{job_id}/verify-complete", status_code=status.HTTP_204_NO_CONTENT)
async def verify_complete(
    job_id: uuid.UUID, body: VerifyResultIn, agent: CurrentAgent, db: DbSession
) -> None:
    """Record an integrity check; corruption is notified."""
    job = await db.get(Job, job_id, with_for_update=True)
    if job is not None and job.status is JobStatus.SUCCEEDED and job.agent_id == agent.id:
        return  # idempotent
    job = await _own_running_job(db, agent, job_id)
    if job.kind is not JobKind.VERIFY:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "not a verify job")
    bad = body.corrupted_count + len(body.manifests_corrupted)
    job.progress = body.model_dump()
    job.error = f"{bad} corrupted file(s)" if bad else None
    job.status, job.finished_at, job.lease_expires_at = JobStatus.SUCCEEDED, datetime.now(UTC), None
    if bad:
        await notify(
            db,
            "integrity_failed",
            target=body.target,
            count=bad,
            checked=body.checked + body.manifests_checked,
            job=str(job.id),
        )
    await _audit(
        db,
        agent,
        "integrity.verify",
        Outcome.FAILURE if bad else Outcome.SUCCESS,
        target_type="job",
        target_id=str(job.id),
        details={k: v for k, v in body.model_dump().items() if k not in ("corrupted",)},
    )


class ArchiveEntryIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    item_id: int = Field(ge=0)
    folder: int
    folder_path: str = Field(max_length=4096)
    blob: str = Field(pattern=r"^[0-9a-f]{64}$")
    meta_blob: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    meta_size: int = Field(default=0, ge=0)
    flags: str = Field(default="", max_length=32)
    subject: str | None = Field(default=None, max_length=2000)
    sender: str | None = Field(default=None, max_length=1000)
    recipients: str | None = Field(default=None, max_length=2000)
    date: datetime | None = None
    has_attachments: bool = False


class ArchiveStoredIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[ArchiveEntryIn] = Field(max_length=1000)


async def _own_archive_job(db: DbSession, agent: Agent, job_id: uuid.UUID) -> tuple[Job, Account]:
    job = await _own_running_job(db, agent, job_id)
    if job.kind is not JobKind.ARCHIVE or job.account_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "not an archive job")
    account = await db.get(Account, job.account_id)
    assert account is not None  # noqa: S101 - FK
    return job, account


@router.post("/jobs/{job_id}/archive-stored", status_code=status.HTTP_204_NO_CONTENT)
async def archive_stored(
    job_id: uuid.UUID, body: ArchiveStoredIn, agent: CurrentAgent, db: DbSession
) -> None:
    """Index archived messages; called before any of them leaves the mailbox."""
    job, account = await _own_archive_job(db, agent, job_id)
    if job.params.get("mode") == "simulate":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "a simulation stores nothing")
    await archiving.record_stored(
        db, job, account, [e.model_dump(mode="json") for e in body.entries]
    )


class ArchiveResultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["simulate", "copy", "archive"]
    scanned: int = Field(default=0, ge=0)
    candidates: int = Field(default=0, ge=0)
    candidate_bytes: int = Field(default=0, ge=0)
    truncated: bool = False
    by_folder: dict[str, list[int]] = Field(default_factory=dict, max_length=500)
    archived: int = Field(default=0, ge=0)
    archived_bytes: int = Field(default=0, ge=0)
    new_bytes: int = Field(default=0, ge=0)
    removed: list[int] = Field(default_factory=list, max_length=100_000)
    removed_bytes: int = Field(default=0, ge=0)
    errors: dict[str, str] = Field(default_factory=dict, max_length=1000)
    error_count: int = Field(default=0, ge=0)
    manifest_ref: str | None = Field(default=None, max_length=512)
    replica_checked: bool = False
    cancelled: bool = False


@router.post("/jobs/{job_id}/archive-complete", status_code=status.HTTP_204_NO_CONTENT)
async def archive_complete(
    job_id: uuid.UUID, body: ArchiveResultIn, agent: CurrentAgent, db: DbSession
) -> None:
    """Record what left the mailbox; the last job of a run sends the report."""
    job = await db.get(Job, job_id, with_for_update=True)
    if job is not None and job.status is JobStatus.SUCCEEDED and job.agent_id == agent.id:
        return  # idempotent
    job, account = await _own_archive_job(db, agent, job_id)
    if body.mode != job.params.get("mode"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "mode differs from the job's")
    now = datetime.now(UTC)
    marked = await archiving.mark_removed(db, account, body.removed, now)
    summary = body.model_dump(mode="json", exclude={"removed"})
    summary["removed_count"] = len(body.removed)
    summary["errors"] = dict(list(body.errors.items())[:100])
    job.progress = summary
    job.error = f"{body.error_count} message(s) not archived" if body.error_count else None
    job.status, job.finished_at, job.lease_expires_at = JobStatus.SUCCEEDED, now, None
    await _audit(
        db,
        agent,
        f"archive.{body.mode}",
        Outcome.FAILURE if body.error_count else Outcome.SUCCESS,
        target_type="account",
        target_id=account.email,
        domain_id=account.domain_id,
        details={
            "job": str(job.id),
            "policy": job.params.get("policy"),
            "candidates": body.candidates,
            "archived": body.archived,
            "removed": len(body.removed),
            "indexed_removed": marked,
            "removed_bytes": body.removed_bytes,
            "errors": body.error_count,
            "replica_checked": body.replica_checked,
        },
    )
    await archiving.finish_run(db, job, now)


class FetchedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: str = Field(max_length=(archiving.MAX_FETCH_BYTES * 4) // 3 + 8)


@router.post("/jobs/{job_id}/fetch-complete", status_code=status.HTTP_204_NO_CONTENT)
async def fetch_complete(
    job_id: uuid.UUID,
    body: FetchedIn,
    agent: CurrentAgent,
    db: DbSession,
    settings: AppSettings,
) -> None:
    """Keep an archived message for the interface, encrypted, for one hour."""
    job = await _own_running_job(db, agent, job_id)
    if job.kind is not JobKind.FETCH:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "not a fetch job")
    try:
        raw = base64.b64decode(body.data, validate=True)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid base64") from exc
    try:
        sealed = archiving.seal_content(settings, raw)
    except SecretKeyError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    await db.execute(
        insert(FetchedContent)
        .values(job_id=job.id, data=sealed)
        .on_conflict_do_update(index_elements=["job_id"], set_={"data": sealed})
    )
    job.progress = {"size": len(raw)}
    job.status, job.finished_at, job.lease_expires_at = JobStatus.SUCCEEDED, datetime.now(UTC), None


async def _notify_failure(db: DbSession, job: Job) -> None:
    if job.kind is JobKind.FETCH:
        return  # the user waiting in the interface sees the error
    if job.kind is JobKind.STANDBY_SYNC:
        await standby.record(db, job, error=job.error or "failed")
        return
    if job.kind is JobKind.RESTORE and job.params.get("test"):
        await restore_test.record_result(
            db,
            job,
            {"selected": len(job.params.get("item_ids", [])), "error": job.error or "failed"},
        )
        return
    if job.kind is JobKind.ARCHIVE:
        await archiving.finish_run(db, job, datetime.now(UTC))
    account = await db.get(Account, job.account_id) if job.account_id else None
    await notify(
        db,
        "job_failed",
        kind=job.kind.value,
        account=account.email if account else "-",
        error=job.error or "?",
        job=str(job.id),
    )


class FailureIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: str = Field(max_length=2000)
    retryable: bool = True


@router.post("/jobs/{job_id}/release", status_code=status.HTTP_204_NO_CONTENT)
async def release(job_id: uuid.UUID, agent: CurrentAgent, db: DbSession) -> None:
    """The agent is stopping (update, restart): the job may be resumed at once from its
    last checkpoint, and this interrupted attempt does not count."""
    job = await _own_running_job(db, agent, job_id)
    job.lease_expires_at = datetime.now(UTC)  # reclaimable now, as an abandoned job
    job.attempts = max(0, job.attempts - 1)
    await _audit(
        db,
        agent,
        "job.release",
        Outcome.SUCCESS,
        target_type="job",
        target_id=str(job.id),
        details={"reason": "agent stopping"},
    )


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
        await _notify_failure(db, job)
        await notify_batch_done(db, job)
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
