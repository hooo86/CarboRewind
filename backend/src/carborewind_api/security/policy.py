"""Security policy chosen in the interface (``app_settings`` row ``security``).

``require_2fa``: ``none``, ``admins`` (super-administrators and domain administrators) or
``all``. A user who must use two-factor authentication but has not enrolled yet can only
reach ``/api/auth/*`` (the enrolment) until done.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AppSetting, Role, User

KEY = "security"
ADMIN_ROLES = {Role.SUPER_ADMIN, Role.DOMAIN_ADMIN}


@dataclass
class SecurityPolicy:
    require_2fa: Literal["none", "admins", "all"] = "none"

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> SecurityPolicy:
        value = (data or {}).get("require_2fa", "none")
        return cls(require_2fa=value if value in ("none", "admins", "all") else "none")

    def requires_2fa(self, user: User) -> bool:
        if self.require_2fa == "all":
            return True
        if self.require_2fa == "admins":
            return any(Role(r.role) in ADMIN_ROLES for r in user.roles)
        return False


async def load(db: AsyncSession) -> SecurityPolicy:
    row = await db.get(AppSetting, KEY)
    return SecurityPolicy.from_json(row.value if row else None)


async def save(db: AsyncSession, policy: SecurityPolicy) -> None:
    value = asdict(policy)
    await db.execute(
        insert(AppSetting)
        .values(key=KEY, value=value, updated_at=func.now())
        .on_conflict_do_update(
            index_elements=["key"], set_={"value": value, "updated_at": func.now()}
        )
    )
