"""storage samples (trend and saturation forecast of copy 1 / copy 2)

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-19 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "storage_samples",
        sa.Column("hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("target", sa.String(length=16), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("free_bytes", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name=op.f("fk_storage_samples_agent_id_agents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("hour", "agent_id", "target", name=op.f("pk_storage_samples")),
    )
    op.create_index("ix_storage_samples_hour", "storage_samples", ["hour"])


def downgrade() -> None:
    op.drop_index("ix_storage_samples_hour", table_name="storage_samples")
    op.drop_table("storage_samples")
