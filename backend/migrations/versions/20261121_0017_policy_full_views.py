"""full backup schedule on policies, saved views

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-21 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("policies", sa.Column("full_schedule", sa.String(length=128), nullable=True))
    op.add_column(
        "policies", sa.Column("full_last_run_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_table(
        "saved_views",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("page", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("query", sa.String(length=4000), nullable=False),
        sa.Column("shared", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_saved_views_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_views")),
        sa.UniqueConstraint("owner_id", "page", "name", name="uq_saved_views_owner_page_name"),
    )
    op.create_index("ix_saved_views_page", "saved_views", ["page"])


def downgrade() -> None:
    op.drop_index("ix_saved_views_page", table_name="saved_views")
    op.drop_table("saved_views")
    op.drop_column("policies", "full_last_run_at")
    op.drop_column("policies", "full_schedule")
