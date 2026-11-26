"""mail traffic: hourly counters, spam senders, sending-limit alerts, account actions

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-19 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _int(name: str) -> sa.Column[sa.Integer]:
    return sa.Column(name, sa.Integer(), server_default=sa.text("0"), nullable=False)


def _now() -> sa.Column[sa.DateTime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "mail_stats",
        sa.Column("hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=True),
        _int("sent_messages"),
        _int("sent_recipients"),
        _int("spam_received"),
        _int("spam_sent"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_mail_stats_account_id_accounts"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("hour", "email", name=op.f("pk_mail_stats")),
    )
    op.create_index("ix_mail_stats_hour", "mail_stats", ["hour"])
    op.create_index("ix_mail_stats_account_hour", "mail_stats", ["account_id", "hour"])
    op.create_table(
        "spam_senders",
        sa.Column("hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sender", sa.String(length=320), nullable=False),
        _int("count"),
        sa.Column(
            "ips",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("hour", "sender", name=op.f("pk_spam_senders")),
    )
    op.create_index("ix_spam_senders_hour", "spam_senders", ["hour"])
    op.create_table(
        "mail_alerts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        _now(),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_mail_alerts_account_id_accounts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["acknowledged_by"],
            ["users.id"],
            name=op.f("fk_mail_alerts_acknowledged_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_alerts")),
        sa.UniqueConstraint("email", "metric", "window_start", name="uq_mail_alerts_window"),
    )
    op.create_index(
        "ix_mail_alerts_open",
        "mail_alerts",
        ["created_at"],
        postgresql_where=sa.text("acknowledged_at IS NULL"),
    )
    op.create_table(
        "account_actions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("alert_id", sa.UUID(), nullable=True),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        _now(),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "action IN ('lock', 'unlock')", name=op.f("ck_account_actions_action_known")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'done', 'failed')", name=op.f("ck_account_actions_status_known")
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_account_actions_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["alert_id"],
            ["mail_alerts.id"],
            name=op.f("fk_account_actions_alert_id_mail_alerts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_account_actions_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_actions")),
    )
    op.create_index(
        "ix_account_actions_pending",
        "account_actions",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_account_actions_pending", table_name="account_actions")
    op.drop_table("account_actions")
    op.drop_index("ix_mail_alerts_open", table_name="mail_alerts")
    op.drop_table("mail_alerts")
    op.drop_index("ix_spam_senders_hour", table_name="spam_senders")
    op.drop_table("spam_senders")
    op.drop_index("ix_mail_stats_account_hour", table_name="mail_stats")
    op.drop_index("ix_mail_stats_hour", table_name="mail_stats")
    op.drop_table("mail_stats")
