"""notification channel (e-mail or Telegram)

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-20 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notifications",
        sa.Column(
            "channel", sa.String(length=16), server_default=sa.text("'email'"), nullable=False
        ),
    )


def downgrade() -> None:
    op.execute("DELETE FROM notifications WHERE channel <> 'email'")
    op.drop_column("notifications", "channel")
