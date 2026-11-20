"""``/api/views``: saved searches of list pages (filters, grouping, sort), like Odoo's
favourites. A view is personal unless shared; only its owner (or a super admin) deletes it.

A view stores the page's query string only: applying it goes through the list endpoints,
which check the caller's scope as usual, so sharing a view never shares data.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from .. import audit
from ..authz import Permission
from ..db.models import Outcome, SavedView, User
from .deps import CurrentPrincipal, DbSession, client_ip

router = APIRouter(prefix="/api/views", tags=["views"])

ViewPage = Literal["accounts", "jobs"]


class ViewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: ViewPage
    name: str = Field(min_length=1, max_length=100)
    query: str = Field(max_length=4000)
    shared: bool = False

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("a name is required")
        return value

    @field_validator("query")
    @classmethod
    def _query(cls, value: str) -> str:
        return value.removeprefix("?")


class ViewOut(BaseModel):
    id: uuid.UUID
    page: str
    name: str
    query: str
    shared: bool
    owner: str
    mine: bool
    created_at: datetime


@router.get("", response_model=list[ViewOut])
async def list_views(
    principal: CurrentPrincipal, db: DbSession, page: Annotated[ViewPage, Query()]
) -> list[ViewOut]:
    """The caller's views of a page, then the views others shared."""
    rows = await db.execute(
        select(SavedView, User.username)
        .join(User, User.id == SavedView.owner_id)
        .where(
            SavedView.page == page,
            or_(SavedView.owner_id == principal.user.id, SavedView.shared.is_(True)),
        )
        .order_by((SavedView.owner_id != principal.user.id), SavedView.name)
    )
    return [
        ViewOut(
            id=v.id,
            page=v.page,
            name=v.name,
            query=v.query,
            shared=v.shared,
            owner=owner,
            mine=v.owner_id == principal.user.id,
            created_at=v.created_at,
        )
        for v, owner in rows
    ]


@router.post("", response_model=ViewOut, status_code=status.HTTP_201_CREATED)
async def create_view(
    body: ViewIn, principal: CurrentPrincipal, db: DbSession, request: Request
) -> ViewOut:
    view = SavedView(
        owner_id=principal.user.id,
        page=body.page,
        name=body.name,
        query=body.query,
        shared=body.shared,
    )
    try:
        async with db.begin_nested():
            db.add(view)
            await db.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "a view with this name exists") from None
    await db.refresh(view)
    await audit.record(
        db,
        action="view.create",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        target_type="view",
        target_id=str(view.id),
        source_ip=client_ip(request),
        details={"page": view.page, "name": view.name, "shared": view.shared},
    )
    return ViewOut(
        id=view.id,
        page=view.page,
        name=view.name,
        query=view.query,
        shared=view.shared,
        owner=principal.user.username,
        mine=True,
        created_at=view.created_at,
    )


@router.delete("/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_view(
    view_id: uuid.UUID, principal: CurrentPrincipal, db: DbSession, request: Request
) -> None:
    view = await db.get(SavedView, view_id)
    if view is None or not (
        view.owner_id == principal.user.id
        or (view.shared and principal.access.allows(Permission.SETTINGS_MANAGE))
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such view")
    await audit.record(
        db,
        action="view.delete",
        outcome=Outcome.SUCCESS,
        actor_id=str(principal.user.id),
        actor_name=principal.user.username,
        target_type="view",
        target_id=str(view.id),
        source_ip=client_ip(request),
        details={"page": view.page, "name": view.name},
    )
    await db.delete(view)
