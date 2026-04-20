"""Pricing: bot_monthly_usage table

Revision ID: s4t5u6v7w8x9
Revises: r3s4t5u6v7w8
Create Date: 2026-04-20

Per-bot, per-calendar-month order counter to drive Free-tier overage
billing and Pro-tier fair-use outreach. See tech_debt/backlog_pricing.md
§1.2.

bot_id is nullable via ON DELETE SET NULL so billing history survives
bot deletion (mirrors usage_events / daily_cost_summary).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "s4t5u6v7w8x9"
down_revision: Union[str, Sequence[str], None] = "r3s4t5u6v7w8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "bot_monthly_usage",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "bot_id",
            sa.Integer(),
            sa.ForeignKey("bot.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("year_month", sa.String(length=7), nullable=False),
        sa.Column(
            "completed_orders",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "overage_orders",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "overage_amount_brl",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0.0"),
        ),
        sa.Column("overage_billed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("overage_billed_amount", sa.Float(), nullable=True),
        sa.Column("overage_charge_id", sa.String(), nullable=True),
        sa.Column(
            "fair_use_warning_sent_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "fair_use_exceeded_sent_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("enterprise_outreach_status", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "bot_id", "year_month", name="uq_bot_monthly_usage_bot_month"
        ),
    )
    op.create_index("ix_bot_monthly_usage_bot_id", "bot_monthly_usage", ["bot_id"])
    op.create_index(
        "ix_bot_monthly_usage_year_month", "bot_monthly_usage", ["year_month"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_bot_monthly_usage_year_month", table_name="bot_monthly_usage")
    op.drop_index("ix_bot_monthly_usage_bot_id", table_name="bot_monthly_usage")
    op.drop_table("bot_monthly_usage")
