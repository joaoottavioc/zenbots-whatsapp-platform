"""Pricing: wipe legacy plan rows, keep only canonical tiers

Revision ID: w8x9y0z1a2b3
Revises: v7w8x9y0z1a2
Create Date: 2026-04-22

The Plan table accumulated legacy placeholder rows (`pro`, `basic`,
anything seeded from earlier iterations) before the Free/Pro rollout
stabilized. Keeping them alive means the pricing page surfaces stale
tiers, /billing/checkout accepts non-canonical plan_keys, and admin
grants can silently create orphan subscriptions.

This migration:
1. Reassigns any Subscription row whose plan_type is not one of the
   canonical keys to `pro_monthly` (safe lossy default — all legacy
   paid placeholders were variants of Pro). Also stamps plan_id so the
   FK catches up for downstream features.
2. Deletes every Plan row whose key is not in the canonical set.

Canonical keys (backlog_pricing.md 1.1):
  free | pro_monthly | pro_annual | founder

NOTE on downgrade: the original plan_type values are lost in the upgrade.
Downgrade restores a single placeholder `pro` row (R$5, matching the old
seed) so the constraint isn't broken, but per-row history does not
round-trip. If you need to roll back on prod, grab a pre-migration
snapshot first.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "w8x9y0z1a2b3"
down_revision: Union[str, Sequence[str], None] = "v7w8x9y0z1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CANONICAL_KEYS = ("free", "pro_monthly", "pro_annual", "founder")


def upgrade() -> None:
    # 1. Reassign non-canonical subscriptions to pro_monthly.
    op.execute(
        """
        UPDATE subscription
           SET plan_type = 'pro_monthly',
               plan_id = (SELECT id FROM plan WHERE key = 'pro_monthly')
         WHERE plan_type NOT IN ('free', 'pro_monthly', 'pro_annual', 'founder')
        """
    )

    # 2. Drop non-canonical plan rows.
    op.execute(
        """
        DELETE FROM plan
         WHERE key NOT IN ('free', 'pro_monthly', 'pro_annual', 'founder')
        """
    )


def downgrade() -> None:
    # Restore a minimal placeholder so code paths that reference `pro` don't
    # break. This is lossy — per-row history of other legacy plans is gone.
    op.execute(
        """
        INSERT INTO plan (
            key, title, description, price, currency, frequency,
            allows_bot_usage, tier, billing_cycle_months, is_active,
            max_bots, allows_template_messages, created_at
        ) VALUES (
            'pro', 'ZenBotZ Pro (legacy)',
            'Placeholder restored on downgrade — do not sell.',
            5.00, 'BRL', 1, true, 'pro', 1, false, 1, false, NOW()
        )
        ON CONFLICT (key) DO NOTHING
        """
    )
