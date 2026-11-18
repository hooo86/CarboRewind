"""notifications: settings store and e-mail outbox; agent storage report

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-18 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column(
            "value",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_app_settings")),
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "recipients",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "sent",
                "failed",
                name="notification_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("dedup_key", sa.String(length=255), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint("dedup_key", name=op.f("uq_notifications_dedup_key")),
    )
    op.create_index("ix_notifications_pending", "notifications", ["status", "next_attempt_at"])
    op.add_column(
        "agents", sa.Column("storage", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("agents", "storage")
    op.drop_index("ix_notifications_pending", table_name="notifications")
    op.drop_table("notifications")
    op.drop_table("app_settings")
