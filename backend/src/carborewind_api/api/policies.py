"""``/api/policies``: scheduled backup policies (schedule, backup type, retention).

Scope: a global policy needs ``policies:manage`` globally; a domain or account policy needs
it on that domain. Reading follows ``backups:read`` the same way (global policies are visible
to every reader, since they apply to their accounts too).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .. import audit
from ..authz import Permission
from ..db.models import Account, BackupType, Domain, Outcome, Policy, TargetType
from ..scheduling import Cron, CronError, Retention, zone
from .deps import DbSession, Principal, client_ip, deny, require

router = APIRouter(prefix="/api/policies", tags=["policies"])

Reader = Annotated[Principal, Depends(require(Permission.BACKUPS_READ))]
Manager = Annotated[Principal, Depends(require(Permission.POLICIES_MANAGE))]


class RetentionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    last: int = Field(default=0, ge=0, le=10_000)
    daily: int = Field(default=0, ge=0, le=10_000)
    weekly: int = Field(default=0, ge=0, le=10_000)
    monthly: int = Field(default=0, ge=0, le=10_000)


class PolicyFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    backup_type: BackupType = BackupType.INCREMENTAL
    schedule: str = Field(max_length=128, description="5-field cron expression")
    timezone: str = Field(default="UTC", max_length=64, description="IANA time zone")
    retention: RetentionIn = Field(default_factory=RetentionIn)
    priority: int = Field(default=100, ge=0, le=1000)
    enabled: bool = True

    @field_validator("schedule")
    @classmethod
    def _cron(cls, value: str) -> str:
        try:
            Cron.parse(value)
        except CronError as exc:
            raise ValueError(str(exc)) from exc
        return " ".join(value.split())

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        try:
            zone(value)
        except CronError as exc:
            raise ValueError(str(exc)) from exc
        return value


class PolicyIn(PolicyFields):
    target_type: Literal["global", "domain", "account"]
    target_id: uuid.UUID | None = None


class PolicyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    backup_type: BackupType | None = None
    schedule: str | None = Field(default=None, max_length=128)
    timezone: str | None = Field(default=None, max_length=64)
    retention: RetentionIn | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)
    enabled: bool | None = None

    @field_validator("schedule")
    @classmethod
    def _cron(cls, value: str | None) -> str | None:
        return None if value is None else PolicyFields._cron(value)

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str | None) -> str | None:
        return None if value is None else PolicyFields._zone(value)


class PolicyOut(BaseModel):
    id: uuid.UUID
    name: str
    target_type: TargetType
    target_id: uuid.UUID | None
    target_name: str | None
    backup_type: BackupType
    schedule: str
    timezone: str
    retention: RetentionIn
    priority: int
    enabled: bool
    last_run_at: datetime | None
    next_run_at: datetime | None
    created_at: datetime


async def _target_domain(
    db: DbSession, target_type: str, target_id: uuid.UUID | None
) -> tuple[uuid.UUID | None, str | None]:
    """(domain the target belongs to, display name); raises 422 for a bad target."""
    if target_type == "global":
        if target_id is not None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "global policy has no target")
        return None, None
    if target_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "target_id is required")
    if target_type == "domain":
        domain = await db.get(Domain, target_id)
        return (domain.id, domain.name) if domain else (uuid.uuid4(), None)
    account = await db.get(Account, target_id)
    return (account.domain_id, account.email) if account else (uuid.uuid4(), None)


async def _out(db: DbSession, p: Policy) -> PolicyOut:
    _, name = await _target_domain(db, p.target_type.value, p.target_id)
    now = datetime.now(UTC)
    nxt = None
    if p.enabled:
        nxt = Cron.parse(p.schedule).next_slot(max(now, p.last_run_at or now), zone(p.timezone))
    return PolicyOut(
        id=p.id,
        name=p.name,
        target_type=p.target_type,
        target_id=p.target_id,
        target_name=name,
        backup_type=p.backup_type,
        schedule=p.schedule,
        timezone=p.timezone,
        retention=RetentionIn(**Retention.from_json(p.retention).__dict__),
        priority=p.priority,
        enabled=p.enabled,
        last_run_at=p.last_run_at,
        next_run_at=nxt.astimezone(UTC) if nxt else None,
        created_at=p.created_at,
    )


async def _allowed(db: DbSession, principal: Principal, perm: Permission, p: Policy) -> bool:
    domain, _ = await _target_domain(db, p.target_type.value, p.target_id)
    if p.target_type is TargetType.GLOBAL:
        return perm is Permission.BACKUPS_READ or principal.access.allows(perm)
    return principal.access.allows(perm, domain)


@router.get("", response_model=list[PolicyOut])
async def list_policies(principal: Reader, db: DbSession) -> list[PolicyOut]:
    out = []
    for p in await db.scalars(select(Policy).order_by(Policy.name)):
        if await _allowed(db, principal, Permission.BACKUPS_READ, p):
            out.append(await _out(db, p))
    return out


async def _audit(
    db: DbSession, principal: Principal, request: Request, action: str, p: Policy, **details: object
) -> None:
    domain, _ = await _target_domain(db, p.target_type.value, p.target_id)
    await audit.record(
        db,
        action=action,
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        target_type="policy",
        target_id=str(p.id),
        domain_id=domain if p.target_type is not TargetType.GLOBAL else None,
        source_ip=client_ip(request),
        details={"name": p.name, **details},
    )


@router.post("", response_model=PolicyOut, status_code=status.HTTP_201_CREATED)
async def create_policy(
    body: PolicyIn, principal: Manager, db: DbSession, request: Request
) -> PolicyOut:
    domain, name = await _target_domain(db, body.target_type, body.target_id)
    allowed = (
        principal.access.allows(Permission.POLICIES_MANAGE)
        if body.target_type == "global"
        else name is not None and principal.access.allows(Permission.POLICIES_MANAGE, domain)
    )
    if not allowed:
        raise await deny(
            db, principal, request, Permission.POLICIES_MANAGE, target=str(body.target_id)
        )
    policy = Policy(
        name=body.name,
        target_type=TargetType(body.target_type),
        target_id=body.target_id,
        backup_type=body.backup_type,
        schedule=body.schedule,
        timezone=body.timezone,
        retention=body.retention.model_dump(),
        priority=body.priority,
        enabled=body.enabled,
        last_run_at=datetime.now(UTC),  # no catch-up of slots before the policy existed
    )
    db.add(policy)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "a policy with this name exists") from exc
    await db.refresh(policy)
    await _audit(db, principal, request, "policy.create", policy, schedule=policy.schedule)
    return await _out(db, policy)


async def _editable(
    db: DbSession, principal: Principal, request: Request, policy_id: uuid.UUID
) -> Policy:
    policy = await db.get(Policy, policy_id, with_for_update=True)
    if policy is None or not await _allowed(db, principal, Permission.POLICIES_MANAGE, policy):
        raise await deny(db, principal, request, Permission.POLICIES_MANAGE, policy=str(policy_id))
    return policy


@router.patch("/{policy_id}", response_model=PolicyOut)
async def update_policy(
    policy_id: uuid.UUID, body: PolicyPatch, principal: Manager, db: DbSession, request: Request
) -> PolicyOut:
    policy = await _editable(db, principal, request, policy_id)
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    for key, value in changes.items():
        setattr(policy, key, value)
    if "schedule" in changes or "timezone" in changes or changes.get("enabled") is True:
        policy.last_run_at = datetime.now(UTC)  # restart counting from the change
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "a policy with this name exists") from exc
    await _audit(db, principal, request, "policy.update", policy, changes=sorted(changes))
    return await _out(db, policy)


@router.delete("/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(
    policy_id: uuid.UUID, principal: Manager, db: DbSession, request: Request
) -> None:
    policy = await _editable(db, principal, request, policy_id)
    await _audit(db, principal, request, "policy.delete", policy)
    await db.delete(policy)
