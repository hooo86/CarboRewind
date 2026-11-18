"""Role-based access control with per-domain scope.

A user holds role assignments, each global (``domain_id`` NULL) or limited to one domain.
:class:`Access` folds them into, for every permission, either "all domains" or a set of
domain ids. Endpoints then either check one domain (:meth:`Access.allows`) or filter their
queries (:meth:`Access.domains`). The matrix below is the single source of truth and is
rendered in ``docs/rbac.md`` (kept in sync by a test).
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

from .db.models import Role, RoleAssignment


class Permission(enum.StrEnum):
    INVENTORY_READ = "inventory:read"
    BACKUPS_READ = "backups:read"
    CONTENT_PREVIEW = "content:preview"
    BACKUPS_RUN = "backups:run"
    RESTORE_RUN = "restore:run"
    POLICIES_MANAGE = "policies:manage"
    AUDIT_READ = "audit:read"
    USERS_MANAGE = "users:manage"
    AGENTS_MANAGE = "agents:manage"
    SETTINGS_MANAGE = "settings:manage"
    MIGRATIONS_MANAGE = "migrations:manage"


# Permissions that only make sense globally: a domain-scoped grant never confers them.
GLOBAL_ONLY = frozenset(
    {
        Permission.USERS_MANAGE,
        Permission.AGENTS_MANAGE,
        Permission.SETTINGS_MANAGE,
        # Source credentials and writes into any mailbox: global administrators only.
        Permission.MIGRATIONS_MANAGE,
    }
)

MATRIX: dict[Role, frozenset[Permission]] = {
    Role.SUPER_ADMIN: frozenset(Permission),
    Role.DOMAIN_ADMIN: frozenset(
        {
            Permission.INVENTORY_READ,
            Permission.BACKUPS_READ,
            Permission.CONTENT_PREVIEW,
            Permission.BACKUPS_RUN,
            Permission.RESTORE_RUN,
            Permission.POLICIES_MANAGE,
            Permission.AUDIT_READ,
        }
    ),
    Role.RESTORE_OPERATOR: frozenset(
        {
            Permission.INVENTORY_READ,
            Permission.BACKUPS_READ,
            Permission.CONTENT_PREVIEW,
            Permission.RESTORE_RUN,
        }
    ),
    # Metadata only: no mail content, no action.
    Role.VIEWER: frozenset({Permission.INVENTORY_READ, Permission.BACKUPS_READ}),
}


@dataclass
class Access:
    """Effective permissions of one user."""

    global_perms: set[Permission] = field(default_factory=set)
    domain_perms: dict[Permission, set[uuid.UUID]] = field(default_factory=dict)

    @classmethod
    def from_assignments(cls, assignments: Iterable[RoleAssignment]) -> Access:
        access = cls()
        for a in assignments:
            for perm in MATRIX[Role(a.role)]:
                if a.domain_id is None:
                    access.global_perms.add(perm)
                elif perm not in GLOBAL_ONLY:
                    access.domain_perms.setdefault(perm, set()).add(a.domain_id)
        return access

    def allows(self, perm: Permission, domain_id: uuid.UUID | None = None) -> bool:
        """True if granted globally, or on ``domain_id`` when one is given."""
        if perm in self.global_perms:
            return True
        return domain_id is not None and domain_id in self.domain_perms.get(perm, set())

    def allows_any(self, perm: Permission) -> bool:
        return perm in self.global_perms or bool(self.domain_perms.get(perm))

    def domains(self, perm: Permission) -> set[uuid.UUID] | None:
        """Domains on which ``perm`` applies: None means all domains."""
        if perm in self.global_perms:
            return None
        return set(self.domain_perms.get(perm, set()))
