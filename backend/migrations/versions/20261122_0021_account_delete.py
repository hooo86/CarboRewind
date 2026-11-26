"""account actions: delete a mailbox

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-22 12:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_account_actions_action_known"), "account_actions", type_="check")
    op.create_check_constraint(
        op.f("ck_account_actions_action_known"),
        "account_actions",
        "action IN ('lock', 'unlock', 'delete')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM account_actions WHERE action = 'delete'")
    op.drop_constraint(op.f("ck_account_actions_action_known"), "account_actions", type_="check")
    op.create_check_constraint(
        op.f("ck_account_actions_action_known"), "account_actions", "action IN ('lock', 'unlock')"
    )
