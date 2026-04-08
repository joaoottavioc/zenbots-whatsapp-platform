"""daily_cost_summary.bot_id nullable + ON DELETE SET NULL

Revision ID: q2r3s4t5u6v7
Revises: p1q2r3s4t5u6
Create Date: 2026-04-07 21:25:00.000000

Mirror of the previous migration but for daily_cost_summary. Without this,
aggregate_daily_costs() would fail when an aggregated bucket has bot_id=NULL
(events recorded without a bot context, like Cadastro Mágico extraction).

Both ALTERs are non-blocking on PostgreSQL. Reversible.
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "q2r3s4t5u6v7"
down_revision: Union[str, Sequence[str], None] = "p1q2r3s4t5u6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Drop the existing NO ACTION foreign key
    op.drop_constraint(
        "daily_cost_summary_bot_id_fkey", "daily_cost_summary", type_="foreignkey"
    )

    # 2. Make bot_id nullable
    op.alter_column("daily_cost_summary", "bot_id", nullable=True)

    # 3. Recreate the foreign key with ON DELETE SET NULL
    op.create_foreign_key(
        "daily_cost_summary_bot_id_fkey",
        "daily_cost_summary",
        "bot",
        ["bot_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Downgrade schema."""
    # 1. Drop the SET NULL foreign key
    op.drop_constraint(
        "daily_cost_summary_bot_id_fkey", "daily_cost_summary", type_="foreignkey"
    )

    # 2. Backfill any NULL bot_ids — can't safely guess, so delete
    op.execute("DELETE FROM daily_cost_summary WHERE bot_id IS NULL")

    # 3. Restore NOT NULL
    op.alter_column("daily_cost_summary", "bot_id", nullable=False)

    # 4. Recreate the original NO ACTION foreign key
    op.create_foreign_key(
        "daily_cost_summary_bot_id_fkey",
        "daily_cost_summary",
        "bot",
        ["bot_id"],
        ["id"],
    )
