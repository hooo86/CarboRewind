"""scheduling: policy time zone and last run, snapshot purge marker

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-18 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "policies",
        sa.Column(
            "timezone", sa.String(length=64), server_default=sa.text("'UTC'"), nullable=False
        ),
    )
    op.add_column("policies", sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("snapshots", sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))
    # Retention scans: snapshots of an account by age; purge: expired ones not yet purged.
    op.create_index(
        "ix_snapshots_expired_unpurged",
        "snapshots",
        ["account_id"],
        postgresql_where=sa.text("status = 'expired' AND purged_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_snapshots_expired_unpurged", table_name="snapshots")
    op.drop_column("snapshots", "purged_at")
    op.drop_column("policies", "last_run_at")
    op.drop_column("policies", "timezone")
