"""IMAP migrations: sources, batches, mailboxes; job kind 'migrate'

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-19 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = "'backup', 'restore', 'config_backup', 'verify', 'purge'"


def _now() -> sa.Column[sa.DateTime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _jsonb(name: str, default: str) -> sa.Column[sa.Text]:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text(default),
        nullable=False,
    )


def upgrade() -> None:
    op.drop_constraint(op.f("ck_jobs_job_kind"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_job_kind"), "jobs", f"kind IN ({KINDS}, 'migrate')")
    op.create_table(
        "migration_sources",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("host", sa.String(length=253), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column(
            "security",
            sa.Enum(
                "ssl",
                "starttls",
                "none",
                name="imap_security",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("verify_tls", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "auth_mode",
            sa.Enum(
                "password",
                "master_plain",
                "master_login",
                name="migration_auth",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("master_username", sa.String(length=320), nullable=True),
        sa.Column("master_password_enc", sa.Text(), nullable=True),
        sa.Column(
            "master_separator", sa.String(length=8), server_default=sa.text("'*'"), nullable=False
        ),
        _now(),
        sa.CheckConstraint(
            "port BETWEEN 1 AND 65535", name=op.f("ck_migration_sources_port_range")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_migration_sources")),
        sa.UniqueConstraint("name", name=op.f("uq_migration_sources_name")),
    )
    op.create_table(
        "migration_batches",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        _jsonb("options", "'{}'::jsonb"),
        sa.Column("created_by", sa.UUID(), nullable=True),
        _now(),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["migration_sources.id"],
            name=op.f("fk_migration_batches_source_id_migration_sources"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_migration_batches_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_migration_batches")),
        sa.UniqueConstraint("name", name=op.f("uq_migration_batches_name")),
    )
    op.create_table(
        "migration_mailboxes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("source_email", sa.String(length=320), nullable=False),
        sa.Column("source_login", sa.String(length=320), nullable=True),
        sa.Column("password_enc", sa.Text(), nullable=True),
        sa.Column("target_account_id", sa.UUID(), nullable=True),
        sa.Column("match", sa.String(length=16), nullable=True),
        sa.Column("root", sa.String(length=128), nullable=True),
        sa.Column("last_job_id", sa.UUID(), nullable=True),
        _jsonb("state", "'{}'::jsonb"),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        _now(),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["migration_batches.id"],
            name=op.f("fk_migration_mailboxes_batch_id_migration_batches"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_account_id"],
            ["accounts.id"],
            name=op.f("fk_migration_mailboxes_target_account_id_accounts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["last_job_id"],
            ["jobs.id"],
            name=op.f("fk_migration_mailboxes_last_job_id_jobs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_migration_mailboxes")),
        sa.UniqueConstraint(
            "batch_id",
            "source_email",
            "target_account_id",
            name="uq_migration_mailboxes_pair",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index("ix_migration_mailboxes_batch_id", "migration_mailboxes", ["batch_id"])
    op.create_index("ix_migration_mailboxes_target", "migration_mailboxes", ["target_account_id"])


def downgrade() -> None:
    op.drop_index("ix_migration_mailboxes_target", table_name="migration_mailboxes")
    op.drop_index("ix_migration_mailboxes_batch_id", table_name="migration_mailboxes")
    op.drop_table("migration_mailboxes")
    op.drop_table("migration_batches")
    op.drop_table("migration_sources")
    op.execute("DELETE FROM jobs WHERE kind = 'migrate'")
    op.drop_constraint(op.f("ck_jobs_job_kind"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_job_kind"), "jobs", f"kind IN ({KINDS})")
