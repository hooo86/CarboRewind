"""``GET /api/accounts``: every account in the caller's scope, with Carbonio details and
backup figures, filterable on any field (Odoo-like), sortable and paged.

Filters are repeated ``f=<field>:<operator>:<value>`` parameters, all combined with AND:

=========  =========================================================================
type       operators
=========  =========================================================================
text       ``contains``, ``not_contains``, ``eq``, ``ne``, ``set``, ``not_set``
number     ``eq``, ``ne``, ``gt``, ``ge``, ``lt``, ``le``, ``set``, ``not_set``
date       ``before``, ``after`` (ISO date), ``older_days``, ``within_days`` (N), ``set``,
           ``not_set``
choice     ``in``, ``not_in`` (comma-separated values)
boolean    ``is`` (``true`` / ``false``)
=========  =========================================================================

Only whitelisted fields can be filtered or sorted on; values are bound parameters.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import (
    Boolean,
    ColumnElement,
    Float,
    Text,
    and_,
    case,
    cast,
    exists,
    false,
    func,
    literal,
    or_,
    select,
    true,
)
from sqlalchemy.orm import aliased

from ..authz import Permission
from ..db.models import (
    Account,
    AccountStatus,
    BackupType,
    Domain,
    Policy,
    PolicyAccount,
    Snapshot,
    SnapshotStatus,
    TargetType,
)
from .accounts import latest_snapshots
from .deps import DbSession, Principal, require
from .schemas import MAX_PAGE, Page

router = APIRouter(tags=["accounts"])

Reader = Annotated[Principal, Depends(require(Permission.INVENTORY_READ))]

FieldType = Literal["text", "number", "date", "choice", "boolean"]


@dataclass(frozen=True)
class Field:
    type: FieldType
    expr: Callable[[Any], Any]  # column expression; receives the snapshot entity


def _covering_policies() -> Any:
    """Enabled policies whose scope includes ``Account`` (correlated)."""
    return (
        select(Policy.name)
        .select_from(Policy)
        .correlate(Account)
        .where(
            Policy.enabled.is_(True),
            or_(
                Policy.target_type == TargetType.GLOBAL,
                and_(
                    Policy.target_type == TargetType.DOMAIN, Policy.target_id == Account.domain_id
                ),
                and_(Policy.target_type == TargetType.ACCOUNT, Policy.target_id == Account.id),
                and_(
                    Policy.target_type == TargetType.ACCOUNTS,
                    exists().where(
                        PolicyAccount.policy_id == Policy.id, PolicyAccount.account_id == Account.id
                    ),
                ),
            ),
        )
    )


def _usage_pct() -> ColumnElement[Any]:
    return case(
        (
            and_(Account.quota_bytes > 0, Account.used_bytes.is_not(None)),
            cast(Account.used_bytes, Float) * 100.0 / cast(Account.quota_bytes, Float),
        ),
        else_=None,
    )


def _snapshot_count() -> ColumnElement[Any]:
    snap = aliased(Snapshot)
    return (
        select(func.count())
        .select_from(snap)
        .where(snap.account_id == Account.id, snap.status != SnapshotStatus.EXPIRED)
        .correlate(Account)
        .scalar_subquery()
    )


FIELDS: dict[str, Field] = {
    "email": Field("text", lambda s: Account.email),
    "display_name": Field("text", lambda s: Account.display_name),
    "first_name": Field("text", lambda s: Account.first_name),
    "last_name": Field("text", lambda s: Account.last_name),
    "aliases": Field("text", lambda s: cast(Account.aliases, Text)),
    "domain": Field("text", lambda s: Domain.name),
    "status": Field("choice", lambda s: Account.status),
    "cos": Field("choice", lambda s: Account.cos),
    "is_admin": Field("boolean", lambda s: Account.is_admin),
    "quota_bytes": Field("number", lambda s: Account.quota_bytes),
    "used_bytes": Field("number", lambda s: Account.used_bytes),
    "usage_pct": Field("number", lambda s: _usage_pct()),
    "last_login_at": Field("date", lambda s: Account.last_login_at),
    "carbonio_created_at": Field("date", lambda s: Account.carbonio_created_at),
    "last_seen_at": Field("date", lambda s: Account.last_seen_at),
    "last_backup_at": Field("date", lambda s: s.taken_at),
    "last_backup_status": Field("choice", lambda s: s.status),
    "last_backup_type": Field("choice", lambda s: s.backup_type),
    "snapshot_count": Field("number", lambda s: _snapshot_count()),
    "stored_bytes": Field("number", lambda s: s.size_bytes),
    "policy": Field(
        "text",
        lambda s: func.coalesce(
            _covering_policies()
            .with_only_columns(func.string_agg(Policy.name, literal(", ")))
            .scalar_subquery(),
            "",
        ),
    ),
    "has_policy": Field("boolean", lambda s: exists(_covering_policies())),
}

OPERATORS: dict[FieldType, set[str]] = {
    "text": {"contains", "not_contains", "eq", "ne", "set", "not_set"},
    "number": {"eq", "ne", "gt", "ge", "lt", "le", "set", "not_set"},
    "date": {"before", "after", "older_days", "within_days", "set", "not_set"},
    "choice": {"in", "not_in", "set", "not_set"},
    "boolean": {"is"},
}


def _like_literal(value: str) -> str:
    escaped = value.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _bad(message: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, message)


def parse_filter(raw: str, snap: Any, now: datetime) -> ColumnElement[bool]:
    """``field:operator[:value]`` -> SQL condition (raises 422 on anything unknown)."""
    name, _, rest = raw.partition(":")
    op, _, value = rest.partition(":")
    field = FIELDS.get(name)
    if field is None:
        raise _bad(f"unknown filter field {name!r}")
    if op not in OPERATORS[field.type]:
        raise _bad(f"operator {op!r} not allowed on {name}")
    col: ColumnElement[Any] = field.expr(snap)
    if op == "set":
        return col.is_not(None) if field.type != "text" else func.coalesce(col, "") != ""
    if op == "not_set":
        return col.is_(None) if field.type != "text" else func.coalesce(col, "") == ""
    try:
        if field.type == "text":
            if op == "contains":
                return func.lower(func.coalesce(col, "")).like(_like_literal(value), escape="\\")
            if op == "not_contains":
                return ~func.lower(func.coalesce(col, "")).like(_like_literal(value), escape="\\")
            if op == "eq":
                return func.lower(func.coalesce(col, "")) == value.lower()
            return func.lower(func.coalesce(col, "")) != value.lower()
        if field.type == "number":
            number = float(value)
            return {
                "eq": col == number,
                "ne": col != number,
                "gt": col > number,
                "ge": col >= number,
                "lt": col < number,
                "le": col <= number,
            }[op]
        if field.type == "date":
            if op in ("older_days", "within_days"):
                limit = now - timedelta(days=float(value))
                return col < limit if op == "older_days" else col >= limit
            day = datetime.combine(date.fromisoformat(value[:10]), datetime.min.time(), UTC)
            return col < day if op == "before" else col >= day + timedelta(days=1)
        if field.type == "choice":
            values = [v for v in value.split(",") if v]
            if not values:
                raise _bad(f"no value for {name}")
            matches = col.in_(values)
            return matches if op == "in" else or_(~matches, col.is_(None))
        flag = {"true": true(), "false": false()}.get(value.lower())
        if flag is None:
            raise _bad(f"{name} expects true or false")
        return cast(col, Boolean) == flag
    except ValueError as exc:
        raise _bad(f"invalid value for {name}: {value!r}") from exc


class AccountListItem(BaseModel):
    id: uuid.UUID
    email: str
    domain_id: uuid.UUID
    domain: str
    status: AccountStatus
    display_name: str | None
    first_name: str | None
    last_name: str | None
    aliases: list[str]
    cos: str | None
    quota_bytes: int | None
    used_bytes: int | None
    usage_pct: float | None
    is_admin: bool
    last_login_at: datetime | None
    carbonio_created_at: datetime | None
    last_seen_at: datetime | None
    last_backup_at: datetime | None
    last_backup_status: SnapshotStatus | None
    last_backup_type: BackupType | None
    snapshot_count: int
    stored_bytes: int | None
    policies: list[str]


@router.get("/api/accounts", response_model=Page[AccountListItem])
async def list_all_accounts(
    principal: Reader,
    db: DbSession,
    request: Request,
    q: Annotated[
        str | None, Query(max_length=320, description="Search address, name or alias")
    ] = None,
    f: Annotated[
        list[str] | None, Query(max_length=50, description="Filters: field:operator:value")
    ] = None,
    sort: str = "email",
    order: Literal["asc", "desc"] = "asc",
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[AccountListItem]:
    f = f or []
    now = datetime.now(UTC)
    latest = latest_snapshots().subquery()
    snap = Snapshot
    where: list[ColumnElement[bool]] = []
    scope = principal.access.domains(Permission.INVENTORY_READ)
    if scope is not None:
        where.append(Account.domain_id.in_(scope))
    if not any(item.startswith("status:") for item in f):
        where.append(Account.status != AccountStatus.DELETED)  # unless asked for explicitly
    if q:
        pattern = _like_literal(q)
        where.append(
            or_(
                func.lower(Account.email).like(pattern, escape="\\"),
                func.lower(func.coalesce(Account.display_name, "")).like(pattern, escape="\\"),
                func.lower(cast(Account.aliases, Text)).like(pattern, escape="\\"),
            )
        )
    where.extend(parse_filter(item, snap, now) for item in f)
    if sort not in FIELDS:
        raise _bad(f"cannot sort on {sort!r}")
    sort_col = FIELDS[sort].expr(snap)
    ordering = sort_col.desc().nulls_last() if order == "desc" else sort_col.asc().nulls_last()

    base = (
        select(
            Account,
            Domain.name,
            Snapshot,
            _usage_pct().label("usage_pct"),
            _snapshot_count().label("snapshot_count"),
            _covering_policies()
            .with_only_columns(func.array_agg(Policy.name))
            .scalar_subquery()
            .label("policies"),
        )
        .join(Domain, Domain.id == Account.domain_id)
        .outerjoin(latest, latest.c.account_id == Account.id)
        .outerjoin(Snapshot, and_(Snapshot.account_id == Account.id, Snapshot.seq == latest.c.seq))
        .where(*where)
    )
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    rows = await db.execute(base.order_by(ordering, Account.email).limit(limit).offset(offset))
    items = []
    for account, domain_name, s, usage, count, policies in rows:
        items.append(
            AccountListItem(
                id=account.id,
                email=account.email,
                domain_id=account.domain_id,
                domain=domain_name,
                status=account.status,
                display_name=account.display_name,
                first_name=account.first_name,
                last_name=account.last_name,
                aliases=list(account.aliases or []),
                cos=account.cos,
                quota_bytes=account.quota_bytes,
                used_bytes=account.used_bytes,
                usage_pct=round(usage, 1) if usage is not None else None,
                is_admin=account.is_admin,
                last_login_at=account.last_login_at,
                carbonio_created_at=account.carbonio_created_at,
                last_seen_at=account.last_seen_at,
                last_backup_at=s.taken_at if s else None,
                last_backup_status=s.status if s else None,
                last_backup_type=s.backup_type if s else None,
                snapshot_count=int(count or 0),
                stored_bytes=s.size_bytes if s else None,
                policies=sorted(p for p in (policies or []) if p),
            )
        )
    return Page(items=items, total=total or 0, limit=limit, offset=offset)


class Facets(BaseModel):
    domains: list[dict[str, Any]]
    cos: list[str]
    fields: dict[str, FieldType]


@router.get("/api/accounts/facets", response_model=Facets)
async def account_facets(principal: Reader, db: DbSession) -> Facets:
    """Values for filter dropdowns, within the caller's scope."""
    scope = principal.access.domains(Permission.INVENTORY_READ)
    domains = select(Domain.id, Domain.name).order_by(Domain.name)
    cos = select(Account.cos).where(Account.cos.is_not(None)).distinct().order_by(Account.cos)
    if scope is not None:
        domains = domains.where(Domain.id.in_(scope))
        cos = cos.where(Account.domain_id.in_(scope))
    return Facets(
        domains=[{"id": str(i), "name": n} for i, n in await db.execute(domains)],
        cos=[c for c in await db.scalars(cos) if c],
        fields={name: field.type for name, field in FIELDS.items()},
    )
