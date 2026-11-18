"""``/api/audit``: consult, export and verify the audit trail."""

from __future__ import annotations

import csv
import io
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import Select, false, func, or_, select

from .. import audit
from ..authz import Permission
from ..db.models import ActorType, AuditEvent, Outcome
from .deps import DbSession, Principal, client_ip, deny, require
from .schemas import MAX_PAGE, Page

router = APIRouter(prefix="/api/audit", tags=["audit"])

Auditor = Annotated[Principal, Depends(require(Permission.AUDIT_READ))]

CSV_COLUMNS = [
    "id", "ts", "actor_type", "actor_id", "actor_name", "action", "outcome",
    "target_type", "target_id", "domain_id", "source_ip", "details", "prev_hash", "hash",
]  # fmt: skip


class AuditEventOut(BaseModel):
    id: int
    ts: datetime
    actor_type: ActorType
    actor_id: str | None
    actor_name: str | None
    action: str
    outcome: Outcome
    target_type: str | None
    target_id: str | None
    domain_id: uuid.UUID | None
    source_ip: str | None
    details: dict[str, Any]
    prev_hash: str
    hash: str

    @classmethod
    def of(cls, e: AuditEvent) -> AuditEventOut:
        return cls(
            id=e.id,
            ts=e.ts,
            actor_type=e.actor_type,
            actor_id=e.actor_id,
            actor_name=e.actor_name,
            action=e.action,
            outcome=e.outcome,
            target_type=e.target_type,
            target_id=e.target_id,
            domain_id=e.domain_id,
            source_ip=str(e.source_ip) if e.source_ip else None,
            details=e.details,
            prev_hash=e.prev_hash,
            hash=e.hash,
        )


class VerifyOut(BaseModel):
    ok: bool
    checked: int
    head_id: int | None
    head_hash: str | None
    broken_id: int | None
    reason: str | None


class Filters(BaseModel):
    action: str | None = None
    actor: str | None = None
    outcome: Outcome | None = None
    target_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None


def _filters(
    action: Annotated[
        str | None, Query(max_length=128, description="Exact, or prefix ending in *")
    ] = None,
    actor: Annotated[str | None, Query(max_length=255, description="actor_name, exact")] = None,
    outcome: Outcome | None = None,
    target_id: Annotated[str | None, Query(max_length=255)] = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Filters:
    return Filters(
        action=action, actor=actor, outcome=outcome, target_id=target_id, since=since, until=until
    )


QueryFilters = Annotated[Filters, Depends(_filters)]


def _query(principal: Principal, f: Filters) -> Select[AuditEvent]:
    query = select(AuditEvent)
    scope = principal.access.domains(Permission.AUDIT_READ)
    if scope is not None:
        # Domain-scoped auditors see their domains' events and their own actions.
        query = query.where(
            or_(
                AuditEvent.domain_id.in_(scope) if scope else false(),
                AuditEvent.actor_id == str(principal.user.id),
            )
        )
    if f.action:
        if f.action.endswith("*"):
            prefix = f.action[:-1].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            query = query.where(AuditEvent.action.like(prefix + "%", escape="\\"))
        else:
            query = query.where(AuditEvent.action == f.action)
    if f.actor:
        query = query.where(AuditEvent.actor_name == f.actor)
    if f.outcome:
        query = query.where(AuditEvent.outcome == f.outcome)
    if f.target_id:
        query = query.where(AuditEvent.target_id == f.target_id)
    if f.since:
        query = query.where(AuditEvent.ts >= f.since)
    if f.until:
        query = query.where(AuditEvent.ts < f.until)
    return query


@router.get("", response_model=Page[AuditEventOut])
async def list_events(
    principal: Auditor,
    db: DbSession,
    filters: QueryFilters,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[AuditEventOut]:
    """Most recent first. Domain admins only see events of their domains and their own."""
    query = _query(principal, filters)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    events = await db.scalars(query.order_by(AuditEvent.id.desc()).limit(limit).offset(offset))
    return Page(
        items=[AuditEventOut.of(e) for e in events], total=total or 0, limit=limit, offset=offset
    )


def _csv_safe(value: object) -> str:
    """Neutralise spreadsheet formula injection (OWASP CSV injection)."""
    text = "" if value is None else str(value)
    if text and text[0] in "=+-@\t\r":
        return "'" + text
    return text


@router.get(
    "/export.csv",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/csv": {}}, "description": "CSV export (UTF-8)"}},
)
async def export_csv(
    principal: Auditor, db: DbSession, filters: QueryFilters, request: Request
) -> StreamingResponse:
    """Export the filtered events (oldest first) as CSV. The export itself is audited."""
    await audit.record(
        db,
        action="audit.export",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        details={"filters": filters.model_dump(mode="json", exclude_none=True)},
    )
    await db.commit()
    query = _query(principal, filters).order_by(AuditEvent.id)

    async def rows() -> AsyncIterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(CSV_COLUMNS)
        yield buffer.getvalue()
        result = await db.stream_scalars(query.execution_options(yield_per=1000))
        async for event in result:
            out = AuditEventOut.of(event).model_dump(mode="json")
            buffer.seek(0)
            buffer.truncate()
            writer.writerow([_csv_safe(_cell(out[c])) for c in CSV_COLUMNS])
            yield buffer.getvalue()

    return StreamingResponse(
        rows(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="carborewind-audit.csv"'},
    )


def _cell(value: object) -> object:
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


@router.get("/verify", response_model=VerifyOut)
async def verify(principal: Auditor, db: DbSession, request: Request) -> VerifyOut:
    """Recompute the full hash chain (global auditors only)."""
    if not principal.access.allows(Permission.AUDIT_READ):
        raise await deny(db, principal, request, Permission.AUDIT_READ, scope="global")
    result = await audit.verify_chain(db)
    await audit.record(
        db,
        action="audit.verify",
        outcome=Outcome.SUCCESS if result.ok else Outcome.FAILURE,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        source_ip=client_ip(request),
        details={"checked": result.checked, "broken_id": result.broken_id},
    )
    return VerifyOut(**result.__dict__)


@router.get("/head", response_model=VerifyOut)
async def head(principal: Auditor, db: DbSession, request: Request) -> VerifyOut:
    """Latest event id and hash, to anchor outside the database (cheap, no recomputation)."""
    if not principal.access.allows(Permission.AUDIT_READ):
        raise await deny(db, principal, request, Permission.AUDIT_READ, scope="global")
    last = await db.scalar(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(1))
    count = await db.scalar(select(func.count()).select_from(AuditEvent))
    return VerifyOut(
        ok=True,
        checked=count or 0,
        head_id=last.id if last else None,
        head_hash=last.hash if last else None,
        broken_id=None,
        reason=None,
    )
