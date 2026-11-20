"""SQLAlchemy ORM models of the CarboRewind control plane.

Design notes
------------
* Primary keys are UUIDv4 except for high-volume append-only tables (catalogue, audit),
  which use ``BIGINT`` identity columns.
* Enumerations are stored as ``VARCHAR`` + ``CHECK`` constraint (non-native enums) so that
  adding a value is a simple migration.
* The catalogue is **temporal**: a row of :class:`ItemVersion` / :class:`FolderVersion` is valid
  for the snapshots whose per-account sequence number ``seq`` satisfies
  ``from_seq <= seq < to_seq`` (``to_seq`` NULL = still present). Listing the content of a
  mailbox at snapshot *N* therefore needs no per-snapshot copy of the item list, which keeps
  the catalogue linear in the number of *changes* rather than ``items * snapshots``.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, time
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Deterministic constraint names: required for reliable Alembic migrations.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base with naming convention and common type mapping."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSONB,
        uuid.UUID: UUID(as_uuid=True),
    }


def _enum(cls: type[enum.Enum], name: str) -> Enum:
    return Enum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------- enumerations


class AuthSource(enum.StrEnum):
    LOCAL = "local"
    LDAP = "ldap"
    OIDC = "oidc"


class Role(enum.StrEnum):
    SUPER_ADMIN = "super_admin"
    DOMAIN_ADMIN = "domain_admin"
    RESTORE_OPERATOR = "restore_operator"
    VIEWER = "viewer"


class AccountStatus(enum.StrEnum):
    ACTIVE = "active"
    LOCKED = "locked"
    CLOSED = "closed"
    DELETED = "deleted"  # no longer present on Carbonio; restorable from backups


class AgentStatus(enum.StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    REVOKED = "revoked"


class TargetType(enum.StrEnum):
    ACCOUNT = "account"
    ACCOUNTS = "accounts"  # an explicit selection, see PolicyAccount
    DOMAIN = "domain"
    GLOBAL = "global"


class BackupType(enum.StrEnum):
    FULL = "full"
    INCREMENTAL = "incremental"


class JobKind(enum.StrEnum):
    BACKUP = "backup"
    RESTORE = "restore"
    CONFIG_BACKUP = "config_backup"
    VERIFY = "verify"
    PURGE = "purge"
    MIGRATE = "migrate"  # copy a mailbox from a remote IMAP server
    REPLICATE = "replicate"  # mirror the repository onto copy 02
    ARCHIVE = "archive"  # move old messages of one account into the archive
    FETCH = "fetch"  # read one archived message for the interface


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SnapshotStatus(enum.StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"  # finished with per-item errors, still restorable
    EXPIRED = "expired"  # retention elapsed, manifest purged


class ItemKind(enum.StrEnum):
    MESSAGE = "message"
    CONTACT = "contact"
    APPOINTMENT = "appointment"
    TASK = "task"
    DOCUMENT = "document"


class NotificationStatus(enum.StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class ImapSecurity(enum.StrEnum):
    SSL = "ssl"  # implicit TLS (993)
    STARTTLS = "starttls"  # 143 + STARTTLS
    NONE = "none"  # clear text: only on a trusted network


class MigrationAuth(enum.StrEnum):
    PASSWORD = "password"  # noqa: S105 - each mailbox's own password (a mode, not a secret)
    MASTER_PLAIN = "master_plain"  # AUTHENTICATE PLAIN, admin acting for the mailbox
    MASTER_LOGIN = "master_login"  # LOGIN mailbox<separator>master (Dovecot master user)


class ActorType(enum.StrEnum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class Outcome(enum.StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


# --------------------------------------------------------------------------- identity & RBAC


class User(Base):
    """Administrator of CarboRewind (not a mailbox user)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    username: Mapped[str] = mapped_column(String(255), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    auth_source: Mapped[AuthSource] = mapped_column(_enum(AuthSource, "auth_source"))
    # Argon2id hash, only for local accounts.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    failed_logins: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Two-factor authentication (TOTP). The secret is Fernet-encrypted; it is set during
    # enrolment and only active once confirmed with a first code (totp_enabled).
    totp_secret_enc: Mapped[str | None] = mapped_column(Text)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger)
    # SHA-256 of the unused one-time recovery codes.
    recovery_codes: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Personal dashboard layout (widgets, their order, size and options); NULL = default.
    dashboard: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()

    roles: Mapped[list[RoleAssignment]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "auth_source <> 'local' OR password_hash IS NOT NULL", name="local_has_password"
        ),
    )


class UserSession(Base):
    """Server-side login session. Only the SHA-256 of the bearer token is stored."""

    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # Double-submit CSRF token bound to this session (not secret from the browser).
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created_at()
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Absolute expiry; idle expiry is computed from last_seen_at.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(512))

    __table_args__ = (Index("ix_user_sessions_user_id", "user_id"),)


class RoleAssignment(Base):
    """Grants a role to a user, globally (``domain_id`` NULL) or on one domain."""

    __tablename__ = "role_assignments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[Role] = mapped_column(_enum(Role, "role"))
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = _created_at()

    user: Mapped[User] = relationship(back_populates="roles")

    __table_args__ = (
        # NULLS NOT DISTINCT: a global grant (domain_id NULL) cannot be duplicated either.
        UniqueConstraint(
            "user_id",
            "role",
            "domain_id",
            name="uq_role_assignments_grant",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("role <> 'super_admin' OR domain_id IS NULL", name="super_admin_is_global"),
    )


# --------------------------------------------------------------------------- Carbonio inventory


class Domain(Base):
    __tablename__ = "domains"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(253), unique=True)
    carbonio_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = _created_at()


class Account(Base):
    """A Carbonio mailbox account as last seen by inventory."""

    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    domain_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("domains.id", ondelete="RESTRICT"))
    email: Mapped[str] = mapped_column(String(320))
    # zimbraId of the account; stable across renames.
    carbonio_id: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[AccountStatus] = mapped_column(_enum(AccountStatus, "account_status"))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Carbonio details refreshed by every inventory.
    display_name: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    aliases: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    cos: Mapped[str | None] = mapped_column(String(255))
    quota_bytes: Mapped[int | None] = mapped_column(BigInteger)  # None or 0: unlimited
    used_bytes: Mapped[int | None] = mapped_column(BigInteger)
    is_admin: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    carbonio_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Profile from Carbonio (title, company, department, phones, address, notes…).
    profile: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        # An address can be reused after deletion: unique among non-deleted accounts only.
        Index(
            "uq_accounts_email_live",
            "email",
            unique=True,
            postgresql_where=text("status <> 'deleted'"),
        ),
        Index("ix_accounts_domain_id", "domain_id"),
    )


class Agent(Base):
    """Agent running on a Carbonio node, authenticated by mTLS client certificate."""

    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), unique=True)
    hostname: Mapped[str] = mapped_column(String(253))
    status: Mapped[AgentStatus] = mapped_column(_enum(AgentStatus, "agent_status"))
    # SHA-256 of the client certificate (hex). NULL until enrolment completes.
    cert_fingerprint: Mapped[str | None] = mapped_column(String(64), unique=True)
    cert_not_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # One-time enrolment token: only its SHA-256 is stored; cleared once used.
    enrollment_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    enrollment_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[str | None] = mapped_column(String(64))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Repository location and free space, reported with each inventory.
    storage: Mapped[dict[str, Any] | None]
    # Carbonio services and mail queue, reported every minute (see monitoring.record_services).
    services: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime] = _created_at()


# --------------------------------------------------------------------------- policies & jobs


class Policy(Base):
    """Scheduled backup policy."""

    __tablename__ = "policies"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), unique=True)
    target_type: Mapped[TargetType] = mapped_column(_enum(TargetType, "target_type"))
    # Account or domain id depending on target_type; NULL for global.
    target_id: Mapped[uuid.UUID | None]
    backup_type: Mapped[BackupType] = mapped_column(_enum(BackupType, "backup_type"))
    # Standard 5-field cron expression, validated by the API.
    schedule: Mapped[str] = mapped_column(String(128))
    window_start: Mapped[time | None] = mapped_column(Time)
    window_end: Mapped[time | None] = mapped_column(Time)
    priority: Mapped[int] = mapped_column(Integer, server_default=text("100"))
    # e.g. {"daily": 14, "weekly": 8, "monthly": 12}
    retention: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # IANA zone the cron expression is evaluated in.
    timezone: Mapped[str] = mapped_column(String(64), server_default=text("'UTC'"))
    # Last schedule slot already turned into jobs (missed slots are caught up once).
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Optional second schedule running full backups (e.g. incremental daily, full on
    # Sundays). When both fall at once the full backup wins.
    full_schedule: Mapped[str | None] = mapped_column(String(128))
    full_last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint(
            "(target_type IN ('global', 'accounts')) = (target_id IS NULL)",
            name="target_id_matches_type",
        ),
        CheckConstraint(
            "(window_start IS NULL) = (window_end IS NULL)", name="window_both_or_none"
        ),
    )


class PolicyAccount(Base):
    """Accounts of a policy whose target_type is ``accounts`` (a selection)."""

    __tablename__ = "policy_accounts"

    policy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("policies.id", ondelete="CASCADE"), primary_key=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )

    __table_args__ = (Index("ix_policy_accounts_account_id", "account_id"),)


class Job(Base):
    """Unit of work executed by an agent (or a Celery worker for purge/verify)."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[JobKind] = mapped_column(_enum(JobKind, "job_kind"))
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus, "job_status"), server_default=JobStatus.QUEUED.value
    )
    priority: Mapped[int] = mapped_column(Integer, server_default=text("100"))
    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("policies.id", ondelete="SET NULL")
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # Validated, typed parameters (restore mode, target, dry_run…).
    params: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    # Resume point written by the agent; opaque to the API.
    checkpoint: Mapped[dict[str, Any] | None]
    progress: Mapped[dict[str, Any] | None]
    # Prevents duplicate jobs for the same scheduled run / client request.
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, server_default=text("3"))
    # A running job whose lease expired is considered abandoned and can be resumed.
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("attempts >= 0 AND attempts <= max_attempts", name="attempts_bounded"),
        CheckConstraint(
            "status <> 'running' OR (agent_id IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="running_has_lease",
        ),
        # Dispatch query: next queued jobs by priority then age.
        Index(
            "ix_jobs_dispatch",
            "priority",
            "created_at",
            postgresql_where=text("status = 'queued'"),
        ),
        Index("ix_jobs_lease", "lease_expires_at", postgresql_where=text("status = 'running'")),
        Index("ix_jobs_account_created", "account_id", "created_at"),
        # One backup of a mailbox at a time: a second request is refused, not queued.
        Index(
            "uq_jobs_one_backup_per_account",
            "account_id",
            unique=True,
            postgresql_where=text("kind = 'backup' AND status IN ('queued', 'running')"),
        ),
    )


# --------------------------------------------------------------------------- backups & catalogue


class Snapshot(Base):
    """Point-in-time state of one account, materialised by a manifest in the repository."""

    __tablename__ = "snapshots"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    # Monotonic per account; used by the temporal catalogue.
    seq: Mapped[int] = mapped_column(Integer)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    backup_type: Mapped[BackupType] = mapped_column(_enum(BackupType, "backup_type"))
    status: Mapped[SnapshotStatus] = mapped_column(_enum(SnapshotStatus, "snapshot_status"))
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Repository-relative path of the encrypted manifest.
    manifest_ref: Mapped[str] = mapped_column(String(512))
    # Carbonio SyncRequest token at the end of this snapshot (next incremental starts here).
    sync_token: Mapped[str | None] = mapped_column(String(128))
    item_count: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    size_bytes: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    # Bytes actually added to the repository (after deduplication and compression).
    new_bytes: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    # Account attributes (COS, quota, aliases…) needed to recreate a deleted account.
    account_attrs: Mapped[dict[str, Any] | None]
    # Set once the agent has deleted the manifest of an expired snapshot.
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("account_id", "seq", name="uq_snapshots_account_seq"),
        CheckConstraint("seq >= 1", name="seq_positive"),
        Index("ix_snapshots_account_taken", "account_id", "taken_at"),
        Index(
            "ix_snapshots_expired_unpurged",
            "account_id",
            postgresql_where=text("status = 'expired' AND purged_at IS NULL"),
        ),
    )


class Blob(Base):
    """Content-addressed object stored in the repository (encrypted)."""

    __tablename__ = "blobs"

    # HMAC-SHA-256 of the plaintext, hex encoded.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    size: Mapped[int] = mapped_column(BigInteger)
    stored_size: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (CheckConstraint("id ~ '^[0-9a-f]{64}$'", name="id_is_hex_sha256"),)


class FolderVersion(Base):
    """Folder of an account, valid for snapshots ``from_seq <= seq < to_seq``."""

    __tablename__ = "folder_versions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    carbonio_folder_id: Mapped[int] = mapped_column(Integer)
    parent_folder_id: Mapped[int | None] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(Text)
    view: Mapped[ItemKind] = mapped_column(_enum(ItemKind, "item_kind"))
    # Colour, ACL/grants, flags… needed for a faithful restore.
    attrs: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    from_seq: Mapped[int] = mapped_column(Integer)
    to_seq: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint("to_seq IS NULL OR to_seq > from_seq", name="valid_range"),
        Index("ix_folder_versions_account_range", "account_id", "from_seq", "to_seq"),
    )


class ItemVersion(Base):
    """Version of a mailbox item, valid for snapshots ``from_seq <= seq < to_seq``.

    A modification closes the current row (sets ``to_seq``) and opens a new one; a deletion
    only closes it. Searchable metadata is denormalised here; content lives in the blob.
    """

    __tablename__ = "item_versions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    carbonio_item_id: Mapped[int] = mapped_column(Integer)
    carbonio_folder_id: Mapped[int] = mapped_column(Integer)
    kind: Mapped[ItemKind] = mapped_column(_enum(ItemKind, "item_kind"))
    content_blob_id: Mapped[str] = mapped_column(ForeignKey("blobs.id", ondelete="RESTRICT"))
    # Flags, tags, IMAP UID, modified sequence… as returned by Carbonio.
    meta_blob_id: Mapped[str | None] = mapped_column(ForeignKey("blobs.id", ondelete="RESTRICT"))
    subject: Mapped[str | None] = mapped_column(Text)
    sender: Mapped[str | None] = mapped_column(Text)
    item_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    size: Mapped[int] = mapped_column(BigInteger)
    from_seq: Mapped[int] = mapped_column(Integer)
    to_seq: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint("to_seq IS NULL OR to_seq > from_seq", name="valid_range"),
        # At most one open version per item.
        Index(
            "uq_item_versions_open",
            "account_id",
            "carbonio_item_id",
            unique=True,
            postgresql_where=text("to_seq IS NULL"),
        ),
        Index("ix_item_versions_account_range", "account_id", "from_seq", "to_seq"),
        Index("ix_item_versions_account_date", "account_id", "item_date"),
        Index("ix_item_versions_content_blob", "content_blob_id"),
        Index("ix_item_versions_meta_blob", "meta_blob_id"),
        # Trigram indexes for ILIKE search on subject / sender (pg_trgm).
        Index(
            "ix_item_versions_subject_trgm",
            "subject",
            postgresql_using="gin",
            postgresql_ops={"subject": "gin_trgm_ops"},
        ),
        Index(
            "ix_item_versions_sender_trgm",
            "sender",
            postgresql_using="gin",
            postgresql_ops={"sender": "gin_trgm_ops"},
        ),
    )


# --------------------------------------------------------------------------- migrations


class MigrationSource(Base):
    """Remote IMAP server mailboxes are copied from. Passwords are Fernet-encrypted."""

    __tablename__ = "migration_sources"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), unique=True)
    host: Mapped[str] = mapped_column(String(253))
    port: Mapped[int] = mapped_column(Integer)
    security: Mapped[ImapSecurity] = mapped_column(_enum(ImapSecurity, "imap_security"))
    verify_tls: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    auth_mode: Mapped[MigrationAuth] = mapped_column(_enum(MigrationAuth, "migration_auth"))
    master_username: Mapped[str | None] = mapped_column(String(320))
    master_password_enc: Mapped[str | None] = mapped_column(Text)
    master_separator: Mapped[str] = mapped_column(String(8), server_default=text("'*'"))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (CheckConstraint("port BETWEEN 1 AND 65535", name="port_range"),)


class MigrationBatch(Base):
    """A set of mailboxes copied from one source with the same options."""

    __tablename__ = "migration_batches"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), unique=True)
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("migration_sources.id", ondelete="RESTRICT")
    )
    # root folder, excluded folders, date range (validated by the API).
    options: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created_at()


class MigrationMailbox(Base):
    """One source mailbox -> one Carbonio account. A source may appear several times
    (copied into several accounts) and several sources may target one account."""

    __tablename__ = "migration_mailboxes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("migration_batches.id", ondelete="CASCADE")
    )
    source_email: Mapped[str] = mapped_column(String(320))
    # IMAP login when it differs from the address.
    source_login: Mapped[str | None] = mapped_column(String(320))
    password_enc: Mapped[str | None] = mapped_column(Text)
    target_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL")
    )
    # How the target was chosen: exact, alias, local_part, manual (NULL: no match yet).
    match: Mapped[str | None] = mapped_column(String(16))
    # Folder below which this mailbox is copied (overrides the batch option).
    root: Mapped[str | None] = mapped_column(String(128))
    last_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL")
    )
    # Incremental state written by the agent: per folder UIDVALIDITY / last UID / failed UIDs.
    state: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    stats: Mapped[dict[str, Any] | None]
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "source_email",
            "target_account_id",
            name="uq_migration_mailboxes_pair",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_migration_mailboxes_batch_id", "batch_id"),
        Index("ix_migration_mailboxes_target", "target_account_id"),
    )


# --------------------------------------------------------------------------- archiving


class ArchivePolicy(Base):
    """Which old messages leave the mailboxes for the archive, and when.

    ``mode``: ``simulate`` (count only), ``copy`` (archive, keep in the mailbox) or
    ``archive`` (archive, verify, then delete from the mailbox). ``archive`` is only accepted
    once a simulation finished after the last change of the selection criteria.
    """

    __tablename__ = "archive_policies"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    mode: Mapped[str] = mapped_column(String(16), server_default=text("'simulate'"))
    # Scope: empty lists = every domain / every account of these domains.
    domain_ids: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    account_ids: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    exclude_account_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    older_than_days: Mapped[int] = mapped_column(Integer, server_default=text("365"))
    # Folder paths left alone (with their sub-folders), on top of Trash, Junk and Drafts.
    exclude_folders: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    keep_flagged: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    keep_unread: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    include_admins: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    max_per_run: Mapped[int] = mapped_column(Integer, server_default=text("5000"))
    schedule: Mapped[str] = mapped_column(String(128), server_default=text("'30 1 * * *'"))
    timezone: Mapped[str] = mapped_column(String(64), server_default=text("'UTC'"))
    # 0 = archives kept forever.
    retention_days: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    criteria_changed_at: Mapped[datetime] = _created_at()
    simulated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint("mode IN ('simulate', 'copy', 'archive')", name="mode_valid"),
        CheckConstraint("older_than_days >= 1", name="age_positive"),
        CheckConstraint("max_per_run BETWEEN 1 AND 50000", name="max_per_run_bounded"),
        CheckConstraint("retention_days >= 0", name="retention_positive"),
    )


class ArchivedMessage(Base):
    """One message kept in the archive (content in the repository, metadata here)."""

    __tablename__ = "archived_messages"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("archive_policies.id", ondelete="SET NULL")
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    carbonio_item_id: Mapped[int] = mapped_column(Integer)
    folder_path: Mapped[str] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    sender: Mapped[str | None] = mapped_column(Text)
    recipients: Mapped[str | None] = mapped_column(Text)
    item_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    size: Mapped[int] = mapped_column(BigInteger)
    has_attachments: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    flags: Mapped[str] = mapped_column(String(32), server_default=text("''"))
    content_blob_id: Mapped[str] = mapped_column(ForeignKey("blobs.id", ondelete="RESTRICT"))
    meta_blob_id: Mapped[str | None] = mapped_column(ForeignKey("blobs.id", ondelete="RESTRICT"))
    archived_at: Mapped[datetime] = _created_at()
    # Set once the message was deleted from the mailbox (NULL: copy only, still there).
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Legal hold: kept whatever the policies' retention says, until explicitly released.
    legal_hold: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))

    __table_args__ = (
        UniqueConstraint(
            "account_id", "carbonio_item_id", "content_blob_id", name="uq_archived_messages_item"
        ),
        Index("ix_archived_messages_account_date", "account_id", "item_date"),
        Index("ix_archived_messages_archived_at", "archived_at"),
        Index("ix_archived_messages_job_id", "job_id"),
        Index("ix_archived_messages_content_blob", "content_blob_id"),
        Index("ix_archived_messages_meta_blob", "meta_blob_id"),
        Index(
            "ix_archived_messages_subject_trgm",
            "subject",
            postgresql_using="gin",
            postgresql_ops={"subject": "gin_trgm_ops"},
        ),
        Index(
            "ix_archived_messages_sender_trgm",
            "sender",
            postgresql_using="gin",
            postgresql_ops={"sender": "gin_trgm_ops"},
        ),
    )


class FetchedContent(Base):
    """Archived message read by the agent for the interface; encrypted, kept one hour."""

    __tablename__ = "fetched_contents"

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = _created_at()


# --------------------------------------------------------------------------- monitoring


class StorageSample(Base):
    """Space of copy 1 / copy 2 as reported by an agent, one sample per hour (trend)."""

    __tablename__ = "storage_samples"

    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    target: Mapped[str] = mapped_column(String(16), primary_key=True)  # primary | replica
    total_bytes: Mapped[int] = mapped_column(BigInteger)
    free_bytes: Mapped[int] = mapped_column(BigInteger)

    __table_args__ = (Index("ix_storage_samples_hour", "hour"),)


# --------------------------------------------------------------------------- mail traffic


class MailStat(Base):
    """Mail sent and spam per account and UTC hour, from the MTA log (absolute counters)."""

    __tablename__ = "mail_stats"

    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL")
    )
    sent_messages: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    sent_recipients: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    spam_received: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    spam_sent: Mapped[int] = mapped_column(Integer, server_default=text("0"))

    __table_args__ = (
        Index("ix_mail_stats_hour", "hour"),
        Index("ix_mail_stats_account_hour", "account_id", "hour"),
    )


class SpamSender(Base):
    """Senders of spam received, per UTC hour, with the client IPs seen."""

    __tablename__ = "spam_senders"

    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    sender: Mapped[str] = mapped_column(String(320), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    ips: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))

    __table_args__ = (Index("ix_spam_senders_hour", "hour"),)


class LoginFailure(Base):
    """Failed logins and suspensions by Carbonio's invalid login filter, per UTC hour and
    client IP (from mailbox.log and the SASL lines of the MTA log)."""

    __tablename__ = "login_failures"

    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    ip: Mapped[str] = mapped_column(String(64), primary_key=True)
    failed_mailbox: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    failed_smtp: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    suspended: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accounts: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))

    __table_args__ = (Index("ix_login_failures_hour", "hour"),)


class MailAlert(Base):
    """A sending limit was reached (once per account, metric and window)."""

    __tablename__ = "mail_alerts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL")
    )
    email: Mapped[str] = mapped_column(String(320))
    # sent_messages_hour, sent_messages_day, sent_recipients_hour, sent_recipients_day,
    # spam_sent_day
    metric: Mapped[str] = mapped_column(String(32))
    value: Mapped[int] = mapped_column(Integer)
    threshold: Mapped[int] = mapped_column(Integer)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    action: Mapped[str] = mapped_column(String(16))  # alert | lock
    created_at: Mapped[datetime] = _created_at()
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint("email", "metric", "window_start", name="uq_mail_alerts_window"),
        Index(
            "ix_mail_alerts_open", "created_at", postgresql_where=text("acknowledged_at IS NULL")
        ),
    )


class IpAction(Base):
    """Add an IP address to (``trust``) or remove it from (``untrust``) Carbonio's trusted
    addresses (``zimbraHttpThrottleSafeIPs``), carried out by the agent's mail watcher. A
    trusted address is never blocked by the invalid login filter."""

    __tablename__ = "ip_actions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    ip: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(16))  # trust | untrust
    status: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"))
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created_at()
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("action IN ('trust', 'untrust')", name="action_known"),
        CheckConstraint("status IN ('pending', 'done', 'failed')", name="status_known"),
    )


class AccountAction(Base):
    """Lock or unlock a Carbonio account, carried out by the agent's mail watcher."""

    __tablename__ = "account_actions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(String(16))  # lock | unlock
    reason: Mapped[str | None] = mapped_column(Text)
    alert_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mail_alerts.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"))
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created_at()
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("action IN ('lock', 'unlock')", name="action_known"),
        CheckConstraint("status IN ('pending', 'done', 'failed')", name="status_known"),
        Index(
            "ix_account_actions_pending", "created_at", postgresql_where=text("status = 'pending'")
        ),
    )


# --------------------------------------------------------------------------- audit


class AuditEvent(Base):
    """Append-only, hash-chained audit trail.

    ``hash = SHA-256(prev_hash || canonical_json(event))``; UPDATE / DELETE are refused by a
    database trigger (see the initial migration). Chain computation lives in the audit service.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor_type: Mapped[ActorType] = mapped_column(_enum(ActorType, "actor_type"))
    actor_id: Mapped[str | None] = mapped_column(String(64))
    actor_name: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(128))
    target_type: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(255))
    domain_id: Mapped[uuid.UUID | None]
    outcome: Mapped[Outcome] = mapped_column(_enum(Outcome, "outcome"))
    source_ip: Mapped[str | None] = mapped_column(INET)
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)

    __table_args__ = (
        Index("ix_audit_events_ts", "ts"),
        Index("ix_audit_events_actor", "actor_type", "actor_id", "ts"),
        Index("ix_audit_events_target", "target_type", "target_id"),
        Index("ix_audit_events_domain_ts", "domain_id", "ts"),
    )


class AppSetting(Base):
    """Settings edited from the web interface (key -> JSON). Secrets inside are encrypted."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    updated_at: Mapped[datetime] = _created_at()


class Notification(Base):
    """Outbox of e-mail notifications, delivered by the scheduler with retries."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event: Mapped[str] = mapped_column(String(64))
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    recipients: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # "email" or "telegram" (recipients are then chat ids).
    channel: Mapped[str] = mapped_column(String(16), server_default=text("'email'"))
    status: Mapped[NotificationStatus] = mapped_column(
        _enum(NotificationStatus, "notification_status"),
        server_default=NotificationStatus.PENDING.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    # Same key = same message: digests and alerts are sent once.
    dedup_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    created_at: Mapped[datetime] = _created_at()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_notifications_pending", "status", "next_attempt_at"),)


class SavedView(Base):
    """A named search of a list page (filters, grouping, sort), personal or shared."""

    __tablename__ = "saved_views"

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    page: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(100))
    # The page's URL query string (validated again by the list endpoint when applied).
    query: Mapped[str] = mapped_column(String(4000))
    shared: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("owner_id", "page", "name", name="uq_saved_views_owner_page_name"),
        Index("ix_saved_views_page", "page"),
    )
