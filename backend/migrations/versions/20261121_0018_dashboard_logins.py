"""personal dashboard layout, failed logins and blocked IPs

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-21 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("dashboard", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_table(
        "login_failures",
        sa.Column("hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=False),
        sa.Column("failed_mailbox", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failed_smtp", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("suspended", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_suspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "accounts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("hour", "ip", name=op.f("pk_login_failures")),
    )
    op.create_index("ix_login_failures_hour", "login_failures", ["hour"])


def downgrade() -> None:
    op.drop_index("ix_login_failures_hour", table_name="login_failures")
    op.drop_table("login_failures")
    op.drop_column("users", "dashboard")
