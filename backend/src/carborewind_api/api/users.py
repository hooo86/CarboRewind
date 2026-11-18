"""``/api/users``: administration of CarboRewind users and role assignments (super-admin)."""

from __future__ import annotations

import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from .. import audit
from ..auth.service import normalise_username
from ..auth.sessions import revoke_all_sessions
from ..authz import Permission
from ..db.models import AuthSource, Domain, Outcome, Role, RoleAssignment, User
from ..security.passwords import WeakPasswordError, hash_password
from .deps import DbSession, Principal, client_ip, require
from .schemas import MAX_PAGE, Page, UserOut

router = APIRouter(prefix="/api/users", tags=["users"])

Admin = Annotated[Principal, Depends(require(Permission.USERS_MANAGE))]

USERNAME_PATTERN = r"^[a-z0-9][a-z0-9._@+-]{0,254}$"


class UserCreate(BaseModel):
    """Create a local account, or pre-register an LDAP account (no password)."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=255)
    auth_source: AuthSource = AuthSource.LOCAL
    password: str | None = Field(default=None, max_length=1024, repr=False)
    display_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)

    @model_validator(mode="after")
    def _check(self) -> UserCreate:
        self.username = normalise_username(self.username)
        if not re.fullmatch(USERNAME_PATTERN, self.username):
            raise ValueError("invalid username")
        if self.auth_source is AuthSource.OIDC:
            raise ValueError("OIDC accounts are not supported yet")
        if (self.auth_source is AuthSource.LOCAL) != (self.password is not None):
            raise ValueError("a password is required for local accounts, and only for them")
        return self


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_active: bool | None = None
    display_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    unlock: bool = Field(default=False, description="Clear the failed-login lock")


class RoleGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role
    domain_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _check(self) -> RoleGrant:
        if self.role is Role.SUPER_ADMIN and self.domain_id is not None:
            raise ValueError("super_admin is always global")
        return self


async def _get_user(db: DbSession, user_id: uuid.UUID) -> User:
    user = await db.scalar(
        select(User).options(selectinload(User.roles)).where(User.id == user_id).with_for_update()
    )
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    return user


async def _active_super_admins(db: DbSession) -> int:
    count = await db.scalar(
        select(func.count(func.distinct(User.id)))
        .join(RoleAssignment, RoleAssignment.user_id == User.id)
        .where(User.is_active.is_(True), RoleAssignment.role == Role.SUPER_ADMIN)
    )
    return int(count or 0)


async def _audit(
    db: DbSession, admin: Principal, request: Request, action: str, user: User, **details: object
) -> None:
    await audit.record(
        db,
        action=action,
        outcome=Outcome.SUCCESS,
        actor_id=str(admin.user.id),
        actor_name=admin.user.username,
        target_type="user",
        target_id=str(user.id),
        source_ip=client_ip(request),
        details={"username": user.username, **details},
    )


@router.get("", response_model=Page[UserOut])
async def list_users(
    admin: Admin,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[UserOut]:
    total = await db.scalar(select(func.count()).select_from(User))
    users = await db.scalars(
        select(User)
        .options(selectinload(User.roles))
        .order_by(User.username)
        .limit(limit)
        .offset(offset)
    )
    return Page(items=[UserOut.of(u) for u in users], total=total or 0, limit=limit, offset=offset)


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(body: UserCreate, admin: Admin, db: DbSession, request: Request) -> UserOut:
    try:
        password_hash = hash_password(body.password) if body.password is not None else None
    except WeakPasswordError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    user = User(
        username=body.username,
        auth_source=body.auth_source,
        password_hash=password_hash,
        display_name=body.display_name,
        email=body.email,
        roles=[],
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "username already exists") from exc
    await _audit(db, admin, request, "user.create", user, auth_source=body.auth_source.value)
    return UserOut.of(user)


@router.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: uuid.UUID, admin: Admin, db: DbSession) -> UserOut:
    return UserOut.of(await _get_user(db, user_id))


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID, body: UserUpdate, admin: Admin, db: DbSession, request: Request
) -> UserOut:
    user = await _get_user(db, user_id)
    changes: dict[str, object] = {}
    if body.is_active is not None and body.is_active != user.is_active:
        if not body.is_active:
            if user.id == admin.user.id:
                raise HTTPException(status.HTTP_409_CONFLICT, "cannot deactivate yourself")
            is_super = any(r.role is Role.SUPER_ADMIN for r in user.roles)
            if is_super and await _active_super_admins(db) <= 1:
                raise HTTPException(status.HTTP_409_CONFLICT, "last active super_admin")
            changes["sessions_revoked"] = await revoke_all_sessions(db, user.id)
        user.is_active = body.is_active
        changes["is_active"] = body.is_active
    for field in ("display_name", "email"):
        value = getattr(body, field)
        if value is not None and value != getattr(user, field):
            setattr(user, field, value)
            changes[field] = value
    if body.unlock and (user.locked_until or user.failed_logins):
        user.locked_until = None
        user.failed_logins = 0
        changes["unlocked"] = True
    if changes:
        await _audit(db, admin, request, "user.update", user, changes=changes)
    return UserOut.of(user)


@router.post("/{user_id}/roles", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def grant_role(
    user_id: uuid.UUID, body: RoleGrant, admin: Admin, db: DbSession, request: Request
) -> UserOut:
    user = await _get_user(db, user_id)
    if body.domain_id is not None and await db.get(Domain, body.domain_id) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown domain")
    if any(r.role is body.role and r.domain_id == body.domain_id for r in user.roles):
        raise HTTPException(status.HTTP_409_CONFLICT, "role already granted")
    user.roles.append(RoleAssignment(role=body.role, domain_id=body.domain_id))
    await db.flush()
    await _audit(
        db,
        admin,
        request,
        "user.role.grant",
        user,
        role=body.role.value,
        domain_id=str(body.domain_id) if body.domain_id else None,
    )
    return UserOut.of(user)


@router.delete("/{user_id}/roles/{assignment_id}", response_model=UserOut)
async def revoke_role(
    user_id: uuid.UUID, assignment_id: uuid.UUID, admin: Admin, db: DbSession, request: Request
) -> UserOut:
    user = await _get_user(db, user_id)
    assignment = next((r for r in user.roles if r.id == assignment_id), None)
    if assignment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "role assignment not found")
    if (
        assignment.role is Role.SUPER_ADMIN
        and user.is_active
        and sum(r.role is Role.SUPER_ADMIN for r in user.roles) == 1
        and await _active_super_admins(db) <= 1
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "last active super_admin")
    user.roles.remove(assignment)
    await db.flush()
    # Sessions carry the old permissions only through the DB lookup, but revoking forces a
    # fresh login so the change is unambiguous for the user.
    revoked = await revoke_all_sessions(db, user.id)
    await _audit(
        db,
        admin,
        request,
        "user.role.revoke",
        user,
        role=assignment.role.value,
        domain_id=str(assignment.domain_id) if assignment.domain_id else None,
        sessions_revoked=revoked,
    )
    return UserOut.of(user)
