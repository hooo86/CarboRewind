"""standby server: agent role, standby state per account, job kind 'standby_sync'

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-22 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = (
    "'backup', 'restore', 'config_backup', 'verify', 'purge', 'migrate', 'replicate', "
    "'archive', 'fetch'"
)


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "role", sa.String(length=16), server_default=sa.text("'primary'"), nullable=False
        ),
    )
    op.create_check_constraint(
        op.f("ck_agents_role_known"), "agents", "role IN ('primary', 'standby')"
    )
    op.create_table(
        "standby_states",
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("snapshot_id", sa.UUID(), nullable=True),
        sa.Column("snapshot_taken_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'ok'"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "stats",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('ok', 'partial', 'failed')", name=op.f("ck_standby_states_status_known")
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_standby_states_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["snapshots.id"],
            name=op.f("fk_standby_states_snapshot_id_snapshots"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_standby_states")),
    )
    op.drop_constraint(op.f("ck_jobs_job_kind"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_job_kind"), "jobs", f"kind IN ({KINDS}, 'standby_sync')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM jobs WHERE kind = 'standby_sync'")
    op.drop_constraint(op.f("ck_jobs_job_kind"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_job_kind"), "jobs", f"kind IN ({KINDS})")
    op.drop_table("standby_states")
    op.drop_constraint(op.f("ck_agents_role_known"), "agents", type_="check")
    op.drop_column("agents", "role")
