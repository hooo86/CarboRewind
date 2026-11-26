"""job kind 'replicate' (copy 02 of the repository on a second share)

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-19 15:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = "'backup', 'restore', 'config_backup', 'verify', 'purge', 'migrate'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_jobs_job_kind"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_job_kind"), "jobs", f"kind IN ({KINDS}, 'replicate')")


def downgrade() -> None:
    op.execute("DELETE FROM jobs WHERE kind = 'replicate'")
    op.drop_constraint(op.f("ck_jobs_job_kind"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_job_kind"), "jobs", f"kind IN ({KINDS})")
