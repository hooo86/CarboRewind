"""accounts: password age and expiry; account actions: reset the password

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-26 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts", sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("accounts", sa.Column("password_max_age_days", sa.Integer(), nullable=True))
    op.add_column(
        "accounts", sa.Column("password_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "accounts",
        sa.Column(
            "password_must_change", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.create_index("ix_accounts_password_expires_at", "accounts", ["password_expires_at"])
    # The new password, encrypted (CARBOREWIND_SECRET_KEY), kept only until the agent applies it.
    op.add_column("account_actions", sa.Column("secret", sa.Text(), nullable=True))
    op.drop_constraint(op.f("ck_account_actions_action_known"), "account_actions", type_="check")
    op.create_check_constraint(
        op.f("ck_account_actions_action_known"),
        "account_actions",
        "action IN ('lock', 'unlock', 'delete', 'quota', 'password')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM account_actions WHERE action = 'password'")
    op.drop_constraint(op.f("ck_account_actions_action_known"), "account_actions", type_="check")
    op.create_check_constraint(
        op.f("ck_account_actions_action_known"),
        "account_actions",
        "action IN ('lock', 'unlock', 'delete', 'quota')",
    )
    op.drop_column("account_actions", "secret")
    op.drop_index("ix_accounts_password_expires_at", table_name="accounts")
    for column in (
        "password_must_change",
        "password_expires_at",
        "password_max_age_days",
        "password_changed_at",
    ):
        op.drop_column("accounts", column)
