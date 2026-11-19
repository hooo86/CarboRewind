"""Response/request models shared by several routers."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel

from ..db.models import AuthSource, Role, User

T = TypeVar("T")

MAX_PAGE = 200


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class RoleOut(BaseModel):
    id: uuid.UUID
    role: Role
    domain_id: uuid.UUID | None


class UserOut(BaseModel):
    id: uuid.UUID
    username: str
    display_name: str | None
    email: str | None
    auth_source: AuthSource
    is_active: bool
    locked_until: datetime | None
    last_login_at: datetime | None
    roles: list[RoleOut]
    totp_enabled: bool = False
    failed_logins: int = 0
    created_at: datetime | None = None

    @classmethod
    def of(cls, user: User) -> UserOut:
        return cls(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            email=user.email,
            auth_source=user.auth_source,
            is_active=user.is_active,
            locked_until=user.locked_until,
            last_login_at=user.last_login_at,
            roles=[RoleOut(id=r.id, role=r.role, domain_id=r.domain_id) for r in user.roles],
            totp_enabled=bool(user.totp_enabled),
            failed_logins=user.failed_logins or 0,
            created_at=user.created_at,
        )
