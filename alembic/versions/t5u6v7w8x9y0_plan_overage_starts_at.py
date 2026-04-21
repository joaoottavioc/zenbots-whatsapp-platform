"""Pricing: add plan.overage_starts_at (grace window above monthly_order_cap)

Revision ID: t5u6v7w8x9y0
Revises: s4t5u6v7w8x9
Create Date: 2026-04-21

Adds a second threshold on `plan` so the Free tier can have a warning/grace
zone between the displayed cap (15) and the point where overage charges
actually begin (25). Below `monthly_order_cap` = safe/warn; between cap and
`overage_starts_at` = "cap" stage (grace, no charge); above
`overage_starts_at` = charged at `overage_per_order_brl`.

NULL means "no grace window" — falls back to `monthly_order_cap` as the
overage trigger (existing pre-grace behavior).

Also backfills the Free plan to overage_starts_at=25.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "t5u6v7w8x9y0"
down_revision: Union[str, Sequence[str], None] = "s4t5u6v7w8x9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("plan", sa.Column("overage_starts_at", sa.Integer(), nullable=True))

    # Free plan: 15-order warning cap, overage charges begin at 25.
    op.execute(
        """
        UPDATE plan
           SET overage_starts_at = 25
         WHERE key = 'free'
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("plan", "overage_starts_at")
