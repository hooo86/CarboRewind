"""archiving: policies, archived messages index, fetched contents; job kinds archive/fetch

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-20 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = "'backup', 'restore', 'config_backup', 'verify', 'purge', 'migrate', 'replicate'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_jobs_job_kind"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_job_kind"), "jobs", f"kind IN ({KINDS}, 'archive', 'fetch')"
    )
    op.create_table(
        "archive_policies",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "mode", sa.String(length=16), server_default=sa.text("'simulate'"), nullable=False
        ),
        sa.Column(
            "domain_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "account_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "exclude_account_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("older_than_days", sa.Integer(), server_default=sa.text("365"), nullable=False),
        sa.Column(
            "exclude_folders",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("keep_flagged", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("keep_unread", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("include_admins", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("max_per_run", sa.Integer(), server_default=sa.text("5000"), nullable=False),
        sa.Column(
            "schedule",
            sa.String(length=128),
            server_default=sa.text("'30 1 * * *'"),
            nullable=False,
        ),
        sa.Column(
            "timezone", sa.String(length=64), server_default=sa.text("'UTC'"), nullable=False
        ),
        sa.Column("retention_days", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "criteria_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("simulated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "mode IN ('simulate', 'copy', 'archive')", name=op.f("ck_archive_policies_mode_valid")
        ),
        sa.CheckConstraint(
            "max_per_run BETWEEN 1 AND 50000", name=op.f("ck_archive_policies_max_per_run_bounded")
        ),
        sa.CheckConstraint("older_than_days >= 1", name=op.f("ck_archive_policies_age_positive")),
        sa.CheckConstraint(
            "retention_days >= 0", name=op.f("ck_archive_policies_retention_positive")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_archive_policies")),
        sa.UniqueConstraint("name", name=op.f("uq_archive_policies_name")),
    )
    op.create_table(
        "archived_messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("policy_id", sa.UUID(), nullable=True),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("carbonio_item_id", sa.Integer(), nullable=False),
        sa.Column("folder_path", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("sender", sa.Text(), nullable=True),
        sa.Column("recipients", sa.Text(), nullable=True),
        sa.Column("item_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("has_attachments", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("flags", sa.String(length=32), server_default=sa.text("''"), nullable=False),
        sa.Column("content_blob_id", sa.String(length=64), nullable=False),
        sa.Column("meta_blob_id", sa.String(length=64), nullable=True),
        sa.Column(
            "archived_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_archived_messages_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["content_blob_id"],
            ["blobs.id"],
            name=op.f("fk_archived_messages_content_blob_id_blobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_archived_messages_job_id_jobs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["meta_blob_id"],
            ["blobs.id"],
            name=op.f("fk_archived_messages_meta_blob_id_blobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["archive_policies.id"],
            name=op.f("fk_archived_messages_policy_id_archive_policies"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_archived_messages")),
        sa.UniqueConstraint(
            "account_id", "carbonio_item_id", "content_blob_id", name="uq_archived_messages_item"
        ),
    )
    op.create_index(
        "ix_archived_messages_account_date",
        "archived_messages",
        ["account_id", "item_date"],
        unique=False,
    )
    op.create_index(
        "ix_archived_messages_archived_at", "archived_messages", ["archived_at"], unique=False
    )
    op.create_index(
        "ix_archived_messages_content_blob", "archived_messages", ["content_blob_id"], unique=False
    )
    op.create_index("ix_archived_messages_job_id", "archived_messages", ["job_id"], unique=False)
    op.create_index(
        "ix_archived_messages_meta_blob", "archived_messages", ["meta_blob_id"], unique=False
    )
    op.create_index(
        "ix_archived_messages_sender_trgm",
        "archived_messages",
        ["sender"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"sender": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_archived_messages_subject_trgm",
        "archived_messages",
        ["subject"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"subject": "gin_trgm_ops"},
    )
    op.create_table(
        "fetched_contents",
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_fetched_contents_job_id_jobs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("job_id", name=op.f("pk_fetched_contents")),
    )


def downgrade() -> None:
    op.drop_table("fetched_contents")
    op.drop_index(
        "ix_archived_messages_subject_trgm",
        table_name="archived_messages",
        postgresql_using="gin",
        postgresql_ops={"subject": "gin_trgm_ops"},
    )
    op.drop_index(
        "ix_archived_messages_sender_trgm",
        table_name="archived_messages",
        postgresql_using="gin",
        postgresql_ops={"sender": "gin_trgm_ops"},
    )
    op.drop_index("ix_archived_messages_meta_blob", table_name="archived_messages")
    op.drop_index("ix_archived_messages_job_id", table_name="archived_messages")
    op.drop_index("ix_archived_messages_content_blob", table_name="archived_messages")
    op.drop_index("ix_archived_messages_archived_at", table_name="archived_messages")
    op.drop_index("ix_archived_messages_account_date", table_name="archived_messages")
    op.drop_table("archived_messages")
    op.drop_table("archive_policies")
    op.execute("DELETE FROM jobs WHERE kind IN ('archive', 'fetch')")
    op.drop_constraint(op.f("ck_jobs_job_kind"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_job_kind"), "jobs", f"kind IN ({KINDS})")
