"""account details from Carbonio; policies on a selection of accounts

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-18 17:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name, column_type in (
        ("display_name", sa.String(length=255)),
        ("first_name", sa.String(length=255)),
        ("last_name", sa.String(length=255)),
        ("cos", sa.String(length=255)),
        ("quota_bytes", sa.BigInteger()),
        ("used_bytes", sa.BigInteger()),
        ("last_login_at", sa.DateTime(timezone=True)),
        ("carbonio_created_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("accounts", sa.Column(name, column_type, nullable=True))
    op.add_column(
        "accounts",
        sa.Column(
            "aliases",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "accounts",
        sa.Column("is_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )

    # target_type 'accounts': an explicit list of accounts in policy_accounts.
    op.drop_constraint(op.f("ck_policies_target_type"), "policies", type_="check")
    op.create_check_constraint(
        op.f("ck_policies_target_type"),
        "policies",
        "target_type IN ('account', 'accounts', 'domain', 'global')",
    )
    op.drop_constraint(op.f("ck_policies_target_id_matches_type"), "policies", type_="check")
    op.create_check_constraint(
        op.f("ck_policies_target_id_matches_type"),
        "policies",
        "(target_type IN ('global', 'accounts')) = (target_id IS NULL)",
    )
    op.create_table(
        "policy_accounts",
        sa.Column("policy_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["policies.id"],
            name=op.f("fk_policy_accounts_policy_id_policies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_policy_accounts_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("policy_id", "account_id", name=op.f("pk_policy_accounts")),
    )
    op.create_index("ix_policy_accounts_account_id", "policy_accounts", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_policy_accounts_account_id", table_name="policy_accounts")
    op.drop_table("policy_accounts")
    op.execute("DELETE FROM policies WHERE target_type = 'accounts'")
    op.drop_constraint(op.f("ck_policies_target_id_matches_type"), "policies", type_="check")
    op.create_check_constraint(
        op.f("ck_policies_target_id_matches_type"),
        "policies",
        "(target_type = 'global') = (target_id IS NULL)",
    )
    op.drop_constraint(op.f("ck_policies_target_type"), "policies", type_="check")
    op.create_check_constraint(
        op.f("ck_policies_target_type"),
        "policies",
        "target_type IN ('account', 'domain', 'global')",
    )
    for name in (
        "is_admin",
        "aliases",
        "carbonio_created_at",
        "last_login_at",
        "used_bytes",
        "quota_bytes",
        "cos",
        "last_name",
        "first_name",
        "display_name",
    ):
        op.drop_column("accounts", name)
