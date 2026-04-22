"""Subscription: cancel_at_period_end + founder price snapshot

Revision ID: u6v7w8x9y0z1
Revises: t5u6v7w8x9y0
Create Date: 2026-04-22

Adds the missing scaffolding for:
- Self-serve downgrade/cancel (1.6): cancel_at_period_end, cancelled_at,
  cancelled_reason. Cancellation now schedules a drop-to-Free for the end
  of the paid period instead of immediately hard-blocking the bot.
- Founder lifetime price lock (2.2): snapshotted_price_brl records the
  price a founder signed up at, so the R$59,90 lifetime guarantee survives
  Plan row edits, price bumps, or plan deactivation.

All columns are nullable and back-compatible: existing rows keep the old
"authorized / cancelled / paused" semantics until the downgrade endpoints
start setting cancel_at_period_end.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "u6v7w8x9y0z1"
down_revision: Union[str, Sequence[str], None] = "t5u6v7w8x9y0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "subscription",
        sa.Column(
            "cancel_at_period_end",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "subscription",
        sa.Column(
            "cancelled_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "subscription",
        sa.Column("cancelled_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "subscription",
        sa.Column("snapshotted_price_brl", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscription", "snapshotted_price_brl")
    op.drop_column("subscription", "cancelled_reason")
    op.drop_column("subscription", "cancelled_at")
    op.drop_column("subscription", "cancel_at_period_end")
