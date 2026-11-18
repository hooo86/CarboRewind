import itertools
import uuid

from carborewind_api.authz import GLOBAL_ONLY, MATRIX, Access, Permission
from carborewind_api.db.models import Role, RoleAssignment

D1, D2 = uuid.uuid4(), uuid.uuid4()


def _access(*grants: tuple[Role, uuid.UUID | None]) -> Access:
    return Access.from_assignments(RoleAssignment(role=r, domain_id=d) for r, d in grants)


def test_super_admin_has_everything_everywhere() -> None:
    access = _access((Role.SUPER_ADMIN, None))
    assert all(access.allows(p) for p in Permission)
    assert access.domains(Permission.RESTORE_RUN) is None


def test_domain_admin_is_limited_to_its_domain() -> None:
    access = _access((Role.DOMAIN_ADMIN, D1))
    assert access.allows(Permission.RESTORE_RUN, D1)
    assert not access.allows(Permission.RESTORE_RUN, D2)
    assert not access.allows(Permission.RESTORE_RUN)  # no domain given -> needs global
    assert access.domains(Permission.RESTORE_RUN) == {D1}
    assert access.allows_any(Permission.RESTORE_RUN)


def test_domain_scope_never_grants_global_only_permissions() -> None:
    # Even a (forbidden by DB constraint) domain-scoped super_admin gets no global-only perm.
    access = _access((Role.SUPER_ADMIN, D1))
    for perm in GLOBAL_ONLY:
        assert not access.allows_any(perm)


def test_viewer_cannot_see_content_nor_act() -> None:
    access = _access((Role.VIEWER, None))
    assert access.allows(Permission.BACKUPS_READ, D1)
    for perm in (Permission.CONTENT_PREVIEW, Permission.RESTORE_RUN, Permission.BACKUPS_RUN):
        assert not access.allows_any(perm)


def test_grants_accumulate() -> None:
    access = _access((Role.VIEWER, None), (Role.RESTORE_OPERATOR, D2))
    assert access.allows(Permission.INVENTORY_READ, D1)
    assert access.allows(Permission.RESTORE_RUN, D2)
    assert not access.allows(Permission.RESTORE_RUN, D1)
    assert access.domains(Permission.CONTENT_PREVIEW) == {D2}
    assert access.domains(Permission.AUDIT_READ) == set()


def test_matrix_is_monotonic() -> None:
    order = [Role.VIEWER, Role.RESTORE_OPERATOR, Role.DOMAIN_ADMIN, Role.SUPER_ADMIN]
    for lower, higher in itertools.pairwise(order):
        assert MATRIX[lower] <= MATRIX[higher], (lower, higher)
