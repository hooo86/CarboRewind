"""``/api/domains``: Carbonio inventory, filtered by the caller's domain scope."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select

from ..authz import Permission
from ..db.models import Account, AccountStatus, Domain
from .deps import DbSession, Principal, deny, require
from .schemas import MAX_PAGE, Page

router = APIRouter(prefix="/api/domains", tags=["inventory"])

Reader = Annotated[Principal, Depends(require(Permission.INVENTORY_READ))]


class DomainOut(BaseModel):
    id: uuid.UUID
    name: str
    account_count: int


class AccountOut(BaseModel):
    id: uuid.UUID
    email: str
    status: AccountStatus
    last_seen_at: datetime | None


@router.get("", response_model=Page[DomainOut])
async def list_domains(
    principal: Reader,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[DomainOut]:
    """Domains visible to the caller (all for global roles)."""
    scope = principal.access.domains(Permission.INVENTORY_READ)
    accounts = (
        select(Account.domain_id, func.count().label("n"))
        .where(Account.status != AccountStatus.DELETED)
        .group_by(Account.domain_id)
        .subquery()
    )
    query = select(Domain, func.coalesce(accounts.c.n, 0)).outerjoin(
        accounts, accounts.c.domain_id == Domain.id
    )
    count = select(func.count()).select_from(Domain)
    if scope is not None:
        query = query.where(Domain.id.in_(scope))
        count = count.where(Domain.id.in_(scope))
    rows = await db.execute(query.order_by(Domain.name).limit(limit).offset(offset))
    return Page(
        items=[DomainOut(id=d.id, name=d.name, account_count=n) for d, n in rows],
        total=await db.scalar(count) or 0,
        limit=limit,
        offset=offset,
    )


@router.get("/{domain_id}/accounts", response_model=Page[AccountOut])
async def list_accounts(
    domain_id: uuid.UUID,
    principal: Reader,
    db: DbSession,
    request: Request,
    include_deleted: bool = False,
    q: Annotated[str | None, Query(max_length=320, description="Address substring")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[AccountOut]:
    """Accounts of one domain. 403 outside the caller's scope, 404 if the domain is unknown."""
    if not principal.access.allows(Permission.INVENTORY_READ, domain_id):
        raise await deny(db, principal, request, Permission.INVENTORY_READ, domain=str(domain_id))
    if await db.get(Domain, domain_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "domain not found")
    where = [Account.domain_id == domain_id]
    if not include_deleted:
        where.append(Account.status != AccountStatus.DELETED)
    if q:
        # Bound parameter; % and _ are escaped so the input is a literal substring.
        literal = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append(func.lower(Account.email).like(f"%{literal}%", escape="\\"))
    total = await db.scalar(select(func.count()).select_from(Account).where(*where))
    accounts = await db.scalars(
        select(Account).where(*where).order_by(Account.email).limit(limit).offset(offset)
    )
    return Page(
        items=[
            AccountOut(id=a.id, email=a.email, status=a.status, last_seen_at=a.last_seen_at)
            for a in accounts
        ],
        total=total or 0,
        limit=limit,
        offset=offset,
    )
