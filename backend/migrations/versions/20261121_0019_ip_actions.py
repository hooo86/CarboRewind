"""trust / untrust IP addresses in Carbonio's invalid login filter

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-21 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ip_actions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "action IN ('trust', 'untrust')", name=op.f("ck_ip_actions_action_known")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'done', 'failed')", name=op.f("ck_ip_actions_status_known")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_ip_actions_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ip_actions")),
    )


def downgrade() -> None:
    op.drop_table("ip_actions")
