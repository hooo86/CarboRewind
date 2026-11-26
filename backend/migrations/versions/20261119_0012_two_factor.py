"""two-factor authentication (TOTP, recovery codes) and password change date

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-19 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("totp_secret_enc", sa.Text(), nullable=True))
    op.add_column(
        "users",
        sa.Column("totp_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("users", sa.Column("totp_last_step", sa.BigInteger(), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "recovery_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "users", sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    for column in (
        "password_changed_at",
        "recovery_codes",
        "totp_last_step",
        "totp_enabled",
        "totp_secret_enc",
    ):
        op.drop_column("users", column)
