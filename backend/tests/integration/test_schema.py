"""Schema, constraints and query patterns on a real PostgreSQL."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from carborewind_api.db.models import (
    Account,
    AccountStatus,
    ActorType,
    AuditEvent,
    AuthSource,
    BackupType,
    Base,
    Blob,
    Domain,
    ItemKind,
    ItemVersion,
    Job,
    JobKind,
    JobStatus,
    Outcome,
    Policy,
    Role,
    RoleAssignment,
    Snapshot,
    SnapshotStatus,
    TargetType,
    User,
)

from .conftest import alembic_config

pytestmark = pytest.mark.integration


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Session inside a transaction rolled back after each test."""
    with engine.connect() as conn:
        trans = conn.begin()
        with Session(bind=conn, join_transaction_mode="create_savepoint") as s:
            yield s
        trans.rollback()


def _account(session: Session, email: str = "alice@example.test") -> Account:
    domain = Domain(name=email.split("@")[1])
    session.add(domain)
    session.flush()
    account = Account(
        domain_id=domain.id,
        email=email,
        carbonio_id=str(uuid.uuid4()),
        status=AccountStatus.ACTIVE,
    )
    session.add(account)
    session.flush()
    return account


def _blob(session: Session, n: int) -> Blob:
    blob = Blob(id=f"{n:064x}", size=100, stored_size=60)
    session.add(blob)
    session.flush()
    return blob


def test_models_match_migrations(engine: Engine) -> None:
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_downgrade_then_upgrade_is_clean(migrated: str) -> None:
    cfg = alembic_config(migrated)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


def test_enum_values_are_checked(session: Session) -> None:
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO users (id, username, auth_source, password_hash) "
                "VALUES (gen_random_uuid(), 'x', 'kerberos', 'h')"
            )
        )


def test_local_user_requires_password(session: Session) -> None:
    session.add(User(username="bob", auth_source=AuthSource.LOCAL))
    with pytest.raises(IntegrityError):
        session.flush()


def test_super_admin_cannot_be_domain_scoped(session: Session) -> None:
    account = _account(session)
    user = User(username="carol", auth_source=AuthSource.LDAP)
    session.add(user)
    session.flush()
    session.add(RoleAssignment(user_id=user.id, role=Role.SUPER_ADMIN, domain_id=account.domain_id))
    with pytest.raises(IntegrityError):
        session.flush()


def test_global_grant_is_not_duplicated(session: Session) -> None:
    user = User(username="dave", auth_source=AuthSource.LDAP)
    session.add(user)
    session.flush()
    session.add(RoleAssignment(user_id=user.id, role=Role.VIEWER))
    session.flush()
    session.add(RoleAssignment(user_id=user.id, role=Role.VIEWER))
    with pytest.raises(IntegrityError):
        session.flush()


def test_email_reusable_after_account_deletion(session: Session) -> None:
    old = _account(session, "eve@example.test")
    old.status = AccountStatus.DELETED
    session.flush()
    session.add(
        Account(
            domain_id=old.domain_id,
            email="eve@example.test",
            carbonio_id=str(uuid.uuid4()),
            status=AccountStatus.ACTIVE,
        )
    )
    session.flush()  # allowed: the first one is deleted
    session.add(
        Account(
            domain_id=old.domain_id,
            email="eve@example.test",
            carbonio_id=str(uuid.uuid4()),
            status=AccountStatus.ACTIVE,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_policy_target_consistency(session: Session) -> None:
    session.add(
        Policy(
            name="bad",
            target_type=TargetType.GLOBAL,
            target_id=uuid.uuid4(),
            backup_type=BackupType.INCREMENTAL,
            schedule="0 1 * * *",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_running_job_requires_lease(session: Session) -> None:
    session.add(Job(kind=JobKind.BACKUP, status=JobStatus.RUNNING))
    with pytest.raises(IntegrityError):
        session.flush()


def test_job_idempotency_key_is_unique(session: Session) -> None:
    session.add(Job(kind=JobKind.BACKUP, idempotency_key="policy:1:2026-11-18T01:00"))
    session.flush()
    session.add(Job(kind=JobKind.BACKUP, idempotency_key="policy:1:2026-11-18T01:00"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_blob_id_must_be_hex_sha256(session: Session) -> None:
    session.add(Blob(id="not-a-hash", size=1, stored_size=1))
    with pytest.raises(IntegrityError):
        session.flush()


def test_point_in_time_listing(session: Session) -> None:
    """Item 1 kept, item 2 deleted at seq 3, item 3 modified at seq 2 (two versions)."""
    account = _account(session)
    for seq in (1, 2, 3):
        session.add(
            Snapshot(
                account_id=account.id,
                seq=seq,
                backup_type=BackupType.FULL if seq == 1 else BackupType.INCREMENTAL,
                status=SnapshotStatus.COMPLETE,
                taken_at=datetime(2026, 11, seq, tzinfo=UTC),
                manifest_ref=f"manifests/{account.id}/{seq}",
            )
        )
    blobs = [_blob(session, n) for n in range(1, 5)]

    def version(item: int, blob: Blob, from_seq: int, to_seq: int | None) -> ItemVersion:
        return ItemVersion(
            account_id=account.id,
            carbonio_item_id=item,
            carbonio_folder_id=2,
            kind=ItemKind.MESSAGE,
            content_blob_id=blob.id,
            subject=f"Facture n°{item}",
            sender="billing@example.test",
            size=100,
            from_seq=from_seq,
            to_seq=to_seq,
        )

    session.add_all(
        [
            version(1, blobs[0], 1, None),
            version(2, blobs[1], 1, 3),
            version(3, blobs[2], 1, 2),
            version(3, blobs[3], 2, None),
        ]
    )
    session.flush()

    def at(seq: int) -> list[tuple[int, str]]:
        rows = session.execute(
            select(ItemVersion.carbonio_item_id, ItemVersion.content_blob_id)
            .where(
                ItemVersion.account_id == account.id,
                ItemVersion.from_seq <= seq,
                (ItemVersion.to_seq.is_(None)) | (ItemVersion.to_seq > seq),
            )
            .order_by(ItemVersion.carbonio_item_id)
        )
        return [(r[0], r[1][-1]) for r in rows]

    assert at(1) == [(1, "1"), (2, "2"), (3, "3")]
    assert at(2) == [(1, "1"), (2, "2"), (3, "4")]
    assert at(3) == [(1, "1"), (3, "4")]

    # Trigram search on subject.
    hits = session.scalar(
        select(func.count())
        .select_from(ItemVersion)
        .where(ItemVersion.account_id == account.id, ItemVersion.subject.ilike("%facture%"))
    )
    assert hits == 4


def test_only_one_open_version_per_item(session: Session) -> None:
    account = _account(session)
    blob = _blob(session, 9)
    for _ in range(2):
        session.add(
            ItemVersion(
                account_id=account.id,
                carbonio_item_id=42,
                carbonio_folder_id=2,
                kind=ItemKind.MESSAGE,
                content_blob_id=blob.id,
                size=1,
                from_seq=1,
            )
        )
    with pytest.raises(IntegrityError):
        session.flush()


def _audit(session: Session) -> AuditEvent:
    event = AuditEvent(
        actor_type=ActorType.USER,
        actor_name="admin",
        action="restore.item",
        outcome=Outcome.SUCCESS,
        source_ip="192.0.2.10",
        prev_hash="0" * 64,
        hash="a" * 64,
    )
    session.add(event)
    session.flush()
    return event


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET action = 'tampered'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    ],
)
def test_audit_is_append_only(session: Session, statement: str) -> None:
    _audit(session)
    with pytest.raises(DBAPIError, match="append-only"):
        session.execute(text(statement))
