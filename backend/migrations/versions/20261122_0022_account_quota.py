"""account actions: set the mail quota

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-22 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("account_actions", sa.Column("value", sa.BigInteger(), nullable=True))
    op.drop_constraint(op.f("ck_account_actions_action_known"), "account_actions", type_="check")
    op.create_check_constraint(
        op.f("ck_account_actions_action_known"),
        "account_actions",
        "action IN ('lock', 'unlock', 'delete', 'quota')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM account_actions WHERE action = 'quota'")
    op.drop_constraint(op.f("ck_account_actions_action_known"), "account_actions", type_="check")
    op.create_check_constraint(
        op.f("ck_account_actions_action_known"),
        "account_actions",
        "action IN ('lock', 'unlock', 'delete')",
    )
    op.drop_column("account_actions", "value")
