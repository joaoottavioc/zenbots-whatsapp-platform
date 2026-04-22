"""Pricing: reset Plan.max_bots=1 — subscriptions are per-bot

Revision ID: v7w8x9y0z1a2
Revises: u6v7w8x9y0z1
Create Date: 2026-04-22

The original pricing seed gave Pro/Founder plans max_bots=3 on the
assumption that one subscription could cover three bots. That conflicts
with the actual data model: Subscription.bot_id is UNIQUE — each bot gets
its own subscription. A restaurant owner who wants three bots pays for
three subscriptions.

This migration aligns the data with the model (max_bots=1 on every plan)
so the dashboard doesn't advertise "3 bots" for a Pro subscription that
only covers one.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "v7w8x9y0z1a2"
down_revision: Union[str, Sequence[str], None] = "u6v7w8x9y0z1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE plan
           SET max_bots = 1
         WHERE key IN ('pro_monthly', 'pro_annual', 'founder')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE plan
           SET max_bots = 3
         WHERE key IN ('pro_monthly', 'pro_annual', 'founder')
        """
    )
