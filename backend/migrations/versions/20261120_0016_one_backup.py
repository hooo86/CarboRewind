"""one backup of a mailbox at a time

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-20 18:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Duplicates already queued: keep the running one (else the oldest), cancel the rest.
    op.execute(
        """
        UPDATE jobs SET status = 'cancelled', finished_at = now(),
               error = 'cancelled: another backup of this mailbox was already pending'
        WHERE id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY account_id
                    ORDER BY (status = 'running') DESC, created_at
                ) AS rank
                FROM jobs
                WHERE kind = 'backup' AND status IN ('queued', 'running')
                  AND account_id IS NOT NULL
            ) ranked
            WHERE rank > 1 AND id IN (SELECT id FROM jobs WHERE status = 'queued')
        )
        """
    )
    op.create_index(
        "uq_jobs_one_backup_per_account",
        "jobs",
        ["account_id"],
        unique=True,
        postgresql_where="kind = 'backup' AND status IN ('queued', 'running')",
    )


def downgrade() -> None:
    op.drop_index("uq_jobs_one_backup_per_account", table_name="jobs")
