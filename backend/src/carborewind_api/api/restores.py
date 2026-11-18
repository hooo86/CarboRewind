"""Browse a snapshot's catalogue and request restores.

* ``GET /api/accounts/{id}/snapshots/{sid}/folders``: folder tree as it was at that snapshot,
  with item counts (``backups:read``: metadata only).
* ``GET /api/accounts/{id}/snapshots/{sid}/items``: items at that snapshot, filtered by folder
  or text (``content:preview``: subjects and senders are mailbox content).
* ``POST /api/restores``: queue a restore (or a dry run) for the agent (``restore:run`` on
  both the source and the target domain).

"As it was at snapshot ``seq``" means catalogue rows with ``from_seq <= seq`` and
(``to_seq`` null or ``> seq``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import ColumnElement, and_, func, or_, select

from .. import audit
from ..authz import Permission
from ..db.models import (
    Account,
    AccountStatus,
    FolderVersion,
    ItemKind,
    ItemVersion,
    Job,
    JobKind,
    JobStatus,
    Outcome,
    Snapshot,
    SnapshotStatus,
)
from .backups import JobOut
from .deps import DbSession, Principal, client_ip, deny, require
from .schemas import MAX_PAGE, Page

router = APIRouter(tags=["restores"])

Reader = Annotated[Principal, Depends(require(Permission.BACKUPS_READ))]
Previewer = Annotated[Principal, Depends(require(Permission.CONTENT_PREVIEW))]
Restorer = Annotated[Principal, Depends(require(Permission.RESTORE_RUN))]

MAX_ITEMS = 10_000
MAX_FOLDERS = 1_000


def _valid_at(model: type[ItemVersion] | type[FolderVersion], seq: int) -> ColumnElement[bool]:
    return and_(model.from_seq <= seq, or_(model.to_seq.is_(None), model.to_seq > seq))


async def _snapshot(
    db: DbSession,
    principal: Principal,
    request: Request,
    perm: Permission,
    account_id: uuid.UUID,
    snapshot_id: uuid.UUID,
) -> tuple[Account, Snapshot]:
    account = await db.get(Account, account_id)
    snapshot = await db.get(Snapshot, snapshot_id)
    if (
        account is None
        or snapshot is None
        or snapshot.account_id != account.id
        or not principal.access.allows(perm, account.domain_id)
    ):
        raise await deny(
            db, principal, request, perm, account=str(account_id), snapshot=str(snapshot_id)
        )
    if snapshot.status is SnapshotStatus.EXPIRED:
        raise HTTPException(status.HTTP_410_GONE, "snapshot expired by retention")
    return account, snapshot


class FolderOut(BaseModel):
    id: int
    parent_id: int | None
    path: str
    view: ItemKind
    item_count: int


@router.get(
    "/api/accounts/{account_id}/snapshots/{snapshot_id}/folders",
    response_model=list[FolderOut],
)
async def snapshot_folders(
    account_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    principal: Reader,
    db: DbSession,
    request: Request,
) -> list[FolderOut]:
    account, snap = await _snapshot(
        db, principal, request, Permission.BACKUPS_READ, account_id, snapshot_id
    )
    rows = await db.execute(
        select(ItemVersion.carbonio_folder_id, func.count())
        .where(ItemVersion.account_id == account.id, _valid_at(ItemVersion, snap.seq))
        .group_by(ItemVersion.carbonio_folder_id)
    )
    counts: dict[int, int] = {folder: count for folder, count in rows}
    folders = await db.scalars(
        select(FolderVersion)
        .where(FolderVersion.account_id == account.id, _valid_at(FolderVersion, snap.seq))
        .order_by(FolderVersion.path)
    )
    return [
        FolderOut(
            id=f.carbonio_folder_id,
            parent_id=f.parent_folder_id,
            path=f.path,
            view=f.view,
            item_count=counts.get(f.carbonio_folder_id, 0),
        )
        for f in folders
    ]


class ItemOut(BaseModel):
    id: int
    folder_id: int
    kind: ItemKind
    subject: str | None
    sender: str | None
    date: datetime | None
    size: int


@router.get(
    "/api/accounts/{account_id}/snapshots/{snapshot_id}/items",
    response_model=Page[ItemOut],
)
async def snapshot_items(
    account_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    principal: Previewer,
    db: DbSession,
    request: Request,
    folder_id: int | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[ItemOut]:
    """Items as they were at that snapshot, newest first. ``q`` searches subject and sender."""
    account, snap = await _snapshot(
        db, principal, request, Permission.CONTENT_PREVIEW, account_id, snapshot_id
    )
    query = select(ItemVersion).where(
        ItemVersion.account_id == account.id, _valid_at(ItemVersion, snap.seq)
    )
    if folder_id is not None:
        query = query.where(ItemVersion.carbonio_folder_id == folder_id)
    if q:
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.where(
            or_(ItemVersion.subject.ilike(pattern), ItemVersion.sender.ilike(pattern))
        )
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = await db.scalars(
        query.order_by(ItemVersion.item_date.desc().nulls_last(), ItemVersion.carbonio_item_id)
        .limit(limit)
        .offset(offset)
    )
    return Page(
        items=[
            ItemOut(
                id=i.carbonio_item_id,
                folder_id=i.carbonio_folder_id,
                kind=i.kind,
                subject=i.subject,
                sender=i.sender,
                date=i.item_date,
                size=i.size,
            )
            for i in rows
        ],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: uuid.UUID = Field(description="Account the snapshot belongs to")
    snapshot_id: uuid.UUID
    target_account_id: uuid.UUID | None = Field(
        default=None, description="Account to restore into (default: the source account)"
    )
    mode: Literal["new_folder", "original"] = Field(
        default="new_folder",
        description="new_folder: into a new dated folder (never touches existing data); "
        "original: back into the original folders, skipping items still present",
    )
    dry_run: bool = Field(default=False, description="Compute the plan without writing")
    folder_ids: list[int] = Field(default_factory=list, max_length=MAX_FOLDERS)
    item_ids: list[int] = Field(default_factory=list, max_length=MAX_ITEMS)

    @model_validator(mode="after")
    def _one_selection(self) -> RestoreRequest:
        if self.folder_ids and self.item_ids:
            raise ValueError("select either folders or items, not both")
        return self


@router.post("/api/restores", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def start_restore(
    body: RestoreRequest, principal: Restorer, db: DbSession, request: Request
) -> JobOut:
    source, snap = await _snapshot(
        db, principal, request, Permission.RESTORE_RUN, body.account_id, body.snapshot_id
    )
    target = source
    if body.target_account_id is not None and body.target_account_id != source.id:
        found = await db.get(Account, body.target_account_id)
        if found is None or not principal.access.allows(Permission.RESTORE_RUN, found.domain_id):
            raise await deny(
                db,
                principal,
                request,
                Permission.RESTORE_RUN,
                account=str(body.target_account_id),
            )
        target = found
    if target.status is AccountStatus.DELETED:
        raise HTTPException(status.HTTP_409_CONFLICT, "target account is deleted")
    if body.mode == "original" and target.id != source.id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "mode 'original' restores into the source account only",
        )
    label = f"CarboRewind {snap.taken_at:%Y-%m-%d %H.%M} UTC #{snap.seq}"
    job = Job(
        kind=JobKind.RESTORE,
        status=JobStatus.QUEUED,
        account_id=target.id,
        created_by=principal.user.id,
        priority=40,  # a user is waiting: before on-demand backups
        max_attempts=1 if body.dry_run else 3,
        params={
            "source_account_id": str(source.id),
            "source_carbonio_id": source.carbonio_id,
            "source_email": source.email,
            "snapshot_id": str(snap.id),
            "seq": snap.seq,
            "mode": body.mode,
            "dry_run": body.dry_run,
            "label": label,
            "folder_ids": sorted(set(body.folder_ids)),
            "item_ids": sorted(set(body.item_ids)),
        },
    )
    db.add(job)
    await db.flush()
    await db.refresh(job)
    await audit.record(
        db,
        action="restore.request",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        target_type="account",
        target_id=target.email,
        domain_id=target.domain_id,
        source_ip=client_ip(request),
        details={
            "job": str(job.id),
            "source": source.email,
            "seq": snap.seq,
            "mode": body.mode,
            "dry_run": body.dry_run,
            "folders": len(body.folder_ids),
            "items": len(body.item_ids),
        },
    )
    return JobOut.of(job, target.email)
